# Rounds 2 and 3 results: free voice chain and speech engines (14 Nov 2026)

Machine: i5-13400F (16 threads), 15.8 GB RAM, Docker VM 8.2 GB, RTX 5060 8 GB (driver 591.86; the Windows desktop
itself uses ≈ 1.27 GB of the card). Test speech: 10 Piper-made clips of known text (3 en, 3 ar, 2 hi, 2 zh,
≈ 4–6 s each, `F:/kx-bench/clips/truth.json`). The LLM is the fake server (1–2 s before its first token), so
reply times below include that delay; a real model adds its own.

Source data in `results/`: `r2_cpu_summary.json`, `r2_gpu_summary.json`, `r2_app_summary.json`,
`r2_callers_*.json`, `r2_*_stats.csv`, `r3_*.json`, `r2b_*`. Scripts: `r2_driver.py`, `voice_callers.py`,
`voice_agent.py`, `app_voice_worker.py`, `speech_server.py`, `speech_bench.py`, `whispercpp_client.py`,
`r3_cpu.sh`, `r3_gpu.sh`.

## Round 2: the free voice chain with 1, 3 and 10 simultaneous callers

LiveKit server (self-hosted) + LiveKit Agents + Silero VAD + LiveKit multilingual turn detector + faster-whisper
large-v3-turbo (STT) + Piper (TTS) through `speech_server.py`. "Reply" = time from the end of the caller's speech to
the first audible agent audio. Each caller speaks 4 turns.

### 2a. Bench agent (`voice_agent.py`, livekit-agents 1.8.12)

| Speech-to-text on | Callers | Turns answered | Reply p50 / p95 / max | STT speed (RTF) | Speech server | Agent worker RAM idle / peak |
|---|---|---|---|---|---|---|
| CPU (int8, 8 threads) | 1 | 4/4 | 12.8 / 14.2 / 14.2 s | 1.37 | 2.1 cores avg, 6.5 peak; 2.4 GiB | 1.40 / 1.48 GiB |
| CPU | 3 | 12/12 | 25.0 / 27.2 / 27.2 s | 2.6 | 6.0 cores avg, 11.5 peak; 2.8 GiB | / 1.63 GiB |
| CPU | 10 | **0/40** (none within 60 s) | — | 24 (queued) | 8.4 cores avg, 11 peak; 3.3 GiB | / 2.19 GiB |
| GPU (float16, RTX 5060) | 1 | 4/4 | 3.9 / 6.9 / 6.9 s | 0.11 | ≈ 0.6 GB RAM, ≈ 2.7 GB VRAM | 1.38 / 1.46 GiB |
| GPU | 3 | 12/12 | 4.5 / 6.0 / 6.0 s | 0.07 | same | / 1.61 GiB |
| GPU | 10 | 36/40 (9 of 10 calls) | 4.7 / 5.7 / 6.1 s | 0.14 | same | / 2.07 GiB |

- On CPU, Whisper processes a fixed 30-second window for every short utterance, so one caller already waits
  ≈ 13 s. **A CPU-only server cannot run the free voice chain for live calls.**
- Piper TTS is never the bottleneck (RTF 0.03–0.10 in every run).
- GPU, 10 callers: the one call that got no agent waited for a new worker process ("no warmed process available").
- LiveKit server: 16–90 MiB RAM, ≤ 1 core.

### 2b. The platform's REAL voice worker (`kx voice` code, livekit-agents 1.8.10, LiveKit server built from source)

`scripts/app_voice_worker.py` runs `arifaa_pack`'s own container (`build()`), `kx.agents.voice.make_livekit_entrypoint`
(Kairox graphs through `AgentService.handle_turn`, price guard before TTS, recording notice, voice opt-out, D34
voice-minute counter) with the free engines plugged in through the platform's `VoicePlugins` interface. The LiveKit
server is `repos/livekit-server` @ v1.13.11 built locally (133 MB image). The plugins are `repos/livekit-agents` @
1.8.10 built locally. Callers carry real `deal_ref` values from the Round 1 database and the call language
(en / ar / hi / zh in turn). The embedding server was left off (Docker RAM), so knowledge search used its keyword
fallback (D28). STT on the GPU.

Clean run (`results/r2_app2_summary.json`, `r2_callers_app2_c*.json`; only this worker registered):

| Callers | Calls that got replies | Turns answered | Reply p50 / p95 / max | Voice worker RAM avg / peak | Voice worker CPU avg / peak |
|---|---|---|---|---|---|
| idle | | | | 1.41 GiB (4 warm processes) | 0.05 core |
| 1 | 1/1 | 4/4 | 9.8 / 12.4 / 12.4 s | 1.74 / 1.75 GiB | 0.3 / 1.4 cores |
| 3 | 3/3 | 12/12 | 8.6 / 17.7 / 17.7 s | 2.24 / 2.33 GiB | 0.6 / 3.8 cores |
| 10 | 10/10 | 29/40 | 10.0 / 16.6 / 18.1 s | 3.50 / **4.21 GiB** | 1.9 / **10.1 cores** |

- Same LiveKit, speech server and fake model as the bench agent (2a, GPU), which answered 10 callers at
  p50 4.7 s with 2.1 GiB: the platform's worker is about **2× slower per reply and 2× heavier**. Per active call
  ≈ 0.3–0.4 GiB RAM on top of the 1.41 GiB idle.
- **Missed turns are the first turns of calls.** Calls 6, 7 and 8 (notice started 2.7–5.2 s after joining) had
  every turn answered. The other 7 calls lost their first one or two turns (no reply within 60 s): 6 of them heard
  the notice start only 12–30 s after joining (cold process); one (call 1) heard it at 2.1 s and still lost its
  first two turns while the other calls' processes were building. Likely cause: `cli._voice` builds the whole app container
  (database pools, LangGraph pool …) inside the FIRST JOB of each process, not in LiveKit's prewarm, and LiveKit runs
  each call in a fresh process — so every call pays that build at its start, ten at once under load. Replacement
  warm processes also hit LiveKit's 10 s start timeout under that load (6 × `TimeoutError`, process killed).
- Recording notice: first audio 2.1–29.8 s after joining (cold processes at the long end); the caller cannot be
  heard until it ends (`allow_interruptions=False`).
- The graphs answered through the expected roles (factory_concierge, lead_qualification, manufacturing_intelligence,
  product_intelligence): 63 `voice.utterance` agent runs, avg 5.5 s, max 11.2 s (fake model delay included), 1 failed.
- Per answered voice turn (clean run, 45 answered turns): ≈ 1.9 model calls (84 / 45), ≈ 6.0 KB sent to the model
  per call (506 KB / 84), ≈ 6.6 KB of Phoenix traces (295 KB / 45).
- Remedies (code, `cli._voice`): build the app container in a `prewarm_fnc` so warm processes are ready to talk;
  `num_idle_processes` = expected peak simultaneous calls (LiveKit default min(cores, 4); ≈ 0.3 GiB each);
  a longer `initialize_process_timeout`.

> First run (`r2_app_summary.json`) is INVALID: my test container of the unmodified `kx voice` (problem 1) was still
> registered with the same LiveKit server, which sent 6 of the 10 calls to it ("no worker available to handle job";
> those calls got no agent). Kept only for the record.

### Problems found in the platform's voice code (not fixed: the bench never edits source)

1. **`kx voice` cannot start a single job.** `arifaa_pack/cli.py:_voice` defines the job entry function
   inside `_voice`. LiveKit starts job processes with multiprocessing "forkserver" on Linux ("spawn" elsewhere;
   livekit-agents 1.8.10 `worker.py:263`), which pickles the entry function; a local function cannot be pickled.
   The unmodified command logged `Can't pickle local object <function _voice.<locals>.entry>` 3,380 times in
   90 s and never took a call (`results/r2b_kx_voice_production.log`). Fix: define the function at module level.
2. **The voice agent never answers.** `kx/agents/voice.py` builds `AgentSession(stt=…, tts=…, vad=…)` without an
   `llm`. livekit-agents 1.8.10 skips every reply when the session has no LLM (`voice/agent_activity.py:2914`,
   `elif self.llm is None: return  # skip response if no llm is set`), before `KairoxVoiceAgent.llm_node` runs. The
   caller hears the recording notice and then silence, with no error logged. Fix: pass any LLM object to the
   session (the overridden `llm_node` replaces its use).
3. **Every call builds the whole app container at its start** (`cli._voice` builds it in the first job of each
   process, LiveKit uses a fresh process per call): under a burst of 10 calls the first one or two turns of 7 calls
   went unanswered within 60 s (above). Build it in `prewarm_fnc` instead.
4. The platform's `voice.py` docstring already says the voice path is "Not exercised live yet"; problems 1 and 2
   are why it would not have worked.

The bench works around 1 and 2 (`app_voice_worker.py`: module-level `entry`, placeholder LLM that is never called —
the fake server counted 0 requests to it) and changes nothing else.

### Bandwidth

LiveKit server traffic (both legs, both directions): **0.3–1.7 MB per call-minute**, lower when callers are
mostly silent (Opus compresses silence). The internet-facing caller leg is about half: ≈ 0.15–0.85 MB per
call-minute (≈ 20–110 kbit/s). No speech-vendor traffic in the free stack.

## Round 3: speech engines alone

Each engine ran alone on the machine. "10 at once" = the 10 clips (59 s of audio) submitted together.

### Speech-to-text (same model family: Whisper large-v3-turbo)

| Engine | Device | Model on disk | RAM after load | One clip: time (RTF) | 10 at once: all done in | Peak RAM / VRAM |
|---|---|---|---|---|---|---|
| faster-whisper float16 | GPU | 1.62 GB | +0.16 GB RAM | 0.24 s (0.041) | 2.7 s | ≈ 3.5 GB VRAM |
| faster-whisper int8_float16 | GPU | 1.62 GB | | 0.23 s (0.039) | 3.0 s | ≈ 2.9 GB VRAM |
| faster-whisper int8 | CPU (16 threads) | 1.62 GB | 1.8 GB | 5.5 s (0.94) | 39.5 s | 4.3 GB RAM |
| sherpa-onnx int8 | CPU | 1.04 GB | 1.5 GB | (0.86) | 42 s | 7.2 GB RAM |
| whisper.cpp q5_0 (whisper-server, 8 threads) | CPU | 0.57 GB | 0.61 GB | 11.7 s (1.98) | 118 s (one at a time) | 0.78 GB RAM |
| whisper.cpp full | CPU | 1.62 GB | 1.62 GB | 11.7 s (1.99) | 121 s | 1.78 GB RAM |

GPU VRAM figures are the card total minus the desktop's ≈ 1.27 GB (the bench's own delta for the second GPU
configuration was polluted by the first model still holding memory). GPU transcription still used 6–7 CPU cores
for audio preparation when 10 clips ran at once.

**Accuracy (transcripts read against the reference text, `results/r3_wcpp_*_cpu.json`):** English and Arabic
essentially perfect (numbers come back as digits, US spelling, punctuation). Hindi: meaning intact, small spelling
slips (कागस for कागज़, वाटसाप for WhatsApp). **Chinese: real errors on product words** (铝箔→铝博, 餐盒→餐和,
纸盖→纸杆/纸钙, 起订量→企定量, 交货→叫货); partly Piper's weak Chinese voice, so Chinese needs testing with real
speech. All engines produce nearly identical text (same model). The WER figures inside the JSON files are not
usable: the scorer counts "750" against "seven hundred and fifty" as errors.

### Text-to-speech

| Engine | Device | Model on disk | RAM / VRAM | Speed (RTF) | First audio | 10 at once |
|---|---|---|---|---|---|---|
| Piper (4 voices: en, ar, hi, zh) | CPU | 0.25 GB (63 MB per voice) | 0.40 GB loaded; 2.1 GB peak with 10 jobs | 0.027–0.051 | 0.11–0.29 s | 157 s of speech in 4.3 s; first audio p95 2.1 s |
| Chatterbox multilingual | GPU | 13.9 GB repo downloaded | ≈ 4.0 GB VRAM (torch peak 3.5 GB); 5.2 GB RAM while loading | 0.84–0.88 (zh 1.98) | 4.7–11.3 s (not streamed) | not tested (one call already ≈ real time) |

Piper has no Tamil or Sinhala voice. Chatterbox sounds more natural but is too slow for live calls on this GPU.

## Bench notes (my mistakes, fixed)

- The first agent run had the turn detector imported lazily, so its model was never downloaded; fixed.
- faster-whisper 1.2.1 passes `metadata_errors` to PyAV 19, which rejects it; the bench decodes WAV itself.
- The caller script's p95 was miscomputed and its 20 s wait recorded slow replies as "missed"; fixed (60 s).
- Chatterbox first failed with "paging file too small" while the Docker voice stack was running at the same time
  (Windows commit 37 of 43 GB); it ran after the stack was stopped.
- whisper.cpp first failed on a Git-Bash path conversion in my script; fixed and rerun.
- The real-worker driver crashed while summarising (Docker reports `--` for stopped containers); the summary was
  rebuilt from the caller files' own start/end times with the same window function.
