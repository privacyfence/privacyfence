"""Rasterise the first pages of a PDF preview to PNG, so an approval card can show the document
where the browser has no inline PDF viewer: Android Chrome renders an ``<embed>`` of a PDF blank,
iOS Safari shows one static page, and a narrow desktop pane shows only the viewer's toolbar. See
ADR 0080 for why this happens on the server rather than with pdf.js or a separate route.

This is a parser of untrusted input. The bytes are whatever a connector fetched (drive.py's
``pdf_bytes``), and they are parsed before a human has decided anything, the same position
text_extraction.py is in. So, like that module, :func:`render_first_pages` never raises: a file
it cannot open, cannot render, or cannot render within its limits yields ``None``, and the caller
shows the document's extracted text instead (card_builder.py). Its limits:

- **pages**: only the first ``max_pages`` (default :data:`MAX_PAGES`) are rendered;
- **pixels**: each page is scaled to :data:`PAGE_WIDTH_PX` wide, and scaled down further if it
  would exceed :data:`MAX_PAGE_PIXELS`, so a page with an absurd aspect ratio (1pt by 14,400pt)
  cannot allocate a gigapixel bitmap;
- **bytes**: rendering stops before the PNGs would pass :data:`MAX_OUTPUT_BYTES` in total, since
  every one of them is inlined into the card document as a ``data:`` URI; an input larger than
  :data:`MAX_INPUT_BYTES` is not opened at all;
- **time**: the render runs on a worker thread that the caller waits on for at most
  :data:`TIMEOUT_SECONDS`. PDFium is C code and a page render cannot be interrupted from Python,
  so a render that overruns is abandoned, not stopped: the caller falls back to text at the
  deadline, and the worker checks a cancel flag before each further page so it stops at the next
  page boundary.

PDFium is not thread-safe, so every render holds :data:`_PDFIUM_LOCK`. A worker still busy with
an abandoned render therefore makes the next caller wait for the lock, and that wait counts
against the next caller's own deadline, which makes it fall back to text as well rather than
queue behind a stuck render.

The PNG encoder is the few lines of stdlib ``zlib`` below: Pillow is not a runtime dependency on
Linux (pyproject.toml), and PDFium already hands back raw RGB rows.
"""
from __future__ import annotations

import logging
import struct
import threading
import time
import zlib
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

logger = logging.getLogger(__name__)

# How many pages a card shows, and how wide each is rendered. 1000px is about twice a phone's CSS
# width, so text stays sharp on a high-density screen without inlining a print-resolution image.
MAX_PAGES = 5
PAGE_WIDTH_PX = 1000
MAX_PAGE_PIXELS = PAGE_WIDTH_PX * 2000
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 5.0

_PDFIUM_LOCK = threading.Lock()


@dataclass(frozen=True)
class RenderedPdf:
    """``pages`` are PNG files, one per rendered page, in order; ``page_count`` is the document's
    total, so the card can say how much of it the reviewer is seeing."""
    pages: tuple[bytes, ...]
    page_count: int


class _Cancelled(Exception):
    pass


def render_first_pages(
    data: bytes, *,
    max_pages: int = MAX_PAGES,
    width_px: int = PAGE_WIDTH_PX,
    timeout: float = TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> RenderedPdf | None:
    """The first ``max_pages`` pages of ``data`` as PNGs, or ``None`` if the PDF cannot be
    rendered within the limits in the module docstring (including an encrypted file, which
    PDFium will not open without its password). Never raises."""
    if not data or len(data) > MAX_INPUT_BYTES or max_pages < 1:
        return None
    deadline = time.monotonic() + timeout
    cancel = threading.Event()
    outcome: list[RenderedPdf | None] = []

    def work() -> None:
        try:
            if not _PDFIUM_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise _Cancelled
            try:
                outcome.append(_render(data, max_pages, width_px, max_output_bytes, cancel))
            finally:
                _PDFIUM_LOCK.release()
        except _Cancelled:
            pass
        except Exception:  # noqa: BLE001 -- any failure in a parser of untrusted input means "no pages"
            logger.warning("pdf_render: could not render a PDF preview", exc_info=True)

    worker = threading.Thread(target=work, name="pdf-render", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        cancel.set()
        logger.warning("pdf_render: gave up on a PDF preview after %.1fs", timeout)
        return None
    return outcome[0] if outcome else None


def _render(
    data: bytes, max_pages: int, width_px: int, max_output_bytes: int, cancel: threading.Event,
) -> RenderedPdf | None:
    pdf = pdfium.PdfDocument(data)
    try:
        page_count = len(pdf)
        pages: list[bytes] = []
        total = 0
        for index in range(min(page_count, max_pages)):
            if cancel.is_set():
                raise _Cancelled
            png = _render_page(pdf, index, width_px)
            if total + len(png) > max_output_bytes:
                break
            pages.append(png)
            total += len(png)
    finally:
        pdf.close()
    if not pages:
        return None
    return RenderedPdf(pages=tuple(pages), page_count=page_count)


def _render_page(pdf: pdfium.PdfDocument, index: int, width_px: int) -> bytes:
    page = pdf[index]
    try:
        width, height = page.get_size()
        if not (width > 0 and height > 0):
            raise ValueError(f"page {index + 1} has no area")
        scale = width_px / width
        if (width * scale) * (height * scale) > MAX_PAGE_PIXELS:
            scale = (MAX_PAGE_PIXELS / (width * height)) ** 0.5
        bitmap = page.render(
            scale=scale, force_bitmap_format=pdfium_c.FPDFBitmap_BGR, rev_byteorder=True,
        )
        try:
            return encode_png_rgb(bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride)
        finally:
            bitmap.close()
    finally:
        page.close()


def encode_png_rgb(buffer: bytes, width: int, height: int, stride: int) -> bytes:
    """A PNG of 8-bit RGB rows, each ``stride`` bytes apart in ``buffer`` (a row may carry
    padding past its ``3 * width`` pixel bytes, which is dropped).

    A page with no colour on it at all, which is most text, is written as a one-channel greyscale
    PNG: deflate compresses one grey channel far better than the same values interleaved three
    times (a dense page of text measured 25 KB against 390 KB), and every PNG ends up inlined in
    the card document."""
    row_bytes = 3 * width
    pixels = b"".join(buffer[y * stride:y * stride + row_bytes] for y in range(height))
    red = pixels[0::3]
    if red == pixels[1::3] == pixels[2::3]:
        return _png(red, width, height, channels=1)
    return _png(pixels, width, height, channels=3)


def _png(pixels: bytes, width: int, height: int, *, channels: int) -> bytes:
    row_bytes = channels * width
    # Filter type 0 (None) on every row: the other filters need per-byte arithmetic, which is
    # slow in pure Python and measured no smaller here.
    raw = b"".join(b"\x00" + pixels[y * row_bytes:(y + 1) * row_bytes] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    colour_type = 0 if channels == 1 else 2  # greyscale or truecolour, 8 bits, no interlace
    header = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    )
