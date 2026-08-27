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

**Poppler is used when it is installed.** Measured against three arbitrary
real PDFs on 2026-08-27, the reader below returned 12% of the text of one and
93% of another, where `pdftotext` returned all of both. That gap is not a
tuning problem: a partial extraction reads as a success and loses pages
silently, which is the same failure shape as retrieval over noise. So the rule
from `RETRIEVAL_PROVIDER=auto` applies here too — **use what the institution
already has, ship a fallback anyway, and say which one ran.** `pdftotext` is a
dependency of ByCS's `local_ai_content` as well, so an institution running that
stack already has it. Set `PDF_EXTRACTOR=builtin` to force the fallback, or
`poppler` to require Poppler and fail loudly if it is missing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zlib

__all__ = [
    "extract_pdf_pages",
    "extract_pdf_pages_with_engine",
    "PdfExtractionError",
]


class PdfExtractionError(Exception):
    """Raised when a file is not a PDF we can read.

    Callers are expected to catch this and skip the file rather than fail the
    whole indexing run: on a real course one unreadable file must not stop the
    other twenty from being indexed.
    """


class _NoTextFound(PdfExtractionError):
    """Poppler read the file and found no text — that is, it is a scan.

    A subclass so every existing caller still catches it. It exists so the
    auto path can tell "read it, there is no text" (say OCR) apart from "could
    not parse it at all" (the fallback's message is more informative there).
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
# PDF 1.7 §7.2.3: a line may end CR, LF or CRLF, and `stream` is followed by
# CRLF or LF (never CR alone) — but the EOL *before* `endstream` may be any
# of the three, or absent. Requiring LF there hid every stream in a
# CR-delimited file: 107 of 107 in the EUA report (checked 2026-08-27).
_STREAM = re.compile(rb"stream\r?\n(.*?)[\r\n]{0,2}endstream", re.DOTALL)


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


# Poppler ships `pdftotext` on every platform this is likely to run on, and it
# is already a dependency of ByCS `local_ai_content` (their README asks for
# poppler-utils). Resolved through PATH so an operator can point at their own
# build; looked up per call rather than cached so a test can patch it.
def _poppler_path() -> str | None:
    """Return the path to `pdftotext`, or None when Poppler is not installed."""
    return shutil.which(os.environ.get("PDFTOTEXT_BIN", "pdftotext"))


def _extract_pdf_pages_poppler(data: bytes, binary: str) -> list[str]:
    """Extract per page with Poppler, preserving page numbering.

    `-layout` is deliberately not passed: it reproduces columns visually, which
    reads worse once chunked. Page boundaries come from the form feed pdftotext
    writes between pages (PDF 1.7 §14.8 has no notion of one, so this is
    pdftotext's own convention and the reason the split is on \f).
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(data)
        handle.flush()
        try:
            done = subprocess.run(
                [binary, "-enc", "UTF-8", "-q", handle.name, "-"],
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfExtractionError(
                "pdftotext timed out after 120s — the PDF may be malformed"
            ) from exc
        except OSError as exc:  # pragma: no cover - depends on the host
            raise PdfExtractionError(f"could not run {binary}: {exc}") from exc

    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip() or "no detail"
        raise PdfExtractionError(f"pdftotext failed: {detail}")

    text = done.stdout.decode("utf-8", "replace")
    # Trailing form feed produces a phantom final page; drop it, but keep every
    # interior empty page so that page N stays at index N-1 and a locator of
    # "S. 12" still points at page 12.
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    pages = [page.strip() for page in pages]

    # Poppler reads a scanned PDF happily and returns empty pages. Returning
    # those would index the file as a document with no text, which is how a
    # course quietly loses a scan: nothing errors, nothing is searchable, and
    # the gap only shows when someone asks about the missing material. The
    # fallback refuses this case explicitly, and so must this path.
    if not any(pages):
        raise _NoTextFound(
            "no extractable text — the PDF is scanned or image-only and needs "
            "OCR, which this module does not do"
        )
    return pages


def extract_pdf_pages_with_engine(
    data: bytes, engine: str = ""
) -> tuple[list[str], str]:
    """Extract per page, returning the pages and which engine produced them.

    The engine is part of the return value rather than a log line because the
    difference is visible in the output: a course indexed by the fallback may
    be missing pages that Poppler would have read. A caller that records which
    ran can explain a thin index later; one that does not, cannot.
    """
    if not data.startswith(b"%PDF"):
        raise PdfExtractionError("not a PDF (missing %PDF header)")

    mode = (engine or os.environ.get("PDF_EXTRACTOR", "auto")).strip().lower()

    if mode == "builtin":
        return _extract_pdf_pages_builtin(data), "builtin"

    binary = _poppler_path()
    if mode == "poppler":
        if not binary:
            # Asked for explicitly and unusable. Unlike the retrieval fallback,
            # which warns and carries on, this raises: the cost of guessing
            # wrong is whole pages missing from a citation.
            raise PdfExtractionError(
                "PDF_EXTRACTOR=poppler but pdftotext was not found on PATH — "
                "install poppler-utils or set PDF_EXTRACTOR=builtin"
            )
        return _extract_pdf_pages_poppler(data, binary), "poppler"

    if binary:
        try:
            return _extract_pdf_pages_poppler(data, binary), "poppler"
        except PdfExtractionError as poppler_error:
            # A PDF Poppler cannot read is unlikely to be one the fallback can,
            # but trying costs a moment and the fallback has its own strengths
            # on damaged files.
            try:
                return _extract_pdf_pages_builtin(data), "builtin"
            except PdfExtractionError:
                # When Poppler read the file and found no text, its message is
                # the accurate one — it names OCR, where the fallback would
                # advise installing the Poppler this reader plainly has. When
                # Poppler could not parse the file at all, the fallback knows
                # more about why and its message is kept.
                if isinstance(poppler_error, _NoTextFound):
                    raise poppler_error from None
                raise
    return _extract_pdf_pages_builtin(data), "builtin"


def extract_pdf_pages(data: bytes) -> list[str]:
    """Extract text from a PDF, one string per page.

    Returns a list whose index + 1 is the page number, so a caller can build
    `Source.locator` as "S. {i+1}". Kept as the module's simple entry point;
    use `extract_pdf_pages_with_engine` when the caller needs to record which
    extractor ran.
    """
    return extract_pdf_pages_with_engine(data)[0]

def _extract_pdf_pages_builtin(data: bytes) -> list[str]:
    """Extract text from a PDF with the stdlib reader, one string per page.

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
