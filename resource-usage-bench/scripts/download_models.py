"""Download the free speech models listed in `free version.docx` to F:\\kx-bench\\models (official HF repos).

  F:\\kx-bench\\venv\\gpu\\Scripts\\python.exe download_models.py
Prints the on-disk size of each model (that size goes into the report as disk usage).
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("HF_HOME", r"F:\kx-bench\hf")
from huggingface_hub import snapshot_download  # noqa: E402

MODELS = Path(r"F:\kx-bench\models")
PIPER = ["ar/ar_JO/kareem/medium", "en/en_US/lessac/medium", "hi/hi_IN/pratham/medium", "zh/zh_CN/huayan/medium"]

JOBS: list[tuple[str, str, list[str] | None]] = [
    ("faster-whisper-large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", None),
    ("whisper.cpp", "ggerganov/whisper.cpp", ["ggml-large-v3-turbo.bin", "ggml-large-v3-turbo-q5_0.bin"]),
    ("sherpa-onnx-whisper-turbo", "csukuangfj/sherpa-onnx-whisper-turbo", ["*int8*", "*tokens*"]),
    ("piper-voices", "rhasspy/piper-voices", [f"{v}/*" for v in PIPER]),
    ("chatterbox", "ResembleAI/chatterbox", None),
    ("turn-detector", "livekit/turn-detector", None),
]


def size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> None:
    for name, repo, patterns in JOBS:
        target = MODELS / name
        try:
            snapshot_download(repo_id=repo, local_dir=target, allow_patterns=patterns)
            print(f"{name}: {size(target) / 1e9:.2f} GB ({repo})", flush=True)
        except Exception as exc:  # noqa: BLE001 - report and continue with the next model
            print(f"{name}: FAILED {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    main()
