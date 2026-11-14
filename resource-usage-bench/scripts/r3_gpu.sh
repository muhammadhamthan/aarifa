#!/usr/bin/env bash
# Round 3, GPU half (RTX 5060, Windows): faster-whisper large-v3-turbo (float16 and int8_float16) and Chatterbox
# multilingual TTS, using the Python 3.13 + torch cu128 environment on F: (nothing installed in the project).
#   bash resource-usage-bench/scripts/r3_gpu.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/../results"
V=/f/kx-bench/venv/gpu313/Scripts/python.exe
SP=/f/kx-bench/venv/gpu313/Lib/site-packages/nvidia
# CTranslate2 (faster-whisper) loads cuBLAS / cuDNN from PATH on Windows
export PATH="$SP/cublas/bin:$SP/cudnn/bin:$SP/cuda_nvrtc/bin:$PATH"
export HF_HOME='F:\kx-bench\hf' HF_HUB_OFFLINE=0 PYTHONIOENCODING=utf-8
nvidia-smi --query-gpu=name,memory.used,memory.total,driver_version --format=csv,noheader > "$RES/r3_gpu_info.txt"
"$V" "$HERE/speech_bench.py" fwhisper --models 'F:\kx-bench\models' --clips 'F:\kx-bench\clips' --device cuda \
  --out "$RES/r3_fw_gpu.json" > "$RES/r3_fw_gpu.log" 2>&1
echo "fwhisper exit $?"
"$V" "$HERE/speech_bench.py" chatterbox --device cuda --out "$RES/r3_chatterbox_gpu.json" > "$RES/r3_chatterbox_gpu.log" 2>&1
echo "chatterbox exit $?"
