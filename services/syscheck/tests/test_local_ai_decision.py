# test_local_ai_decision.py - The local AI hardware ladder
# (wh-ai-installer-hardware-decision)
#
# One function decides where the model runs, so the installer and syscheck
# cannot give a user two different answers. Every machine here is DESCRIBED --
# a dict written out in the test -- not read off whatever hardware happens to
# be running the suite. A test that passes only on the developer machine
# proves nothing about the rung it claims to cover.
#
# The thresholds come from the measurements in
# docs/superpowers/specs/2026-07-26-local-ai-runtime-design.md: E4B needs about
# 5.3 GB of working memory on the processor, and its 5.15 GB of weights have to
# fit in video memory for graphics acceleration to be worth choosing.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from recommendations import get_local_ai_decision


def machine(*, vram_gb=0.0, ram_gb=16, vendor_id=0x10DE, software=False, gpus=None):
    """A described machine, in the shape syscheck emits."""
    if gpus is None:
        gpus = []
        if vram_gb:
            gpus = [{
                "index": 0,
                "name": "Described GPU",
                "vendor_id": vendor_id,
                "dedicated_vram_bytes": int(vram_gb * 1024**3),
                "software": software,
                "compute_support": {"d3d11": True},
            }]
    return {
        "cpu": {"flags": {}},
        "memory": {"total_bytes": int(ram_gb * 1024**3)},
        "gpu": gpus,
        "disks": [],
    }


class TestTheGraphicsCardRung:
    """A card with 4 GB or more of video memory: E4B, all layers on the card."""

    @pytest.mark.parametrize("vram_gb", [4, 6, 8, 12, 24])
    def test_a_card_with_enough_video_memory_runs_the_model_on_it(self, vram_gb):
        decision = get_local_ai_decision(machine(vram_gb=vram_gb, ram_gb=16))
        assert decision["install"] is True
        assert decision["model"] == "gemma4_e4b_qat"
        assert decision["gpu_layers"] == 99

    def test_the_card_wins_even_when_system_memory_is_small(self):
        """The weights sit in video memory, so system memory stops mattering.
        Reading the ladder the other way round would refuse a perfectly good
        machine."""
        decision = get_local_ai_decision(machine(vram_gb=8, ram_gb=8))
        assert decision["install"] is True
        assert decision["gpu_layers"] == 99

    def test_the_largest_card_is_the_one_that_counts(self):
        """A laptop with integrated graphics alongside a discrete card lists
        both. Taking the first would answer for the wrong one."""
        gpus = [
            {"index": 0, "name": "Integrated", "vendor_id": 0x8086,
             "dedicated_vram_bytes": 128 * 1024**2, "software": False,
             "compute_support": {}},
            {"index": 1, "name": "Discrete", "vendor_id": 0x10DE,
             "dedicated_vram_bytes": 8 * 1024**3, "software": False,
             "compute_support": {}},
        ]
        decision = get_local_ai_decision(machine(ram_gb=16, gpus=gpus))
        assert decision["gpu_layers"] == 99

    @pytest.mark.parametrize("vendor_id", [0x10DE, 0x1002, 0x8086])
    def test_any_vendor_qualifies(self, vendor_id):
        """The build is Vulkan, which covers NVIDIA, AMD, and Intel alike.
        A CUDA-only check here would refuse every AMD machine."""
        decision = get_local_ai_decision(
            machine(vram_gb=8, ram_gb=16, vendor_id=vendor_id)
        )
        assert decision["gpu_layers"] == 99

    def test_a_software_adapter_is_not_a_graphics_card(self):
        """Microsoft's Basic Render Driver reports as a GPU. Counting it would
        put the model on a card that does not exist."""
        gpus = [{
            "index": 0, "name": "Microsoft Basic Render Driver",
            "vendor_id": 0x1414, "dedicated_vram_bytes": 8 * 1024**3,
            "software": True, "compute_support": {},
        }]
        decision = get_local_ai_decision(machine(ram_gb=32, gpus=gpus))
        assert decision["gpu_layers"] == 0, "should fall to the processor rung"


class TestTheProcessorRung:
    """16 GB or more of system memory and no suitable card: E4B, no layers on
    the card."""

    @pytest.mark.parametrize("ram_gb", [16, 32, 64])
    def test_enough_system_memory_runs_the_model_on_the_processor(self, ram_gb):
        decision = get_local_ai_decision(machine(vram_gb=0, ram_gb=ram_gb))
        assert decision["install"] is True
        assert decision["model"] == "gemma4_e4b_qat"
        assert decision["gpu_layers"] == 0

    @pytest.mark.parametrize("vram_gb", [0.5, 1, 2, 3])
    def test_a_card_too_small_for_the_weights_falls_to_the_processor(self, vram_gb):
        """Placing 5.15 GB of weights on a 2 GB card is worse than not using
        it: llama.cpp spills, and the request is slower than the processor."""
        decision = get_local_ai_decision(machine(vram_gb=vram_gb, ram_gb=32))
        assert decision["install"] is True
        assert decision["gpu_layers"] == 0

    def test_no_graphics_card_at_all_is_handled(self):
        decision = get_local_ai_decision(machine(ram_gb=16, gpus=[]))
        assert decision["install"] is True
        assert decision["gpu_layers"] == 0

    def test_the_processor_rung_says_it_will_be_slow(self):
        """About 3 seconds for a short correction and 11.6 for a long one. A
        user who is not told this reads the wait as a fault."""
        decision = get_local_ai_decision(machine(ram_gb=16))
        assert decision["reason"], "the processor rung must explain itself"
        assert "slow" in decision["reason"].lower()


class TestTheRefusalRung:
    """Anything else: no local model."""

    @pytest.mark.parametrize("ram_gb", [4, 8, 12])
    def test_too_little_of_either_kind_of_memory_installs_nothing(self, ram_gb):
        decision = get_local_ai_decision(machine(vram_gb=0, ram_gb=ram_gb))
        assert decision["install"] is False
        assert decision["model"] is None
        assert decision["gpu_layers"] is None

    def test_a_small_card_does_not_rescue_a_machine_short_on_memory(self):
        decision = get_local_ai_decision(machine(vram_gb=2, ram_gb=8))
        assert decision["install"] is False

    def test_the_refusal_explains_itself(self):
        """The installer prints this. "Not supported" alone tells the user
        nothing about what would change the answer."""
        decision = get_local_ai_decision(machine(vram_gb=0, ram_gb=8))
        assert decision["reason"]
        assert "4 GB" in decision["reason"] or "16 GB" in decision["reason"]


class TestTheBoundaries:
    """A ladder is wrong at exactly one place: its edges."""

    def test_four_gigabytes_of_video_memory_is_enough(self):
        decision = get_local_ai_decision(machine(vram_gb=4, ram_gb=8))
        assert decision["gpu_layers"] == 99

    def test_just_under_four_gigabytes_is_not(self):
        gpus = [{"index": 0, "name": "Card", "vendor_id": 0x10DE,
                 "dedicated_vram_bytes": 4 * 1024**3 - 1, "software": False,
                 "compute_support": {}}]
        decision = get_local_ai_decision(machine(ram_gb=8, gpus=gpus))
        assert decision["install"] is False

    def test_sixteen_gigabytes_of_system_memory_is_enough(self):
        decision = get_local_ai_decision(machine(vram_gb=0, ram_gb=16))
        assert decision["install"] is True
        assert decision["gpu_layers"] == 0

    def test_just_under_sixteen_gigabytes_is_not(self):
        state = machine(vram_gb=0)
        state["memory"]["total_bytes"] = 16 * 1024**3 - 1
        decision = get_local_ai_decision(state)
        assert decision["install"] is False


class TestMissingAndMalformedInput:
    """syscheck degrades to empty sections when a probe fails. The decision
    must refuse rather than raise -- an exception here aborts the install."""

    @pytest.mark.parametrize("state", [
        {},
        {"memory": {}, "gpu": []},
        {"memory": {"total_bytes": 0}, "gpu": []},
        {"gpu": [{"name": "no vram key", "software": False}]},
        {"gpu": "not a list", "memory": {"total_bytes": 0}},
        {"gpu": [None, "text"], "memory": {}},
        # A byte count that arrived as a numeric string is coerced, so this
        # one reaches the ladder and refuses on the merits: 16 bytes.
        {"memory": {"total_bytes": "16"}, "gpu": []},
    ])
    def test_a_section_that_is_missing_or_empty_refuses(self, state):
        """These reach the ladder and fall off the bottom of it."""
        decision = get_local_ai_decision(state)
        assert decision["install"] is False
        assert decision["reason"]

    @pytest.mark.parametrize("field,state,expected_layers", [
        ("video memory",
         {"memory": {"total_bytes": 8 * 1024**3},
          "gpu": [{"dedicated_vram_bytes": str(8 * 1024**3), "software": False}]},
         99),
        ("system memory",
         {"memory": {"total_bytes": str(32 * 1024**3)}, "gpu": []},
         0),
    ])
    def test_a_byte_count_written_as_a_number_in_quotes_still_counts(
        self, field, state, expected_layers
    ):
        """Both byte counts are read the same way. Reading one of them as it
        arrives makes a machine's answer depend on which field a third-party
        or hand-edited snapshot happened to quote."""
        decision = get_local_ai_decision(state)
        assert decision["install"] is True, field
        assert decision["gpu_layers"] == expected_layers, field

    @pytest.mark.parametrize("state", [
        None,
        [],
        "not a dict",
        {"memory": [{"total_bytes": 1}], "gpu": []},
        {"memory": {"total_bytes": "lots"}, "gpu": []},
        {"gpu": [{"dedicated_vram_bytes": "plenty", "software": False}]},
    ])
    def test_input_that_is_not_the_expected_shape_refuses(self, state):
        """These do NOT reach the ladder -- calling .get on a list, or turning
        a word into a byte count, raises before any threshold is read. Split
        from the cases above because the earlier version of this test class
        covered only the harmless ones: narrowing the except clause to an
        exception that cannot occur left every test green."""
        decision = get_local_ai_decision(state)
        assert decision["install"] is False
        assert decision["model"] is None
        assert decision["gpu_layers"] is None
        assert "could not read" in decision["reason"]


class TestTheShapeOfTheAnswer:

    @pytest.mark.parametrize("state", [
        machine(vram_gb=8, ram_gb=32),
        machine(vram_gb=0, ram_gb=16),
        machine(vram_gb=0, ram_gb=8),
    ])
    def test_every_answer_carries_the_same_keys(self, state):
        """The installer reads these four by name on every path."""
        decision = get_local_ai_decision(state)
        assert set(decision) == {"install", "model", "gpu_layers", "reason"}

    def test_an_installable_answer_names_a_registered_component(self):
        """The model name is looked up in the installer's component registry;
        a name that is not there fails at download time, not here."""
        decision = get_local_ai_decision(machine(vram_gb=8, ram_gb=32))
        assert decision["model"] == "gemma4_e4b_qat"
