# Local AI Benchmark — Gemma 4 on RTX 3060 12GB

**Date:** 2026-04-07
**Hardware:** RTX 3060 12GB, i7-12700F, 32GB RAM (Ikon dev machine)
**Conditions:** WheelHouse running with Whisper Small resident (~2634 MB baseline VRAM used, ~9475 MB free)
**Tool:** `services/wheelhouse/scripts/benchmark_ai_model.py`
**Raw results:** `docs/design/benchmarks/2026-04-07-*.json`
**Related beads:** wh-m5d (help chat VRAM), wh-3ph (gemma4 eval), wh-btp (26B-A4B add), wh-e4p (E2B add)

## Purpose

Empirically characterize the three Gemma 4 variants (E2B, E4B, 26B-A4B) for
WheelHouse's text correction and multi-turn help chat workloads. Specifically
answer the wh-m5d question: **is it possible to run the 26B-A4B model for
help chat on an RTX 3060 12GB, and if so, what configuration?**

## TL;DR

**Yes, 26B-A4B can run help chat on a 12GB card, contrary to my initial
conclusion.** The winning configuration is:

```
n_gpu_layers = 17
n_ctx = 16384       (help_n_ctx)
flash_attn = true
type_k = q4_0
type_v = q4_0
n_threads = 8
n_threads_batch = 8
```

Every knob matters. Omitting any one of them either fails to load or
degrades performance significantly. The details are the story.

## Methodology

Standalone Python benchmark that:

- Imports `llama_cpp.Llama` directly (bypasses WheelHouse's `AIService`)
- Runs each configuration in a fresh subprocess for clean Vulkan state
- Reads VRAM before/after load via `nvidia-smi --query-gpu=memory.used`
- Loads the real production knowledge base (`wheelhouse/knowledge/wheelhouse_help.md`,
  ~49 KB / 12500 tokens)
- Uses the real `HELP_CHAT_SYSTEM` prompt from `ai/prompts.py`
- Runs text correction with the same prompt used in the prior wh-btp benchmark
  (for continuity with historical numbers)
- Asks three help questions selected to exercise different KB sections:
  1. "How do I move a window to the left half of my screen using voice commands?"
  2. "What is the difference between toggle mode and push-to-talk mode?"
  3. "My computer has 8 GB of VRAM. Can I still run the local AI help chat?"

## Models tested

| Model | Size on disk | Effective params | Native context |
|---|---|---|---|
| Gemma 4 E2B | 3.3 GB | 2.3 B | 262144 |
| Gemma 4 E4B | 5.1 GB | 4.5 B | 131072 |
| Gemma 4 26B-A4B (MoE) | 16.0 GB | 4.0 B active / 26 B total | 262144 |

All at Q4_K_M quantization from Google's official releases.

## Result 1: Gemma 4 E4B — works out of the box

**Config:** `n_gpu_layers=-1, n_ctx=16384` (all layers on GPU)

| Metric | Value |
|---|---|
| Load (cold) | 15.66 s |
| VRAM delta | 6878 MB |
| VRAM after load | 9313 MB |
| Text correction | 2.23 s, 11.21 tok/s |
| Help Q1 (cold, 12506 prompt tokens) | 22.48 s |
| Help Q2 (warm) | 3.07 s |
| Help Q3 (warm) | 5.92 s |
| Avg help Q&A | ~16 tok/s effective |

E4B is the happy-path choice on 12GB hardware. Leaves ~2 GB headroom and
delivers sub-10-second responses. Response quality is acceptable but notably
more terse than 26B-A4B on the same knowledge base (see Result 3).

## Result 2: Gemma 4 26B-A4B — five configurations that fail, and why

Before finding the working configuration, the following configurations all
fail to load or crash during inference. Recording them here because the
failure modes are instructive:

| Config | Failure |
|---|---|
| `ngl=20, n_ctx=16384` | OOM on 896 MB allocation (KV cache) |
| `ngl=20, n_ctx=16384, flash_attn=true` | Same 896 MB OOM (FA alone doesn't reduce this) |
| `ngl=20, n_ctx=16384, fa, q8_0 KV` | Same 896 MB OOM |
| `ngl=20, n_ctx=16384, fa, q4_0 KV` | Same 896 MB OOM |
| `ngl=20, n_ctx=14336, fa, q4_0 KV` | 672 MB OOM (scaled down but still blocked) |
| `ngl=19, n_ctx=16384, fa, q4_0 KV` | 553 MB OOM |
| `ngl=19, n_ctx=13312, fa, q4_0 KV` | Loads but inference crashes with Windows C++ exception 0xe06d7363 |
| `ngl=18, n_ctx=16384, fa, q4_0 KV` | Loads but help_qa crashes with 352 MB runtime allocation failure |
| `ngl=18, n_ctx=13312, fa, q4_0 KV` | **Loads and works**, but limited to single-turn (~832 bytes headroom after KB) |

### What the verbose log revealed

Running with `verbose=True` at the critical `ngl=20 + fa + q4_0` config
showed the actual layer structure:

```
llama_kv_cache: layer  12: dev = Vulkan0
llama_kv_cache: layer  13: dev = Vulkan0
...
llama_kv_cache: layer  17: filtered         <-- iSWA layer
llama_kv_cache: layer  23: filtered         <-- iSWA layer
llama_kv_cache: layer  29: filtered         <-- iSWA layer
llama_kv_cache:        CPU KV buffer size = 1280.00 MiB
ggml_vulkan: Device memory allocation of size 939524096 failed.
llama_init_from_model: failed to initialize the context:
    failed to allocate buffer for kv cache
```

**Key architectural facts:**

- Gemma 4 26B-A4B has **30 hidden layers**, of which **5 use iSWA** (interleaved
  sliding window attention) at positions 5, 11, 17, 23, 29
- The remaining 25 layers use full attention with KV cache scaling with `n_ctx`
- The llama.cpp warning `using full-size SWA cache (ref: ggml-org/llama.cpp#13194)`
  indicates the current wheel does not yet use the optimized iSWA cache layout
- At `n_ctx=16384` with 25 full-attention layers and q4_0 quantization, the
  total full-attention KV cache is ~2.2 GB split between CPU and GPU
- The failing allocation is the GPU portion of the full-attention KV cache

### The Vulkan heap ceiling

Regardless of which allocation fails at `ngl=20`, the total VRAM successfully
committed before failure is consistently ~8700-8800 MB. This is **not** the
same as the 9475 MB "free" that nvidia-smi reports. Windows WDDM, the NVIDIA
driver, and the desktop compositor reserve overhead that eats into the
practical per-process budget. Observed practical ceiling: **~8700 MB**.

## Result 3: Gemma 4 26B-A4B at ngl=17 — the working configuration

After discovering the compute buffer and Vulkan heap constraints, testing
lower `n_gpu_layers` values revealed a non-obvious sweet spot:

| Config | Q1 cold | Q2 warm | Q3 warm | VRAM | Notes |
|---|---|---|---|---|---|
| `ngl=15` | 156.5 s | 10.55 s | 20.46 s | 8137 MB | slowest |
| `ngl=16` | 146.9 s | 17.24 s | 21.35 s | 8367 MB | worse on warm Q2 |
| **`ngl=17`** | **~140 s** | **~7.8 s** | **~16.5 s** | **~8280 MB** | **local optimum** |
| `ngl=18` (at n_ctx=13312) | 135.1 s | 19.89 s | 18.80 s | 8272 MB | single-turn only |

**`n_gpu_layers=17` wins on warm-path latency and supports the full 16384
context** (which in turn supports multi-turn conversation — see context budget
below). Going lower is consistently slower on warm metrics; going higher
either fails to load or crashes on inference.

### Thread tuning — the biggest win

The initial 26B-A4B tests used `n_threads=4` (inherited from the earlier
benchmark script). On a 12700F with 8 P-cores + 4 E-cores, 4 threads is
severely under-utilized. Testing the cross-product of `n_threads` and the
separate `n_threads_batch` knob:

| Threading | Q2 warm | Q3 warm | Generation |
|---|---|---|---|
| `nt=4/nb=4` (default) | 9.28 s | 20.89 s | ~9.0 tok/s |
| `nt=4/nb=8` | 8.91 s | 19.69 s | ~8.9 tok/s |
| `nt=8/nb=4` | 8.32 s | 17.07 s | ~9.8 tok/s |
| **`nt=8/nb=8`** | **7.83 s** | **16.47 s** | **~10.5 tok/s** |
| `nt=6/nb=8` | ~8.2 s | ~17.4 s | ~9.9 tok/s |
| `nt=8/nb=12` | 7.85 s | 16.37 s | ~10.4 tok/s |
| `nt=8/nb=16` | 7.75 s | 16.25 s | ~10.5 tok/s |
| `nt=12/nb=12` | 16.66 s | 18.63 s | ~10.4 tok/s |

**`n_threads=8` with `n_threads_batch=8` is the plateau.** Higher values are
statistically indistinguishable on warm metrics. Going above 8 threads
engages E-cores which introduces thread scheduling heterogeneity that
measurably hurts warm throughput (see `nt=12/nb=12` row). Below 8 threads
every reduction costs speed.

**Real warm-path improvement vs default:** ~16-21% across Q2, Q3, and
generation.

### Q1 cold latency is variance-dominated, NOT config-dominated

Five independent runs of the winning config produced these Q1 cold times:

```
Run 1:  48.13 s   <-- outlier
Run 2: 146.59 s
Run 3: 138.53 s
Run 4: 143.89 s
Run 5: 139.26 s
```

Excluding the one outlier, the real Q1 cold is **~142 ± 4 s**. Across ALL
tested configurations (regardless of knobs), every Q1 cold measurement
landed in the **121-156 second window**. This is dominated by Vulkan shader
JIT compilation on first inference, which is a fixed cost that no config
knob reduces. Mitigation paths (not yet implemented): pre-warm at startup
with a silent dummy query, or integrate shader cache persistence if the
llama-cpp-python wheel gains that feature.

### Knobs that don't help

| Knob | Result |
|---|---|
| `use_mlock=True` | No Q1 improvement, slower load |
| `q8_0` KV quant | Worse than q4_0 (more memory, more pressure, slower) |
| `n_batch=768` | Uses more VRAM, ambiguous on speed |
| `n_batch=1024` | Load fails (compute buffer too large) |
| `offload_kqv=False` | Fast Q1 (53 s!) but warm path tanks (23 s Q2, 51 s Q3) |
| `n_threads=12` | E-core thrashing regresses warm metrics |

The `offload_kqv=False` trade is interesting but unusable — it gives fast
cold startup in exchange for ~3x slower warm follow-ups.

## Result 4: Response quality comparison (qualitative)

Both models were asked the three help questions against the same knowledge
base. The benchmark accidentally revealed that the KB doesn't cover window
move commands or interaction mode distinctions — both models correctly
refused to hallucinate features not in their context. Quality comparison on
Q2 ("What is the difference between toggle mode and push-to-talk mode?"):

**E4B's answer:**
> *"I don't have information about the difference between toggle mode and
> push-to-talk mode in the knowledge base. You can reach the developer at
> [support channel]."*

**26B-A4B's answer:**
> *"I don't have information about a 'toggle mode' or 'push-to-talk mode'
> in my knowledge base. I can, however, tell you about the three Speech
> Modes that WheelHouse uses to process your speech: Command Mode
> (triggered when you start an utterance with a known command word like
> 'undo' or 'select'), Dictation Mode (triggered when you start speaking
> words that aren't commands; these are typed directly into your focused
> application), Replacement Mode (triggered by specific words like 'period'
> or 'comma' that are replaced with punctuation)..."*

**26B-A4B inferred that the user might have conflated terms and proactively
offered related content from the KB that E4B didn't surface at all.** This
is measurable higher-quality RAG behavior from the larger model. In exchange,
it runs ~2x slower on warm follow-ups.

## Context budget at the winning config

| Component | Tokens |
|---|---|
| Total context window (`n_ctx`) | 16384 |
| System prompt boilerplate | ~20 |
| Knowledge base (full `wheelhouse_help.md`) | ~12480 |
| User question | 20-100 |
| Response budget (`ai.help.max_response_tokens`) | 800 |
| **Available for conversation history** | **~3100** |

At ~400-600 tokens per complete turn pair, that's roughly **5-7 prior turns
of multi-turn conversation** before the oldest turns need to be dropped.
Going below `n_ctx=16384` would force single-turn mode because the KB alone
consumes 12480 tokens.

## Corrections to prior claims

Several confident-wrong claims were made during the investigation before
empirical data corrected them. For the record:

1. **"26B-A4B cannot do help chat on a 3060 12GB"** — Wrong. The winning
   config works.
2. **"flash_attn doesn't help"** — Wrong. FA is required so llama.cpp
   doesn't pad the iSWA V cache to 2048 dim.
3. **"KV cache quantization has no effect"** — Wrong. q4_0 saves ~700 MB
   vs f16 and is strictly better for this workload.
4. **"Prompt eval is 6.86 tok/s → 30 minutes per help question"** — Wrong.
   That measurement was for a 69-token prompt dominated by cold-warmup
   overhead. Warm large-prompt throughput is ~94 tok/s; real KB prefill
   is ~140 s, not 1800 s.
5. **"Fewer GPU layers is always slower"** — Wrong for this model. ngl=17
   beats both ngl=18 and ngl=16 on warm metrics.
6. **"nt=8/nb=8 cuts Q1 cold from 125 s to 48 s"** — Wrong (statistical
   fluke). Real Q1 cold is ~140 s regardless of thread config.

## Recommendations

### For the WheelHouse default on 12GB hardware

Either E4B or 26B-A4B is viable. Tradeoffs:

| | Gemma 4 E4B | Gemma 4 26B-A4B (at ngl=17) |
|---|---|---|
| Fits with VRAM headroom | ✓ (~6.9 GB used, 2 GB free) | Tight (~8.3 GB used, 1.2 GB free) |
| First help question (cold) | ~22 s | ~140 s |
| Warm follow-up | 3-6 s | 7-17 s |
| Text correction | ~2 s | ~4 s |
| Generation speed | ~15 tok/s | ~10.5 tok/s |
| Response quality | Adequate | Measurably better for nuanced questions |
| Tool use capability | Untested | Untested |
| Multi-turn support | Full (5-7 turns) | Full (5-7 turns) |

**For the open-source launch**, E4B is the safer default — shorter cold
latency, more headroom for users with other GPU-using processes, and
similar warm performance. Users on 16GB+ cards or who value response
quality can opt in to 26B-A4B via the tray menu model switcher.

### For existing users on the dev branch

The ngl=17 configuration is now live in `models.toml`. Switching to
`gemma4-a4b` via the GUI will load the model with the winning knobs
automatically. The eager-load architecture catches any load failure and
reports it via the WorkingDialog without crashing the AI service.

### For wh-3ph (Gemma 4 eval docs)

This document serves as the eval writeup. The raw JSON results in
`docs/design/benchmarks/` can be referenced for reproducing measurements.
The benchmark script at `services/wheelhouse/scripts/benchmark_ai_model.py`
is reusable for future model evaluations.

## Future work

- **Pre-warm strategy** to reduce perceived first-question latency.
  Implementation: after `AIService.start()` returns, kick off a silent
  background task that runs a dummy `chat_help()` call against the KB.
  User-visible startup is ~150 s longer but every help click after boot
  is in the warm regime.
- **Rebuild llama-cpp-python wheel** when upstream lands the iSWA cache
  optimization from PR #13194. That should reduce the full-attention KV
  cache footprint and potentially enable ngl=18-20 configurations.
- **Update `wheelhouse_help.md`** with the documentation gaps surfaced
  during this benchmark (tracked in wh-g1y).
- **Benchmark future models** using the new harness. The harness is
  configuration-agnostic and produces comparable tables.
