# test_recommendations.py - Tests for hardware recommendations engine
#
# Uses existing syscheck JSON fixtures for testing.

import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# Import the module under test
from recommendations import (

    classify_gpu_tier,
    get_stt_recommendation,
    get_tts_recommendation,
    get_ai_recommendation,
    get_disk_recommendation,
    get_full_recommendation,
    TIER_ORDER,
)


# ========================= Fixtures =========================

@pytest.fixture
def high_end_syscheck():
    """Load the is_admin_true.json fixture (RTX 3060 + 32GB RAM)."""
    fixture_path = Path(__file__).parent.parent / "is_admin_true.json"
    with open(fixture_path) as f:
        return json.load(f)


@pytest.fixture
def low_end_syscheck():
    """Synthetic low-end system: no discrete GPU, 8GB RAM."""
    return {
        "cpu": {
            "vendor": "GenuineIntel",
            "model": "Intel Core i3-4130",
            "physical_cores": 2,
            "logical_cores": 4,
            "flags": {
                "sse4_1": True,
                "sse4_2": True,
                "avx": True,
                "avx2": False,
                "avx512": False,
                "fma": False,
            },
        },
        "memory": {
            "total_bytes": 8 * 1024**3,  # 8GB
            "available_bytes": 4 * 1024**3,
        },
        "gpu": [
            {
                "index": 0,
                "name": "Intel HD Graphics 4400",
                "vendor_id": 0x8086,
                "dedicated_vram_bytes": 0,
                "software": False,
                "compute_support": {"d3d11": False},
            }
        ],
        "disks": [
            {
                "drive": "C:",
                "kind": "hdd",
                "total_bytes": 500 * 1024**3,
                "free_bytes": 50 * 1024**3,
            }
        ],
    }


@pytest.fixture
def minimum_syscheck():
    """Synthetic minimum system: very low RAM."""
    return {
        "cpu": {"flags": {}},
        "memory": {"total_bytes": 4 * 1024**3},
        "gpu": [],
        "disks": [],
    }


@pytest.fixture
def intel_arc_syscheck():
    """Synthetic Intel Arc system with AVX-512."""
    return {
        "cpu": {
            "vendor": "GenuineIntel",
            "flags": {"avx512": True, "avx2": True},
        },
        "memory": {"total_bytes": 16 * 1024**3},
        "gpu": [
            {
                "name": "Intel Arc A770",
                "vendor_id": 0x8086,
                "dedicated_vram_bytes": 16 * 1024**3,
                "software": False,
            }
        ],
        "disks": [],
    }


@pytest.fixture
def mid_tier_nvidia_syscheck():
    """NVIDIA GPU with 6GB VRAM, 16GB RAM, AVX2."""
    return {
        "cpu": {
            "vendor": "GenuineIntel",
            "flags": {"avx2": True, "avx512": False},
        },
        "memory": {"total_bytes": 16 * 1024**3},
        "gpu": [
            {
                "name": "NVIDIA GeForce GTX 1060",
                "vendor_id": 0x10DE,
                "dedicated_vram_bytes": 6 * 1024**3,
                "software": False,
                "compute_support": {"d3d11": True},
            }
        ],
        "disks": [],
    }


@pytest.fixture
def amd_gpu_syscheck():
    """AMD GPU with 16GB VRAM, 32GB RAM - no CUDA."""
    return {
        "cpu": {
            "vendor": "AuthenticAMD",
            "flags": {"avx2": True, "avx512": False},
        },
        "memory": {"total_bytes": 32 * 1024**3},
        "gpu": [
            {
                "name": "AMD Radeon RX 6800 XT",
                "vendor_id": 0x1002,
                "dedicated_vram_bytes": 16 * 1024**3,
                "software": False,
                "compute_support": {"d3d11": True},
            }
        ],
        "disks": [],
    }


@pytest.fixture
def cpu_avx2_syscheck():
    """CPU-only system with AVX2, 16GB RAM."""
    return {
        "cpu": {
            "vendor": "GenuineIntel",
            "flags": {"avx2": True, "avx512": False},
        },
        "memory": {"total_bytes": 16 * 1024**3},
        "gpu": [],
        "disks": [],
    }


@pytest.fixture
def cpu_no_avx2_syscheck():
    """CPU-only system WITHOUT AVX2, 8GB RAM."""
    return {
        "cpu": {
            "vendor": "GenuineIntel",
            "flags": {"avx2": False, "avx512": False},
        },
        "memory": {"total_bytes": 8 * 1024**3},
        "gpu": [],
        "disks": [],
    }


# ========================= Tier Classification Tests =========================

class TestClassifyGpuTier:
    def test_high_tier_rtx3060(self, high_end_syscheck):
        """RTX 3060 12GB + 32GB RAM should be 'high' tier."""
        tier = classify_gpu_tier(high_end_syscheck)
        assert tier == "high"

    def test_low_tier_integrated(self, low_end_syscheck):
        """Integrated GPU + 8GB RAM should be 'low' tier."""
        tier = classify_gpu_tier(low_end_syscheck)
        assert tier == "low"

    def test_minimum_tier_low_ram(self, minimum_syscheck):
        """<8GB RAM should be 'minimum' tier."""
        tier = classify_gpu_tier(minimum_syscheck)
        assert tier == "minimum"

    def test_intel_tier_arc(self, intel_arc_syscheck):
        """Intel Arc + AVX-512 should be 'intel' tier."""
        tier = classify_gpu_tier(intel_arc_syscheck)
        assert tier == "intel"

    def test_ultra_tier(self):
        """24GB+ VRAM + 64GB+ RAM should be 'ultra' tier."""
        syscheck = {
            "cpu": {"flags": {}},
            "memory": {"total_bytes": 64 * 1024**3},
            "gpu": [
                {
                    "name": "NVIDIA RTX 4090",
                    "vendor_id": 0x10DE,
                    "dedicated_vram_bytes": 24 * 1024**3,
                    "software": False,
                }
            ],
        }
        tier = classify_gpu_tier(syscheck)
        assert tier == "ultra"

    def test_mid_tier_nvidia(self, mid_tier_nvidia_syscheck):
        """GTX 1060 6GB + 16GB RAM should be 'mid' tier."""
        tier = classify_gpu_tier(mid_tier_nvidia_syscheck)
        assert tier == "mid"

    def test_amd_high_vram_classified_as_high(self, amd_gpu_syscheck):
        """AMD GPU with 16GB VRAM should be 'high' tier (tier is hardware, not CUDA)."""
        tier = classify_gpu_tier(amd_gpu_syscheck)
        assert tier == "high"


# ========================= STT Recommendation Tests =========================

class TestSttRecommendation:
    def test_high_tier_nvidia_recommends_turbo(self, high_end_syscheck):
        """High tier NVIDIA should recommend faster_whisper_turbo."""
        recs = get_stt_recommendation(high_end_syscheck)
        assert recs["recommended"] == "distil_medium_en"

    def test_mid_tier_nvidia_recommends_turbo(self, mid_tier_nvidia_syscheck):
        """Mid tier NVIDIA with CUDA should recommend faster_whisper_turbo."""
        recs = get_stt_recommendation(mid_tier_nvidia_syscheck)
        assert recs["recommended"] == "distil_medium_en"

    def test_amd_gpu_recommends_cpu_provider(self, amd_gpu_syscheck):
        """AMD GPU (no CUDA) should recommend faster_whisper_cpu."""
        recs = get_stt_recommendation(amd_gpu_syscheck)
        assert recs["recommended"] == "sherpa_offline_parakeet_stt_server"

    def test_cpu_only_with_avx2_recommends_cpu_provider(self, cpu_avx2_syscheck):
        """CPU-only with AVX2 should recommend faster_whisper_cpu."""
        recs = get_stt_recommendation(cpu_avx2_syscheck)
        assert recs["recommended"] == "sherpa_offline_parakeet_stt_server"

    def test_cpu_without_avx2_recommends_cloud(self, cpu_no_avx2_syscheck):
        """CPU without AVX2 should recommend cloud STT."""
        recs = get_stt_recommendation(cpu_no_avx2_syscheck)
        assert recs["recommended"] == "google_cloud_stt"

    def test_minimum_tier_recommends_cloud(self, minimum_syscheck):
        """Minimum tier should recommend cloud STT."""
        recs = get_stt_recommendation(minimum_syscheck)
        assert recs["recommended"] == "google_cloud_stt"

    def test_low_tier_no_avx2_recommends_cloud(self, low_end_syscheck):
        """Low tier without AVX2 should recommend cloud (int8 too slow)."""
        recs = get_stt_recommendation(low_end_syscheck)
        assert recs["recommended"] == "google_cloud_stt"

    def test_all_options_have_required_fields(self, high_end_syscheck):
        """All options should have id, name, pros, cons."""
        recs = get_stt_recommendation(high_end_syscheck)
        for opt in recs["options"]:
            assert "id" in opt
            assert "name" in opt
            assert "pros" in opt
            assert "cons" in opt

    def test_warnings_present_when_no_avx2(self, cpu_no_avx2_syscheck):
        """Should include AVX2 warning when CPU lacks AVX2."""
        recs = get_stt_recommendation(cpu_no_avx2_syscheck)
        assert "warnings" in recs
        assert any("AVX2" in w for w in recs["warnings"])

    def test_no_warnings_when_avx2_present(self, high_end_syscheck):
        """Should not include AVX2 warning when CPU has AVX2."""
        recs = get_stt_recommendation(high_end_syscheck)
        warnings = recs.get("warnings", [])
        assert not any("AVX2" in w for w in warnings)

    def test_cloud_always_available_as_option(self, high_end_syscheck):
        """Cloud STT should always be available as a fallback option."""
        recs = get_stt_recommendation(high_end_syscheck)
        option_ids = [o["id"] for o in recs["options"]]
        assert "google_cloud_stt" in option_ids

    def test_intel_arc_gets_cpu_provider(self, intel_arc_syscheck):
        """Intel Arc (no CUDA) should get faster_whisper_cpu."""
        recs = get_stt_recommendation(intel_arc_syscheck)
        assert recs["recommended"] == "sherpa_offline_parakeet_stt_server"


# ========================= TTS Recommendation Tests =========================

class TestTtsRecommendation:
    def test_high_tier_recommends_chatterbox(self, high_end_syscheck):
        """High tier should recommend Chatterbox or Piper."""
        recs = get_tts_recommendation(high_end_syscheck)
        assert recs["recommended"] in ("chatterbox", "piper")

    def test_low_tier_recommends_piper(self, low_end_syscheck):
        """Low tier should recommend Piper."""
        recs = get_tts_recommendation(low_end_syscheck)
        assert recs["recommended"] == "piper"


# ========================= AI Recommendation Tests =========================

class TestAiRecommendation:
    def test_high_tier_recommends_local_llm(self, high_end_syscheck):
        """High tier should recommend local LLM."""
        recs = get_ai_recommendation(high_end_syscheck)
        assert recs["recommended"] in ("llama_8b", "phi3_medium")

    def test_minimum_tier_recommends_cloud(self, minimum_syscheck):
        """Minimum tier should recommend cloud AI."""
        recs = get_ai_recommendation(minimum_syscheck)
        assert recs["recommended"] == "gemini"


# ========================= Disk Recommendation Tests =========================

class TestDiskRecommendation:
    def test_prefers_ssd_over_hdd(self, high_end_syscheck):
        """Should rank SSDs higher than HDDs."""
        recs = get_disk_recommendation(high_end_syscheck)
        # The fixture has SSDs and HDDs; SSDs should come first
        ssd_indices = [i for i, d in enumerate(recs) if d["kind"] == "ssd"]
        hdd_indices = [i for i, d in enumerate(recs) if d["kind"] == "hdd"]
        if ssd_indices and hdd_indices:
            assert min(ssd_indices) < min(hdd_indices)

    def test_excludes_network_drives(self):
        """Should exclude network drives."""
        syscheck = {
            "disks": [
                {"drive": "Z:", "kind": "network", "free_bytes": 100 * 1024**3, "total_bytes": 1000 * 1024**3},
                {"drive": "C:", "kind": "ssd", "free_bytes": 50 * 1024**3, "total_bytes": 500 * 1024**3},
            ]
        }
        recs = get_disk_recommendation(syscheck)
        drives = [r["drive"] for r in recs]
        assert "Z:" not in drives
        assert "C:" in drives

    def test_excludes_low_space_drives(self):
        """Should exclude drives with <5GB free."""
        syscheck = {
            "disks": [
                {"drive": "C:", "kind": "ssd", "free_bytes": 2 * 1024**3, "total_bytes": 500 * 1024**3},
                {"drive": "D:", "kind": "ssd", "free_bytes": 100 * 1024**3, "total_bytes": 500 * 1024**3},
            ]
        }
        recs = get_disk_recommendation(syscheck)
        drives = [r["drive"] for r in recs]
        assert "C:" not in drives
        assert "D:" in drives


# ========================= Full Recommendation Tests =========================

class TestFullRecommendation:
    def test_returns_all_sections(self, high_end_syscheck):
        """Full recommendation should include all sections."""
        recs = get_full_recommendation(high_end_syscheck)
        assert "tier" in recs
        assert "tier_description" in recs
        assert "stt" in recs
        assert "tts" in recs
        assert "ai" in recs
        assert "disk" in recs

    def test_tier_in_order(self, high_end_syscheck):
        """Tier should be a valid tier string."""
        recs = get_full_recommendation(high_end_syscheck)
        assert recs["tier"] in TIER_ORDER
