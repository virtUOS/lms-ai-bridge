"""Default text extraction for course files.

**This is a fallback, not a destination.** Two funded German teams are building
course-grounded RAG for Moodle with their own extraction — ByCS/ISB's
`local_ai_content` and OSKI.nrw's LMS-RAG (both public projects). Where one of those is deployed, its extraction should be used and this
module should not run: point the bridge's retrieval provider at the real engine
and this code becomes dead weight by design.

It exists for two reasons the research supports:

1. **Neither engine has shipped.** `local_ai_content` was a proof of concept as
   of 2026-08-18 and OSKI's backend is unpublished. A contract whose grounded
   path only works once someone else releases is a contract nobody can try.
2. **Only Moodle is covered by them at all.** ILIAS and Stud.IP have no core AI
   layer and no engine aimed at them, so on those platforms there is nothing to
   defer to — and Stud.IP is, per live probing, the *easier* platform to extract
   from (`file-refs` and `folders` are exposed directly).

Scope is deliberately a floor, not parity:

- **PDF only.** Not because other formats do not matter, but because one format
  proves the seam and a second would be chasing engines that are further along.
- **Text, not layout.** No OCR, no tables, no reading order beyond what the
  content stream gives. A scanned PDF yields nothing, and says so.
- **Per page**, so a citation can carry "S. 12". That is the point of doing this
  at all: `Source.locator` exists in the contract and OSKI cites filename and
  page, but until now nothing in this prototype ever populated it.

Standard library only, like the rest of the prototype — a colleague must be able
to run the demo without installing anything. That rules out `pypdf`, so what
follows is a small PDF content-stream reader: enough for the text-bearing PDFs a
course actually contains, and honest about what it cannot do.
"""

from __future__ import annotations

import re
import zlib

__all__ = ["extract_pdf_pages", "PdfExtractionError"]


class PdfExtractionError(Exception):
    """Raised when a file is not a PDF we can read.

    Callers are expected to catch this and skip the file rather than fail the
    whole indexing run: on a real course one unreadable file must not stop the
    other twenty from being indexed.
    """


# --------------------------------------------------------------------------
# Object plumbing
# --------------------------------------------------------------------------
# A PDF is a set of numbered objects. We need very little of that machinery:
# find the objects, decompress the ones that are content streams, and read the
# text-showing operators. We deliberately do not build an object graph — page
# order comes from document order, which holds for the linear, generated PDFs
# that lecture material overwhelmingly consists of.

_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.DOTALL)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


def _inflate(raw: bytes, header: bytes) -> bytes | None:
    """Decompress a stream, or return None if we do not handle its filter.

    Only FlateDecode is supported. It is what essentially every generator
    produces; the rest (LZW, RunLength, DCT) are rare in lecture PDFs and
    supporting them would be scope creep for a fallback.
    """
    if b"/Filter" in header and b"/FlateDecode" not in header:
        return None
    if b"/FlateDecode" not in header:
        return raw            # uncompressed content stream
    try:
        return zlib.decompress(raw)
    except zlib.error:
        # Some writers leave junk after the stream data; retry leniently.
        try:
            return zlib.decompressobj().decompress(raw)
        except zlib.error:
            return None


# --------------------------------------------------------------------------
# Text extraction from a content stream
# --------------------------------------------------------------------------
# Text lives in `(literal) Tj` and `[(arr) -250 (ay)] TJ` operators. The array
# form carries kerning offsets between fragments; a large negative offset is how
# most generators represent a space, so we reinsert one rather than running
# words together.

_TEXT_OP = re.compile(rb"(\[.*?\]\s*TJ|\(.*?(?<!\\)\)\s*Tj)", re.DOTALL)
_LITERAL = re.compile(rb"\((.*?)(?<!\\)\)", re.DOTALL)
_KERN = re.compile(rb"\)\s*(-?\d+(?:\.\d+)?)")

# Below this kerning offset (thousandths of an em, negated) generators are
# almost always representing a word break rather than tightening a pair.
_SPACE_KERN = -140.0

_ESCAPES = {
    b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
    b"(": b"(", b")": b")", b"\\": b"\\",
}


def _unescape(raw: bytes) -> bytes:
    """Resolve PDF string escapes, including \\ooo octal codes."""
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i : i + 1]
        if c != b"\\":
            out += c
            i += 1
            continue
        nxt = raw[i + 1 : i + 2]
        if nxt in _ESCAPES:
            out += _ESCAPES[nxt]
            i += 2
        elif nxt.isdigit():
            octal = raw[i + 1 : i + 4]
            digits = bytes(ch for ch in octal if 0x30 <= ch <= 0x37)
            if digits:
                out += bytes([int(digits, 8) & 0xFF])
                i += 1 + len(digits)
            else:
                i += 2
        elif nxt in (b"\n", b"\r"):
            i += 2          # line continuation inside a string
        else:
            out += nxt
            i += 2
    return bytes(out)


# Single-byte PDF encodings we can distinguish. The default is WinAnsi
# (effectively cp1252), but macOS generators emit MacRoman and declare it — and
# guessing wrong turns "Größe" into "Gr\x9a\xa7e", which corrupts both the
# retrieval tokens and the citation a reader sees. So we read what the font
# declares rather than assuming.
_PDF_ENCODINGS = {
    b"/MacRomanEncoding": "mac-roman",
    b"/WinAnsiEncoding": "cp1252",
    b"/MacExpertEncoding": "mac-roman",   # symbol set; mac-roman is the closer miss
    b"/StandardEncoding": "latin-1",
}
_ENCODING_DECL = re.compile(rb"/Encoding\s*(/[A-Za-z0-9]+)")

# Bytes that are undefined in cp1252. If a document contains them, it is not
# cp1252, and MacRoman is overwhelmingly the alternative in practice.
_CP1252_UNDEFINED = {0x81, 0x8D, 0x8F, 0x90, 0x9D}


def _document_encoding(data: bytes) -> str:
    """Pick the single-byte encoding for a document's text strings.

    Uses the first `/Encoding` a font declares. Mixed-encoding documents exist
    but are rare in lecture material, and a fallback extractor guessing one
    encoding well beats it guessing two badly.
    """
    m = _ENCODING_DECL.search(data)
    if m and m.group(1) in _PDF_ENCODINGS:
        return _PDF_ENCODINGS[m.group(1)]
    return ""          # unknown; _decode falls back to a heuristic


def _decode(raw: bytes, encoding: str = "") -> str:
    """Decode a PDF string to text.

    PDF text is either UTF-16BE (marked by a BOM) or a single-byte encoding
    named by the font. When nothing is declared we guess between the two common
    ones by looking for bytes that cp1252 leaves undefined. Never raises —
    undecodable bytes become replacement characters rather than losing the page.
    """
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    if not encoding:
        encoding = "mac-roman" if any(b in _CP1252_UNDEFINED for b in raw) else "cp1252"
    return raw.decode(encoding, "replace")


def _text_from_stream(data: bytes, encoding: str = "") -> str:
    parts: list[str] = []
    for op in _TEXT_OP.findall(data):
        if op.lstrip().startswith(b"["):
            # Array form: fragments interleaved with kerning offsets.
            frag = bytearray()
            for piece in re.split(rb"(\(.*?(?<!\\)\))", op, flags=re.DOTALL):
                if piece.startswith(b"(") and piece.endswith(b")"):
                    frag += _unescape(piece[1:-1])
                else:
                    for kern in _KERN.findall(b")" + piece):
                        if float(kern) <= _SPACE_KERN:
                            frag += b" "
            parts.append(_decode(bytes(frag), encoding))
        else:
            m = _LITERAL.search(op)
            if m:
                parts.append(_decode(_unescape(m.group(1)), encoding))

    text = "".join(parts)
    # Generators emit one operator per line-fragment; collapse the resulting
    # ragged whitespace but keep paragraph breaks, which the chunker splits on.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_pages(data: bytes) -> list[str]:
    """Extract text from a PDF, one string per page.

    Returns a list whose index + 1 is the page number, so a caller can build
    `Source.locator` as "S. {i+1}". Pages with no extractable text are kept as
    empty strings rather than dropped — otherwise every later page number would
    be wrong, which is worse than an empty page.

    Raises `PdfExtractionError` if the bytes are not a PDF at all. A PDF that is
    genuinely image-only returns a list of empty strings: that is a real answer
    ("this is scanned, it needs OCR we do not do"), not a failure.
    """
    if not data.startswith(b"%PDF"):
        raise PdfExtractionError("not a PDF (missing %PDF header)")

    encoding = _document_encoding(data)

    pages: list[str] = []
    for _num, _gen, body in _OBJ.findall(data):
        stream = _STREAM.search(body)
        if not stream:
            continue
        header = body[: stream.start()]
        # Content streams are the ones without a /Subtype (images, fonts and
        # metadata all carry one). Cheap filter, and wrong only for exotic files.
        if b"/Subtype" in header and b"/Image" in header:
            continue
        payload = _inflate(stream.group(1), header)
        if payload is None:
            continue
        if b"Tj" not in payload and b"TJ" not in payload:
            continue
        pages.append(_text_from_stream(payload, encoding))

    if not pages:
        raise PdfExtractionError(
            "no readable content streams — the PDF is scanned, encrypted, or "
            "uses a compression filter this fallback extractor does not support"
        )
    return pages
