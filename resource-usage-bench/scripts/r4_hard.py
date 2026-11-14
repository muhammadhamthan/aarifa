"""Round 4 follow-up: restart each embedding model with every file already cached (fair cold start and idle RAM,
no download page-cache in the number), then run the harder look-alike retrieval test (quality_hard.py).

  python r4_hard.py      -> results/r4_hard_<name>.json and r4_hard_summary.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from r4_driver import COMPOSE, MODELS, RES, idle_ram  # noqa: E402

BENCH = Path(__file__).resolve().parents[1]


def wait_ready(limit_s: float = 900) -> float:
    t0 = time.time()
    while time.time() - t0 < limit_s:
        try:
            r = httpx.post("http://localhost:28081/v1/embeddings", json={"input": ["x"], "model": "m"}, timeout=30)
            if r.status_code == 200:
                return round(time.time() - t0, 1)
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError("embedding server never became ready")


def main() -> None:
    out: dict[str, object] = {}
    for name, model_id, fmt in MODELS:
        env = dict(os.environ, MODEL_ID=model_id, HF_OFFLINE="1")  # offline: proves nothing is downloaded
        subprocess.run([*COMPOSE, "down"], check=False, capture_output=True)
        t0 = time.time()
        subprocess.run([*COMPOSE, "up", "-d", "--force-recreate"], env=env, check=True, capture_output=True)
        wait_ready()
        ready = round(time.time() - t0, 1)  # from `compose up` to the first answered request
        time.sleep(60)
        idle = idle_ram()
        res_path = RES / f"r4_hard_{name}.json"
        subprocess.run([sys.executable, str(BENCH / "scripts" / "quality_hard.py"), "--url", "http://localhost:28081",
                        "--format", fmt, "--out", str(res_path)], check=False)
        res = json.loads(res_path.read_text(encoding="utf-8"))
        logs = subprocess.run(["docker", "logs", "kxembed-embeddings-1"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace").stdout
        backend = "candle" if "text_embeddings_backend_candle" in logs else "onnxruntime"  # TEI tries ORT first
        res.update({"model": model_id, "cold_start_cached_s": ready, "ram_idle_cached_mib": idle, "tei_backend": backend})
        res_path.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        out[name] = {k: res[k] for k in ("cold_start_cached_s", "ram_idle_cached_mib", "tei_backend", "top1_all",
                                         "top1_distractor_questions", "top1_non_english_questions", "mrr")}
        out[name]["misses"] = len(res["misses"])
        subprocess.run([*COMPOSE, "down"], check=False, capture_output=True)
    (RES / "r4_hard_summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
