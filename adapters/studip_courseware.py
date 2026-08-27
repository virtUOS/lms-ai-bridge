"""Extract text from Stud.IP Courseware.

**Why this matters more than the file extractors.** On a survey of six real
courses, four had Courseware and *three of those had no files at all* — the
Mikromodule are Courseware end to end. Everything the bridge learned to read
from PDFs, slides and spreadsheets retrieves exactly nothing on those courses.
Modern, self-contained Stud.IP course material lives here.

The structure, established by probing a live module (2026-08-25):

    courseware-unit
      └── structural-element (root)          the module; payload.description
            └── children                     the chapters, titled and ordered
                  └── containers             layout groupings
                        └── blocks           the content: text, test, video, …

Two things make this cheap. `…/descendants` returns the whole tree in one call
rather than a walk per chapter, and text blocks carry ordinary HTML that the
adapter's existing `strip_html` already handles.

Chapter titles become the locator — "Kapitel 2: Videogenerative Künstliche
Intelligenz" is a citation a reader can act on, which is the whole reason the
locator field exists.

**What is deliberately not extracted:**

- **Quiz content.** A `test` block carries only `{"assignment": "83876"}` — a
  reference, not the questions or answers. Following it would mean indexing
  assessment material, and putting a course's answers where a student can ask an
  assistant for them is a governance decision, not an implementation detail. The
  block's *title* is indexed, so the assistant knows a self-test exists on that
  chapter; the content is left alone. Stud.IP's own KI-Toolbox rules
  are where such a policy would be expressed.
- **Images.** Embedded as `sendfile.php` URLs, which need a web session the
  JSON-API credentials do not provide (see `studip_download`). A vision model
  could describe them, but not before that authentication is solved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Block types whose payload carries prose worth indexing. Others contribute
# their title only — enough for the assistant to know the element is there.
_TEXT_KEYS = ("text", "content", "description", "caption")

# Blocks that reference material rather than containing it. Their titles are
# indexed; their targets are not followed.
_REFERENCE_BLOCKS = {"test", "assignment", "folder", "file", "link", "embed"}


def _payload_text(payload) -> str:
    """Pull prose out of a block payload, whatever shape it takes.

    Payloads are not uniform across block types, so this looks for the keys that
    actually hold text rather than assuming one schema. Falls back to nothing
    rather than dumping raw JSON — the previous implementation serialised the
    whole payload, which indexed colour names and layout keys as if they were
    course content.
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n\n".join(parts)


def fetch_courseware(studip_get, strip_html, course_id: str) -> list[dict]:
    """Return indexable documents for a course's Courseware, one per chapter.

    `studip_get` and `strip_html` are passed in rather than imported so this
    module stays independent of the adapter's global configuration and can be
    tested with a stub.
    """
    try:
        units = studip_get(f"/courses/{course_id}/courseware-units?page[limit]=20")
    except RuntimeError:
        return []

    docs: list[dict] = []
    for unit in units.get("data", []) or []:
        rel = (unit.get("relationships") or {}).get("structural-element") or {}
        root_id = (rel.get("data") or {}).get("id")
        if not root_id:
            continue
        docs.extend(_fetch_unit(studip_get, strip_html, course_id, root_id))
    return docs


def _fetch_unit(studip_get, strip_html, course_id: str, root_id: str) -> list[dict]:
    try:
        root = studip_get(f"/courseware-structural-elements/{root_id}")
    except RuntimeError:
        return []
    root_attrs = (root.get("data") or {}).get("attributes", {}) or {}
    module_title = str(root_attrs.get("title") or "Courseware")

    docs: list[dict] = []

    # The module description is short but high value: it is the one place that
    # says what the module is *about*, which is what "Worum geht es?" needs.
    description = strip_html(_payload_text(root_attrs.get("payload")))
    if len(description) >= 40:
        docs.append({
            "activity_ref": f"studip:{course_id}:courseware:{root_id}",
            "title": module_title,
            "locator": "Überblick",
            "text": description,
        })

    # One call for the whole tree.
    try:
        tree = studip_get(
            f"/courseware-structural-elements/{root_id}/descendants?page[limit]=200"
        )
    except RuntimeError:
        return docs

    elements = [e for e in (tree.get("data") or []) if e.get("id") != root_id]
    elements.sort(key=lambda e: (
        int((e.get("attributes") or {}).get("position") or 0), str(e.get("id"))))

    for element in elements:
        attrs = element.get("attributes", {}) or {}
        title = str(attrs.get("title") or "").strip()
        pieces: list[str] = []

        intro = strip_html(_payload_text(attrs.get("payload")))
        if intro:
            pieces.append(intro)
        pieces.extend(_element_text(studip_get, strip_html, element.get("id")))

        body = "\n\n".join(p for p in pieces if p.strip())
        if len(body) < 40:
            continue

        docs.append({
            "activity_ref":
                f"studip:{course_id}:courseware:{element.get('id')}",
            "title": module_title,
            # The chapter title, which is what a reader needs to find the place.
            "locator": title or f"Abschnitt {attrs.get('position', '')}".strip(),
            "text": body,
        })

    return docs


def _element_text(studip_get, strip_html, element_id: str) -> list[str]:
    """Text of every block on one structural element, via its containers."""
    out: list[str] = []
    try:
        containers = studip_get(
            f"/courseware-structural-elements/{element_id}/containers?page[limit]=50"
        )
    except RuntimeError:
        return out

    for container in containers.get("data", []) or []:
        try:
            blocks = studip_get(
                f"/courseware-containers/{container.get('id')}/blocks?page[limit]=100"
            )
        except RuntimeError:
            continue

        entries = blocks.get("data", []) or []
        entries.sort(key=lambda b: (
            int((b.get("attributes") or {}).get("position") or 0), str(b.get("id"))))

        for block in entries:
            attrs = block.get("attributes", {}) or {}
            if attrs.get("visible") is False:
                continue                      # unpublished: not course material
            kind = str(attrs.get("block-type") or "")
            title = str(attrs.get("title") or "").strip()

            if kind in _REFERENCE_BLOCKS:
                # Record that it exists; do not follow it. See the module
                # docstring on why quiz content is left alone.
                if title:
                    label = "Selbsttest" if kind == "test" else kind
                    out.append(f"[{label}: {title}]")
                continue

            text = strip_html(_payload_text(attrs.get("payload")))
            if title and text:
                out.append(f"{title}\n{text}")
            elif text:
                out.append(text)
            elif title:
                out.append(title)
    return out
