#!/usr/bin/env bash
# Round 3, CPU half: each speech engine alone on the CPU (nothing else running), same Piper test clips.
#   Piper TTS, faster-whisper large-v3-turbo int8, sherpa-onnx Whisper turbo int8 (python:3.12-slim + libraries
#   on F:), whisper.cpp large-v3-turbo (q5_0 and full) through its own whisper-server.
#   bash resource-usage-bench/scripts/r3_cpu.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/../results"
SCRIPTS_W="$(cd "$HERE" && pwd -W)"
RES_W="$(cd "$RES" && pwd -W)"
export MSYS_NO_PATHCONV=1
PYRUN=(docker run --rm -v "$SCRIPTS_W:/bench:ro" -v F:/kx-bench/models:/models:ro -v F:/kx-bench/clips:/clips:ro
       -v F:/kx-bench/pylib:/pylib:ro -v "$RES_W:/results" -e PYTHONPATH=/pylib/cpu -w /bench python:3.12-slim)

[ "${ONLY_WCPP:-0}" = 1 ] || for job in "piper:piper --models /models --out /results/r3_piper_cpu.json" \
           "fwhisper:fwhisper --models /models --clips /clips --device cpu --out /results/r3_fw_cpu.json" \
           "sherpa:sherpa --models /models --clips /clips --out /results/r3_sherpa_cpu.json"; do
  name="${job%%:*}"; args="${job#*:}"
  "${PYRUN[@]}" python speech_bench.py $args > "$RES/r3_${name}_cpu.log" 2>&1
  echo "$name exit $?"
done

V=/f/kx-bench/venv/gpu313/Scripts/python.exe
for model in ggml-large-v3-turbo-q5_0.bin ggml-large-v3-turbo.bin; do
  tag="${model#ggml-large-v3-turbo}"; tag="${tag%.bin}"; tag="${tag/-/_}"; tag="wcpp${tag:-_f16}"
  docker rm -f kx-wcpp >/dev/null 2>&1
  docker run -d --name kx-wcpp -p 28181:8080 -v F:/kx-bench/models/whisper.cpp:/m:ro ghcr.io/ggml-org/whisper.cpp:main \
    "whisper-server -m /m/$model -t 8 --host 0.0.0.0 --port 8080 --convert" > /dev/null
  PYTHONIOENCODING=utf-8 "$V" "$SCRIPTS_W/whispercpp_client.py" --url http://localhost:28181 --clips 'F:\kx-bench\clips' \
    --container kx-wcpp --out "$RES_W/r3_${tag}_cpu.json" > "$RES/r3_${tag}_cpu.log" 2>&1
  echo "$tag exit $?"
  docker logs kx-wcpp > "$RES/r3_${tag}_server.log" 2>&1
  docker rm -f kx-wcpp > /dev/null 2>&1
done
