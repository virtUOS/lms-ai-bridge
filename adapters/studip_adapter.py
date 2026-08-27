"""Stud.IP → LMS AI Bridge adapter.

Pulls the content of a Stud.IP Veranstaltung via the JSON-API (expanded in
Stud.IP 6) and pushes it to the bridge for indexing.

**This is where the bridge earns its place.** Stud.IP has no core AI layer at
all — the research pass found zero AI plugins in its marketplace — so there is
nothing here to duplicate and no existing engine to defer to. Whatever course
grounding Stud.IP gets, someone has to build; this adapter is the smallest
version of that, resting on a contract shared with the other platforms.

Auth is an OAuth token or HTTP Basic against the JSON-API, depending on how the
instance is configured. Both are supported below.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.extract import PdfExtractionError, extract_pdf_pages  # noqa: E402
from bridge.extract_office import (  # noqa: E402
    OfficeExtractionError, extract_office_units, office_kind,
)
from adapters.studip_courseware import fetch_courseware  # noqa: E402
from bridge.providers.transcription import is_transcribable  # noqa: E402

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8080")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
STUDIP_URL = os.environ.get("STUDIP_URL", "").rstrip("/")
STUDIP_TOKEN = os.environ.get("STUDIP_TOKEN", "")
STUDIP_USER = os.environ.get("STUDIP_USER", "")
STUDIP_PASSWORD = os.environ.get("STUDIP_PASSWORD", "")

_TAG = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html or ""))).strip()


def _auth_header() -> dict[str, str]:
    if STUDIP_TOKEN:
        return {"Authorization": f"Bearer {STUDIP_TOKEN}"}
    if STUDIP_USER and STUDIP_PASSWORD:
        raw = f"{STUDIP_USER}:{STUDIP_PASSWORD}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    raise RuntimeError(
        "Set STUDIP_TOKEN, or STUDIP_USER and STUDIP_PASSWORD (see .env.example)."
    )


def studip_get(path: str) -> dict:
    if not STUDIP_URL:
        raise RuntimeError("Set STUDIP_URL (see .env.example).")
    url = f"{STUDIP_URL}/jsonapi.php/v1{path}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.api+json", **_auth_header()},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Stud.IP {e.code} on {path}: {detail}") from e


def studip_download(file_ref_id: str) -> bytes:
    """Download a file's bytes over the JSON-API. Verified 2026-08-24.

    `GET /file-refs/{id}/content` is the documented download route and works
    with the same HTTP Basic credentials as the rest of the API.

    **Send no `Accept` header.** That is the whole trick, and it cost this
    project three wrong conclusions. With `Accept: */*` the route returns HTTP
    500; with no `Accept` header it returns 200 and `application/pdf`. Stud.IP
    negotiates content on this route and does not treat `*/*` as "anything".
    So: do not add an `Accept` header here to be tidy — it will break.

    The route also answers HEAD with an `ETag` (`"<id> <mtime> <size>"`), which
    is what a future incremental indexer should use to skip unchanged files.

    ---

    **What was wrong before, recorded because it was wrong three times.**

    1. A first pass guessed this URL, sent `Accept: */*`, got 500, and concluded
       the route did not exist. A 500 means the route matched and the request
       was malformed — only a 404 means absent. That distinction was the whole
       answer and it was skipped.
    2. A second pass switched to `meta.download-url`, which points at
       `sendfile.php` in the **web front end**. That genuinely does not accept
       Basic auth: it returns HTTP 200 carrying the login page. True, but
       irrelevant — it is not the API's download route, and chasing it produced
       a confident write-up of a limitation that does not exist.
    3. The two PDFs on the studip-test HPC course are separately broken (stored
       blobs gone; they fail in a logged-in browser too), which made every early
       failure look like the same problem. A healthy production file was what
       finally separated the causes.

    The documentation said so all along:
    https://luniki.github.io/docs-studip-plugin-jsonapi/ — "Mit dieser Route
    kann der Inhalt einer Datei heruntergeladen werden", and HTTP Basic is one
    of its three supported authentication methods. Reading it would have been
    cheaper than six probes. Note it names the metadata field `download-link`
    while this instance returns `download-url`, so the docs are close to but not
    exactly this version.

    `sendfile.php` remains the wrong door, and the login-page guard below stays:
    it is what turns "200 with 23 KB of HTML" into a clear error instead of a
    bogus "corrupt PDF".
    """
    if not file_ref_id:
        raise RuntimeError("no file-ref id")
    url = f"{STUDIP_URL}/jsonapi.php/v1/file-refs/{file_ref_id}/content"
    # No Accept header: see the docstring. Adding one breaks this route.
    req = urllib.request.Request(url, headers=_auth_header())
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    # Content check, not status check: an unauthenticated request to the web
    # front end answers 200 with the login page, so the status cannot be trusted.
    if data[:5].lstrip().startswith(b"<") or b"<!DOCTYPE html" in data[:200]:
        raise RuntimeError(
            "received an HTML page instead of the file — the request reached "
            "Stud.IP's web front end (which needs a session), not the JSON-API"
        )
    return data


_EXTRACTABLE_SUFFIXES = (
    ".pdf", ".pptx", ".docx", ".xlsx", ".odp", ".odt", ".ods",
)


def _looks_extractable(name: str, mime: str) -> bool:
    """Filter on the metadata before spending a download on a file.

    Checked by suffix *and* MIME type because Stud.IP gives both and either can
    be unhelpful — the file listing on a real course showed MIME types truncated
    mid-string (`application/vnd.openxmlfor`), which no exact match would catch.
    The extractors sniff the actual bytes afterwards, so a false positive here
    costs one download and a skip message, not a wrong result.
    """
    lowered = name.lower()
    if lowered.endswith(_EXTRACTABLE_SUFFIXES):
        return True
    m = mime.lower()
    return ("pdf" in m
            or "openxmlformats" in m
            or "opendocument" in m
            or "officedocument" in m)


def fetch_course_files(course_id: str, limit: int = 25) -> list[dict]:
    """Extract text from a course's PDF attachments, one document per page.

    **This uses the bridge's fallback extractor, and only because nothing else
    covers Stud.IP.** On Moodle, ByCS's `local_ai_content` and OSKI.nrw's LMS-RAG
    do this properly, with embeddings and a vector store; where one of those is
    deployed the retrieval provider should be pointed at it and this path left
    unused. Stud.IP has no such engine and no core AI layer, so without this the
    grounded path sees only wiki text — and on a real course most of the
    substance is in the files.

    One document per page, so `Source.locator` carries "S. 12" and a reader can
    check a claim rather than being handed a filename. Non-PDFs are skipped;
    they are the next format to add if this proves worth keeping.

    A file that cannot be read never fails the run — a scanned PDF among twenty
    good ones must not cost the other nineteen.

    Note the two governance fields the API offers and this honours:
    `is-downloadable`, checked before fetching, and `terms-of-use`, which is
    reported per file. Indexing copies course material into a store, so the
    licence it arrived under is exactly what a data-protection review will ask
    about.
    """
    try:
        refs = studip_get(f"/courses/{course_id}/file-refs?page[limit]={limit}")
    except RuntimeError:
        return []

    docs: list[dict] = []
    for ref in refs.get("data", []) or []:
        a = ref.get("attributes", {}) or {}
        name = str(a.get("name", "")) or "Datei"
        mime = str(a.get("mime-type", ""))
        if not _looks_extractable(name, mime):
            continue

        ref_id = ref.get("id")
        # The API says outright whether this account may download the file;
        # asking anyway would turn a clear "no" into a confusing error.
        if a.get("is-downloadable") is False:
            print(f"  skipped {name}: not downloadable for this account", file=sys.stderr)
            continue

        try:
            blob = studip_download(ref_id)
        except (urllib.error.URLError, RuntimeError) as e:
            print(f"  skipped {name}: {e}", file=sys.stderr)
            continue

        # (locator, text) pairs, whatever the format: "S. 12" for a PDF page,
        # "Folie 4" for a slide, a sheet name for a spreadsheet, "" where the
        # format has no honest subdivision (Word has no pages until it renders).
        try:
            if blob.startswith(b"%PDF"):
                units = [(f"S. {i}", t)
                         for i, t in enumerate(extract_pdf_pages(blob), 1)]
            elif office_kind(blob):
                units = extract_office_units(blob)
            else:
                print(f"  skipped {name}: no extractor for this format",
                      file=sys.stderr)
                continue
        except (PdfExtractionError, OfficeExtractionError) as e:
            print(f"  skipped {name}: {e}", file=sys.stderr)
            continue

        kept = 0
        for locator, text in units:
            body = re.sub(r"\s+", " ", text).strip()
            # Lower than the 40 used for wiki pages: a slide legitimately
            # carries very little text, and dropping sparse slides would shift
            # nothing but lose the page a student is most likely asking about.
            # This only skips dividers and bare cover pages.
            if len(body) < 15:
                continue
            docs.append(
                {
                    "activity_ref": f"studip:{course_id}:file:{ref_id}",
                    "title": name,
                    "locator": locator,
                    "text": body,
                }
            )
            kept += 1
        terms = ((ref.get("relationships") or {}).get("terms-of-use") or {}).get("data") or {}
        licence = terms.get("id", "unknown")
        print(f"  {name}: {kept}/{len(units)} units with text  [licence: {licence}]",
              file=sys.stderr)

    return docs


def fetch_course_media(course_id: str, limit: int = 25,
                      max_mb: int = 200) -> list[dict]:
    """Collect audio and video for the bridge to transcribe in the background.

    Returns entries for `/v1/index`'s `media` list — the file's bytes, not its
    text. The bridge queues them, calls whatever transcription provider the
    institution has, and adds the transcript to the index when it is ready. The
    text material is indexed and answerable long before that finishes; see
    `bridge/jobs.py` for why that is the right order.

    **Base64 in a JSON body is the wrong shape for a 500 MB lecture video**, and
    is used here because it keeps the prototype to one endpoint and the standard
    library. `max_mb` caps it so a large file is skipped with a clear message
    rather than exhausting memory on both ends. A production contract should
    take a URL the bridge fetches, or a multipart upload — worth raising with
    the working group, since it is exactly the sort of detail that decides
    whether a contract survives contact with real courses.
    """
    try:
        refs = studip_get(f"/courses/{course_id}/file-refs?page[limit]={limit}")
    except RuntimeError:
        return []

    media: list[dict] = []
    for ref in refs.get("data", []) or []:
        a = ref.get("attributes", {}) or {}
        name = str(a.get("name", "")) or "Aufnahme"
        mime = str(a.get("mime-type", ""))
        if not is_transcribable(name, mime):
            continue
        if a.get("is-downloadable") is False:
            print(f"  skipped {name}: not downloadable for this account",
                  file=sys.stderr)
            continue

        size_mb = (a.get("filesize") or 0) / 1_000_000
        if size_mb > max_mb:
            print(f"  skipped {name}: {size_mb:.0f} MB exceeds the {max_mb} MB "
                  f"limit for inline upload", file=sys.stderr)
            continue

        ref_id = ref.get("id")
        try:
            blob = studip_download(ref_id)
        except (urllib.error.URLError, RuntimeError) as e:
            print(f"  skipped {name}: {e}", file=sys.stderr)
            continue

        terms = ((ref.get("relationships") or {}).get("terms-of-use") or {}).get("data") or {}
        print(f"  {name}: {len(blob) / 1_000_000:.1f} MB queued for transcription "
              f"[licence: {terms.get('id', 'unknown')}]", file=sys.stderr)
        media.append({
            "activity_ref": f"studip:{course_id}:file:{ref_id}",
            "title": name,
            "content_base64": base64.b64encode(blob).decode("ascii"),
        })
    return media


def fetch_course_documents(course_id: str) -> tuple[str, list[dict]]:
    """Return (course_ref, documents) for a Stud.IP Veranstaltung.

    Indexes, in order of how much substance they usually carry:

      1. **Wiki pages** — on a real course these are the richest source. On the
         HPC test course, ten wiki pages carry ~16 000 characters of prose while
         the course description carries 132.
      2. **Courseware**, one document per chapter, with the chapter title as
         the locator. Often the *only* content: three of six surveyed courses
         are Courseware-only with no files whatsoever.
      3. The course description and subtitle.
      4. News, when the instance exposes them.
      5. **PDF attachments**, one document per page — see `fetch_course_files`.
         This uses the bridge's fallback extractor and runs only because no
         engine covers Stud.IP; on Moodle the ByCS and OSKI work does it
         properly. Other formats are still out of scope.
    """
    docs: list[dict] = []

    course = studip_get(f"/courses/{course_id}")
    attrs = (course.get("data") or {}).get("attributes", {}) or {}
    title = attrs.get("title", "Veranstaltung")

    # 1. Wiki pages — richest source on a real course.
    try:
        wiki = studip_get(f"/courses/{course_id}/wiki-pages?page[limit]=100")
        for page in wiki.get("data", []) or []:
            a = page.get("attributes", {}) or {}
            body = strip_html(a.get("content", ""))
            if len(body) < 40:      # skip stubs
                continue
            docs.append(
                {
                    "activity_ref": f"studip:{course_id}:wiki:{page.get('id')}",
                    "title": strip_html(a.get("name", "")) or "Wiki-Seite",
                    "text": body,
                }
            )
    except RuntimeError:
        pass

    # 2. Courseware — walked properly, one document per chapter.
    #
    # This was previously a JSON dump of the unit payload through strip_html,
    # which indexed colour names and layout keys as though they were course
    # content. It matters more than it looks: of six real courses surveyed,
    # four had Courseware and three of those had **no files at all**.
    docs.extend(fetch_courseware(studip_get, strip_html, course_id))

    # 3. Description and subtitle.
    for field in ("description", "sub-title"):
        text = strip_html(attrs.get(field, ""))
        if len(text) >= 40:
            docs.append(
                {
                    "activity_ref": f"studip:{course_id}:{field}",
                    "title": f"{title} — {field}",
                    "text": text,
                }
            )

    # 4. News, when exposed.
    try:
        news = studip_get(f"/courses/{course_id}/news?page[limit]=50")
        for item in news.get("data", []) or []:
            a = item.get("attributes", {}) or {}
            body = strip_html(a.get("content", ""))
            if body:
                docs.append(
                    {
                        "activity_ref": f"studip:{course_id}:news:{item.get('id')}",
                        "title": strip_html(a.get("title", "")) or "Ankündigung",
                        "text": body,
                    }
                )
    except RuntimeError:
        pass

    # 5. PDF attachments, one document per page.
    docs.extend(fetch_course_files(course_id))

    return f"studip:{course_id}", docs


def fetch_course_ai_rules(course_id: str) -> list[dict]:
    """Read the KI-Toolbox rules for a course, if any.

    The KI-Toolbox exposes per-course AI rules over the JSON-API. This research
    found that mechanism to be the only academic-integrity disclosure feature on
    any of the three platforms — and being machine-readable, it can be *honoured*
    by a tool rather than merely displayed to a student.

    Returns [] when the course has no rules configured (HTTP 404) or when the
    account may not read them (HTTP 403). Both are normal.
    """
    try:
        r = studip_get(f"/courses/{course_id}/kitoolbox-rules")
    except RuntimeError:
        return []
    return [e.get("attributes", {}) for e in (r.get("data") or [])]


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


def index_course(course_id: str, with_media: bool = True) -> dict:
    """Index a course. Text is indexed synchronously; media is queued.

    Media is only collected when the bridge says it can transcribe — asking an
    institution's adapter to upload a lecture recording to a deployment with no
    transcription provider would waste the bandwidth of both.
    """
    course_ref, docs = fetch_course_documents(course_id)
    if not docs:
        raise RuntimeError(f"no indexable text found in Stud.IP course {course_id}")

    media: list[dict] = []
    if with_media and _bridge_can_transcribe():
        media = fetch_course_media(course_id)

    payload = {"course_ref": course_ref, "documents": docs, "replace": True}
    if media:
        payload["media"] = media
    return bridge_post("/v1/index", payload)


def _bridge_can_transcribe() -> bool:
    """Ask the bridge rather than assume. This is what /v1/capabilities is for:
    the same adapter serves deployments with very different infrastructure."""
    try:
        req = urllib.request.Request(
            f"{BRIDGE_URL}/v1/capabilities",
            headers={"Authorization": f"Bearer {BRIDGE_TOKEN}"} if BRIDGE_TOKEN else {},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            caps = json.loads(r.read().decode("utf-8")).get("capabilities", [])
        return "transcription" in caps
    except (urllib.error.URLError, RuntimeError, ValueError):
        return False


def ask(course_id: str, question: str) -> dict:
    return bridge_post(
        "/v1/chat",
        {
            "course_ref": f"studip:{course_id}",
            "messages": [{"role": "user", "content": question}],
            "locale": "de",
        },
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("usage:")
        print("  python3 adapters/studip_adapter.py index <course_id>")
        print("  python3 adapters/studip_adapter.py ask   <course_id> <question>")
        return 2

    cmd = argv[1]
    if cmd == "index" and len(argv) >= 3:
        print(json.dumps(index_course(argv[2]), indent=2, ensure_ascii=False))
        return 0
    if cmd == "ask" and len(argv) >= 4:
        result = ask(argv[2], " ".join(argv[3:]))
        print(result["answer"])
        if result.get("sources"):
            print("\nQuellen:")
            for i, s in enumerate(result["sources"], 1):
                print(f"  [{i}] {s['title']}")
        return 0

    print("unknown command; run without arguments for usage")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
