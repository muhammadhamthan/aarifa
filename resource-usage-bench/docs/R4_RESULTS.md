# Round 4 results: embedding models on CPU (14 Nov 2026)

Source data: `results/r4_qwen3_06b.json`, `r4_bge_m3.json`, `r4_e5_large.json` (speed, RAM, easy quality),
`results/r4_hard_*.json` + `r4_hard_summary.json` (cached restart, hard quality), `r4_*_stats.csv` (docker stats).
Scripts: `scripts/r4_driver.py`, `scripts/embed_bench.py`, `scripts/r4_hard.py`, `scripts/quality_hard.py`.

Each model ran **alone** on the same TEI CPU server the app uses (`text-embeddings-inference:cpu-1.8`,
`--auto-truncate --max-batch-tokens 1024`), on the i5-13400F (16 threads), Docker VM 8.2 GB. Queries were written
the way each model expects: Qwen3 with the exact instruction the app sends (`HttpEmbedder.embed_query`), e5 with
`query:` / `passage:`, BGE-M3 plain.

## Results

| | Qwen3-Embedding-0.6B (app default, D28) | BAAI/bge-m3 | intfloat/multilingual-e5-large |
|---|---|---|---|
| Engine TEI picked | Candle (no ONNX file in the repo) | ONNX Runtime | ONNX Runtime |
| RAM idle, cached start (M) | **2.82 GiB** (process memory 2,843 MiB) | 4.74 GiB (process memory 4,727 MiB) | 4.69 GiB |
| RAM peak while indexing | 3.10 GiB | 4.83 GiB | 4.76 GiB |
| Start time, files cached | 32 s | 33 s | 30 s |
| Disk the server needs | 1.2 GB | 2.3 GB (ONNX copy) | 2.2 GB (ONNX copy) |
| Indexing speed | 167 tokens/s, **4.7 s per chunk** | 507 tokens/s, **0.75 s per chunk** | 430 tokens/s, **0.88 s per chunk** |
| A 2,500-chunk catalogue, full re-index | ≈ 3 h 15 min | ≈ 31 min | ≈ 37 min |
| One query | 0.16 s | 0.07 s | 0.07 s |
| 10 queries at the same moment (p50 / max) | 1.26 / 1.93 s | 0.30 / 0.42 s | 0.38 / 0.44 s |
| Queries over the app's 2 s limit (D28) | 0 of 60 (closest 1.93 s) | 0 | 0 |
| CPU while indexing | 6–9 cores | up to 16 threads, short bursts | up to 16 threads, short bursts |
| Easy test: 24 questions, 8 different facts, en/ar/hi/zh | 24/24 | 24/24 | 24/24 |
| Easy test, only English passages searched | 24/24 | 24/24 | 24/24 |
| **Hard test**: 38 questions, 16 facts in look-alike pairs | **35/38** (MRR 0.961) | **36/38** (MRR 0.964) | **35/38** (MRR 0.961) |
| Longest input it reads | 32k tokens (bench cut at 1,024) | 8,192 tokens (bench cut at 1,024) | **512 tokens** |
| Vector size | 1024 (fits `vector(1024)`) | 1024 (fits) | 1024 (fits) |
| Licence | Apache-2.0 | MIT | MIT |

Token counts differ by tokenizer: the same 48 chunks are 37,320 tokens for Qwen3 and ~18,200 for BGE-M3 / e5,
so "seconds per chunk" is the fairer speed comparison.

## What the numbers say

1. **Answer quality is a tie.** All three found the right fact for every easy question, in all four languages and
   when only English text was searched. In the hard test they differ by one question, and all three miss the same
   look-alikes ("1-litre box *without* lid" vs "750 ml *with* lid"; a Chinese oven question vs the Chinese
   paper-lid note). 38 questions cannot separate them.
2. **The real trade-off is RAM against speed.** Qwen3 on Candle uses ≈ 1.9 GiB less RAM; BGE-M3 / e5 on ONNX
   Runtime re-index 5–6× faster and answer 10 simultaneous questions 3–4× faster.
3. **Qwen3 on CPU is close to the app's 2-second limit** when 10 buyers ask at once (1.93 s worst). Above it,
   the app silently falls back to keyword search only (D28). BGE-M3 / e5 stay under 0.5 s.
4. **e5-large reads only 512 tokens.** Longer catalogue chunks are cut off, so it suits short chunks only.

## Switching models is not configuration-only

- The app always adds the Qwen3 instruction to every query (`apps/arifaa-pack/src/arifaa_pack/adapters.py:57`,
  `HttpEmbedder.embed_query`); `container.py` does not pass another `query_instruction`.
- e5 also needs `passage:` in front of every indexed chunk, which the app never adds.
- So BGE-M3 needs a small change (no instruction for queries); e5 needs two (query and passage prefixes).
  All three keep the `vector(1024)` column, but switching model means re-indexing the whole catalogue.

## Bench notes

- First pass: BGE-M3 / e5 start time included a 2.2 GB ONNX download (332 s / 441 s) and their disk total
  included an unused PyTorch copy (4.5 GB). The cached restart (`r4_hard.py`, offline mode) gives the fair
  30–33 s start; the server needs only the ONNX copy.
- The higher RAM of BGE-M3 / e5 was checked inside the container (`memory.stat`): it is process memory, not
  file cache.
- My first hard-test run crashed on a Windows text-encoding bug in the bench script (no data). Fixed (UTF-8)
  and rerun.
- Not tried: running BGE-M3 on Candle (deleting the ONNX copy would make TEI fall back), which might give
  Qwen3-like RAM with BGE-M3 speed in between. One extra run if wanted.
