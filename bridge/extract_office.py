"""Text extraction for Office and OpenDocument files.

A companion to `extract.py`, and the same kind of thing: a **fallback**, not a
competitor to the engines ByCS and OSKI.nrw are building. See that module's
docstring for the argument. What is worth repeating here is the part specific to
these formats — **on a real course, the slides are the lecture.** Of the twelve
files on the virtUOS-Weiterbildung course this was built against, three are
presentations or spreadsheets; a PDF-only extractor sees none of them.

Both families are handled because both turn up in practice. A German university
course contains LibreOffice files as readily as Microsoft ones — that course has
a `.pptx`, an `.xlsx`, and an `.odp` side by side — and supporting only the
Microsoft half would look like an oversight rather than a scope decision.

Every format here is a ZIP of XML, so `zipfile` plus the stdlib XML parser does
the whole job. No dependency, consistent with the promise that a colleague can
run the demo with nothing installed.

Scope, deliberately narrow:

- **Text only.** No layout, no styles, no charts, no embedded images. A slide's
  speaker notes are included because they often carry what the slide elides.
- **Per slide / per sheet**, so `Source.locator` can say "Folie 4" or
  "Tabelle 2" the way the PDF path says "S. 12". A citation that cannot be
  checked is barely better than no citation.
- **Legacy `.doc`/`.ppt`/`.xls` are not supported.** Those are OLE compound
  files, not ZIPs — a different and much larger problem, and rare enough in
  recent course material not to earn the complexity here.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

__all__ = ["extract_office_units", "OfficeExtractionError", "office_kind"]


class OfficeExtractionError(Exception):
    """Raised when a file is not an Office/OpenDocument file we can read.

    Callers should skip the file and carry on, exactly as with
    `PdfExtractionError`: one unreadable attachment must not cost a course its
    other twenty.
    """


# --------------------------------------------------------------------------
# Which format is this?
# --------------------------------------------------------------------------
# Detection is by content, not filename: Stud.IP reports a MIME type and a name,
# and both can be wrong or truncated. A ZIP that contains `ppt/slides/` is a
# presentation whatever it claims to be called.

_OOXML = {
    "pptx": "ppt/slides/",
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
}
_ODF_MIME = {
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
}


def office_kind(data: bytes) -> str:
    """Return 'pptx' | 'docx' | 'xlsx' | 'odp' | 'odt' | 'ods', or ''.

    Cheap enough to call before deciding whether to extract, and returns ''
    rather than raising so a caller can fall through to another handler.
    """
    if not data.startswith(b"PK\x03\x04"):
        return ""
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            names = set(z.namelist())
            # OpenDocument declares itself in a `mimetype` entry.
            if "mimetype" in names:
                mime = z.read("mimetype").decode("ascii", "replace").strip()
                if mime in _ODF_MIME:
                    return _ODF_MIME[mime]
            for kind, marker in _OOXML.items():
                if any(n.startswith(marker) or n == marker for n in names):
                    return kind
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    return ""


# --------------------------------------------------------------------------
# XML helpers
# --------------------------------------------------------------------------
# Both families namespace everything, and the namespace URIs differ by format
# and version. Matching on the *local* tag name avoids hardcoding any of them —
# a small trick that removes a whole class of "works on my file" bugs.


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text_of(node: ET.Element, wanted: str = "t") -> str:
    """Concatenate the text of every descendant element named `wanted`."""
    out: list[str] = []
    for el in node.iter():
        if _local(el.tag) == wanted and el.text:
            out.append(el.text)
    return "".join(out)


def _tidy(text: str) -> str:
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _slide_sort_key(name: str) -> tuple[int, str]:
    """Sort slide2.xml before slide10.xml, which a plain string sort would not."""
    m = re.search(r"(\d+)", name.rsplit("/", 1)[-1])
    return (int(m.group(1)) if m else 0, name)


# --------------------------------------------------------------------------
# OOXML (Microsoft)
# --------------------------------------------------------------------------


def _pptx(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(label, text) per slide, with speaker notes appended.

    Notes matter more here than they look: a slide often carries three words and
    a diagram while the substance sits in the notes the lecturer wrote.
    """
    slides = sorted((n for n in z.namelist()
                     if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=_slide_sort_key)
    units: list[tuple[str, str]] = []
    for i, name in enumerate(slides, 1):
        body = _text_of(ET.fromstring(z.read(name)), "t")
        notes_name = f"ppt/notesSlides/notesSlide{i}.xml"
        if notes_name in z.namelist():
            notes = _text_of(ET.fromstring(z.read(notes_name)), "t")
            # Drop the slide-number placeholder PowerPoint puts in every note.
            notes = re.sub(r"^\s*\d+\s*$", "", notes).strip()
            if notes:
                body = f"{body}\n\nNotizen: {notes}"
        units.append((f"Folie {i}", _tidy(body)))
    return units


def _docx(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """One unit for the document; paragraphs preserved as line breaks.

    Word has no page boundaries in the XML — pagination happens at render time —
    so there is no honest way to say "S. 4" here. The locator is left empty
    rather than invented.
    """
    root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for el in root.iter():
        if _local(el.tag) == "p":
            line = _text_of(el, "t")
            if line.strip():
                paras.append(line.strip())
    return [("", _tidy("\n\n".join(paras)))]


def _xlsx(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(sheet name, text) per worksheet, cells joined by spaces.

    Spreadsheets are the weakest fit for retrieval — a grid of numbers reads
    poorly as prose — but the *labels* are often the useful part: a survey's
    question wording, a column of category names.
    """
    # Strings live in a shared table and are referenced by index from cells.
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared = [_text_of(si, "t") for si in root if _local(si.tag) == "si"]

    names: dict[str, str] = {}
    if "xl/workbook.xml" in z.namelist():
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        for i, sheet in enumerate((el for el in wb.iter()
                                   if _local(el.tag) == "sheet"), 1):
            names[f"sheet{i}.xml"] = sheet.get("name", f"Tabelle {i}")

    units: list[tuple[str, str]] = []
    paths = sorted((n for n in z.namelist()
                    if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                   key=_slide_sort_key)
    for i, path in enumerate(paths, 1):
        root = ET.fromstring(z.read(path))
        cells: list[str] = []
        for c in root.iter():
            if _local(c.tag) != "c":
                continue
            value = ""
            for child in c:
                if _local(child.tag) == "v":
                    value = child.text or ""
                elif _local(child.tag) == "is":
                    value = _text_of(child, "t")
            if not value:
                continue
            if c.get("t") == "s":               # index into the shared table
                try:
                    value = shared[int(value)]
                except (ValueError, IndexError):
                    continue
            if value.strip():
                cells.append(value.strip())
        label = names.get(path.rsplit("/", 1)[-1], f"Tabelle {i}")
        units.append((label, _tidy(" ".join(cells))))
    return units


# --------------------------------------------------------------------------
# OpenDocument (LibreOffice)
# --------------------------------------------------------------------------
# All three ODF types keep their content in one `content.xml`. Presentations
# divide it into `drawing:page` elements; text and spreadsheets do not divide
# usefully, so they become a single unit.


def _odf(z: zipfile.ZipFile, kind: str) -> list[tuple[str, str]]:
    root = ET.fromstring(z.read("content.xml"))

    if kind == "odp":
        pages = [el for el in root.iter() if _local(el.tag) == "page"]
        return [(f"Folie {i}", _tidy(_text_of(page, "p")))
                for i, page in enumerate(pages, 1)]

    if kind == "ods":
        units: list[tuple[str, str]] = []
        tables = [el for el in root.iter() if _local(el.tag) == "table"]
        for i, table in enumerate(tables, 1):
            label = next((v for k, v in table.attrib.items()
                          if _local(k) == "name"), f"Tabelle {i}")
            units.append((label, _tidy(" ".join(
                t for t in (_text_of(cell, "p") for cell in table.iter()
                            if _local(cell.tag) == "table-cell") if t.strip()))))
        return units

    # odt: paragraphs, no page structure to speak of.
    paras = [el.text or _text_of(el, "span")
             for el in root.iter() if _local(el.tag) == "p"]
    return [("", _tidy("\n\n".join(p for p in paras if p and p.strip())))]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def extract_office_units(data: bytes) -> list[tuple[str, str]]:
    """Extract text as a list of `(locator, text)` pairs.

    The locator is what a citation shows — "Folie 4", a sheet name — or "" for
    formats with no meaningful subdivision (Word, ODF text). Empty units are
    kept rather than dropped so that slide numbering stays truthful: dropping
    slide 3 would silently renumber every slide after it.

    Raises `OfficeExtractionError` when the file is not a format handled here,
    including the legacy binary `.doc`/`.ppt`/`.xls`, which are not ZIPs.
    """
    kind = office_kind(data)
    if not kind:
        if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise OfficeExtractionError(
                "legacy binary Office format (.doc/.ppt/.xls) — not supported; "
                "re-save as the modern XML format to index it"
            )
        raise OfficeExtractionError("not a recognised Office or OpenDocument file")

    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            if kind == "pptx":
                units = _pptx(z)
            elif kind == "docx":
                units = _docx(z)
            elif kind == "xlsx":
                units = _xlsx(z)
            else:
                units = _odf(z, kind)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
        raise OfficeExtractionError(f"could not read the {kind}: {e}") from e

    if not any(text.strip() for _, text in units):
        raise OfficeExtractionError(
            f"no extractable text in this {kind} — it may hold only images, "
            "which needs a vision model rather than a parser"
        )
    return units
