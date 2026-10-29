"""The documents of an indexed course, and links back into their LMS.

Used by the MCP App view (bridge/apps/course_view.html, BRIDGE_MCP_APPS):
a course view that lists materials, and source cards that open the material
in its LMS.

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


_MODNAME = re.compile(r"^[a-z_]+$")
_PAGE = re.compile(r"^S\. (\d+)$")


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
        # Checked in a browser on 2026-10-29 (Stud.IP 6.1, logged in): the file
        # page shows the file with Stud.IP's own preview or player. The
        # download route (`sendfile.php`) downloads instead, so a page or
        # timestamp in the URL is lost; the card shows the locator instead.
        if kind == "file" and item:
            return f"{base}/dispatch.php/file/details/{quote(item)}?cid={quote(course)}"
        if kind == "courseware":
            # A chapter, checked in a browser on 2026-10-29. Refs indexed
            # before the unit id was kept get the course's Courseware instead.
            unit = parts[4] if len(parts) > 4 else ""
            if item and unit:
                return (f"{base}/dispatch.php/course/courseware/courseware/{quote(unit)}"
                        f"?cid={quote(course)}#/structural_element/{quote(item)}")
            return f"{base}/dispatch.php/course/courseware?cid={quote(course)}"
        return f"{base}/dispatch.php/course/overview?cid={quote(course)}"

    if platform == "ilias":
        base = _base("ILIAS_URL")
        if not base:
            return ""
        client = os.environ.get("ILIAS_CLIENT", "")
        suffix = f"&client_id={quote(client)}" if client else ""
        if kind in ("file", "obj") and item:
            # ILIAS's permanent link. `file_<ref>` opens the file's info page;
            # checked 2026-10-29 only so far as ILIAS keeps the target through
            # its login redirect. `obj` items have no type in the ref, so they
            # get the course instead.
            if kind == "file":
                return f"{base}/goto.php?target=file_{quote(item)}{suffix}"
        return f"{base}/goto.php?target=crs_{quote(course)}{suffix}"

    if platform == "moodle":
        base = _base("MOODLE_URL")
        if not base:
            return ""
        modname = parts[4] if len(parts) > 4 and _MODNAME.match(parts[4]) else ""
        if kind in ("file", "module") and item and modname == "resource":
            # Checked in a browser on 2026-10-29: redirect=1 forwards to the
            # file itself, which the browser opens, and a #page=N survives the
            # redirect, so a PDF opens at the cited page.
            m = _PAGE.match(locator or "")
            return (f"{base}/mod/resource/view.php?id={quote(item)}&redirect=1"
                    + (f"#page={m.group(1)}" if m else ""))
        if kind in ("file", "module") and item and modname:
            return f"{base}/mod/{modname}/view.php?id={quote(item)}"
        if kind in ("file", "module") and item:
            # Refs indexed before the module type was kept: the course page,
            # scrolled to the activity.
            return f"{base}/course/view.php?id={quote(course)}#module-{quote(item)}"
        return f"{base}/course/view.php?id={quote(course)}"

    return ""
