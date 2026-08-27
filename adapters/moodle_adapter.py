"""Moodle → LMS AI Bridge adapter.

Pulls the content of a Moodle course via Moodle's web-service API and pushes it
to the bridge for indexing.

**Note on scope.** For an institution running `local_ai_manager` plus
`local_ai_content`, this adapter is probably redundant — RAG will live inside
Moodle. That is fine and expected: the bridge earns its place on ILIAS and
Stud.IP, which have no equivalent. This adapter exists so the working group can
see one contract serving all three, and so a Moodle-only institution has a route
that does not require adopting the full ISB stack.

Auth is a Moodle **web-service token**, not a username and password:
  Site administration → Server → Web services → Manage tokens → Create token
Tokens are scoped to a service and revocable; passwords are neither.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from html import unescape

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
MOODLE_URL = os.environ.get("MOODLE_URL", "").rstrip("/")
MOODLE_TOKEN = os.environ.get("MOODLE_TOKEN", "")

_TAG = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    """Moodle returns HTML in most text fields; we index plain text."""
    text = _TAG.sub(" ", html or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def moodle_call(function: str, **params) -> dict | list:
    """Call a Moodle web-service function.

    Moodle's REST endpoint takes flat form-encoded params, so nested lists need
    the `name[0][key]` convention. Only flat params are used here.
    """
    if not MOODLE_URL or not MOODLE_TOKEN:
        raise RuntimeError(
            "Set MOODLE_URL and MOODLE_TOKEN (see .env.example). "
            "Create a token in Moodle: Site administration → Server → "
            "Web services → Manage tokens."
        )
    payload = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": function,
        "moodlewsrestformat": "json",
        **params,
    }
    url = f"{MOODLE_URL}/webservice/rest/server.php"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=60) as r:
        out = json.loads(r.read().decode("utf-8"))
    if isinstance(out, dict) and out.get("exception"):
        raise RuntimeError(
            f"Moodle error in {function}: {out.get('errorcode')} — "
            f"{out.get('message')}"
        )
    return out


# Moodle's own download route. Two things about it are easy to get wrong and
# both cost a working afternoon (verified against a live instance 2026-08-27).
_EXTRACTABLE = re.compile(
    r"\.(pdf|docx?|odt|pptx?|odp|xlsx?|ods|txt|md)$", re.IGNORECASE
)


def with_token(fileurl: str) -> str:
    """Append the web-service token to a `pluginfile.php` URL.

    **`&`, not `?`.** `fileurl` from `core_course_get_contents` already ends in
    `?forcedownload=1`, so `?token=…` builds a second query string and Moodle
    answers **HTTP 200 with a JSON error body** — "A required parameter (token)
    was missing" — while a token is plainly being sent. A 200 carrying an error
    is the same trap as Stud.IP's `Accept` header: the status code cannot be
    trusted to mean what it says.
    """
    separator = "&" if "?" in fileurl else "?"
    # Read at call time rather than from the module constant: the constant is
    # bound at import, which makes this untestable and means a token supplied
    # after import is ignored.
    token = os.environ.get("MOODLE_TOKEN", MOODLE_TOKEN)
    return f"{fileurl}{separator}token={token}"


def moodle_download(fileurl: str) -> bytes:
    """Fetch a course file's bytes.

    Guards against receiving JSON: the route answers 200 with an error body for
    several failures, so "did I get a document" has to be checked rather than
    assumed from the status.
    """
    with urllib.request.urlopen(with_token(fileurl), timeout=120) as response:
        blob = response.read()
        kind = response.headers.get("Content-Type", "")
    if kind.startswith("application/json") or blob[:1] in (b"{", b"["):
        raise RuntimeError(f"expected a file, got {kind or 'JSON'}")
    return blob


def _file_documents(course_id: int, mod: dict) -> list[dict]:
    """Download a module's files and return one document per page or slide.

    Mirrors the Stud.IP adapter: locators are "S. 12" for a PDF page and
    "Folie 4" for a slide, so a citation can be checked. One unreadable file
    costs its own document and never the course — a scanned PDF among twenty
    good ones should not empty the index.
    """
    docs: list[dict] = []
    for content in mod.get("contents", []) or []:
        if content.get("type") != "file":
            continue
        name = content.get("filename", "")
        fileurl = content.get("fileurl")
        if not fileurl or not _EXTRACTABLE.search(name):
            if fileurl:
                print(f"  skipped {name}: no extractor for this format",
                      file=sys.stderr)
            continue

        try:
            blob = moodle_download(fileurl)
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            print(f"  skipped {name}: {exc}", file=sys.stderr)
            continue

        try:
            if blob.startswith(b"%PDF"):
                pages, engine = extract_pdf_pages_with_engine(blob)
                if engine == "builtin":
                    print(f"  {name}: stdlib PDF reader (install poppler-utils "
                          f"for complete extraction)", file=sys.stderr)
                units = [(f"S. {i}", text) for i, text in enumerate(pages, 1)]
            elif office_kind(blob):
                units = extract_office_units(blob)
            else:
                print(f"  skipped {name}: no extractor for this format",
                      file=sys.stderr)
                continue
        except (PdfExtractionError, OfficeExtractionError) as exc:
            print(f"  skipped {name}: {exc}", file=sys.stderr)
            continue

        kept = 0
        for locator, text in units:
            body = re.sub(r"\s+", " ", text).strip()
            # Same floor as the Stud.IP adapter: enough to drop dividers and
            # bare cover pages, low enough to keep a sparse but real slide.
            if len(body) < 15:
                continue
            docs.append(
                {
                    "activity_ref": f"moodle:{course_id}:file:{mod.get('id')}:{name}",
                    "title": name,
                    "locator": locator,
                    "text": body,
                }
            )
            kept += 1
        print(f"  {name}: {kept}/{len(units)} units with text", file=sys.stderr)
    return docs


def fetch_course_documents(course_id: int) -> tuple[str, list[dict]]:
    """Return (course_name, documents) for a Moodle course.

    Uses `core_course_get_contents`, which returns sections and modules with
    their descriptions. This is intentionally shallow: it indexes what Moodle
    exposes as text, not the contents of attached PDFs. Extracting file content
    is real work and is listed as out of scope in the prototype README.

    **Verified against a real instance (Moodle 5.1.3+, 2026-08-24) and the
    limitation is sharper than expected:** many real courses carry almost no
    indexable text at this level. Section summaries and module descriptions are
    often empty, and the substance lives in *attached files* (PDF, PPTX) and in
    activity content that `core_course_get_contents` does not return. On the test
    instance, two of three courses yielded zero documents — the adapter worked,
    the courses were genuinely empty of extractable text.

    This matters for the whole project: a course-grounded assistant that only
    reads section summaries will look impressive on a prepared course and useless
    on a real one. File-content extraction is not a nice-to-have; it is most of
    the value. Worth confirming how ByCS `local_ai_content` and OSKI.nrw handle
    it — OSKI cites filename *and page number*, which implies they parse the
    files themselves.
    """
    sections = moodle_call("core_course_get_contents", courseid=course_id)
    docs: list[dict] = []

    # The course description, which `core_course_get_contents` does not return.
    # Worth a separate call: it is the one text field a lecturer reliably fills
    # in, it usually says what the course is *about* — the thing a student asks
    # first — and without it a course whose material is all attachments has no
    # document describing itself. Failure here is not fatal: a least-privilege
    # token may not carry `core_course_get_courses`, and losing the description
    # is better than losing the course.
    try:
        for course in moodle_call("core_course_get_courses") or []:
            if course.get("id") != course_id:
                continue
            summary = strip_html(course.get("summary", ""))
            if summary:
                docs.append(
                    {
                        "activity_ref": f"moodle:{course_id}:course",
                        "title": strip_html(course.get("fullname", ""))
                        or "Kursbeschreibung",
                        "text": summary,
                    }
                )
            break
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"  no course description: {exc}", file=sys.stderr)

    for section in sections:
        sec_name = strip_html(section.get("name", ""))
        sec_summary = strip_html(section.get("summary", ""))
        if sec_summary:
            docs.append(
                {
                    "activity_ref": f"moodle:{course_id}:section:{section.get('id')}",
                    "title": sec_name or "Abschnitt",
                    "text": sec_summary,
                }
            )
        for mod in section.get("modules", []):
            description = strip_html(mod.get("description", ""))
            if description:
                docs.append(
                    {
                        "activity_ref": f"moodle:{course_id}:module:{mod.get('id')}",
                        "title": strip_html(mod.get("name", "")) or "Aktivität",
                        "text": description,
                    }
                )
            # The files themselves, not their names. Until 2026-08-27 this
            # recorded "[Datei: skript.pdf]" and stopped, so a course whose
            # substance sits in attachments indexed as a list of filenames.
            docs.extend(_file_documents(course_id, mod))

    return f"moodle:{course_id}", docs


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


def index_course(course_id: int) -> dict:
    course_ref, docs = fetch_course_documents(course_id)
    if not docs:
        raise RuntimeError(f"no indexable text found in Moodle course {course_id}")
    return bridge_post(
        "/v1/index",
        {"course_ref": course_ref, "documents": docs, "replace": True},
    )


def ask(course_id: int, question: str) -> dict:
    return bridge_post(
        "/v1/chat",
        {
            "course_ref": f"moodle:{course_id}",
            "messages": [{"role": "user", "content": question}],
            "locale": "de",
        },
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("usage:")
        print("  python3 adapters/moodle_adapter.py index <course_id>")
        print("  python3 adapters/moodle_adapter.py ask   <course_id> <question>")
        return 2

    cmd = argv[1]
    if cmd == "index" and len(argv) >= 3:
        result = index_course(int(argv[2]))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if cmd == "ask" and len(argv) >= 4:
        result = ask(int(argv[2]), " ".join(argv[3:]))
        print(result["answer"])
        if result.get("sources"):
            print("\nQuellen:")
            for i, s in enumerate(result["sources"], 1):
                loc = f", {s['locator']}" if s.get("locator") else ""
                print(f"  [{i}] {s['title']}{loc}")
        return 0

    print("unknown command; run without arguments for usage")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
