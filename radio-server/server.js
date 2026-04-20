const express = require("express");
const { spawn } = require("child_process");
const { randomUUID } = require("crypto");

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;

// ==================== RADIO ENGINE ====================

let queue = [];
let history = [];
let currentSong = null;
let isPlaying = false;
let clients = [];
let ffmpegProcess = null;
let ytdlpProcess = null;

function log(msg, data) {
  const ts = new Date().toISOString().substring(11, 19);
  console.log(`[${ts}] ${msg}`, data ? JSON.stringify(data) : "");
}

function broadcast(chunk) {
  const dead = [];
  for (const client of clients) {
    try {
      client.res.write(chunk);
    } catch {
      dead.push(client.id);
    }
  }
  clients = clients.filter(c => !dead.includes(c.id));
}

function stopAll() {
  if (ytdlpProcess) { try { ytdlpProcess.kill("SIGKILL"); } catch {} ytdlpProcess = null; }
  if (ffmpegProcess) { try { ffmpegProcess.kill("SIGKILL"); } catch {} ffmpegProcess = null; }
}

function startStream(songUrl) {
  log("Getting direct audio URL for", songUrl.substring(0, 60));

  const getUrl = spawn("yt-dlp", [
    "--no-playlist", "-f", "bestaudio/best", "-g",
    "--no-warnings", songUrl
  ]);

  let directUrl = "";
  let errBuf = "";
  getUrl.stdout.on("data", d => { directUrl += d.toString(); });
  getUrl.stderr.on("data", d => { errBuf += d.toString(); });

  getUrl.on("close", code => {
    directUrl = directUrl.trim().split("\n")[0].trim();
    if (code !== 0 || !directUrl.startsWith("http")) {
      log("Failed to get direct URL, skipping", { code, err: errBuf.substring(0, 100) });
      playNext();
      return;
    }
    log("Got direct URL, starting ffmpeg");

    const ffmpeg = spawn("ffmpeg", [
      "-re", "-reconnect", "1", "-reconnect_streamed", "1",
      "-reconnect_delay_max", "5", "-i", directUrl,
      "-vn", "-acodec", "libmp3lame", "-ab", "128k",
      "-ar", "44100", "-f", "mp3", "-"
    ], { stdio: ["ignore", "pipe", "pipe"] });

    ffmpeg.stderr.on("data", d => {
      const msg = d.toString();
      if (!msg.includes("frame=") && !msg.includes("size=") && !msg.includes("time=")) {
        // log("ffmpeg", msg.trim().substring(0, 80));
      }
    });
    ffmpeg.stdout.on("data", chunk => broadcast(chunk));
    ffmpeg.on("close", code => {
      log("Song finished, moving to next", { code });
      playNext();
    });
    ffmpeg.on("error", err => {
      log("ffmpeg error", err.message);
      playNext();
    });
    ffmpegProcess = ffmpeg;
  });

  getUrl.on("error", err => {
    log("yt-dlp error", err.message);
    playNext();
  });

  ytdlpProcess = getUrl;
}

function playNext() {
  if (queue.length === 0) {
    currentSong = null;
    isPlaying = false;
    log("Queue empty, radio stopped");
    return;
  }
  currentSong = queue.shift();
  if (currentSong) history.unshift(currentSong);
  isPlaying = true;
  log("Now playing", { title: currentSong.title });
  stopAll();
  startStream(currentSong.url);
}

function searchYT(query) {
  return new Promise((resolve, reject) => {
    const proc = spawn("yt-dlp", [
      "--flat-playlist", "--no-warnings",
      "--print", "%(id)s\n%(title)s\n%(uploader)s\n%(duration)s\n%(webpage_url)s",
      `ytsearch8:${query}`
    ]);
    let out = "", err = "";
    proc.stdout.on("data", d => { out += d.toString(); });
    proc.stderr.on("data", d => { err += d.toString(); });
    proc.on("close", code => {
      if (code !== 0 && out.trim() === "") return reject(new Error(err));
      const lines = out.trim().split("\n");
      const results = [];
      for (let i = 0; i + 4 < lines.length; i += 5) {
        const url = lines[i + 4].trim() || `https://www.youtube.com/watch?v=${lines[i].trim()}`;
        results.push({
          id: lines[i].trim(),
          title: lines[i + 1].trim(),
          uploader: lines[i + 2].trim(),
          duration: parseFloat(lines[i + 3].trim()) || 0,
          url,
        });
      }
      resolve(results);
    });
    proc.on("error", reject);
  });
}

// ==================== ROUTES ====================

app.get("/", (req, res) => {
  res.json({
    name: "ديسكو مصر راديو",
    stream: "/api/radio/stream",
    endpoints: ["GET /api/radio/stream", "POST /api/radio/request", "POST /api/radio/skip", "GET /api/radio/queue", "GET /api/radio/current"]
  });
});

app.get("/api/radio/stream", (req, res) => {
  const clientId = randomUUID();
  res.setHeader("Content-Type", "audio/mpeg");
  res.setHeader("Cache-Control", "no-cache, no-store");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("Transfer-Encoding", "chunked");
  res.setHeader("icy-name", "ديسكو مصر راديو");
  res.setHeader("icy-genre", "Arabic Music");
  res.setHeader("icy-br", "128");
  res.flushHeaders();
  clients.push({ id: clientId, res });
  log("Listener connected", { id: clientId, total: clients.length });
  req.on("close", () => {
    clients = clients.filter(c => c.id !== clientId);
    log("Listener disconnected", { id: clientId, total: clients.length });
  });
});

app.post("/api/radio/request", async (req, res) => {
  const { query, requestedBy } = req.body;
  if (!query || typeof query !== "string" || !query.trim()) {
    return res.status(400).json({ error: "مطلوب اسم الأغنية" });
  }
  try {
    log("Searching for", query);
    const results = await searchYT(query.trim());
    if (!results.length) throw new Error("مش لاقيش نتايج");

    const song = { ...results[0], requestedBy: requestedBy || "مجهول" };
    queue.push(song);
    const wasPlaying = isPlaying;
    if (!isPlaying) playNext();

    res.json({
      success: true,
      isQueued: wasPlaying,
      queuePosition: wasPlaying ? queue.length : 0,
      song
    });
  } catch (err) {
    log("Search error", err.message);
    res.status(500).json({ error: err.message || "فشل البحث، جرب تاني" });
  }
});

app.post("/api/radio/skip", (req, res) => {
  const skipped = currentSong;
  const next = queue[0] || null;
  stopAll();
  playNext();
  res.json({ success: true, skipped, next });
});

app.get("/api/radio/queue", (req, res) => {
  res.json({
    isPlaying,
    currentSong,
    queue,
    listeners: clients.length
  });
});

app.get("/api/radio/current", (req, res) => {
  if (!currentSong) return res.json({ playing: false });
  res.json({ playing: true, song: currentSong });
});

app.post("/api/radio/previous", (req, res) => {
  if (history.length < 2) return res.json({ success: false, error: "مفيش أغاني سابقة" });
  const prev = history[1];
  queue.unshift(prev);
  stopAll();
  playNext();
  res.json({ success: true, song: prev });
});

// ==================== START ====================
app.listen(PORT, () => {
  log(`ديسكو مصر راديو شغال على بورت ${PORT}`);
});
