"""Ten fake website callers for the voice bench. Each joins its own LiveKit room (the agent worker is
dispatched automatically), says a real speech clip (Piper-made, en/ar/hi/zh), waits for the agent's spoken
reply, and repeats. Measures time from the end of the caller's speech to the agent's first audible audio.

  python voice_callers.py --callers 10 --turns 4 --clips /clips --out /results/r2_callers.json
Env: LIVEKIT_URL (ws://livekit:7880), LIVEKIT_API_KEY, LIVEKIT_API_SECRET.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
from livekit import api, rtc

RATE = 48_000
FRAME = RATE // 100  # 10 ms


def load_clip(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        sr = w.getframerate()
    n = int(x.size * RATE / sr)
    return np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.int16)


async def caller(i: int, clips: list[np.ndarray], turns: int, out: list[dict[str, Any]],
                 meta: dict[str, str] | None = None) -> None:
    """`meta` is the participant metadata the platform's token endpoint would set (deal_ref, language)."""
    url, key, secret = os.environ["LIVEKIT_URL"], os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    room_name = f"bench-call-{i}-{int(time.time())}"
    tok = (api.AccessToken(key, secret).with_identity(f"caller-{i}")
           .with_grants(api.VideoGrants(room_join=True, room=room_name)))
    if meta:
        tok = tok.with_metadata(json.dumps(meta))
    token = tok.to_jwt()
    room = rtc.Room()
    heard: asyncio.Queue[float] = asyncio.Queue()
    speaking_done = {"t": 0.0, "waiting": False}
    audio = {"first": 0.0, "last": 0.0}  # agent audio seen at all (for the recording notice)

    async def listen(track: rtc.Track) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=RATE, num_channels=1):
            pcm = np.frombuffer(ev.frame.data, dtype=np.int16)
            loud = bool(pcm.size) and float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) > 500
            if loud:
                now = time.perf_counter()
                audio["first"] = audio["first"] or now
                audio["last"] = now
            if speaking_done["waiting"] and loud:
                speaking_done["waiting"] = False
                heard.put_nowait(time.perf_counter())

    @room.on("track_subscribed")
    def _on(track: rtc.Track, *_: object) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.ensure_future(listen(track))

    t_join = time.perf_counter()
    await room.connect(url, token)
    source = rtc.AudioSource(RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    joined = time.perf_counter() - t_join
    silence = np.zeros(FRAME, dtype=np.int16)

    async def push(samples: np.ndarray) -> None:
        for k in range(0, samples.size - FRAME + 1, FRAME):
            await source.capture_frame(rtc.AudioFrame(samples[k:k + FRAME].tobytes(), RATE, 1, FRAME))

    for _ in range(100):  # 1 s of silence so the agent joins and starts listening (greeting ignored)
        await source.capture_frame(rtc.AudioFrame(silence.tobytes(), RATE, 1, FRAME))
    notice: dict[str, float | None] = {"first_audio_s": None, "done_s": None}
    if meta:  # the real worker speaks the recording notice first and cannot be interrupted: wait it out
        t_wait = time.perf_counter()
        while time.perf_counter() - t_wait < 40:
            await push(np.tile(silence, 25))
            if audio["first"] and time.perf_counter() - audio["last"] > 1.5:
                notice = {"first_audio_s": round(audio["first"] - t_join, 2), "done_s": round(audio["last"] - t_join, 2)}
                break
    else:
        await asyncio.sleep(4)
    rows = []
    for t in range(turns):
        clip = clips[(i + t) % len(clips)]
        await push(clip)
        speaking_done["t"] = time.perf_counter()
        speaking_done["waiting"] = True
        try:
            while True:
                fill = asyncio.ensure_future(push(np.tile(silence, 50)))  # keep sending silence (0.5 s)
                try:
                    heard_at = await asyncio.wait_for(heard.get(), timeout=0.5)
                    break
                except TimeoutError:
                    if time.perf_counter() - speaking_done["t"] > 60:  # measure slow replies, do not drop them
                        raise
                finally:
                    await fill
            rows.append(round(heard_at - speaking_done["t"], 3))
        except TimeoutError:
            rows.append(None)
            speaking_done["waiting"] = False
        await push(np.tile(silence, 600))  # let the agent finish talking (6 s)
    out.append({"caller": i, "join_s": round(joined, 3), "notice": notice, "reply_after_speech_s": rows})
    await room.disconnect()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--callers", type=int, default=10)
    p.add_argument("--turns", type=int, default=4)
    p.add_argument("--clips", type=Path, default=Path("/clips"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--meta", type=Path, help="JSON list of {deal_ref, language}, one per caller (real worker)")
    a = p.parse_args()
    paths = sorted(a.clips.glob("clip*.wav"))
    clips = [load_clip(c) for c in paths]
    metas: list[dict[str, str]] = json.loads(a.meta.read_text(encoding="utf-8")) if a.meta else []

    def clips_for(i: int) -> list[np.ndarray]:  # a caller speaks the language its deal is in
        if not metas:
            return clips
        lang = metas[i % len(metas)]["language"]
        return [c for pth, c in zip(paths, clips, strict=True) if pth.stem.endswith(f"_{lang}")] or clips

    out: list[dict[str, Any]] = []
    t0 = time.time()
    await asyncio.gather(*(caller(i, clips_for(i), a.turns, out, metas[i % len(metas)] if metas else None)
                           for i in range(a.callers)))
    lat = [x for r in out for x in r["reply_after_speech_s"] if x is not None]
    summary = {"start": t0, "end": time.time(), "callers": a.callers, "turns_each": a.turns,
               "answered": len(lat), "missed": sum(x is None for r in out for x in r["reply_after_speech_s"]),
               "reply_s_p50": round(statistics.median(lat), 2) if lat else None,
               "reply_s_p95": round(sorted(lat)[min(len(lat) - 1, max(0, math.ceil(len(lat) * 0.95) - 1))], 2) if lat else None,
               "replies_under_3s": sum(x <= 3 for x in lat),
               "reply_s_max": max(lat) if lat else None, "per_caller": out}
    a.out.write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "per_caller"}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
