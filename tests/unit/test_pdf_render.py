"""Tests for pdf_render.py: the first pages of a PDF preview as PNGs, within page, pixel, byte and
time limits, and None (never an exception) for anything it cannot render."""
from __future__ import annotations

import io
import math
import struct
import threading
import zlib

import pytest
from pypdf import PdfWriter

from privacyfence import pdf_render


def text_pdf(pages: list[list[str]], *, width: int = 595, height: int = 842) -> bytes:
    """A minimal valid PDF with one line of Helvetica per string, one page per list. Hand-built
    because pypdf cannot write text onto a page, and a blank page cannot tell a page that
    rendered from one that did not."""
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_ids = []
    next_id = 4
    for lines in pages:
        text = " ".join("(%s) '" % line for line in lines)
        content = f"BT /F1 11 Tf 56 {height - 72} Td 14 TL {text} ET".encode("latin-1")
        objects[next_id] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)
        objects[next_id + 1] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (width, height, next_id)
        )
        page_ids.append(next_id + 1)
        next_id += 2
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    xref = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    out += b"".join(b"%010d 00000 n \n" % offsets[n] for n in range(1, size))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password="secret", owner_password="owner")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _png_header(png: bytes) -> tuple[int, int, int]:
    """(width, height, colour type) from a PNG's IHDR chunk."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, colour_type = struct.unpack(">IIBB", png[16:26])
    return width, height, colour_type


def _pages(n: int) -> list[list[str]]:
    return [[f"Page {p + 1} of the quarterly report"] for p in range(n)]


class TestRenderFirstPages:
    def test_renders_each_page_as_a_png_of_the_configured_width(self):
        rendered = pdf_render.render_first_pages(text_pdf(_pages(2)))
        assert rendered is not None
        assert rendered.page_count == 2
        assert len(rendered.pages) == 2
        for png in rendered.pages:
            width, height, _ = _png_header(png)
            assert width == pdf_render.PAGE_WIDTH_PX
            assert height == math.ceil(842 * pdf_render.PAGE_WIDTH_PX / 595)  # PDFium rounds up

    def test_stops_at_the_page_limit_and_reports_the_total(self):
        rendered = pdf_render.render_first_pages(text_pdf(_pages(8)))
        assert rendered is not None
        assert len(rendered.pages) == pdf_render.MAX_PAGES == 5
        assert rendered.page_count == 8

    def test_the_page_limit_is_configurable(self):
        rendered = pdf_render.render_first_pages(text_pdf(_pages(8)), max_pages=2)
        assert rendered is not None and len(rendered.pages) == 2 and rendered.page_count == 8

    def test_a_page_with_an_absurd_aspect_ratio_is_scaled_to_the_pixel_limit(self):
        rendered = pdf_render.render_first_pages(text_pdf([["x"]], width=10, height=14400))
        assert rendered is not None
        width, height, _ = _png_header(rendered.pages[0])
        # PDFium rounds each side up, so the product may pass the limit by a pixel per row.
        assert width * height <= pdf_render.MAX_PAGE_PIXELS + height
        assert height > width

    def test_a_page_pdfium_reports_with_no_area_is_no_render(self, monkeypatch):
        # PDFium substitutes a default size for a zero MediaBox itself; this is the guard behind it.
        monkeypatch.setattr(pdf_render.pdfium.PdfPage, "get_size", lambda self: (0.0, 842.0))
        assert pdf_render.render_first_pages(text_pdf(_pages(1))) is None

    def test_stops_before_the_byte_limit(self):
        one = pdf_render.render_first_pages(text_pdf(_pages(1)))
        assert one is not None
        size = len(one.pages[0])
        rendered = pdf_render.render_first_pages(text_pdf(_pages(4)), max_output_bytes=size * 2 + size // 2)
        assert rendered is not None
        assert len(rendered.pages) == 2 and rendered.page_count == 4

    def test_a_first_page_over_the_byte_limit_is_no_render(self):
        assert pdf_render.render_first_pages(text_pdf(_pages(2)), max_output_bytes=100) is None

    @pytest.mark.parametrize(
        "data",
        [b"", b"not a pdf at all", b"%PDF-1.4\n1 0 obj << /Type /Catalog", b"\x00" * 64],
        ids=["empty", "not-pdf", "truncated", "zeros"],
    )
    def test_malformed_input_is_none_not_an_exception(self, data):
        assert pdf_render.render_first_pages(data) is None

    def test_an_encrypted_pdf_is_none(self):
        assert pdf_render.render_first_pages(encrypted_pdf()) is None

    def test_an_oversized_input_is_not_opened(self, monkeypatch):
        def fail(*_a, **_kw):
            raise AssertionError("opened an input over the byte limit")

        monkeypatch.setattr(pdf_render.pdfium, "PdfDocument", fail)
        data = text_pdf(_pages(1))
        monkeypatch.setattr(pdf_render, "MAX_INPUT_BYTES", len(data) - 1)
        assert pdf_render.render_first_pages(data) is None

    def test_a_render_over_the_time_limit_is_abandoned(self, monkeypatch):
        release = threading.Event()
        real_render_page = pdf_render._render_page
        calls = []

        def slow_render_page(pdf, index, width_px):
            calls.append(index)
            release.wait(5)
            return real_render_page(pdf, index, width_px)

        monkeypatch.setattr(pdf_render, "_render_page", slow_render_page)
        try:
            assert pdf_render.render_first_pages(text_pdf(_pages(3)), timeout=0.2) is None
        finally:
            release.set()
        # The abandoned worker stops at the next page boundary instead of rendering the rest.
        with pdf_render._PDFIUM_LOCK:
            assert calls == [0]

    def test_a_caller_does_not_queue_behind_a_render_that_holds_the_lock(self):
        with pdf_render._PDFIUM_LOCK:
            assert pdf_render.render_first_pages(text_pdf(_pages(1)), timeout=0.2) is None
        assert pdf_render.render_first_pages(text_pdf(_pages(1))) is not None


class TestEncodePng:
    def _decode(self, png: bytes) -> bytes:
        idat = png.index(b"IDAT")
        (length,) = struct.unpack(">I", png[idat - 4:idat])
        return zlib.decompress(png[idat + 4:idat + 4 + length])

    def test_a_grey_bitmap_becomes_a_one_channel_png(self):
        # 2x2, grey, rows padded to a 8-byte stride.
        buffer = bytes([10, 10, 10, 20, 20, 20, 0, 0, 30, 30, 30, 40, 40, 40, 0, 0])
        png = pdf_render.encode_png_rgb(buffer, 2, 2, 8)
        assert _png_header(png) == (2, 2, 0)
        assert self._decode(png) == bytes([0, 10, 20, 0, 30, 40])

    def test_a_colour_bitmap_stays_rgb(self):
        buffer = bytes([255, 0, 0, 0, 255, 0])
        png = pdf_render.encode_png_rgb(buffer, 2, 1, 6)
        assert _png_header(png) == (2, 1, 2)
        assert self._decode(png) == bytes([0, 255, 0, 0, 0, 255, 0])

    def test_chunks_carry_valid_crcs(self):
        png = pdf_render.encode_png_rgb(bytes(3 * 4), 2, 2, 6)
        offset = 8
        kinds = []
        while offset < len(png):
            (length,) = struct.unpack(">I", png[offset:offset + 4])
            kind = png[offset + 4:offset + 8]
            body = png[offset + 8:offset + 8 + length]
            (crc,) = struct.unpack(">I", png[offset + 8 + length:offset + 12 + length])
            assert crc == zlib.crc32(kind + body)
            kinds.append(kind)
            offset += 12 + length
        assert kinds == [b"IHDR", b"IDAT", b"IEND"]
