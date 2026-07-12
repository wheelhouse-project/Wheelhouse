"""Per-pixel-alpha Qt-to-GDI bitmap bridge for the numbered overlay.

This module converts a painted ``QImage`` (format
``Format_ARGB32_Premultiplied``) into a top-down 32-bit DIB section
suitable for ``UpdateLayeredWindow`` per-pixel-alpha compositing, plus a
thin wrapper for the ``UpdateLayeredWindow`` call itself.

Authoritative reference: section "Painting technique (per-pixel alpha;
r2.7)" of
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``
(around line 127). Slice bead wh-n29v.9; source leaf wh-gd5287.

Why this exists instead of ``SetLayeredWindowAttributes``
---------------------------------------------------------

``handlers/software_dimmer.py`` uses ``SetLayeredWindowAttributes`` --
one uniform alpha over the whole window, no source bitmap -- which
cannot render the anti-aliased (alpha-blended) edges of the
outlined-numeral badges. The overlay therefore takes the
``UpdateLayeredWindow`` / ``ULW_ALPHA`` path, which composites a 32-bit
per-pixel-alpha source bitmap over the desktop.

The three classic pitfalls, each handled below and proven by the unit
test (``tests/test_overlay_bitmap.py``):

1. **Byte order.** A Windows 32-bit DIB stores bytes B, G, R, A in
   memory. ``QImage::Format_ARGB32_Premultiplied`` stores them the same
   way on a little-endian machine, so the copy is a straight ``memcpy``
   here -- but the bridge is structured so the copy is the only place
   that assumption lives, and the test reads the DIB bytes back to prove
   the resulting order is BGRA, not RGBA.
2. **Row order.** ``CreateDIBSection`` defaults to a bottom-up DIB
   (positive ``biHeight``), which vertically flips a QImage whose row 0
   is at the top. We set ``biHeight`` NEGATIVE for a top-down DIB so the
   QImage's row 0 maps to DIB row 0.
3. **Premultiplied alpha.** GDI alpha compositing requires premultiplied
   alpha (``AC_SRC_ALPHA``); non-premultiplied produces dark fringes.
   The caller must paint into a ``Format_ARGB32_Premultiplied`` image;
   this module asserts that format and copies the bits verbatim.

64-bit type safety (project rule): every gdi32 / user32 function used
here declares ``argtypes`` and ``restype`` with pointer-sized handle
types. ``CreateDIBSection``, ``CreateCompatibleDC``, ``SelectObject``,
``DeleteObject`` and ``DeleteDC`` all take or return ``HDC`` / ``HBITMAP``
/ ``HGDIOBJ`` -- pointer-sized on 64-bit Windows. A missing or 32-bit
prototype truncates the upper 32 bits of a handle and causes access
violations. This mirrors the prototype-declaration discipline in
``handlers/software_dimmer.py`` and ``shared/monitor_geometry.py``.

The live ``UpdateLayeredWindow`` call (``composite_layered_window``)
needs a real on-screen window and is NOT unit-tested in this slice; it
is exercised by the dependent GUI window slice wh-h7cvz1.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass, field

from PySide6.QtGui import QImage


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Win32 constants.
# ---------------------------------------------------------------------------

# ``DIB_RGB_COLORS`` -- the BITMAPINFO's bmiColors holds literal RGB
# values, not palette indices. For a 32-bit BI_RGB DIB the colour table
# is unused, but the iUsage argument is still required.
_DIB_RGB_COLORS = 0

# ``BI_RGB`` -- uncompressed RGB. For 32-bit this means each pixel is a
# little-endian DWORD laid out in memory as B, G, R, A.
_BI_RGB = 0

# ``UpdateLayeredWindow`` flags / blend constants.
_ULW_ALPHA = 0x00000002
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01


# ---------------------------------------------------------------------------
# ctypes structures.
# ---------------------------------------------------------------------------


class _BITMAPINFOHEADER(ctypes.Structure):
    """``BITMAPINFOHEADER`` from ``wingdi.h``.

    ``biHeight`` is set NEGATIVE by ``build_layered_dib`` to request a
    top-down DIB (row 0 first in memory). ``biBitCount`` is 32 and
    ``biCompression`` is ``BI_RGB`` (0), giving a B, G, R, A byte layout
    per pixel.
    """

    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    """``BITMAPINFO`` from ``wingdi.h``.

    The single ``bmiColors`` RGBQUAD is unused for a 32-bit BI_RGB DIB
    (the colour table is empty) but the struct must carry at least one
    entry for a well-formed layout.
    """

    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 1),
    ]


class _BLENDFUNCTION(ctypes.Structure):
    """``BLENDFUNCTION`` from ``wingdi.h`` (per-pixel alpha compositing).

    For the overlay: ``BlendOp = AC_SRC_OVER``, ``BlendFlags = 0``,
    ``SourceConstantAlpha = 255`` (let the per-pixel alpha drive the
    blend, no extra whole-surface attenuation), ``AlphaFormat =
    AC_SRC_ALPHA`` (the source has a per-pixel alpha channel).
    """

    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


# ---------------------------------------------------------------------------
# gdi32 / user32 prototype declaration.
# ---------------------------------------------------------------------------

# Bind the DLLs at import time. On a non-Windows host these attribute
# lookups raise; this module is Windows-only by design (it lives in the
# GUI painting layer of a Windows voice-control app) and the import is
# allowed to fail loudly off-Windows.
_gdi32 = ctypes.windll.gdi32
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# HBITMAP CreateDIBSection(HDC, const BITMAPINFO*, UINT iUsage,
#                          void** ppvBits, HANDLE hSection, DWORD offset)
_gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(_BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP

# HDC CreateCompatibleDC(HDC)
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC

# HGDIOBJ SelectObject(HDC, HGDIOBJ)
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ

# BOOL DeleteObject(HGDIOBJ)
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL

# BOOL DeleteDC(HDC)
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL

# BOOL UpdateLayeredWindow(HWND, HDC dst, POINT* posDst, SIZE* size,
#                          HDC src, POINT* posSrc, COLORREF key,
#                          BLENDFUNCTION* blend, DWORD flags)
_user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND,
    wintypes.HDC,
    ctypes.POINTER(_POINT),
    ctypes.POINTER(_SIZE),
    wintypes.HDC,
    ctypes.POINTER(_POINT),
    wintypes.COLORREF,
    ctypes.POINTER(_BLENDFUNCTION),
    wintypes.DWORD,
]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL

# DWORD GetLastError(void)
_kernel32.GetLastError.argtypes = []
_kernel32.GetLastError.restype = wintypes.DWORD


# ---------------------------------------------------------------------------
# Bundle returned by build_layered_dib.
# ---------------------------------------------------------------------------


@dataclass
class LayeredDib:
    """A prepared top-down 32-bit DIB section plus its backing GDI objects.

    Exposes the ``width`` / ``height`` in pixels, the ``HBITMAP`` and
    memory ``HDC`` for the ``UpdateLayeredWindow`` source, and
    ``bi_height`` (the negative BITMAPINFOHEADER height, exposed so a test
    can assert top-down). The raw pixel bytes are NOT eagerly copied out
    -- the production compositor blits directly from ``hdc`` and never
    needs a Python-side copy; a test or caller that wants to inspect the
    DIB bytes calls :meth:`read_pixels` while the bundle is still alive.

    ``release()`` restores the DC's original bitmap, deletes the
    HBITMAP, and deletes the memory HDC exactly once; it is safe to call
    more than once (idempotent) and is also invoked on context-manager
    exit.
    """

    width: int
    height: int
    hbitmap: int
    hdc: int
    bi_height: int
    # Internal: the void* to the DIB bits and the bitmap the DC held
    # before SelectObject, needed for read_pixels and for cleanup.
    _bits_ptr: int = field(default=0, repr=False)
    _old_bitmap: int = field(default=0, repr=False)
    _released: bool = field(default=False, repr=False)

    def read_pixels(self) -> bytes:
        """Copy the DIB pixel buffer out as raw bytes.

        Bytes are B, G, R, A per pixel, top-down, rows tightly packed at
        ``width * 4``. Valid only while the bundle is alive; raises if
        called after :meth:`release` (the underlying GDI buffer is freed
        on release, so reading it then would be a use-after-free). This
        is for verification/inspection only -- it is NOT on the paint hot
        path, where the compositor blits from ``hdc`` directly.
        """
        if self._released or not self._bits_ptr:
            raise RuntimeError("read_pixels() called after release()")
        row_bytes = self.width * 4
        return bytes(
            (ctypes.c_char * (row_bytes * self.height)).from_address(
                self._bits_ptr
            )
        )

    def release(self) -> None:
        """Tear down the DC and bitmap. Idempotent (safe to call twice)."""
        if self._released:
            return
        self._released = True
        # Restore the DC's original bitmap before deleting ours, so the
        # HBITMAP is not still selected into the DC at delete time. Check
        # the restore for NULL: if it fails, our HBITMAP stays selected and
        # the DeleteObject below cannot free it (GDI refuses to delete a
        # still-selected object), so the warning here names the real cause
        # of the DeleteObject failure that would otherwise look unexplained.
        if self.hdc and self._old_bitmap:
            restored = _gdi32.SelectObject(wintypes.HDC(self.hdc),
                                           wintypes.HGDIOBJ(self._old_bitmap))
            if not restored:
                logger.warning(
                    "overlay_bitmap: SelectObject(restore) failed, err=%s",
                    _kernel32.GetLastError(),
                )
        if self.hbitmap:
            if not _gdi32.DeleteObject(wintypes.HGDIOBJ(self.hbitmap)):
                logger.warning(
                    "overlay_bitmap: DeleteObject(HBITMAP) failed, err=%s",
                    _kernel32.GetLastError(),
                )
            self.hbitmap = 0
        if self.hdc:
            if not _gdi32.DeleteDC(wintypes.HDC(self.hdc)):
                logger.warning(
                    "overlay_bitmap: DeleteDC failed, err=%s",
                    _kernel32.GetLastError(),
                )
            self.hdc = 0
        # Zero the freed DIB pointer too (DeleteObject above released its
        # backing memory), matching the delete-then-zero pattern used for
        # hbitmap and hdc. read_pixels() already fails closed on _released,
        # but this makes the "not self._bits_ptr" guard a second line of
        # defense against a future direct access to a dangling pointer.
        self._bits_ptr = 0

    def __enter__(self) -> "LayeredDib":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


# ---------------------------------------------------------------------------
# QImage -> top-down 32-bit DIB section.
# ---------------------------------------------------------------------------


def build_layered_dib(image: QImage) -> LayeredDib:
    """Convert a premultiplied ARGB32 ``QImage`` into a top-down 32-bit
    DIB section and return a :class:`LayeredDib` bundle.

    The image is converted to ``Format_ARGB32_Premultiplied`` if it is
    not already in that format (GDI requires premultiplied alpha). A
    memory DC is created via ``CreateCompatibleDC(0)``, a top-down 32-bit
    DIB section is created (``biHeight`` negative) and selected into the
    DC, and the QImage bits are copied row by row into the DIB bits in
    B, G, R, A order. On a little-endian machine the QImage's
    premultiplied ARGB32 buffer is already laid out B, G, R, A, so the
    per-row copy is a straight ``memmove`` of ``width * 4`` bytes (the
    test proves the resulting byte order).

    The returned bundle owns the HBITMAP and HDC; the caller MUST call
    ``release()`` (or use it as a context manager) to free them.
    """
    src = image
    if src.format() != QImage.Format.Format_ARGB32_Premultiplied:
        src = src.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    width = src.width()
    height = src.height()

    # Build a top-down BITMAPINFO: biHeight NEGATIVE so DIB row 0 is the
    # QImage's row 0 (no vertical flip).
    info = _BITMAPINFO()
    hdr = info.bmiHeader
    hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    hdr.biWidth = width
    hdr.biHeight = -height          # negative => top-down
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = _BI_RGB
    hdr.biSizeImage = 0
    hdr.biXPelsPerMeter = 0
    hdr.biYPelsPerMeter = 0
    hdr.biClrUsed = 0
    hdr.biClrImportant = 0

    hdc = _gdi32.CreateCompatibleDC(wintypes.HDC(0))
    if not hdc:
        raise OSError(
            f"CreateCompatibleDC failed, err={_kernel32.GetLastError()}"
        )

    bits_ptr = ctypes.c_void_p(0)
    hbitmap = _gdi32.CreateDIBSection(
        wintypes.HDC(hdc),
        ctypes.byref(info),
        _DIB_RGB_COLORS,
        ctypes.byref(bits_ptr),
        wintypes.HANDLE(0),
        wintypes.DWORD(0),
    )
    if not hbitmap or not bits_ptr.value:
        err = _kernel32.GetLastError()
        _gdi32.DeleteDC(wintypes.HDC(hdc))
        raise OSError(f"CreateDIBSection failed, err={err}")

    old_bitmap = _gdi32.SelectObject(
        wintypes.HDC(hdc), wintypes.HGDIOBJ(hbitmap)
    )
    if not old_bitmap:
        # SelectObject returns NULL only on failure here. Selecting a bitmap
        # into a fresh memory DC succeeds by returning the DC's default 1x1
        # bitmap (non-NULL), so a 0 return is a real failure, NOT "there was
        # no previous bitmap". Without this check build_layered_dib would
        # return a bundle whose DC has no DIB selected -- composite_layered_window
        # would then composite the wrong (or no) bitmap, and release() would
        # find no old bitmap to restore. Fail closed: free the HBITMAP and
        # HDC and raise, exactly as the CreateDIBSection failure path does.
        err = _kernel32.GetLastError()
        _gdi32.DeleteObject(wintypes.HGDIOBJ(hbitmap))
        _gdi32.DeleteDC(wintypes.HDC(hdc))
        raise OSError(f"SelectObject(DIB) failed, err={err}")

    # Construct the bundle BEFORE the copy. The copy can raise (most
    # realistically MemoryError reading a large source buffer on a
    # full-screen overlay), and once SelectObject has run the HDC and
    # HBITMAP must be freed by release() on any failure. Building the
    # owner first, then copying under try/finally, guarantees no leak in
    # that window; the earlier CreateDIBSection failure path already
    # cleans up its HDC before raising.
    bundle = LayeredDib(
        width=width,
        height=height,
        hbitmap=int(hbitmap),
        hdc=int(hdc),
        bi_height=hdr.biHeight,
        _bits_ptr=int(bits_ptr.value),
        _old_bitmap=int(old_bitmap),
    )

    copied = False
    try:
        # Copy each QImage row straight into the DIB via memoryview slice
        # assignment (a C-level memcpy), honouring bytesPerLine. This avoids
        # materialising the whole QImage backing store as an intermediate
        # Python bytes object, which was a second full-frame heap allocation
        # on every repaint. The DIB rows are tightly packed at width*4 bytes;
        # for the Format_ARGB32_Premultiplied buffers this bridge always
        # produces, bytesPerLine == width*4 (32-bit rows are already 4-byte
        # aligned, so there is no padding), but the per-row arithmetic keeps
        # the copy correct for any source stride. ``src`` (a local) must stay
        # alive while ``src_mv`` views its buffer; it outlives this block.
        row_bytes = width * 4
        src_stride = src.bytesPerLine()
        dib_base = int(bits_ptr.value)

        src_mv = memoryview(src.constBits()).cast("B")
        dest_arr = (ctypes.c_char * (row_bytes * height)).from_address(dib_base)
        dest_mv = memoryview(dest_arr).cast("B")
        for y in range(height):
            src_off = y * src_stride
            dst_off = y * row_bytes
            dest_mv[dst_off:dst_off + row_bytes] = (
                src_mv[src_off:src_off + row_bytes]
            )
        copied = True
    finally:
        if not copied:
            bundle.release()

    return bundle


# ---------------------------------------------------------------------------
# Thin UpdateLayeredWindow composite wrapper (NOT unit-tested here).
# ---------------------------------------------------------------------------


def composite_layered_window(
    hwnd: int,
    screen_dc: int,
    dib: LayeredDib,
    dest_x: int,
    dest_y: int,
) -> bool:
    """Composite ``dib`` onto the layered window ``hwnd`` via
    ``UpdateLayeredWindow`` with per-pixel alpha (``ULW_ALPHA``).

    ``screen_dc`` is a screen DC (e.g. ``GetDC(0)``) the caller owns and
    releases. ``dest_x`` / ``dest_y`` are the overlay window's screen
    origin. The source origin is (0, 0) and the size is the DIB's pixel
    size. The blend uses ``AC_SRC_OVER`` with ``SourceConstantAlpha =
    255`` and ``AlphaFormat = AC_SRC_ALPHA`` so the per-pixel alpha in
    the source bitmap drives the composite.

    Returns ``True`` on success; logs ``GetLastError`` and returns
    ``False`` on failure. This wrapper is exercised by the GUI window
    slice (wh-h7cvz1) against a real HWND, not by this slice's unit test.
    """
    pos_dst = _POINT(dest_x, dest_y)
    pos_src = _POINT(0, 0)
    size = _SIZE(dib.width, dib.height)
    blend = _BLENDFUNCTION(
        BlendOp=_AC_SRC_OVER,
        BlendFlags=0,
        SourceConstantAlpha=255,
        AlphaFormat=_AC_SRC_ALPHA,
    )
    ok = _user32.UpdateLayeredWindow(
        wintypes.HWND(hwnd),
        wintypes.HDC(screen_dc),
        ctypes.byref(pos_dst),
        ctypes.byref(size),
        wintypes.HDC(dib.hdc),
        ctypes.byref(pos_src),
        wintypes.COLORREF(0),
        ctypes.byref(blend),
        wintypes.DWORD(_ULW_ALPHA),
    )
    if not ok:
        logger.warning(
            "overlay_bitmap: UpdateLayeredWindow failed, err=%s",
            _kernel32.GetLastError(),
        )
        return False
    return True
