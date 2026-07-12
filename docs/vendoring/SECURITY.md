# Vendored binary supply-chain posture

Last reviewed: 2026-07-05

WheelHouse vendors native binary wheels under `vendor/wheels/` directories to guarantee reproducible, air-gapped installs for end users. This document records which third-party artifacts we trust, how we trust them, and what our fallback is if any publisher becomes unmaintained.

## Vendoring discipline (applies to every wheel under `vendor/wheels/`)

1. Every vendored wheel has a committed plain-text `.sha256` sidecar containing only the lowercase 64-char hash of the wheel file. The wheel binary itself is **gitignored** (`**/vendor/wheels/*.whl`) -- it is a locally-built or separately-distributed artifact, not a committed file. The committed sidecar is the trust anchor: installers and build scripts verify the on-disk wheel against the sidecar before `pip install`. (Earlier wording of this item said wheels are committed; that predated the gitignore rule and was reconciled 2026-07-05 under wh-hia.)
2. Wheels are never resolved from PyPI or a third-party URL at install time. Hash verification + the locally-present binary is the atomic trust unit.
3. Every vendored wheel has an explicit entry in the "Inventory" table below with source, license, and the threat-model notes specific to that publisher.
4. Provenance check before first commit of a sidecar:
   - **Downloaded wheels:** cross-check the sha256 from a **second independent network** (e.g., mobile-data tether) and diff against the sidecar. Any mismatch -> do not ship.
   - **Locally-compiled wheels** (the Vulkan builds): there is no independent download to diff against. Instead, record the pinned upstream source commit and the build recipe in the Inventory row, so the artifact can be reproduced from source.

## Inventory

| Wheel | Source | License | Threat notes | Fallback |
|-------|--------|---------|--------------|----------|
| `pywhispercpp-1.4.2.dev2+gaaf756bd3.d20260324-cp312-cp312-win_amd64.whl` (at `services/stt_providers/shared/vendor/wheels/`) | Compiled locally from [absadiki/pywhispercpp](https://github.com/absadiki/pywhispercpp) at commit `aaf756bd3` (embedded in the version tag) with whisper.cpp Vulkan acceleration, via `scripts/build_stt_vulkan_wheel.bat` (Vulkan SDK 1.4.341.0, VS Build Tools, Ninja, repairwheel) | MIT (pywhispercpp and whisper.cpp) | Not a downloaded artifact: supply chain is the upstream git repo at the pinned commit plus the local toolchain. The PyPI `pywhispercpp` package is NOT this wheel (PyPI builds are CPU-only). | Rebuild from source with the recipe above (see also `docs/design/stt-and-ai.md` Section 4). Degraded fallback: CPU-only PyPI `pywhispercpp`, which works but loses GPU acceleration. |

_Historical: the Parakeet ITN (Inverse Text Normalization) feature previously vendored NeMo text-processing wheels (`pynini-windows`, `nemo_text_processing`, plus roughly thirty transitive dependencies) under this policy. ITN was retired 2026-04-21 (commit `984c7ba`) and those wheels have been removed from the repository. The retired design spec remains at `docs/superpowers/specs/2026-04-19-parakeet-itn-and-hotwords-design.md` for historical reference._

_Historical: `llama-cpp-python` was vendored as a Vulkan wheel for in-process AI inference. The AI thin-client redesign (wh-ai-thin-client, 2026-06-18) removed all in-process model loading, so that wheel and its support wheels were removed; AI now talks to an external server (Ollama or any OpenAI-compatible endpoint)._

## Re-vendoring procedure (Vulkan wheels)

The Vulkan wheels are custom builds, not PyPI downloads. To re-vendor (new upstream version, new Vulkan SDK, or a fresh machine):

1. Build with the recipe in `docs/design/stt-and-ai.md` Section 4 (requires Vulkan SDK, Visual Studio Build Tools, CMake). Dev machines have a convenience wrapper, `scripts\build_stt_vulkan_wheel.bat` (not part of the public repository), which additionally runs `repairwheel`.
2. Place the wheel under the owning service's `vendor/wheels/` directory and update the `[tool.uv.sources]` path entry if the filename changed.
3. Compute `sha256sum <wheel>` and write the lowercase hash as the only line of `<wheel>.sha256` next to it. Commit the sidecar (the wheel stays gitignored).
4. Update the Inventory row (pinned upstream commit, SDK version).
5. Verify Vulkan acceleration still works after reinstalling the wheel. Note that the wheel is a manual install, not a declared dependency (`docs/design/stt-and-ai.md` Section 4 explains why), so any later `uv sync` in the `shared` service removes it -- reinstall after syncing.
