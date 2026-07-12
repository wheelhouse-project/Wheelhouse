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


# ========================= AI/LLM Recommendations =========================

def get_ai_recommendation(syscheck: dict) -> dict:
    """
    Get AI/LLM recommendations based on hardware.

    Returns:
        dict with 'recommended' (str) and 'options' (list of option dicts)
    """
    tier = classify_gpu_tier(syscheck)
    cpu_flags = syscheck.get("cpu", {}).get("flags", {})
    has_avx512 = cpu_flags.get("avx512", False)

    options = []

    if tier == "ultra":
        options.append({
            "id": "llama_70b_q4",
            "name": "Llama 3 70B (Q4)",
            "pros": ["Most capable", "Near-GPT-4 quality"],
            "cons": ["~40GB download", "Requires 24GB+ VRAM"],
            "size_mb": 40000,
            "suitable": True,
        })

    if tier in ("ultra", "high"):
        options.append({
            "id": "llama_8b",
            "name": "Llama 3 8B",
            "pros": ["Very capable", "Good speed"],
            "cons": ["~5GB download"],
            "size_mb": 5000,
            "suitable": True,
        })

    if tier in ("ultra", "high", "mid"):
        options.append({
            "id": "phi3_medium",
            "name": "Phi-3 Medium",
            "pros": ["Efficient", "Good reasoning"],
            "cons": ["~2GB download"],
            "size_mb": 2000,
            "suitable": True,
        })

    if tier == "intel" or has_avx512:
        options.append({
            "id": "phi3_openvino",
            "name": "Phi-3 (OpenVINO)",
            "pros": ["Optimized for Intel", "Fast inference"],
            "cons": ["Intel-specific"],
            "size_mb": 2500,
            "suitable": has_avx512,
        })

    if tier in ("budget", "low"):
        options.append({
            "id": "phi3_mini",
            "name": "Phi-3 Mini",
            "pros": ["Small footprint", "Reasonable quality"],
            "cons": ["Less capable than larger models"],
            "size_mb": 1500,
            "suitable": True,
        })

    # Cloud options
    options.append({
        "id": "gemini",
        "name": "Gemini (Cloud)",
        "variant": "cloud",
        "pros": ["Very capable", "Free tier available", "No local resources"],
        "cons": ["Requires internet", "API key needed"],
        "size_mb": 5,
        "suitable": True,
    })

    # Filter suitable and pick recommended
    suitable = [o for o in options if o.get("suitable", True)]
    recommended = suitable[0]["id"] if suitable else "gemini"

    return {
        "recommended": recommended,
        "options": suitable,
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
