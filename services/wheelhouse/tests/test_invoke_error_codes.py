"""Unit tests for the Invoke no-side-effect HRESULT allowlist (wh-hvvqq).

Covers membership, non-membership, the documented constant values, the
exact contents of the allowlist collection, and its immutability.
"""

from __future__ import annotations

import pytest

from ui.invoke_error_codes import (
    NO_SIDE_EFFECT_HRESULTS,
    UIA_E_ELEMENTNOTAVAILABLE,
    UIA_E_NOTSUPPORTED,
    is_no_side_effect_hresult,
)


def test_constants_hold_documented_values() -> None:
    # Microsoft UI Automation error codes (UIAutomationCoreApi.h):
    # https://learn.microsoft.com/windows/win32/winauto/uiauto-error-codes
    assert UIA_E_ELEMENTNOTAVAILABLE == 0x80040201
    assert UIA_E_NOTSUPPORTED == 0x80040204


def test_allowlist_is_exactly_the_two_codes() -> None:
    assert NO_SIDE_EFFECT_HRESULTS == frozenset(
        {UIA_E_ELEMENTNOTAVAILABLE, UIA_E_NOTSUPPORTED}
    )
    assert len(NO_SIDE_EFFECT_HRESULTS) == 2


def test_allowlist_is_immutable_frozenset() -> None:
    assert isinstance(NO_SIDE_EFFECT_HRESULTS, frozenset)
    with pytest.raises(AttributeError):
        NO_SIDE_EFFECT_HRESULTS.add(0x80004005)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "hresult",
    [UIA_E_ELEMENTNOTAVAILABLE, UIA_E_NOTSUPPORTED, 0x80040201, 0x80040204],
)
def test_allowlisted_codes_are_recognised(hresult: int) -> None:
    assert is_no_side_effect_hresult(hresult) is True


@pytest.mark.parametrize(
    "hresult",
    [
        0x80004005,  # E_FAIL -- generic COM failure, side effects unknown.
        0x80040200,  # UIA_E_ELEMENTNOTENABLED -- a UIA code NOT on the list.
        0x80040202,  # UIA_E_NOCLICKABLEPOINT -- a UIA code NOT on the list.
        0x00000000,  # S_OK -- not a failure at all.
        0x80131509,  # UIA_E_INVALIDOPERATION -- a UIA code NOT on the list.
    ],
)
def test_non_allowlisted_codes_are_rejected(hresult: int) -> None:
    assert is_no_side_effect_hresult(hresult) is False


def _to_signed_32(value: int) -> int:
    """Reproduce the signed 32-bit form a comtypes COMError would expose."""

    return value - 0x100000000 if value >= 0x80000000 else value


@pytest.mark.parametrize(
    "code",
    [UIA_E_ELEMENTNOTAVAILABLE, UIA_E_NOTSUPPORTED],
)
def test_signed_twin_of_each_allowlisted_code_is_recognised(code: int) -> None:
    signed = _to_signed_32(code)
    # Sanity-check the twin really is the negative signed form.
    assert signed < 0
    assert is_no_side_effect_hresult(signed) is True


def test_documented_signed_form_of_elementnotavailable() -> None:
    # comtypes surfaces UIA_E_ELEMENTNOTAVAILABLE (0x80040201) as the signed
    # 32-bit int -2147220991 (== 0x80040201 - 0x100000000).
    assert is_no_side_effect_hresult(-2147220991) is True


def test_signed_non_allowlisted_code_is_rejected() -> None:
    # E_FAIL (0x80004005) signed twin -- still not on the allowlist.
    assert is_no_side_effect_hresult(_to_signed_32(0x80004005)) is False


@pytest.mark.parametrize(
    "bad_input",
    [None, "0x80040201", 1.0, b"\x01\x02", object()],
)
def test_non_int_input_returns_false_without_raising(bad_input: object) -> None:
    assert is_no_side_effect_hresult(bad_input) is False  # type: ignore[arg-type]


def test_int_above_32bit_range_with_matching_low_bits_is_rejected() -> None:
    # 0x180040201 -- outside the 0..0xFFFFFFFF unsigned range; its low 32
    # bits equal UIA_E_ELEMENTNOTAVAILABLE but it must NOT be accepted.
    assert (0x180040201 & 0xFFFFFFFF) == UIA_E_ELEMENTNOTAVAILABLE
    assert is_no_side_effect_hresult(0x180040201) is False


def test_int_below_32bit_range_with_matching_low_bits_is_rejected() -> None:
    # An int more negative than -0x80000000 whose low 32 bits equal an
    # allowlisted code must NOT be accepted.
    too_negative = UIA_E_ELEMENTNOTAVAILABLE - 0x200000000
    assert too_negative < -0x80000000
    assert (too_negative & 0xFFFFFFFF) == UIA_E_ELEMENTNOTAVAILABLE
    assert is_no_side_effect_hresult(too_negative) is False


def test_bool_input_is_rejected() -> None:
    # bool is an int subclass, so the isinstance(hresult, bool) guard is
    # load-bearing: without it True/False would mask to 1/0 and the check
    # would proceed instead of failing closed.
    assert is_no_side_effect_hresult(True) is False
    assert is_no_side_effect_hresult(False) is False


def test_range_guard_boundaries() -> None:
    # In-range boundary values reach the membership test but are not
    # allowlisted, so they return False.
    assert is_no_side_effect_hresult(-0x80000000) is False
    assert is_no_side_effect_hresult(0xFFFFFFFF) is False
    # Just-outside values are rejected by the range guard before masking.
    assert is_no_side_effect_hresult(-0x80000001) is False
    assert is_no_side_effect_hresult(0x100000000) is False
    # An in-range allowlisted value (the signed twin of an allowlisted code)
    # still passes, proving the range check does not block valid inputs.
    assert is_no_side_effect_hresult(_to_signed_32(UIA_E_ELEMENTNOTAVAILABLE)) is True
