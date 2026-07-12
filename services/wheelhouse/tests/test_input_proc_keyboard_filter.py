"""Tests for keyboard invalidation filtering in input_proc."""

from input_proc import _should_emit_keyboard_invalidation


def test_internal_actions_never_invalidate():
    assert _should_emit_keyboard_invalidation("a", is_internal_action=True) is False
    assert _should_emit_keyboard_invalidation("left", is_internal_action=True) is False


def test_modifier_only_keys_do_not_invalidate():
    assert _should_emit_keyboard_invalidation("ctrl", is_internal_action=False) is False
    assert _should_emit_keyboard_invalidation("alt", is_internal_action=False) is False
    assert _should_emit_keyboard_invalidation("shift", is_internal_action=False) is False
    assert _should_emit_keyboard_invalidation("win", is_internal_action=False) is False


def test_non_modifier_keys_invalidate():
    assert _should_emit_keyboard_invalidation("a", is_internal_action=False) is True
    assert _should_emit_keyboard_invalidation("left", is_internal_action=False) is True
    assert _should_emit_keyboard_invalidation("backspace", is_internal_action=False) is True
