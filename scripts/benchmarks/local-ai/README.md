# Local AI benchmarks

The measurements behind
`docs/superpowers/specs/2026-07-26-local-ai-runtime-design.md`. Kept so the
numbers in that design can be re-checked on other hardware rather than taken on
trust.

None of these run as part of the test suite. They need model files and a
`llama-server.exe` build that are not in this repository, and each takes many
minutes.

| Script | Answers |
|---|---|
| `qat_bench.py` | How often does each model get the correction wrong, with thinking on and off, on the graphics card and on the processor? |
| `length_bench.py` | How long does the user wait, by input length, per model and backend? |
| `wetext_bench.py` | Would deterministic number formatting remove the one defect the smallest model has? |
| `rewrite_prompt_bench.py` | Does a rewriting instruction on its own produce clean output on ordinary text? |
| `rewrite_takeover_bench.py` | Can a competing instruction inside the selected text take over the model, and what wording stops it? |
| `rewrite_format_bench.py` | Which plain-text structures survive a rewrite, and does naming them specifically or generally work better? |
| `rewrite_list_bench.py` | Do lists keep their item count, numbering scheme, and nesting? |
| `rewrite_markdown_bench.py` | Does Markdown in the selection survive, and does Markdown appear in output whose input had none? |

## Setup

`qat_bench.py` and `length_bench.py` need two things, and the paths are written
into the top of each file:

1. `llama-server.exe` from a llama.cpp release, in both a Vulkan build and a
   processor-only build. The design was measured against build b10107.
2. The Gemma 4 quantization-aware-training GGUF files from Hugging Face. These
   repositories are not gated and download without credentials. Do not
   substitute Ollama's stored blobs — Ollama's converted GGUF files do not load
   in upstream llama.cpp, and Ollama's ordinary pulls are Q4_K_M rather than the
   Q4_0 that the quantization-aware-training builds use.

Edit the `MODELS`, `BIN_VULKAN`, and `BIN_CPU` paths, then run with the
repository's Python. Each writes a JSON file of raw results beside itself.

`wetext_bench.py` needs only `pip install wetext` and takes about a second.

The five `rewrite_*` scripts need only the E4B model and the Vulkan build. Each
starts its own server on its own port, so they can run one after another without
editing anything. They back the "How the prompt is assembled" section of
`docs/superpowers/specs/2026-07-26-local-ai-runtime-design.md`.

## Reading the results

Every script here scores responses automatically and reports a failure count per
task, not a single sample.

**The scoring is the hard part, and it has been wrong more often than the
models.** Five traps have produced a wrong answer during development. The first
two are recorded in comments in `qat_bench.py` and `length_bench.py`; all five
are here because the same mistakes recur:

- A scoring pattern that matches correct output as well as the defect. `"$123.50
  and"` matches the correct sentence "was $123.50 and he said", so the check has
  to be narrower.
- A long input built by repeating a shorter one. The model recognises the
  repetition and returns a single copy, so the timing measures nothing. The
  paragraph in `length_bench.py` is deliberately varied for this reason.
- Requiring both spellings of the same fact. Listing `"thirty"` and `"30"` in a
  must-keep list demands both literally, so correct output containing "thirty
  days" is scored as a dropped fact. Either-or groups exist for this; use them.
- Scoring a takeover by a hand-picked word list. A response reading "Ye must
  book yer deliveries. Squawk! Polly says so!" was scored clean because the list
  held `ahoy`, `arr`, `matey`, and `parrot`. Score by vocabulary family, and
  check whether the injected command itself was echoed into the output.
- Counting blank lines when the input ends with a trailing newline and the
  output does not. That difference of one is an artifact of the test input, not
  a lost paragraph break. Strip both sides before comparing structure.

The general lesson: when one of these reports a failure, read the actual output
before believing it.
