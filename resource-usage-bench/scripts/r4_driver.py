"""Round 4 driver: Qwen3-Embedding-0.6B vs BGE-M3 vs multilingual-e5-large, each alone on TEI CPU.

  python r4_driver.py            (writes results/r4_<name>.json, r4_<name>_stats.csv, r4_summary.json)

Per model: start -> 60 s idle (idle RAM) -> embed_bench (index 48 chunks, 30 single + 30 burst queries,
24-question multilingual quality test) while docker stats are recorded -> stop. Model size on disk is read from
the shared Hugging Face cache.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
RES = BENCH / "results"
PY = sys.executable
COMPOSE = ["docker", "compose", "-p", "kxembed", "-f", str(BENCH / "compose" / "r4-embed.yml")]
HF = Path(r"F:\kx-bench\hf")
MODELS = [  # name, model id, prompt format
    ("qwen3_06b", "Qwen/Qwen3-Embedding-0.6B", "qwen3"),
    ("bge_m3", "BAAI/bge-m3", "plain"),
    ("e5_large", "intfloat/multilingual-e5-large", "e5"),
]


def dir_bytes(model_id: str) -> int:
    d = HF / ("models--" + model_id.replace("/", "--"))
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file() and not f.is_symlink())


def stats(path: Path) -> dict[str, float]:
    rows = [r for r in csv.DictReader(path.open()) if r["container"].endswith("embeddings")] if path.exists() else []
    if not rows:
        return {}
    mem = [float(r["mem_bytes"]) for r in rows]
    cpu = [float(r["cpu_pct"]) for r in rows]
    return {"ram_peak_mib": round(max(mem) / 2**20, 1), "ram_avg_mib": round(sum(mem) / len(mem) / 2**20, 1),
            "cpu_peak_pct": round(max(cpu), 1), "cpu_avg_pct": round(sum(cpu) / len(cpu), 1), "samples": len(rows)}


def idle_ram() -> float:
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],
                         capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        if "kxembed" in line:
            v = line.split()[1]
            num = float(v.rstrip("GiBMKk"))
            return round(num * 1024 if "GiB" in v else num, 1)
    return 0.0


def main() -> None:
    summary: dict[str, object] = {}
    only = set(sys.argv[1:])
    for name, model_id, fmt in MODELS:
        if only and name not in only:
            continue
        env = dict(os.environ, MODEL_ID=model_id)
        t0 = time.time()
        subprocess.run([*COMPOSE, "up", "-d", "--force-recreate"], env=env, check=True)
        csv_path = RES / f"r4_{name}_stats.csv"
        sampler = subprocess.Popen([PY, str(BENCH / "scripts" / "sample.py"), "stream", "--project", "kxembed",
                                    "--out", str(csv_path), "--seconds", "3600"])
        out = RES / f"r4_{name}.json"
        # embed_bench waits until the model answers; the time to that point is the cold start
        subprocess.run([PY, "-c", "import httpx,time\nfor _ in range(240):\n try:\n  r=httpx.post('http://localhost:28081/v1/embeddings',json={'input':['x'],'model':'m'},timeout=30)\n  if r.status_code==200: break\n except Exception: pass\n time.sleep(2)"],
                       check=False)
        ready_s = round(time.time() - t0, 1)
        time.sleep(60)
        idle = idle_ram()
        subprocess.run([PY, str(BENCH / "scripts" / "embed_bench.py"), "--url", "http://localhost:28081",
                        "--format", fmt, "--out", str(out)], check=False)
        time.sleep(5)
        sampler.terminate()
        sampler.wait(timeout=30)
        res = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"error": "bench failed"}
        res.update({"model": model_id, "ready_seconds": ready_s, "ram_idle_after_warmup_mib": idle,
                    "model_disk_bytes": dir_bytes(model_id), "container": stats(csv_path)})
        out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        summary[name] = res
        subprocess.run([*COMPOSE, "down"], check=False)
    prev = RES / "r4_summary.json"
    merged = json.loads(prev.read_text(encoding="utf-8")) if prev.exists() else {}
    merged.update(summary)
    prev.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: {kk: v.get(kk) for kk in ("ram_idle_after_warmup_mib", "index_seconds_per_chunk",
                                                    "query_10_at_once_p50_s", "quality")}
                      for k, v in merged.items() if isinstance(v, dict)}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
