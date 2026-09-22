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

## Model artifacts

Not every artifact the installer delivers is a wheel. A speech model's
vocabulary file is generated from a published checkpoint rather than
compiled, so it gets its own table. Item 1 of the vendoring discipline
above applies to it unchanged: a committed plain-text `.sha256` sidecar
beside the file, holding only the lowercase 64-character hash.

The one difference from a wheel is that these files are COMMITTED, not
gitignored. They are small text files, so a clone can verify itself, and
the installer delivers a committed file to an existing installation
without downloading anything.

| Artifact | Source | Generated from | Recipe | Checksum |
|----------|--------|----------------|--------|----------|
| `bpe.vocab` (at `services/stt_providers/sherpa_offline_parakeet_stt_server/model_assets/`) | The sentencepiece tokenizer of [nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), revision `541d1f99c6b0c3cd0b11a95167540bb8edefd82b`. Model licensed CC-BY-4.0 by NVIDIA; attribution required wherever it is redistributed. | `..._tokenizer.model` (360,916 bytes, sha256 `eacec2b0a77f336d4a2ca4a25a7047575d3c2b74de47e997f4c205126ed3135e`), read out of `parakeet-tdt-0.6b-v3.nemo` with an HTTP range request. The `.nemo` is an uncompressed tar and the tokenizer sits in its first 680 KB, so the 2.51 GB weights member is never transferred. | `scripts/nemo/generate_bpe_vocab.py` from k2-fsa/sherpa-onnx at tag `v1.13.3` (3,533 bytes, sha256 `d898c4261b480fa189a6d5375fb308703dc6fd7a82f805fbcf6f01ede91fe73c`), unmodified, its `generate_bpe_vocab_from_tokenizer` called with a `sentencepiece` 0.2.2 processor over that tokenizer. Isolated scratch venv, CPython 3.12.10, no NeMo and no torch. Output converted from CRLF to LF before commit. | LF form, 117,408 bytes, sha256 `41d5e71b3591642eff088151efd7acd4e750124cc0054c8ba9fa3245187a4804`. **Whole-file digest not verified; tokens.txt match verified.** |

**What "whole-file digest not verified" means here.** HuggingFace publishes
a SHA-256 for the whole checkpoint
(`3cbdc85877e668ca7b82d0d56770eb1fac76691f55d6b97545e8d61ca588d10d`,
2,509,332,480 bytes), and a range request that transfers 2 MB cannot be
checked against a digest of 2.51 GB. What replaces it is a match against
the shipped model's own token list: all 8192 pieces of `bpe.vocab` appear
in `tokens.txt` in the same order, the only extra row there is `<blk>`,
and the `tokens.txt` inside the release archive and the copy on the
HuggingFace mirror have the same SHA-256
(`d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d`).
A tampered range would have to reproduce that list piece for piece. To
upgrade this to a verified publisher digest, download the full 2.51 GB
checkpoint once, check it against the digest above, and confirm the
extracted tokenizer still hashes to `eacec2b0...3135e`.

The line ending is part of the checksum. The generator opens its output in
Python text mode, so it writes CRLF on Windows and LF elsewhere. The
committed form is LF, which is also what `.gitattributes` (`* text=auto
eol=lf`) checks out on every machine, so the recorded hash matches the
working-tree file anywhere.

## Install-time Microsoft runtime fetch

One artifact is fetched at install time rather than vendored: the Microsoft
Visual C++ Redistributable (`vc_redist.x64.exe`). `install-wheelhouse.ps1`
downloads it from Microsoft's permanent link, and only when the x64
`msvcp140.dll` in the Windows system directory is missing or below the
version the speech engine needs. The installer builds that path from
`$env:SystemRoot`, not a literal `C:\Windows`, because Windows is not always
on C. A 32-bit PowerShell host reaches the x64 directory through `Sysnative`,
because WOW64 would otherwise redirect `System32` to `SysWOW64` and read the
x86 file instead. A machine already at or above that version downloads
nothing. No Microsoft file enters this repository or the release archive, so
WheelHouse distributes no Microsoft file.

**Why item 1 of the vendoring discipline cannot apply.** A committed
`.sha256` sidecar is the trust anchor for every vendored wheel. Microsoft's
permanent link serves whatever version is current, so the bytes behind it
change with each Microsoft update. A pinned hash would reject the file the
first time Microsoft ships an update, and the install would then stop for
every user until someone committed a new hash. Pinning a versioned URL
instead would avoid that, at the cost of handing users a runtime that ages
with no way to refresh it.

**The rule that replaces it.** The installer accepts the downloaded file
only when BOTH of these hold:

1. `Get-AuthenticodeSignature` reports `Status` exactly `Valid`.
2. The signer certificate subject names Microsoft Corporation.

A file that fails either check is deleted and never run. The signature check
does for this artifact what the hash does for a wheel: it proves the
publisher. It does not pin the version, and it is not meant to. The download
uses HTTPS, and the signature check does not trust the transport.

**What this costs.** Item 2 of the vendoring discipline forbids resolving a
wheel from a third-party URL at install time, and the reason behind it is
that an install should be reproducible without the network deciding what a
user gets. This fetch gives that up for one file. It is worth it only
because the alternative is worse: without the runtime the speech engine
cannot start at all, and the project holds no licence that permits shipping
Microsoft's file.
The exception is narrow by construction. It covers one file, from one
publisher, fetched only when the local runtime is too old to run the speech
engine, and `-ExpectedSha256` stays mandatory for every other download in
`install-wheelhouse.ps1`.

## Re-vendoring procedure (Vulkan wheels)

The Vulkan wheels are custom builds, not PyPI downloads. To re-vendor (new upstream version, new Vulkan SDK, or a fresh machine):

1. Build with the recipe in `docs/design/stt-and-ai.md` Section 4 (requires Vulkan SDK, Visual Studio Build Tools, CMake). Dev machines have a convenience wrapper, `scripts\build_stt_vulkan_wheel.bat` (not part of the public repository), which additionally runs `repairwheel`.
2. Place the wheel under the owning service's `vendor/wheels/` directory and update the `[tool.uv.sources]` path entry if the filename changed.
3. Compute `sha256sum <wheel>` and write the lowercase hash as the only line of `<wheel>.sha256` next to it. Commit the sidecar (the wheel stays gitignored).
4. Update the Inventory row (pinned upstream commit, SDK version).
5. Verify Vulkan acceleration still works after reinstalling the wheel. Note that the wheel is a manual install, not a declared dependency (`docs/design/stt-and-ai.md` Section 4 explains why), so any later `uv sync` in the `shared` service removes it -- reinstall after syncing.
