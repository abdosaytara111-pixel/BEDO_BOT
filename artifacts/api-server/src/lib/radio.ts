import { spawn, type ChildProcess } from "child_process";
import { type Response } from "express";
import { logger } from "./logger";

export interface SongInfo {
  id: string;
  title: string;
  uploader: string;
  thumbnail: string;
  duration: number;
  url: string;
  requestedBy: string;
}

interface Client {
  res: Response;
  id: string;
}

function normalizeArabic(text: string): string {
  return text
    .toLowerCase()
    .replace(/[\u064B-\u065F\u0670]/g, "")
    .replace(/[أإآا]/g, "ا")
    .replace(/[ىي]/g, "ي")
    .replace(/ة/g, "ه")
    .replace(/[^a-z0-9\u0600-\u06FF\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function scoreMatch(query: string, title: string, uploader: string): number {
  const normQuery = normalizeArabic(query);
  const normTitle = normalizeArabic(title);
  const normUploader = normalizeArabic(uploader);

  const queryWords = normQuery.split(/\s+/).filter(w => w.length > 1);
  if (queryWords.length === 0) return 0;

  let titleMatched = 0;
  let uploaderOnlyMatched = 0;
  for (const word of queryWords) {
    if (normTitle.includes(word)) {
      titleMatched++;
    } else if (normUploader.includes(word)) {
      uploaderOnlyMatched++;
    }
  }
  return (titleMatched + uploaderOnlyMatched * 0.25) / queryWords.length;
}

type SearchResult = { id: string; title: string; uploader: string; duration: number; url: string; rank: number };

function parseResults(output: string): SearchResult[] {
  const lines = output.trim().split("\n");
  const results: SearchResult[] = [];
  let rank = 0;
  for (let i = 0; i + 4 < lines.length; i += 5) {
    results.push({
      id: lines[i].trim(),
      title: lines[i + 1].trim(),
      uploader: lines[i + 2].trim(),
      duration: parseFloat(lines[i + 3].trim()) || 0,
      url: lines[i + 4].trim(),
      rank: rank++,
    });
  }
  return results;
}

const MIX_KEYWORDS = /\b(mix|dj set|podcast|nonstop|non-stop|megamix|medley|ميكس|بودكاست|برنامج|episode|حلقة|مقابله|interview|reaction|ردة فعل)\b/i;

function pickBest(query: string, results: SearchResult[]): SearchResult | null {
  if (results.length === 0) return null;

  const scored = results.map(r => ({
    ...r,
    score: scoreMatch(query, r.title, r.uploader),
    isMix: MIX_KEYWORDS.test(r.title),
    goodDuration: r.duration >= 60 && r.duration <= 660,
  }));

  const clean = scored.filter(r => !r.isMix && r.goodDuration);
  const pool = clean.length > 0 ? clean : scored.filter(r => r.goodDuration);
  const finalPool = pool.length > 0 ? pool : scored;

  finalPool.sort((a, b) => b.score - a.score || a.rank - b.rank);

  const best = finalPool[0];
  logger.info({ title: best.title, score: best.score, isMix: best.isMix, rank: best.rank }, "Best result picked");
  return best;
}

class RadioEngine {
  private queue: SongInfo[] = [];
  private currentSong: SongInfo | null = null;
  private clients: Client[] = [];
  private ffmpegProcess: ChildProcess | null = null;
  private ytdlpProcess: ChildProcess | null = null;
  private isPlaying = false;

  private scSearch(searchQuery: string): Promise<SearchResult[]> {
    return new Promise((resolve, reject) => {
      const args = [
        "--flat-playlist",
        "--print", "%(id)s\n%(title)s\n%(uploader)s\n%(duration)s\n%(url)s",
        `scsearch8:${searchQuery}`,
      ];
      const proc = spawn("yt-dlp", args);
      let output = "";
      let errOutput = "";
      proc.stdout.on("data", (d: Buffer) => { output += d.toString(); });
      proc.stderr.on("data", (d: Buffer) => { errOutput += d.toString(); });
      proc.on("close", (code) => {
        if (code !== 0) return reject(new Error(errOutput));
        resolve(parseResults(output));
      });
    });
  }

  async searchAndAdd(query: string, requestedBy: string): Promise<SongInfo> {
    logger.info({ query }, "Searching for song on SoundCloud");

    const results = await this.scSearch(query);

    let picked = pickBest(query, results);

    if (!picked || scoreMatch(query, picked.title, picked.uploader) < 0.4) {
      const stripped = query.replace(/\b(اغنية|اغنيه|موسيقى|song|audio|official|video|كلمات|lyrics)\b/gi, "").trim();
      if (stripped && stripped !== query) {
        logger.info({ stripped }, "Low score, retrying with stripped query");
        const retry = await this.scSearch(stripped);
        const retryPick = pickBest(query, [...results, ...retry]);
        if (retryPick) picked = retryPick;
      }
    }

    if (!picked) throw new Error("مفيش نتايج");

    const finalScore = scoreMatch(query, picked.title, picked.uploader);
    if (finalScore < 0.25) {
      logger.warn({ query, bestTitle: picked.title, score: finalScore }, "Score too low, rejecting result");
      throw new Error(`مش لاقيت "${query}"، جرب كتابة اسم الأغنية والمطرب`);
    }

    const song: SongInfo = {
      id: picked.id,
      title: picked.title,
      uploader: picked.uploader,
      thumbnail: "",
      duration: Math.round(picked.duration),
      url: picked.url,
      requestedBy,
    };

    return this.addToQueue(song);
  }

  addDirectly(song: SongInfo): SongInfo {
    return this.addToQueue(song);
  }

  private addToQueue(song: SongInfo): SongInfo {
    logger.info({ title: song.title, uploader: song.uploader }, "Found song");
    this.queue.push(song);

    const wasPlaying = this.isPlaying;
    if (!this.isPlaying) {
      this.playNext();
    }

    (song as any).queuePosition = wasPlaying ? this.queue.length : 0;
    (song as any).isQueued = wasPlaying;

    return song;
  }

  private playNext() {
    if (this.queue.length === 0) {
      this.currentSong = null;
      this.isPlaying = false;
      logger.info("Queue is empty, radio stopped");
      return;
    }

    this.currentSong = this.queue.shift()!;
    this.isPlaying = true;
    logger.info({ title: this.currentSong.title }, "Now playing");

    this.stopAll();
    this.startStream(this.currentSong.url);
  }

  private startStream(songUrl: string) {
    const getUrlArgs = [
      "--no-playlist",
      "-f", "bestaudio/best",
      "-g",
      songUrl,
    ];

    logger.info({ songUrl }, "Getting direct audio URL from SoundCloud");
    const getUrl = spawn("yt-dlp", getUrlArgs);
    this.ytdlpProcess = getUrl;

    let directUrl = "";
    let errBuf = "";

    getUrl.stdout.on("data", (d: Buffer) => { directUrl += d.toString(); });
    getUrl.stderr.on("data", (d: Buffer) => { errBuf += d.toString(); });

    getUrl.on("close", (code) => {
      if (this.ytdlpProcess !== getUrl) return;
      this.ytdlpProcess = null;

      directUrl = directUrl.trim().split("\n")[0].trim();

      if (code !== 0 || !directUrl.startsWith("http")) {
        logger.error({ code, errBuf, directUrl }, "Failed to get direct audio URL, skipping");
        this.playNext();
        return;
      }

      logger.info({ directUrl: directUrl.substring(0, 80) }, "Got direct URL, starting ffmpeg");

      const ffmpegArgs = [
        "-re",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", directUrl,
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "128k",
        "-ar", "44100",
        "-f", "mp3",
        "-",
      ];

      const ffmpeg = spawn("ffmpeg", ffmpegArgs, { stdio: ["ignore", "pipe", "pipe"] });
      this.ffmpegProcess = ffmpeg;

      ffmpeg.stderr.on("data", (d: Buffer) => {
        const msg = d.toString();
        if (!msg.includes("frame=") && !msg.includes("size=") && !msg.includes("time=") && !msg.includes("speed=")) {
          logger.debug({ msg: msg.trim() }, "ffmpeg");
        }
      });

      ffmpeg.stdout.on("data", (chunk: Buffer) => {
        this.broadcast(chunk);
      });

      ffmpeg.on("close", (code) => {
        if (this.ffmpegProcess !== ffmpeg) return;
        this.ffmpegProcess = null;
        logger.info({ code }, "Song finished, moving to next");
        this.playNext();
      });

      ffmpeg.on("error", (err) => {
        if (this.ffmpegProcess !== ffmpeg) return;
        this.ffmpegProcess = null;
        logger.error({ err }, "ffmpeg error");
        this.playNext();
      });
    });

    getUrl.on("error", (err) => {
      if (this.ytdlpProcess !== getUrl) return;
      this.ytdlpProcess = null;
      logger.error({ err }, "yt-dlp get-url error");
      this.playNext();
    });
  }

  private broadcast(chunk: Buffer) {
    const dead: Client[] = [];
    for (const client of this.clients) {
      try {
        client.res.write(chunk);
      } catch {
        dead.push(client);
      }
    }
    for (const d of dead) {
      this.clients = this.clients.filter((c) => c.id !== d.id);
    }
  }

  private stopAll() {
    if (this.ytdlpProcess) {
      try { this.ytdlpProcess.kill("SIGKILL"); } catch {}
      this.ytdlpProcess = null;
    }
    if (this.ffmpegProcess) {
      try { this.ffmpegProcess.kill("SIGKILL"); } catch {}
      this.ffmpegProcess = null;
    }
  }

  addClient(res: Response, id: string) {
    this.clients.push({ res, id });
    logger.info({ id, total: this.clients.length }, "Client connected to radio");
  }

  removeClient(id: string) {
    this.clients = this.clients.filter((c) => c.id !== id);
    logger.info({ id, total: this.clients.length }, "Client disconnected from radio");
  }

  skip() {
    logger.info("Skipping current song");
    this.stopAll();
    this.playNext();
  }

  pickSong(index: number): { success: boolean; song?: SongInfo; error?: string } {
    if (this.queue.length === 0) {
      return { success: false, error: "القائمة فاضية" };
    }
    const zeroIndex = index - 1;
    if (zeroIndex < 0 || zeroIndex >= this.queue.length) {
      return { success: false, error: `الرقم مش موجود، القائمة فيها ${this.queue.length} أغاني` };
    }
    const [song] = this.queue.splice(zeroIndex, 1);
    this.queue.unshift(song);
    logger.info({ title: song.title, index }, "Song picked from queue, skipping to it");
    this.stopAll();
    this.playNext();
    return { success: true, song };
  }

  getStatus() {
    return {
      isPlaying: this.isPlaying,
      currentSong: this.currentSong,
      queue: this.queue,
      listeners: this.clients.length,
    };
  }

  getQueue() {
    return this.queue;
  }

  getCurrentSong() {
    return this.currentSong;
  }
}

export const radio = new RadioEngine();
