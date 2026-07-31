# recommendations.py - Hardware-based STT/TTS/AI recommendations for WheelHouse Installer
#
# Consumes syscheck.py output and returns ranked recommendations with trade-offs.
#
# :flow: installer.recommendations
# :depends: syscheck.py output (dict)

"""
Hardware recommendation engine for WheelHouse installer.

Example usage:
    import json
    from recommendations import get_full_recommendation

    syscheck_data = json.load(open("is_admin_true.json"))
    recs = get_full_recommendation(syscheck_data)
    print(f"Tier: {recs['tier']}")
    print(f"Recommended STT: {recs['stt']['recommended']}")
"""

from __future__ import annotations

# ========================= Constants =========================

# GPU vendor IDs
VENDOR_NVIDIA = 0x10DE
VENDOR_AMD = 0x1002
VENDOR_INTEL = 0x8086
VENDOR_MICROSOFT = 0x1414  # Basic Render Driver

# VRAM thresholds (bytes)
VRAM_24GB = 24 * 1024**3
VRAM_12GB = 12 * 1024**3
VRAM_8GB = 8 * 1024**3
VRAM_6GB = 6 * 1024**3
VRAM_4GB = 4 * 1024**3

# RAM thresholds (bytes)
RAM_64GB = 64 * 1024**3
RAM_32GB = 32 * 1024**3
RAM_16GB = 16 * 1024**3
RAM_12GB = 12 * 1024**3
RAM_8GB = 8 * 1024**3

# Tier order (best to worst)
TIER_ORDER = ["ultra", "high", "mid", "intel", "budget", "low", "minimum"]


# ========================= Tier Classification =========================

def classify_gpu_tier(syscheck: dict) -> str:
    """
    Classify system into a hardware tier based on GPU and RAM.

    Tiers:
    - ultra: 24GB+ VRAM, 64GB+ RAM (RTX 4090, etc.)
    - high: 8-12GB VRAM, 32GB+ RAM (RTX 3060-3080, RX 6800+)
    - mid: 4-6GB VRAM, 16GB+ RAM (GTX 1650+, RX 580+)
    - intel: Intel Arc/NPU with AVX-512 support
    - budget: 0-4GB VRAM, 8-12GB RAM (integrated/old discrete)
    - low: No discrete GPU, 8GB RAM, CPU-only
    - minimum: <8GB RAM, very limited

    Args:
        syscheck: Output from syscheck.py

    Returns:
        Tier string: one of TIER_ORDER values
    """
    gpus = syscheck.get("gpu", [])
    memory = syscheck.get("memory", {})
    cpu = syscheck.get("cpu", {})

    total_ram = memory.get("total_bytes", 0)
    cpu_flags = cpu.get("flags", {})
    has_avx512 = cpu_flags.get("avx512", False)

    # Find best discrete GPU (not software renderer)
    best_gpu = None
    best_vram = 0
    for gpu in gpus:
        if gpu.get("software", False):
            continue
        vram = gpu.get("dedicated_vram_bytes", 0)
        if vram > best_vram:
            best_vram = vram
            best_gpu = gpu

    # Check for Intel Arc/NPU
    has_intel_arc = False
    if best_gpu:
        vendor = best_gpu.get("vendor_id", 0)
        name = best_gpu.get("name", "").lower()
        if vendor == VENDOR_INTEL and ("arc" in name or "iris xe" in name):
            has_intel_arc = True

    # Minimum tier: very low RAM
    if total_ram < RAM_8GB:
        return "minimum"

    # Intel tier: Intel Arc/NPU with AVX-512 (OpenVINO optimization)
    # Check this BEFORE mid tier since Arc can have significant VRAM
    if has_intel_arc and has_avx512:
        return "intel"

    # Ultra tier: monster GPU + lots of RAM
    if best_vram >= VRAM_24GB and total_ram >= RAM_64GB:
        return "ultra"

    # High tier: good discrete GPU + solid RAM
    # Use 10GB threshold to include RTX 3060 (11.8GB) with 30GB+ RAM
    if best_vram >= VRAM_8GB and total_ram >= RAM_16GB:
        return "high"

    # Mid tier: decent discrete GPU
    if best_vram >= VRAM_4GB and total_ram >= RAM_16GB:
        return "mid"

    # Budget tier: some GPU capability or decent RAM
    if best_vram > 0 or total_ram >= RAM_12GB:
        return "budget"

    # Low tier: CPU-only with 8GB RAM
    if total_ram >= RAM_8GB:
        return "low"

    return "minimum"


def _get_nvidia_cuda_support(gpu: dict) -> bool:
    """Check if GPU supports CUDA (NVIDIA with compute support)."""
    vendor = gpu.get("vendor_id", 0)
    compute = gpu.get("compute_support", {})
    return vendor == VENDOR_NVIDIA and compute.get("d3d11", False)


# ========================= STT Recommendations =========================

def get_stt_recommendation(syscheck: dict) -> dict:
    """
    Get STT (Speech-to-Text) recommendations based on hardware.

    Available providers (post wh-7z3 deprecation cleanup, 2026-04-18):
    - distil_medium_en: distil-whisper medium.en via faster-whisper on CUDA
      (NVIDIA GPU, 4GB+ VRAM). Replaces the retired faster_whisper_turbo path.
    - sherpa_offline_parakeet_stt_server: NVIDIA Parakeet-TDT v3 via sherpa-ONNX
      (CPU). Replaces the retired faster_whisper_cpu path; best open-source WER
      on English. AVX2 still gated because sherpa-ONNX quantized kernels lean
      on AVX2; systems without AVX2 should fall through to cloud.
    - google_cloud_stt: Cloud fallback (no local resources needed).

    Returns:
        dict with 'recommended' (str), 'options' (list), and 'warnings' (list)
    """
    cpu_flags = syscheck.get("cpu", {}).get("flags", {})
    has_avx2 = cpu_flags.get("avx2", False)

    # Find CUDA-capable NVIDIA GPU with sufficient VRAM
    gpus = syscheck.get("gpu", [])
    has_cuda = any(_get_nvidia_cuda_support(g) for g in gpus if not g.get("software"))

    best_nvidia_vram = 0
    for gpu in gpus:
        if gpu.get("software", False):
            continue
        if _get_nvidia_cuda_support(gpu):
            vram = gpu.get("dedicated_vram_bytes", 0)
            if vram > best_nvidia_vram:
                best_nvidia_vram = vram

    total_ram = syscheck.get("memory", {}).get("total_bytes", 0)

    options = []
    warnings = []

    # GPU provider: distil-whisper medium.en on CUDA (NVIDIA 4GB+ VRAM)
    if has_cuda and best_nvidia_vram >= VRAM_4GB:
        options.append({
            "id": "distil_medium_en",
            "name": "Distil-Whisper Medium.en (GPU)",
            "variant": "cuda",
            "pros": ["Excellent WER", "Low latency"],
            "cons": ["~750MB download", "Requires NVIDIA GPU with 4GB+ VRAM"],
            "size_mb": 750,
            "suitable": True,
        })

    # CPU provider: Parakeet-TDT v3 via sherpa-ONNX. AVX2 still gated because
    # quantized sherpa kernels lean heavily on it; AVX2-less systems fall
    # through to cloud per test_cpu_without_avx2_recommends_cloud.
    if has_avx2 and total_ram >= RAM_8GB:
        options.append({
            "id": "sherpa_offline_parakeet_stt_server",
            "name": "Parakeet TDT (CPU)",
            "variant": "cpu",
            "pros": ["Best open-source English WER", "No GPU needed"],
            "cons": ["~600MB download", "Higher latency than GPU"],
            "size_mb": 600,
            "suitable": True,
        })

    if not has_avx2:
        warnings.append(
            "AVX2 not detected - local CPU inference (int8 quantization) "
            "will be very slow. Cloud STT recommended."
        )

    # Cloud fallback: always available
    options.append({
        "id": "google_cloud_stt",
        "name": "Google Cloud STT",
        "variant": "cloud",
        "pros": ["High accuracy", "No local resources needed"],
        "cons": ["Requires internet", "Privacy considerations"],
        "size_mb": 5,
        "suitable": True,
    })

    # Pick recommended: GPU > CPU > Cloud
    suitable = [o for o in options if o.get("suitable", True)]
    recommended = suitable[0]["id"] if suitable else "google_cloud_stt"

    result = {
        "recommended": recommended,
        "options": suitable,
    }
    if warnings:
        result["warnings"] = warnings
    return result


# ========================= TTS Recommendations =========================

def get_tts_recommendation(syscheck: dict) -> dict:
    """
    Get TTS (Text-to-Speech) recommendations based on hardware.

    Returns:
        dict with 'recommended' (str) and 'options' (list of option dicts)
    """
    tier = classify_gpu_tier(syscheck)

    options = []

    if tier in ("ultra", "high"):
        options.append({
            "id": "chatterbox",
            "name": "Chatterbox (Neural TTS)",
            "pros": ["Most natural voice", "Emotion support"],
            "cons": ["~500MB", "GPU recommended"],
            "size_mb": 500,
            "suitable": True,
        })

    # Piper works well on most systems
    options.append({
        "id": "piper",
        "name": "Piper (Local TTS)",
        "pros": ["Fast", "Works offline", "Good quality"],
        "cons": ["~50MB per voice"],
        "size_mb": 50,
        "suitable": True,
    })

    if tier == "minimum":
        options.append({
            "id": "azure_tts",
            "name": "Azure TTS (Cloud)",
            "variant": "cloud",
            "pros": ["Best quality", "Many voices"],
            "cons": ["Requires internet", "Usage costs"],
            "size_mb": 5,
            "suitable": True,
        })
    else:
        # Cloud as fallback option
        options.append({
            "id": "azure_tts",
            "name": "Azure TTS (Cloud)",
            "variant": "cloud",
            "pros": ["Highest quality", "Many voice options"],
            "cons": ["Requires internet"],
            "size_mb": 5,
            "suitable": True,
        })

    recommended = options[0]["id"] if options else "piper"

    return {
        "recommended": recommended,
        "options": options,
    }


# ========================= Local AI Placement =========================

# The model Wheelhouse ships. The name is the component registry key in
# services/installer/components/registry.py.
LOCAL_AI_MODEL = "gemma4_e4b_qat"

# Both thresholds come from the measurements behind
# docs/superpowers/specs/2026-07-26-local-ai-runtime-design.md.
#
# Video memory: the model's weights are 5.15 GB. A card has to hold enough of
# them for the transfer to be worth making; below 4 GB llama.cpp spills back to
# system memory on every layer it cannot place, and the request ends up slower
# than running on the processor outright.
#
# System memory: E4B needs about 5.3 GB of working set with no graphics card.
# 16 GB is the floor at which that leaves room for Windows, the browser the
# user is dictating into, and Wheelhouse itself.
LOCAL_AI_MIN_VRAM = VRAM_4GB
LOCAL_AI_MIN_RAM = RAM_16GB


def get_local_ai_decision(syscheck: dict) -> dict:
    """Decide where -- or whether -- the local AI model runs on this machine.

    One function so the installer and syscheck cannot give a user two
    different answers (design section 6).

    Returns a dict with four keys, always the same four:

    - ``install``     -- whether to download and configure the local model.
    - ``model``       -- the component registry key, or None.
    - ``gpu_layers``  -- 99 (all layers on the graphics card), 0 (processor
      only), or None when nothing is installed. This is written straight into
      ``[ai.runtime] gpu_layers``.
    - ``reason``      -- one plain sentence the installer prints.

    Never raises. syscheck degrades to empty sections when a probe fails, and
    an exception here would abort an install over a hardware question that
    should only turn the AI features off.
    """
    try:
        gpus = syscheck.get("gpu") or []
        best_vram = 0
        for gpu in gpus:
            if not isinstance(gpu, dict) or gpu.get("software", False):
                continue
            # Any vendor: the shipped build is Vulkan, which covers NVIDIA,
            # AMD, and Intel alike. A CUDA-only check would refuse every AMD
            # machine for no reason.
            vram = int(gpu.get("dedicated_vram_bytes", 0) or 0)
            if vram > best_vram:
                best_vram = vram

        total_ram = int((syscheck.get("memory") or {}).get("total_bytes", 0) or 0)
    # int() rather than using the values as they arrive: a byte count that
    # comes through as a string compares fine against another string and
    # raises against an integer, so the comparison below would throw outside
    # this block. Coercing here keeps every failure on one path.
    except (AttributeError, TypeError, ValueError):
        return {
            "install": False,
            "model": None,
            "gpu_layers": None,
            "reason": (
                "The hardware check could not read this machine, so the local "
                "AI model was not installed."
            ),
        }

    if best_vram >= LOCAL_AI_MIN_VRAM:
        return {
            "install": True,
            "model": LOCAL_AI_MODEL,
            "gpu_layers": 99,
            "reason": (
                "This machine has a graphics card with enough video memory to "
                "hold the model, so it will run there."
            ),
        }

    if total_ram >= LOCAL_AI_MIN_RAM:
        return {
            "install": True,
            "model": LOCAL_AI_MODEL,
            "gpu_layers": 0,
            "reason": (
                "This machine has no graphics card with 4 GB of video memory, "
                "so the model will run on the processor. That works, but it is "
                "slow: about 3 seconds for a short correction and about 12 "
                "seconds for a long one."
            ),
        }

    return {
        "install": False,
        "model": None,
        "gpu_layers": None,
        "reason": (
            "The local AI model needs either a graphics card with 4 GB of "
            "video memory or 16 GB of system memory, and this machine has "
            "neither. The correcting and rewriting commands will still work "
            "if you configure your own AI server later."
        ),
    }


# ========================= AI/LLM Recommendations =========================

def get_ai_recommendation(syscheck: dict) -> dict:
    """Say which AI option this machine should use, and why.

    Presentation only: the hardware question is settled by
    ``get_local_ai_decision`` above, and this reads that answer. It used to
    run a ladder of its own over ``classify_gpu_tier``, offering Llama 3 70B,
    Phi-3 Medium and others -- none of which Wheelhouse ships or the installer
    can put on a machine. Two ladders over the same hardware also disagreed:
    one could tell a user their machine ran a model locally while the other
    installed nothing (wh-syscheck-ai-recommendation).

    Returns:
        dict with 'recommended' (str), 'options' (list of option dicts) and
        'reason' (the ladder's own sentence, so the caller shows the same
        wording the installer prints).
    """
    decision = get_local_ai_decision(syscheck)

    options = []
    if decision["install"]:
        on_the_card = decision["gpu_layers"] != 0
        options.append({
            "id": decision["model"],
            "name": "Gemma 4 E4B, on this machine",
            "variant": "local",
            "pros": [
                "Nothing leaves the machine",
                "No account and no key",
                "Runs on the graphics card" if on_the_card
                else "Runs without a graphics card",
            ],
            "cons": [
                "5.2 GB download",
                "About 1 second for a short correction" if on_the_card
                else "About 3 seconds for a short correction, 12 for a long one",
            ],
            "size_mb": 5150,
            "suitable": True,
        })

    options.append({
        "id": "gemini",
        "name": "Gemini (Cloud)",
        "variant": "cloud",
        "pros": ["Very capable", "Free tier available", "No local resources"],
        "cons": ["Requires internet", "API key needed"],
        "size_mb": 5,
        "suitable": True,
    })

    return {
        "recommended": options[0]["id"],
        "options": options,
        "reason": decision["reason"],
    }


# ========================= Disk Recommendations =========================

def get_disk_recommendation(syscheck: dict) -> list[dict]:
    """
    Rank disks for WheelHouse installation.

    Considers:
    - Disk type (SSD preferred over HDD)
    - Free space (need at least 5GB, prefer 50GB+)
    - Not system drive if alternatives exist

    Returns:
        List of disk recommendations sorted by score (best first)
    """
    disks = syscheck.get("disks", [])
    MIN_FREE_GB = 5
    PREFERRED_FREE_GB = 50

    recommendations = []

    for disk in disks:
        drive = disk.get("drive", "")
        kind = disk.get("kind", "unknown")
        free_bytes = disk.get("free_bytes", 0)
        total_bytes = disk.get("total_bytes", 0)

        free_gb = free_bytes / (1024**3)
        total_gb = total_bytes / (1024**3)

        # Skip if not enough space
        if free_gb < MIN_FREE_GB:
            continue

        # Skip network/cloud/removable drives
        if kind in ("network", "cloud", "removable"):
            continue

        pros = []
        cons = []
        score = 50  # Base score

        # Disk type scoring
        if kind == "ssd":
            score += 30
            pros.append("SSD (fast)")
        elif kind == "hdd":
            score -= 10
            cons.append("HDD (slower)")
        elif kind == "virtual":
            score -= 20
            cons.append("Virtual disk")

        # Free space scoring
        if free_gb >= PREFERRED_FREE_GB:
            score += 20
            pros.append(f"{free_gb:.0f}GB free")
        elif free_gb >= MIN_FREE_GB * 2:
            score += 10
            pros.append(f"{free_gb:.0f}GB free")
        else:
            cons.append(f"Only {free_gb:.0f}GB free")

        # Prefer non-C: drives (less system impact)
        if drive.upper() == "C:":
            score -= 5
            cons.append("System drive")
        else:
            pros.append("Non-system drive")

        recommendations.append({
            "drive": drive,
            "kind": kind,
            "free_gb": round(free_gb, 1),
            "total_gb": round(total_gb, 1),
            "score": score,
            "pros": pros,
            "cons": cons,
        })

    # Sort by score descending
    recommendations.sort(key=lambda x: x["score"], reverse=True)

    return recommendations


# ========================= Full Recommendation =========================

def get_full_recommendation(syscheck: dict) -> dict:
    """
    Get complete recommendation package for installer UI.

    Returns:
        dict with tier, stt, tts, ai, and disk recommendations
    """
    tier = classify_gpu_tier(syscheck)

    return {
        "tier": tier,
        "tier_description": _get_tier_description(tier),
        "stt": get_stt_recommendation(syscheck),
        "tts": get_tts_recommendation(syscheck),
        "ai": get_ai_recommendation(syscheck),
        "disk": get_disk_recommendation(syscheck),
    }


def _get_tier_description(tier: str) -> str:
    """Get human-readable description for a tier."""
    descriptions = {
        "ultra": "High-end gaming/workstation - can run largest models locally",
        "high": "Good discrete GPU - can run most models locally with good speed",
        "mid": "Decent GPU - can run medium models locally",
        "intel": "Intel Arc/NPU - optimized for OpenVINO acceleration",
        "budget": "Limited GPU - can run small models locally",
        "low": "CPU-only - limited local inference, cloud recommended",
        "minimum": "Very limited hardware - cloud services recommended",
    }
    return descriptions.get(tier, "Unknown hardware tier")
