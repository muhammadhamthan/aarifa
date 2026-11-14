"""Round 2 driver: the free voice chain (LiveKit server + LiveKit Agents + faster-whisper + Piper + fake LLM)
with 1, 3 and 10 simultaneous callers. The kxvoice stack must already be up (compose/r2-voice.yml).

  python r2_driver.py [--tag cpu]   -> results/r2_<tag>_c<N>.json, r2_<tag>_stats.csv, r2_<tag>_summary.json
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parents[1]
RES = BENCH / "results"
PY = sys.executable
PROJECT = os.environ.get("R2_PROJECT", "kxvoice")  # kxbench for the real platform worker (Round 2b)
FILES = os.environ.get("R2_FILES", "r2-voice.yml").split(",")
CALLER_SERVICE = os.environ.get("R2_CALLER_SERVICE", "callers")
COMPOSE = ["docker", "compose", "-p", PROJECT] + [x for f in FILES for x in ("-f", str(BENCH / "compose" / f))]


def speech_stats() -> dict[str, Any]:
    url = os.environ.get("SPEECH_STATS_URL")  # speech server running outside Docker (GPU run)
    if url:
        import urllib.request

        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (local bench URL)
            return json.loads(r.read().decode())
    out = subprocess.run(["docker", "exec", "kxvoice-speech-1", "python", "-c",
                          "import urllib.request;print(urllib.request.urlopen('http://localhost:8100/stats').read().decode())"],
                         capture_output=True, text=True, check=False).stdout
    return json.loads(out) if out.strip() else {}


def window(csv_path: Path, lo: float, hi: float) -> dict[str, Any]:
    rows = [r for r in csv.DictReader(csv_path.open())
            if lo <= float(r["ts"]) <= hi and r["cpu_pct"].replace(".", "", 1).isdigit()]  # skip stopped ("--")
    by: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by.setdefault(r["container"], []).append(r)
    res = {}
    for name, rs in sorted(by.items()):
        mem = [float(r["mem_bytes"]) for r in rs]
        cpu = [float(r["cpu_pct"]) for r in rs]
        res[name] = {"ram_avg_mib": round(sum(mem) / len(mem) / 2**20, 1), "ram_peak_mib": round(max(mem) / 2**20, 1),
                     "cpu_avg_cores": round(sum(cpu) / len(cpu) / 100, 2), "cpu_peak_cores": round(max(cpu) / 100, 2),
                     "net_rx_mb": round((float(rs[-1]["net_rx"]) - float(rs[0]["net_rx"])) / 1e6, 2),
                     "net_tx_mb": round((float(rs[-1]["net_tx"]) - float(rs[0]["net_tx"])) / 1e6, 2)}
    return res


def main() -> None:
    tag = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "cpu"
    counts = [int(x) for x in os.environ.get("CALLER_COUNTS", "1,3,10").split(",")]
    csv_path = RES / f"r2_{tag}_stats.csv"
    sampler = subprocess.Popen([PY, str(BENCH / "scripts" / "sample.py"), "stream", "--project", PROJECT,
                                "--out", str(csv_path), "--seconds", "7200"])
    summary: dict[str, Any] = {"tag": tag}
    try:
        t = time.time()
        time.sleep(60)
        summary["idle"] = {"span": [t, time.time()]}
        for n in counts:
            s0 = speech_stats()
            t0 = time.time()
            env = dict(os.environ, CALLERS=str(n), TURNS="4", TAG=f"{tag}_c{n}")
            subprocess.run([*COMPOSE, "--profile", "run", "run", "--rm", "--no-deps", CALLER_SERVICE], env=env, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t1 = time.time()
            s1 = speech_stats()
            res = json.loads((RES / f"r2_callers_{tag}_c{n}.json").read_text(encoding="utf-8"))
            d = {k: round(s1.get(k, 0) - s0.get(k, 0), 2) for k in s1 if k != "load_s"}
            summary[f"c{n}"] = {
                "span": [t0, t1],
                "answered": res["answered"], "missed": res["missed"],
                "reply_s_p50": res["reply_s_p50"], "reply_s_p95": res["reply_s_p95"], "reply_s_max": res["reply_s_max"],
                "replies_under_3s": res.get("replies_under_3s"),
                "speech_server": d | {
                    "stt_rtf": round(d["stt_busy_s"] / d["stt_audio_s"], 3) if d.get("stt_audio_s") else None,
                    "tts_rtf": round(d["tts_busy_s"] / d["tts_audio_s"], 3) if d.get("tts_audio_s") else None},
            }
            time.sleep(20)  # let warm processes settle between runs
    finally:
        time.sleep(5)
        sampler.terminate()
        sampler.wait(timeout=30)
    for key, v in summary.items():
        if isinstance(v, dict) and "span" in v:
            v["containers"] = window(csv_path, *v["span"])
    (RES / f"r2_{tag}_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "containers"} if isinstance(v, dict) else v
                      for k, v in summary.items()}, indent=1))


if __name__ == "__main__":
    main()
