"""Tiny PDFs with known content, written by hand so tests need no library.

`make_pdf` takes one spec per page: a list of text lines, and whether the page
also carries a (2x2 pixel) image. That is enough to build a text PDF, a scan
(image, no text) and the mixed case that loses pages silently today.
"""
from __future__ import annotations


def _esc(s: str) -> bytes:
    raw = s.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(pages: list[tuple[list[str], bool]]) -> bytes:
    """pages: [(text lines, has_image), ...] → PDF bytes."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")            # filled in once the page tree exists
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
               b"/Encoding /WinAnsiEncoding >>")
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    image = add(b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length %d >>\n"
                b"stream\n" % len(pixels) + pixels + b"\nendstream")

    kids = []
    for lines, has_image in pages:
        content = b"".join(
            b"BT /F1 12 Tf 72 %d Td (" % (760 - 20 * i) + _esc(t) + b") Tj ET\n"
            for i, t in enumerate(lines))
        if has_image:
            content += b"q 200 0 0 200 72 300 cm /Im1 Do Q\n"
        stream = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream")
        kids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R >> /XObject << /Im1 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (pages_obj, font, image, stream)))

    objs[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objs[pages_obj - 1] = (b"<< /Type /Pages /Kids [" +
                           b" ".join(b"%d 0 R" % k for k in kids) +
                           b"] /Count %d >>" % len(kids))

    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, catalog, xref))
    return out
