"""The documents of an indexed course, and links back into their LMS.

SPIKE (2026-10-29, branch spike/mcp-apps): written for the MCP Apps
experiment — a course view that lists materials, and source cards that open
the passage in the LMS. Throwaway until the experiment says otherwise.

A *document* is everything indexed under one `activity_ref`: a file's pages,
a wiki page, an announcement. Its `doc_id` is a short hash of that ref, short
enough for a model to copy back into `search_course(documents=[...])`.

Links are built from the LMS base URLs the adapters already use. They lead to
the LMS's own pages, so the LMS — not the bridge — decides whether the person
clicking may see the file. Nothing here is fetched; a wrong guess at a URL
format costs a broken link, never a leak.
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import quote


def doc_id(activity_ref: str) -> str:
    return hashlib.sha1(activity_ref.encode("utf-8")).hexdigest()[:10]


def group_documents(entries: list[dict]) -> list[dict]:
    """One row per activity_ref, in index order: title, folder, chunk count and
    the locators ("S. 3", "Folie 2", "2:46") its chunks carry."""
    docs: dict[str, dict] = {}
    for e in entries:
        ref = e.get("activity_ref", "")
        d = docs.get(ref)
        if d is None:
            d = docs[ref] = {
                "doc_id": doc_id(ref),
                "activity_ref": ref,
                "title": e.get("title", ""),
                "folder": e.get("folder", ""),
                "chunks": 0,
                "locators": [],
            }
        d["chunks"] += 1
        loc = e.get("locator", "")
        if loc and loc not in d["locators"]:
            d["locators"].append(loc)
    return list(docs.values())


def _base(env: str) -> str:
    return os.environ.get(env, "").rstrip("/")


_PAGE = re.compile(r"^S\. (\d+)$")
_TIME = re.compile(r"^(?:(\d+):)?(\d+):(\d{2})$")


def _fragment(locator: str) -> str:
    """`#page=N` for a PDF page, `#t=S` for a media timestamp: both understood
    by browsers when the file itself is opened, ignored otherwise."""
    m = _PAGE.match(locator or "")
    if m:
        return f"#page={m.group(1)}"
    m = _TIME.match(locator or "")
    if m:
        h, mm, ss = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return f"#t={h * 3600 + mm * 60 + ss}"
    return ""


def lms_link(activity_ref: str, locator: str = "") -> str:
    """A browser URL for the material behind `activity_ref`, or "" if the
    platform's base URL is not configured. Unverified formats are marked."""
    parts = activity_ref.split(":")
    if len(parts) < 2:
        return ""
    platform, course = parts[0], parts[1]
    kind = parts[2] if len(parts) > 2 else ""
    item = parts[3] if len(parts) > 3 else ""

    if platform == "studip":
        base = _base("STUDIP_URL")
        if not base:
            return ""
        if kind == "file" and item:
            # The front end's download route, as Stud.IP's own `meta.download-url`
            # names it; needs the browser's Stud.IP session, which is the point.
            return f"{base}/sendfile.php?type=0&file_id={quote(item)}{_fragment(locator)}"
        return f"{base}/dispatch.php/course/overview?cid={quote(course)}"

    if platform == "ilias":
        base = _base("ILIAS_URL")
        if not base:
            return ""
        client = os.environ.get("ILIAS_CLIENT", "")
        suffix = f"&client_id={quote(client)}" if client else ""
        if kind in ("file", "obj") and item:
            # Unverified (spike): ILIAS's permanent link. `file_<ref>` opens the
            # file's info page; `obj` items have no type in the ref, so they get
            # the course instead.
            if kind == "file":
                return f"{base}/goto.php?target=file_{quote(item)}{suffix}"
        return f"{base}/goto.php?target=crs_{quote(course)}{suffix}"

    if platform == "moodle":
        base = _base("MOODLE_URL")
        if not base:
            return ""
        if kind in ("file", "module") and item:
            # The course page, scrolled to the activity: works for any module
            # type, which the ref does not record.
            return f"{base}/course/view.php?id={quote(course)}#module-{quote(item)}"
        return f"{base}/course/view.php?id={quote(course)}"

    return ""
