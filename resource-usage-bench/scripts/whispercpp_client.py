"""whisper.cpp bench client: sends the Piper test clips to a running `whisper-server` (model loaded once, like a
real deployment), one by one and ten at once. Same clips, truth and WER as speech_bench.py.

  python whispercpp_client.py --url http://localhost:28181 --clips F:/kx-bench/clips --container NAME --out X.json
RAM comes from `docker stats` of the server container (sampled every 0.5 s while requests run).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from speech_bench import _wer_by_lang


def mem_mib(container: str) -> float:
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
                         capture_output=True, text=True, check=False).stdout.split("/")[0].strip()
    num = float(out.rstrip("GiBMKk") or 0)
    return num * 1024 if "GiB" in out else num


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--clips", type=Path, required=True)
    p.add_argument("--container", required=True)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    truth = json.loads((a.clips / "truth.json").read_text(encoding="utf-8"))
    names = sorted(truth)
    for _ in range(300):  # wait for the model to load
        try:
            if httpx.get(a.url, timeout=5).status_code < 500:
                break
        except httpx.HTTPError:
            pass
        time.sleep(2)
    idle = mem_mib(a.container)

    def run(name: str) -> tuple[float, float, str]:
        t0 = time.perf_counter()
        with (a.clips / name).open("rb") as f:
            r = httpx.post(f"{a.url}/inference", files={"file": (name, f, "audio/wav")},
                           data={"language": truth[name]["lang"], "response_format": "json", "temperature": "0"},
                           timeout=600)
        r.raise_for_status()
        return time.perf_counter() - t0, truth[name]["seconds"], str(r.json().get("text", ""))

    run(names[0])  # warm
    peak = {"v": 0.0}
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            peak["v"] = max(peak["v"], mem_mib(a.container))
            time.sleep(0.5)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    single = [run(n) for n in names]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(10) as ex:
        par = list(ex.map(run, names))
    wall = time.perf_counter() - t0
    stop.set()
    th.join()
    res: dict[str, Any] = {
        "ram_idle_mib": idle, "ram_peak_mib": peak["v"],
        "single_rtf": round(sum(r[0] for r in single) / sum(r[1] for r in single), 3),
        "single_latency_s_avg": round(sum(r[0] for r in single) / len(single), 3),
        "wer_by_lang": _wer_by_lang(names, single, truth),
        "x10_parallel": {"wall_s": round(wall, 2), "audio_s": round(sum(r[1] for r in par), 1),
                         "rtf_effective": round(wall / sum(r[1] for r in par), 3),
                         "latency_s_max": round(max(r[0] for r in par), 2)},
        "transcripts": {n: r[2] for n, r in zip(names, single, strict=True)},
    }
    a.out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "transcripts"}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
