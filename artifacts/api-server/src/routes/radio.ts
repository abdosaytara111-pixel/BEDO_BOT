import { Router } from "express";
import { radio } from "../lib/radio";
import { logger } from "../lib/logger";
import { randomUUID } from "crypto";

const radioRouter = Router();

radioRouter.get("/radio/stream", (req, res) => {
  const clientId = randomUUID();

  res.setHeader("Content-Type", "audio/mpeg");
  res.setHeader("Cache-Control", "no-cache, no-store");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("Transfer-Encoding", "chunked");
  res.setHeader("icy-name", "Highrise Radio");
  res.setHeader("icy-genre", "Music");
  res.setHeader("icy-br", "128");
  res.flushHeaders();

  radio.addClient(res, clientId);

  req.on("close", () => {
    radio.removeClient(clientId);
  });

  req.on("error", () => {
    radio.removeClient(clientId);
  });
});

radioRouter.post("/radio/add", (req, res) => {
  const { title, url, uploader, duration, requestedBy, id } = req.body as {
    title?: string; url?: string; uploader?: string;
    duration?: number; requestedBy?: string; id?: string;
  };

  if (!title || !url) {
    res.status(400).json({ error: "title و url مطلوبين" });
    return;
  }

  const song = radio.addDirectly({
    id: id || url,
    title,
    uploader: uploader || "",
    thumbnail: "",
    duration: duration || 0,
    url,
    requestedBy: requestedBy || "playlist",
  });

  res.json({ success: true, isQueued: (song as any).isQueued ?? false, queuePosition: (song as any).queuePosition ?? 0, song });
});

radioRouter.post("/radio/request", async (req, res) => {
  const { query, requestedBy } = req.body as { query?: string; requestedBy?: string };

  if (!query || typeof query !== "string" || query.trim().length === 0) {
    res.status(400).json({ error: "مطلوب اسم الأغنية" });
    return;
  }

  try {
    const song = await radio.searchAndAdd(query.trim(), requestedBy || "مجهول");
    const songAny = song as any;
    res.json({
      success: true,
      isQueued: songAny.isQueued ?? false,
      queuePosition: songAny.queuePosition ?? 0,
      song: {
        id: song.id,
        title: song.title,
        uploader: song.uploader,
        thumbnail: song.thumbnail,
        duration: song.duration,
        url: song.url,
        requestedBy: song.requestedBy,
      },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "فشل البحث، جرب تاني";
    logger.error({ err }, "Failed to add song");
    res.status(500).json({ error: msg });
  }
});

radioRouter.get("/radio/queue", (_req, res) => {
  const status = radio.getStatus();
  res.json(status);
});

radioRouter.post("/radio/skip", (_req, res) => {
  const skipped = radio.getCurrentSong();
  const nextInQueue = radio.getQueue()[0] ?? null;
  radio.skip();
  res.json({
    success: true,
    skipped: skipped
      ? { title: skipped.title, uploader: skipped.uploader, duration: skipped.duration }
      : null,
    next: nextInQueue
      ? { title: nextInQueue.title, uploader: nextInQueue.uploader, duration: nextInQueue.duration }
      : null,
  });
});

radioRouter.post("/radio/pick", (req, res) => {
  const { index } = req.body as { index?: number };
  if (!index || typeof index !== "number" || !Number.isInteger(index) || index < 1) {
    res.status(400).json({ error: "ابعت رقم صحيح ابتداءً من 1" });
    return;
  }
  const result = radio.pickSong(index);
  if (!result.success) {
    res.status(400).json({ error: result.error });
    return;
  }
  res.json({ success: true, song: result.song });
});

radioRouter.get("/radio/current", (_req, res) => {
  const current = radio.getCurrentSong();
  if (!current) {
    res.json({ playing: false });
    return;
  }
  res.json({ playing: true, song: current });
});

export default radioRouter;
