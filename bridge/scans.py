"""Scans: course material whose text has to be read from pixels.

Scanned PDFs, pages without a text layer inside an otherwise normal PDF, and
image files. The adapters find them and send them in `/v1/index`'s `scans`
list; the bridge reads them in the background with whatever model the
deployment chose (`bridge/providers/image_text.py`), the same way recordings
go to transcription.

**Every page sent costs a model call**, on a shared GPU or a paid API. So the
adapter side sends only what the text path could not use, and the bridge skips
pages that have no image on them at all.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

__all__ = [
    "IMAGE_SUFFIXES", "TEXT_FLOOR", "MAX_PDF_BYTES", "MAX_IMAGE_BYTES",
    "is_image", "image_mime", "short_pages", "scan_entry", "add_scan",
    "can_render_pdf", "pdf_page_count", "pages_with_images", "render_pdf_page",
    "TextCache", "cache_key",
]

# Formats a vision model reads. SVG is left out: it is text already.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp")

# The adapters drop units shorter than this as dividers or bare cover pages.
# A page below it is exactly a page the text path loses, so it is the one to read.
TEXT_FLOOR = 15

# Inline base64 is the prototype's upload shape (see fetch_course_media in the
# Stud.IP adapter). 100 MB is the in-house OCR service's own file limit; a
# single image beyond 20 MB is a photo no model needs at that size.
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def is_image(name: str, mime: str = "") -> bool:
    n, m = name.lower(), (mime or "").lower()
    if n.endswith(IMAGE_SUFFIXES):
        return True
    return m.startswith("image/") and "svg" not in m


def image_mime(blob: bytes) -> str:
    """The image type from its first bytes, "" when it is not one we send.

    From the bytes, not the name: the data URL tells the model how to decode,
    and a mislabelled upload is common enough in a course folder.
    """
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if blob.startswith(b"BM"):
        return "image/bmp"
    return ""


def short_pages(pages: list[str], floor: int = TEXT_FLOOR) -> list[int]:
    """1-based numbers of the pages with too little text to index."""
    return [i for i, text in enumerate(pages, 1)
            if len(re.sub(r"\s+", " ", text).strip()) < floor]


def scan_entry(blob: bytes, *, title: str, activity_ref: str, licence: str = "",
               course_name: str = "", folder: str = "",
               pages: list[int] | None = None) -> dict:
    """One entry for `/v1/index`'s `scans` list.

    `pages` names the PDF pages to read; without it the whole file is read
    (a scanned PDF, or an image).
    """
    entry = {
        "activity_ref": activity_ref,
        "title": title,
        "licence": licence,
        "course_name": course_name,
        "folder": folder,
        "content_base64": base64.b64encode(blob).decode("ascii"),
    }
    if pages is not None:
        entry["pages"] = list(pages)
    return entry


def add_scan(scans: list, blob: bytes, *, title: str, activity_ref: str,
             licence: str = "", course_name: str = "", folder: str = "",
             pages: list[int] | None = None) -> bool:
    """Add a file to an adapter's `scans` list, or say why not. The adapters'
    one entry point, so the checks are the same on every platform."""
    pdf = blob.startswith(b"%PDF")
    if not pdf and not image_mime(blob):
        print(f"  skipped {title}: not an image the reader accepts", file=sys.stderr)
        return False
    limit = MAX_PDF_BYTES if pdf else MAX_IMAGE_BYTES
    if len(blob) > limit:
        print(f"  skipped {title}: {len(blob) / 1_048_576:.0f} MB is over the "
              f"{limit // 1_048_576} MB limit for reading", file=sys.stderr)
        return False
    scans.append(scan_entry(blob, title=title, activity_ref=activity_ref, licence=licence,
                            course_name=course_name, folder=folder, pages=pages))
    which = f"{len(pages)} page(s)" if pages is not None else ("all pages" if pdf else "image")
    print(f"  {title}: {which} queued for reading", file=sys.stderr)
    return True


# --------------------------------------------------------------------------
# Bridge side: turning PDF pages into images, with Poppler
# --------------------------------------------------------------------------
# The same `poppler-utils` that `pdftotext` comes from (bridge/extract.py),
# which the container already installs. Without it, image files can still be
# read; PDF pages cannot, and `can_render_pdf()` says so up front.

_POPPLER_TOOLS = ("pdftoppm", "pdfimages", "pdfinfo")


def can_render_pdf() -> bool:
    return all(shutil.which(tool) for tool in _POPPLER_TOOLS)


def _run(args: list[str], blob: bytes, timeout: int = 120) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(blob)
        done = subprocess.run([*args, str(src)], capture_output=True, timeout=timeout)
        if done.returncode != 0:
            raise RuntimeError(f"{args[0]} failed: "
                               f"{done.stderr.decode('utf-8', 'replace')[:200]}")
        return done.stdout


def pdf_page_count(blob: bytes) -> int:
    out = _run(["pdfinfo"], blob).decode("utf-8", "replace")
    m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    if not m:
        raise RuntimeError("pdfinfo reported no page count")
    return int(m.group(1))


def pages_with_images(blob: bytes) -> set[int]:
    """Pages that carry at least one image.

    A page with too little text and no image is a divider or blank: there is
    nothing for a model to read, so it is not worth a call.
    """
    out = _run(["pdfimages", "-list"], blob).decode("utf-8", "replace")
    pages: set[int] = set()
    for line in out.splitlines()[2:]:          # header and a dashed rule
        first = line.split(maxsplit=1)[:1]
        if first and first[0].isdigit():
            pages.add(int(first[0]))
    return pages


def render_pdf_page(blob: bytes, page: int, longest_edge: int = 2048) -> bytes:
    """One page as PNG, its longer side `longest_edge` pixels."""
    with tempfile.TemporaryDirectory() as tmp:
        src, prefix = Path(tmp) / "in.pdf", Path(tmp) / "page"
        src.write_bytes(blob)
        done = subprocess.run(
            ["pdftoppm", "-png", "-f", str(page), "-l", str(page),
             "-scale-to", str(longest_edge), "-singlefile", str(src), str(prefix)],
            capture_output=True, timeout=120)
        out = prefix.with_suffix(".png")
        if done.returncode != 0 or not out.exists():
            raise RuntimeError(f"pdftoppm could not render page {page}: "
                               f"{done.stderr.decode('utf-8', 'replace')[:200]}")
        return out.read_bytes()


# --------------------------------------------------------------------------
# Remembering what was read
# --------------------------------------------------------------------------
# Re-indexing a course replaces its index, and the scans come back with it. A
# page already read by the same model must not be paid for twice, so results
# are kept by the file's content (not its name: a re-upload under the same
# name is a different file), the page, and which model read it in which mode.

def cache_key(blob: bytes, page: int, reader: str) -> str:
    return f"{hashlib.sha256(blob).hexdigest()}:{page}:{reader}"


class TextCache:
    """A JSON file of results. Small by nature: text, one entry per page."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._data: dict[str, str] = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, text: str) -> None:
        with self._lock:
            self._data[key] = text
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), "utf-8")
            tmp.replace(self.path)
