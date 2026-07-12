"""Unit tests for the Qt-to-GDI per-pixel-alpha bitmap bridge
(``shared/overlay_bitmap.py``, slice wh-n29v.9, source leaf wh-gd5287).

These tests exercise ONLY the QImage -> top-down 32-bit DIB-section
preparation. The live ``UpdateLayeredWindow`` composite call needs a
real on-screen window and is deferred to the dependent GUI window slice
(wh-h7cvz1); it is not unit-tested here.

The three classic pitfalls the bridge must each prove (not assume):

1. **BGRA byte order.** A Windows 32-bit DIB stores bytes B, G, R, A in
   memory. ``QImage.Format_ARGB32_Premultiplied`` stores them the same
   way on a little-endian machine -- but the coincidence is asserted by
   reading the DIB bytes back, not taken on faith.
2. **Top-down row order.** A DIB section defaults to bottom-up (positive
   ``biHeight``), which vertically flips a QImage whose row 0 is at the
   top. The bridge negates ``biHeight``; the test sets a distinct pixel
   at QImage row 0 and asserts it lands at DIB row 0, not the bottom row.
3. **Premultiplied alpha.** GDI alpha compositing requires premultiplied
   alpha. The test paints a semi-transparent pixel and asserts every
   colour channel is ``<= alpha`` (the premultiplied invariant) and
   equals the expected premultiplied number.
"""

from __future__ import annotations

import pytest

from PySide6.QtGui import QImage

from shared import overlay_bitmap


# A 4x3 image is the smallest layout that still proves top-down vs
# bottom-up (3 distinct rows) and column ordering (4 distinct columns).
_W = 4
_H = 3


def _make_known_image() -> QImage:
    """Paint a known pixel layout into a premultiplied ARGB32 QImage.

    Pixels of interest:

    * (0, 0) top-left -- opaque pure red (a=255, r=255, g=0, b=0). The
      asymmetry between R, G, B makes the BGRA byte order unambiguous.
    * (1, 0) -- opaque pure green, second column of the top row. Proves
      column ordering within a row.
    * (0, 2) bottom-left -- opaque pure blue. Distinct from the top-left
      so a vertical flip would be caught (blue would appear at DIB row 0).
    * (2, 1) -- a semi-transparent colour with alpha 128 whose channels
      are non-trivial after premultiplication.

    Every other pixel stays fully transparent (all zero).
    """
    img = QImage(_W, _H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)  # fully transparent everywhere to start

    # ARGB packed 0xAARRGGBB. Opaque values are already premultiplied
    # (alpha 255 leaves channels unchanged), so setPixel stores them
    # verbatim in the premultiplied buffer.
    img.setPixel(0, 0, 0xFFFF0000)  # opaque red
    img.setPixel(1, 0, 0xFF00FF00)  # opaque green
    img.setPixel(0, 2, 0xFF0000FF)  # opaque blue

    # Semi-transparent pixel. setPixel on a premultiplied-format image
    # stores the ARGB value VERBATIM -- it does NOT premultiply for you.
    # 0x80404040 is alpha=128 with channels=64, which is already a valid
    # premultiplied value (64 <= 128). This pixel therefore proves a
    # semi-transparent premultiplied value round-trips through the bridge
    # in BGRA order; it does NOT prove the bridge performs
    # premultiplication. The convertToFormat premultiplication path is
    # covered separately by test_non_premultiplied_input_is_premultiplied.
    img.setPixel(2, 1, 0x80404040)  # a=128, channels already-premult 64
    return img


def test_build_layered_dib_reports_dimensions(qapp):
    """The bundle exposes the source image's width and height."""
    img = _make_known_image()
    bundle = overlay_bitmap.build_layered_dib(img)
    try:
        assert bundle.width == _W
        assert bundle.height == _H
    finally:
        bundle.release()


def test_dib_is_top_down(qapp):
    """biHeight is negative (top-down DIB), no vertical flip."""
    img = _make_known_image()
    bundle = overlay_bitmap.build_layered_dib(img)
    try:
        assert bundle.bi_height < 0
    finally:
        bundle.release()


def _dib_pixel(raw: bytes, x: int, y: int) -> tuple[int, int, int, int]:
    """Read a (B, G, R, A) tuple from the top-down DIB byte buffer.

    Top-down means row 0 is the first row in memory (no flip), and each
    32-bit pixel is stored B, G, R, A.
    """
    stride = _W * 4
    off = y * stride + x * 4
    return raw[off], raw[off + 1], raw[off + 2], raw[off + 3]


def test_dib_byte_order_is_bgra_not_rgba(qapp):
    """Top-left opaque-red pixel reads back as B=0, G=0, R=255, A=255.

    If the bridge wrote RGBA the first byte would be 255 (R). Asserting
    B first proves BGRA.
    """
    img = _make_known_image()
    bundle = overlay_bitmap.build_layered_dib(img)
    try:
        b, g, r, a = _dib_pixel(bundle.read_pixels(), 0, 0)
        assert (b, g, r, a) == (0, 0, 255, 255)
    finally:
        bundle.release()


def test_dib_column_order_within_row(qapp):
    """Second column of the top row is opaque green (B=0, G=255, R=0)."""
    img = _make_known_image()
    bundle = overlay_bitmap.build_layered_dib(img)
    try:
        b, g, r, a = _dib_pixel(bundle.read_pixels(), 1, 0)
        assert (b, g, r, a) == (0, 255, 0, 255)
    finally:
        bundle.release()


def test_dib_top_down_no_vertical_flip(qapp):
    """The blue pixel set at QImage row 2 stays at DIB row 2.

    On a bottom-up DIB the blue would appear at DIB row 0 and the red
    (QImage row 0) would appear at DIB row 2. Asserting blue at row 2
    AND red at row 0 fences both the flip and a false pass.
    """
    img = _make_known_image()
    bundle = overlay_bitmap.build_layered_dib(img)
    try:
        # Row 0 must still be red (top), row 2 must be blue (bottom).
        raw = bundle.read_pixels()
        top = _dib_pixel(raw, 0, 0)
        bottom = _dib_pixel(raw, 0, 2)
        assert top == (0, 0, 255, 255)        # red at top
        assert bottom == (255, 0, 0, 255)     # blue at bottom (B=255)
    finally:
        bundle.release()


def test_premultiplied_value_round_trips_in_bgra(qapp):
    """A semi-transparent premultiplied pixel survives the copy intact.

    The source is already Format_ARGB32_Premultiplied, so the bridge does
    NOT premultiply here -- this asserts byte survival in BGRA order for a
    value (a=128, channels=64) that already satisfies the premultiplied
    invariant (channel <= alpha). It does NOT prove the bridge premultiplies;
    test_non_premultiplied_input_is_premultiplied covers that.
    """
    img = _make_known_image()
    bundle = overlay_bitmap.build_layered_dib(img)
    try:
        b, g, r, a = _dib_pixel(bundle.read_pixels(), 2, 1)
        # The input value already satisfies channel <= alpha; confirm it
        # came back unchanged and in BGRA order.
        assert (b, g, r, a) == (64, 64, 64, 128)
    finally:
        bundle.release()


def test_non_premultiplied_input_is_premultiplied(qapp):
    """A non-premultiplied ARGB32 source is premultiplied by the bridge.

    This exercises the convertToFormat path that the premultiplied-input
    tests never touch. Feed Format_ARGB32 (straight alpha) with a 50%-alpha
    white pixel (a=128, straight r=g=b=255). Premultiplication scales each
    channel by alpha/255, so 255 must drop to 128 -- far below the 255 the
    straight-alpha buffer held. Asserting the output channel is 128, not
    255, proves the bridge actually premultiplied rather than copying the
    straight-alpha bytes through.
    """
    img = QImage(_W, _H, QImage.Format.Format_ARGB32)  # straight alpha
    img.fill(0)
    img.setPixel(0, 0, 0x80FFFFFF)  # a=128, straight r=g=b=255
    bundle = overlay_bitmap.build_layered_dib(img)
    try:
        b, g, r, a = _dib_pixel(bundle.read_pixels(), 0, 0)
        assert a == 128
        # Premultiplied: 255 * 128 / 255 -> 128, not the straight 255.
        assert (b, g, r) == (128, 128, 128)
        assert b <= a and g <= a and r <= a
    finally:
        bundle.release()


def test_release_is_idempotent(qapp):
    """Calling release() twice does not raise (no double-free)."""
    img = _make_known_image()
    bundle = overlay_bitmap.build_layered_dib(img)
    bundle.release()
    bundle.release()  # second call must be a safe no-op


def test_read_pixels_after_release_raises(qapp):
    """read_pixels() must raise after release(): the GDI buffer is freed,
    so reading it would be a use-after-free."""
    img = _make_known_image()
    bundle = overlay_bitmap.build_layered_dib(img)
    # Readable while alive.
    assert len(bundle.read_pixels()) == _W * _H * 4
    bundle.release()
    with pytest.raises(RuntimeError):
        bundle.read_pixels()


def test_context_manager_releases(qapp):
    """The bundle works as a context manager and releases on exit."""
    img = _make_known_image()
    with overlay_bitmap.build_layered_dib(img) as bundle:
        assert bundle.width == _W
    # After the with-block the resources are released; a further explicit
    # release must remain safe.
    bundle.release()


def test_select_object_failure_raises_and_frees_handles(qapp, monkeypatch):
    """A SelectObject failure (NULL return) frees the HBITMAP and HDC and
    raises, rather than returning a bundle whose DC has no DIB selected.

    SelectObject returns NULL only on failure when selecting a bitmap into a
    fresh memory DC (success returns the DC's default 1x1 bitmap). The bridge
    must treat 0 as failure and clean up. This forces the failure by patching
    SelectObject to return 0, and spies on DeleteObject / DeleteDC (delegating
    to the real calls so the genuinely-created handles are still freed) to
    confirm both ran before the raise.
    """
    img = _make_known_image()
    deleted = {"object": 0, "dc": 0}
    real_delete_object = overlay_bitmap._gdi32.DeleteObject
    real_delete_dc = overlay_bitmap._gdi32.DeleteDC

    def spy_delete_object(h):
        deleted["object"] += 1
        return real_delete_object(h)

    def spy_delete_dc(h):
        deleted["dc"] += 1
        return real_delete_dc(h)

    monkeypatch.setattr(overlay_bitmap._gdi32, "SelectObject", lambda *a: 0)
    monkeypatch.setattr(overlay_bitmap._gdi32, "DeleteObject", spy_delete_object)
    monkeypatch.setattr(overlay_bitmap._gdi32, "DeleteDC", spy_delete_dc)

    with pytest.raises(OSError):
        overlay_bitmap.build_layered_dib(img)

    # The HBITMAP and HDC created before the failed select must both be freed.
    assert deleted["object"] >= 1
    assert deleted["dc"] >= 1
