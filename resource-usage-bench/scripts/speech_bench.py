"""Speech-model bench (free stack): RAM, CPU/GPU time, latency and real-time factor, 1 and 10 at once.

  python speech_bench.py piper        --models /models --out piper_cpu.json
  python speech_bench.py clips        --models /models --clips /clips          (makes test speech with Piper)
  python speech_bench.py fwhisper     --models /models --clips /clips --device cpu|cuda --out fw_cpu.json
  python speech_bench.py sherpa       --models /models --clips /clips --out sherpa_cpu.json
  python speech_bench.py chatterbox   --device cpu|cuda --out cb_cpu.json

RTF (real-time factor) = processing time / audio length: below 1.0 is faster than real time; a live call
needs well below 1.0. "x10" runs ten jobs at once, like ten callers. RAM is the process's peak resident memory
(psutil, sampled every 50 ms). WER is measured on Piper-made speech of a known text (a rough check, easier
than real phone audio).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psutil

SENTENCES = {
    "en": ["Our aluminium container holds seven hundred and fifty millilitres and comes with a paper lid.",
           "The minimum order is five thousand pieces, delivered in ten to fourteen working days.",
           "We can print your logo on the lid in up to four colours.",
           "Each carton holds five hundred containers, about eight kilograms.",
           "Thank you for your call, our sales team will send the quotation on WhatsApp."],
    "ar": ["حاوية الألمنيوم لدينا تتسع لسبعمائة وخمسين مليلترا وتأتي مع غطاء ورقي.",
           "الحد الأدنى للطلب خمسة آلاف قطعة، ويتم التسليم خلال عشرة إلى أربعة عشر يوم عمل.",
           "يمكننا طباعة شعاركم على الغطاء بأربعة ألوان.",
           "شكرا لاتصالكم، سيرسل فريق المبيعات عرض السعر عبر واتساب."],
    "hi": ["हमारा एल्यूमिनियम कंटेनर सात सौ पचास मिलीलीटर का है और कागज़ के ढक्कन के साथ आता है।",
           "न्यूनतम ऑर्डर पाँच हज़ार पीस है।",
           "धन्यवाद, हमारी सेल्स टीम व्हाट्सऐप पर कोटेशन भेजेगी।"],
    "zh": ["我们的铝箔餐盒容量为七百五十毫升，配有纸盖。", "最低起订量为五千个，十到十四个工作日内交货。",
           "感谢您的来电，我们的销售团队会通过微信发送报价。"],
}
PIPER_VOICE = {"en": "en/en_US/lessac/medium/en_US-lessac-medium.onnx", "ar": "ar/ar_JO/kareem/medium/ar_JO-kareem-medium.onnx",
               "hi": "hi/hi_IN/pratham/medium/hi_IN-pratham-medium.onnx", "zh": "zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx"}


class Peak:
    """Peak RSS of this process (plus children) and its CPU seconds, while the block runs."""

    def __init__(self) -> None:
        self.proc = psutil.Process()
        self.peak = 0
        self._stop = threading.Event()

    def _rss(self) -> int:
        try:
            return self.proc.memory_info().rss + sum(c.memory_info().rss for c in self.proc.children(recursive=True))
        except psutil.Error:
            return 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self._rss())
            time.sleep(0.05)

    def __enter__(self) -> Peak:
        self.cpu0 = sum(self.proc.cpu_times()[:2])
        self.t0 = time.perf_counter()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self.thread.join()
        self.wall = time.perf_counter() - self.t0
        self.cpu = sum(self.proc.cpu_times()[:2]) - self.cpu0

    def result(self) -> dict[str, float]:
        return {"wall_s": round(self.wall, 3), "cpu_s": round(self.cpu, 2), "cores_busy": round(self.cpu / self.wall, 2),
                "peak_rss_mb": round(self.peak / 1e6, 1)}


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def wer(ref: str, hyp: str, lang: str) -> float:
    tok = (lambda s: list(re.sub(r"\s+", "", s))) if lang == "zh" else (lambda s: re.findall(r"\w+", s.lower()))
    r, h = tok(ref), tok(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


# ------------------------------------------------------------------ Piper TTS
def piper(models: Path, out: Path, workers: int = 10) -> None:
    from piper import PiperVoice

    res: dict[str, Any] = {"model_files_mb": {}}
    base = Path(models) / "piper-voices"
    for lang, rel in PIPER_VOICE.items():
        res["model_files_mb"][lang] = round(((base / rel).stat().st_size + (base / (rel + ".json")).stat().st_size) / 1e6, 1)
    rss0 = psutil.Process().memory_info().rss
    with Peak() as p:
        voices = {lang: PiperVoice.load(str(base / rel)) for lang, rel in PIPER_VOICE.items()}
    res["load_4_voices"] = p.result() | {"added_rss_mb": round((psutil.Process().memory_info().rss - rss0) / 1e6, 1)}

    def synth(lang: str, text: str) -> tuple[float, float, float]:
        v = voices[lang]
        t0 = time.perf_counter()
        first = None
        samples = 0
        for chunk in v.synthesize(text):
            first = first or time.perf_counter() - t0
            samples += len(chunk.audio_int16_bytes) // 2
        total = time.perf_counter() - t0
        return first or total, total, samples / v.config.sample_rate

    per_lang: dict[str, Any] = {}
    for lang, texts in SENTENCES.items():
        synth(lang, texts[0])  # warm
        rows = [synth(lang, t) for t in texts]
        audio = sum(r[2] for r in rows)
        per_lang[lang] = {"ttfa_s_avg": round(sum(r[0] for r in rows) / len(rows), 3),
                          "rtf": round(sum(r[1] for r in rows) / audio, 3), "audio_s": round(audio, 1)}
    res["single"] = per_lang
    jobs = [(lang, t) for lang, ts in SENTENCES.items() for t in ts] * 2
    with Peak() as p, ThreadPoolExecutor(workers) as ex:
        rows = list(ex.map(lambda a: synth(*a), jobs))
    audio = sum(r[2] for r in rows)
    res[f"x{workers}_parallel"] = p.result() | {"jobs": len(jobs), "audio_s": round(audio, 1),
                                                 "throughput_audio_s_per_s": round(audio / p.wall, 2),
                                                 "ttfa_s_p95": round(sorted(r[0] for r in rows)[int(len(rows) * 0.95) - 1], 3)}
    Path(out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))


def clips(models: Path, dest: Path) -> None:
    """Ten test clips (Piper speech, 16 kHz mono WAV) + a JSON of what was said."""
    from piper import PiperVoice

    dest.mkdir(parents=True, exist_ok=True)
    base = Path(models) / "piper-voices"
    truth = {}
    pick = [("en", 0), ("en", 1), ("en", 3), ("ar", 0), ("ar", 1), ("ar", 3), ("hi", 0), ("hi", 2), ("zh", 0), ("zh", 1)]
    for n, (lang, i) in enumerate(pick):
        v = PiperVoice.load(str(base / PIPER_VOICE[lang]))
        text = SENTENCES[lang][i]
        path = dest / f"clip{n:02d}_{lang}.wav"
        with wave.open(str(path), "wb") as w:
            v.synthesize_wav(text, w)
        truth[path.name] = {"lang": lang, "text": text, "seconds": round(wav_seconds(path), 2)}
    (dest / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(truth, ensure_ascii=False, indent=1))


# ------------------------------------------------------------------ faster-whisper STT
def wav_to_16k(src: Any) -> Any:
    """WAV -> 16 kHz mono float32 for faster-whisper. Decoding here avoids faster-whisper's PyAV path:
    faster-whisper 1.2.1 passes `metadata_errors`, which PyAV 19 no longer accepts (same helper as speech_server)."""
    import numpy as np

    with wave.open(src) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != 16_000 and x.size:
        x = np.interp(np.linspace(0, x.size - 1, int(x.size * 16_000 / rate)), np.arange(x.size), x)
    return x.astype(np.float32)


def fwhisper(models: Path, clip_dir: Path, device: str, out: Path) -> None:
    from faster_whisper import WhisperModel

    truth = json.loads((clip_dir / "truth.json").read_text(encoding="utf-8"))
    path = Path(models) / "faster-whisper-large-v3-turbo"
    res: dict[str, Any] = {"device": device, "model_dir_gb": round(sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1e9, 2)}
    configs = [("int8", 1)] if device == "cpu" else [("float16", 1), ("int8_float16", 1)]
    for compute, _ in configs:
        rss0 = psutil.Process().memory_info().rss
        gpu0 = _gpu_mem_mb() if device == "cuda" else None  # the desktop already uses part of the card
        with Peak() as p:
            model = WhisperModel(str(path), device=device, compute_type=compute, cpu_threads=os.cpu_count() or 4,
                                 num_workers=10)
        load = p.result() | {"added_rss_mb": round((psutil.Process().memory_info().rss - rss0) / 1e6, 1)}

        audio = {n: wav_to_16k(str(clip_dir / n)) for n in truth}  # decoded before timing starts

        def run(name: str, m: Any = model) -> tuple[float, float, str]:
            t0 = time.perf_counter()
            segs, _ = m.transcribe(audio[name], language=truth[name]["lang"], beam_size=1, vad_filter=False)
            text = " ".join(s.text for s in segs)
            return time.perf_counter() - t0, truth[name]["seconds"], text

        names = sorted(truth)
        run(names[0])  # warm
        single = [run(n) for n in names]
        with Peak() as p10, ThreadPoolExecutor(10) as ex:
            par = list(ex.map(run, names))
        gpu = _gpu_mem_mb() if device == "cuda" else None
        res[compute] = {
            "load": load,
            "single_rtf": round(sum(r[0] for r in single) / sum(r[1] for r in single), 3),
            "single_latency_s_avg": round(sum(r[0] for r in single) / len(single), 3),
            "wer_by_lang": _wer_by_lang(names, single, truth),
            "x10_parallel": p10.result() | {"audio_s": round(sum(r[1] for r in par), 1),
                                            "rtf_effective": round(p10.wall / sum(r[1] for r in par), 3),
                                            "latency_s_max": round(max(r[0] for r in par), 2)},
            "gpu_mem_used_mb": gpu,
            "gpu_mem_model_mb": round(gpu - gpu0, 1) if gpu is not None and gpu0 is not None else None,
        }
        del model
        import gc

        gc.collect()
    Path(out).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=1, ensure_ascii=False))


def _wer_by_lang(names: list[str], rows: list[tuple[float, float, str]], truth: dict[str, Any]) -> dict[str, float]:
    by: dict[str, list[float]] = {}
    for n, r in zip(names, rows, strict=True):
        by.setdefault(truth[n]["lang"], []).append(wer(truth[n]["text"], r[2], truth[n]["lang"]))
    return {k: round(sum(v) / len(v), 3) for k, v in by.items()}


def _gpu_mem_mb() -> float | None:
    try:
        import subprocess

        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, check=False).stdout
        return float(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


# ------------------------------------------------------------------ sherpa-onnx (Whisper turbo, ONNX int8, CPU)
def sherpa(models: Path, clip_dir: Path, out: Path) -> None:
    import numpy as np
    import sherpa_onnx

    truth = json.loads((clip_dir / "truth.json").read_text(encoding="utf-8"))
    d = Path(models) / "sherpa-onnx-whisper-turbo"
    enc = next(d.rglob("*encoder.int8.onnx"))
    dec = next(d.rglob("*decoder.int8.onnx"))
    tokens = next(d.rglob("*tokens.txt"))
    res: dict[str, Any] = {"model_files_gb": round(sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) / 1e9, 2)}

    def make(lang: str) -> Any:
        return sherpa_onnx.OfflineRecognizer.from_whisper(encoder=str(enc), decoder=str(dec), tokens=str(tokens),
                                                          language=lang, task="transcribe",
                                                          num_threads=os.cpu_count() or 4)

    with Peak() as p:
        recs = {lang: make(lang) for lang in ("en",)}
    res["load_one_recognizer"] = p.result()

    def run(name: str) -> tuple[float, float, str]:
        lang = truth[name]["lang"]
        rec = recs.get(lang) or recs.setdefault(lang, make(lang))
        with wave.open(str(clip_dir / name)) as w:
            sr = w.getframerate()
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        t0 = time.perf_counter()
        s = rec.create_stream()
        s.accept_waveform(sr, audio)
        rec.decode_stream(s)
        return time.perf_counter() - t0, truth[name]["seconds"], s.result.text

    names = sorted(truth)
    for n in names:
        run(n)  # warm + create per-language recognizers
    single = [run(n) for n in names]
    with Peak() as p10, ThreadPoolExecutor(10) as ex:
        par = list(ex.map(run, names))
    res["single_rtf"] = round(sum(r[0] for r in single) / sum(r[1] for r in single), 3)
    res["wer_by_lang"] = _wer_by_lang(names, single, truth)
    res["x10_parallel"] = p10.result() | {"rtf_effective": round(p10.wall / sum(r[1] for r in par), 3)}
    Path(out).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=1, ensure_ascii=False))


# ------------------------------------------------------------------ Chatterbox TTS (multilingual)
def chatterbox(device: str, out: Path) -> None:
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    res: dict[str, Any] = {"device": device, "torch": torch.__version__}
    gpu0 = _gpu_mem_mb() if device == "cuda" else None  # the desktop already uses part of the card
    with Peak() as p:
        model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    res["load"] = p.result()
    rows = {}
    for lang in ("en", "ar", "hi", "zh"):
        text = SENTENCES[lang][0]
        model.generate(text, language_id=lang)  # warm
        t0 = time.perf_counter()
        wav = model.generate(text, language_id=lang)
        dt = time.perf_counter() - t0
        secs = wav.shape[-1] / model.sr
        rows[lang] = {"latency_s": round(dt, 2), "audio_s": round(secs, 2), "rtf": round(dt / secs, 3)}
    res["single"] = rows
    res["gpu_mem_used_mb"] = _gpu_mem_mb() if device == "cuda" else None
    if device == "cuda":
        res["gpu_mem_model_mb"] = round(res["gpu_mem_used_mb"] - gpu0, 1) if res["gpu_mem_used_mb"] and gpu0 else None
        res["torch_max_allocated_mb"] = round(torch.cuda.max_memory_allocated() / 1e6, 1)
    res["peak_rss_mb_total"] = round(psutil.Process().memory_info().rss / 1e6, 1)
    Path(out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["piper", "clips", "fwhisper", "sherpa", "chatterbox"])
    p.add_argument("--models", type=Path, default=Path("/models"))
    p.add_argument("--clips", type=Path, default=Path("/clips"))
    p.add_argument("--device", default="cpu")
    p.add_argument("--out", type=Path, default=Path("out.json"))
    a = p.parse_args()
    if a.cmd == "piper":
        piper(a.models, a.out)
    elif a.cmd == "clips":
        clips(a.models, a.clips)
    elif a.cmd == "fwhisper":
        fwhisper(a.models, a.clips, a.device, a.out)
    elif a.cmd == "sherpa":
        sherpa(a.models, a.clips, a.out)
    else:
        chatterbox(a.device, a.out)


if __name__ == "__main__":
    main()
