"""ILIAS → LMS AI Bridge adapter.

Pulls the content of an ILIAS course over ILIAS's **core SOAP interface** and
pushes it to the bridge for indexing.

## Why SOAP, not REST

The first version of this adapter (August 2026, never run) assumed the
third-party `RESTPlugin`. On first contact with a real instance — ILIAS 10.11
at Hochschule Osnabrück, 2026-09-22 — that assumption fell over: the plugin is
not installed, and nothing in core answers on a REST path. What *is* there,
enabled by default and needing no plugin, is the SOAP server at
`/soap/server.php`, with a WSDL listing some 90 operations. The ones this
adapter needs:

  - `login(client, username, password)` → session id
  - `getCourseXML(sid, ref_id)` → title, description, syllabus
  - `getTreeChilds(sid, ref_id, types, user_id)` → the objects under a node
  - `getFileXML(sid, ref_id, attachment_mode=1)` → file metadata **and the
    file's bytes, base64-encoded inline** — no separate download step
  - `logout(sid)`

Verified end to end on 2026-09-22 against that instance with a read-only
account: 37 documents from a seeded course, page-cited answers, the scanned
PDF refused. Two things learned the hard way, kept here because they fail
silently: `getTreeChilds` must be called *without* `user_id` (0 means nobody
and yields an empty tree), and `addFile` with inline content is broken on
10.11 — this adapter only reads, so it is unaffected, but do not build a
seeding tool on it.

## Credentials

SOAP takes a username and password — there is no token concept — so the
account used here should be a dedicated, least-privilege one with `read` on
the courses to index and nothing else. `ILIAS_CLIENT` is the installation's
client id (shown under Administration → Server Info; `hsos` on the test
instance).

## What this does not do

Same boundaries as the other adapters: no images, no OCR, no legacy Office
formats. ILIAS-specific: learning modules, wikis and content pages are
*not* read yet — SOAP has no operation returning their page text, so that
would go through the export machinery (`getIMSManifestXML` and friends) or a
page-level query, both open questions. Files and the course description are
what this version indexes.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html import unescape
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.extract import (  # noqa: E402
    PdfExtractionError,
    extract_pdf_pages_with_engine,
)
from bridge.extract_office import (  # noqa: E402
    OfficeExtractionError,
    extract_office_units,
    office_kind,
)

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8080")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
ILIAS_URL = os.environ.get("ILIAS_URL", "").rstrip("/")
ILIAS_CLIENT = os.environ.get("ILIAS_CLIENT", "")
ILIAS_USER = os.environ.get("ILIAS_USER", "")
ILIAS_PASSWORD = os.environ.get("ILIAS_PASSWORD", "")

_TAG = re.compile(r"<[^>]+>")
_EXTRACTABLE = re.compile(r"\.(pdf|docx|pptx|xlsx)$", re.IGNORECASE)
_SOAP_NS = "urn:ilUserAdministration"

# Object types worth descending into or reading. Everything else (forums,
# tests, wikis, learning modules) is skipped: SOAP has no operation that
# returns their text, and pretending otherwise would index empty shells.
_CONTAINER_TYPES = {"fold", "grp", "cat", "crs", "itgr"}
_FILE_TYPE = "file"


def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html or ""))).strip()


# --------------------------------------------------------------------------
# SOAP transport
# --------------------------------------------------------------------------


class IliasSoapError(RuntimeError):
    pass


def soap_call(operation: str, params: list[tuple[str, object]], timeout: int = 120) -> str:
    """Call one SOAP operation and return the text of its result element.

    ILIAS's SOAP server is rpc/encoded and names the result element after the
    operation (`<sid>`, `<xml>`, `<success>` …), so the reply is read by
    position — first child of `<opResponse>` — rather than by name. A SOAP
    fault is raised as `IliasSoapError` with the server's fault string, which
    is the only place ILIAS says *why* something failed.
    """
    if not ILIAS_URL:
        raise IliasSoapError("Set ILIAS_URL (see .env.example).")
    body = "".join(f"<{k}>{xml_escape(str(v))}</{k}>" for k, v in params)
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:ns1="{_SOAP_NS}"><SOAP-ENV:Body>'
        f"<ns1:{operation}>{body}</ns1:{operation}>"
        "</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )
    req = urllib.request.Request(
        f"{ILIAS_URL}/soap/server.php",
        data=envelope.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": operation},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reply = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # ILIAS answers a fault with HTTP 500 and the envelope in the body.
        reply = e.read().decode("utf-8", "replace")
    return parse_soap_result(operation, reply)


def parse_soap_result(operation: str, reply: str) -> str:
    fault = re.search(r"<faultstring>(.*?)</faultstring>", reply, re.S)
    if fault:
        raise IliasSoapError(f"ILIAS {operation}: {unescape(fault.group(1)).strip()}")
    m = re.search(rf"<ns1:{operation}Response>\s*<(\w+)[^>]*>(.*?)</\1>", reply, re.S)
    if not m:
        raise IliasSoapError(f"ILIAS {operation}: unrecognised reply: {reply[:200]!r}")
    return unescape(m.group(2))


def login() -> str:
    """Obtain a SOAP session id. `ILIAS_SID` short-circuits for scripted reuse."""
    sid = os.environ.get("ILIAS_SID", "")
    if sid:
        return sid
    if not (ILIAS_CLIENT and ILIAS_USER and ILIAS_PASSWORD):
        raise IliasSoapError(
            "Set ILIAS_URL, ILIAS_CLIENT, ILIAS_USER and ILIAS_PASSWORD "
            "(see .env.example)."
        )
    return soap_call(
        "login",
        [("client", ILIAS_CLIENT), ("username", ILIAS_USER), ("password", ILIAS_PASSWORD)],
        timeout=30,
    )


def logout(sid: str) -> None:
    try:
        soap_call("logout", [("sid", sid)], timeout=30)
    except IliasSoapError:
        pass


# --------------------------------------------------------------------------
# Reading a course
# --------------------------------------------------------------------------


def _text(el: ET.Element | None) -> str:
    return strip_html("".join(el.itertext())) if el is not None else ""


def parse_course_xml(xml: str) -> dict:
    """Title, description and syllabus from `getCourseXML`.

    ILIAS keeps title and description in LOM metadata (`MetaData/General`),
    and the longer free text a lecturer writes under Settings in `Syllabus`.
    Both are indexed: the description is what the course *is*, the syllabus
    is what it *covers*.
    """
    root = ET.fromstring(xml)
    general = root.find("MetaData/General")
    return {
        "title": _text(general.find("Title")) if general is not None else "",
        "description": _text(general.find("Description")) if general is not None else "",
        "syllabus": _text(root.find("Settings/Syllabus")),
        "important": _text(root.find("Settings/ImportantInformation")),
    }


def parse_tree_xml(xml: str) -> list[dict]:
    """Children of a node from `getTreeChilds`: ref_id, obj type and title."""
    items = []
    for obj in ET.fromstring(xml).iter("Object"):
        ref = obj.find("References")
        items.append(
            {
                "type": obj.get("type", ""),
                "obj_id": obj.get("obj_id", ""),
                "ref_id": ref.get("ref_id", "") if ref is not None else "",
                "title": _text(obj.find("Title")),
                "description": _text(obj.find("Description")),
            }
        )
    return items


def parse_file_xml(xml: str) -> tuple[str, str, bytes | None]:
    """(filename, title, bytes) from `getFileXML(…, attachment_mode=1)`.

    With attachment mode 1 the writer emits one `<Version mode="PLAIN">` per
    stored version carrying the base64 content; the last one is current.
    """
    root = ET.fromstring(xml)
    filename = _text(root.find("Filename"))
    title = _text(root.find("Title")) or filename
    blob = None
    for version in root.iter("Version"):
        mode = (version.get("mode") or "").upper()
        payload = (version.text or "").strip()
        if not payload:
            continue
        if mode not in ("", "PLAIN"):
            raise IliasSoapError(
                f"{filename}: content mode {mode} not supported; ask for mode 1"
            )
        blob = base64.b64decode(payload)
    return filename, title, blob


def _file_documents(sid: str, course_ref: str, item: dict, course_name: str,
                    folder: str) -> list[dict]:
    """One document per page or slide of a file object, same as the other
    adapters, so a citation reads "S. 12" or "Folie 4" whichever LMS it
    came from."""
    name = item["title"]
    ref_id = item["ref_id"]
    try:
        filename, title, blob = parse_file_xml(
            soap_call("getFileXML", [("sid", sid), ("ref_id", ref_id), ("attachment_mode", 1)])
        )
    except (IliasSoapError, ET.ParseError) as exc:
        print(f"  skipped {name}: {exc}", file=sys.stderr)
        return []
    if blob is None:
        print(f"  skipped {filename}: no content returned", file=sys.stderr)
        return []
    if not _EXTRACTABLE.search(filename):
        print(f"  skipped {filename}: no extractor for this format", file=sys.stderr)
        return []

    try:
        if blob.startswith(b"%PDF"):
            pages, engine = extract_pdf_pages_with_engine(blob)
            if engine == "builtin":
                print(f"  {filename}: stdlib PDF reader (install poppler-utils "
                      f"for complete extraction)", file=sys.stderr)
            units = [(f"S. {i}", text) for i, text in enumerate(pages, 1)]
        elif office_kind(blob):
            units = extract_office_units(blob)
        else:
            print(f"  skipped {filename}: no extractor for this format", file=sys.stderr)
            return []
    except (PdfExtractionError, OfficeExtractionError) as exc:
        print(f"  skipped {filename}: {exc}", file=sys.stderr)
        return []

    docs = []
    for locator, text in units:
        body = re.sub(r"\s+", " ", text).strip()
        if len(body) < 15:
            continue
        docs.append(
            {
                "activity_ref": f"{course_ref}:file:{ref_id}:{filename}",
                "title": title,
                "locator": locator,
                "text": body,
                "course_name": course_name,
                "folder": folder,
            }
        )
    print(f"  {filename}: {len(docs)}/{len(units)} units with text", file=sys.stderr)
    return docs


def _walk(sid: str, course_ref: str, ref_id: str, course_name: str,
          folder: str, docs: list[dict], depth: int = 0) -> None:
    if depth > 8:
        return
    # No `user_id`: the writer behind getTreeChilds runs a permission check
    # *as that user*, and 0 is nobody — the reply is then an empty <Objects/>
    # with no error, which looked exactly like an empty course on 2026-09-22.
    # Omitted, the check runs as the session's own user.
    tree = soap_call("getTreeChilds", [("sid", sid), ("ref_id", ref_id), ("types", "")])
    for item in parse_tree_xml(tree):
        if item["type"] == _FILE_TYPE:
            docs.extend(_file_documents(sid, course_ref, item, course_name, folder))
        elif item["type"] in _CONTAINER_TYPES and item["ref_id"]:
            sub = f"{folder}/{item['title']}" if folder else item["title"]
            if item["description"]:
                docs.append(
                    {
                        "activity_ref": f"{course_ref}:obj:{item['ref_id']}",
                        "title": item["title"],
                        "text": item["description"],
                        "course_name": course_name,
                        "folder": folder,
                    }
                )
            _walk(sid, course_ref, item["ref_id"], course_name, sub, docs, depth + 1)
        elif item["description"] and len(item["description"]) >= 40:
            # Any other object contributes only its description — honest
            # about what SOAP gives us, which is metadata, not content.
            docs.append(
                {
                    "activity_ref": f"{course_ref}:obj:{item['ref_id']}",
                    "title": item["title"],
                    "text": item["description"],
                    "course_name": course_name,
                    "folder": folder,
                }
            )


def fetch_course_documents(ref_id: str, sid: str | None = None) -> tuple[str, list[dict]]:
    """Return (course_ref, documents) for the course at `ref_id`."""
    own_session = sid is None
    sid = sid or login()
    course_ref = f"ilias:{ref_id}"
    docs: list[dict] = []
    try:
        course = parse_course_xml(soap_call("getCourseXML", [("sid", sid), ("course_id", ref_id)]))
        title = course["title"] or "Kurs"
        for key, label in (("description", "Beschreibung"), ("syllabus", "Inhalt"),
                           ("important", "Wichtige Informationen")):
            if len(course[key]) >= 40:
                docs.append(
                    {
                        "activity_ref": f"{course_ref}:{key}",
                        "title": f"{title} — {label}",
                        "text": course[key],
                        "course_name": title,
                    }
                )
        _walk(sid, course_ref, ref_id, title, "", docs)
    finally:
        if own_session:
            logout(sid)
    return course_ref, docs


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------


def bridge_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {BRIDGE_TOKEN}"} if BRIDGE_TOKEN else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def index_course(ref_id: str) -> dict:
    course_ref, docs = fetch_course_documents(ref_id)
    if not docs:
        raise RuntimeError(f"no indexable text found in ILIAS course {ref_id}")
    return bridge_post(
        "/v1/index", {"course_ref": course_ref, "documents": docs, "replace": True}
    )


def ask(ref_id: str, question: str) -> dict:
    return bridge_post(
        "/v1/chat",
        {
            "course_ref": f"ilias:{ref_id}",
            "messages": [{"role": "user", "content": question}],
            "locale": "de",
        },
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage:")
        print("  python3 adapters/ilias_adapter.py index <ref_id>")
        print("  python3 adapters/ilias_adapter.py ask   <ref_id> <question>")
        print("  python3 adapters/ilias_adapter.py list  <ref_id>   # what would be indexed")
        return 2
    if argv[1] == "list":
        _, docs = fetch_course_documents(argv[2])
        for d in docs:
            print(f"{d['activity_ref']:60} {d.get('locator', ''):10} {len(d['text']):6} chars")
        print(f"{len(docs)} documents")
        return 0
    if argv[1] == "index":
        print(json.dumps(index_course(argv[2]), indent=2, ensure_ascii=False))
        return 0
    if argv[1] == "ask" and len(argv) >= 4:
        result = ask(argv[2], " ".join(argv[3:]))
        print(result["answer"])
        for i, s in enumerate(result.get("sources", []), 1):
            print(f"  [{i}] {s['title']}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
