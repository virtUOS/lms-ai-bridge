"""ILIAS → LMS AI Bridge adapter.

**UNTESTED.** No ILIAS instance was available while this prototype was written,
so unlike the Moodle and Stud.IP adapters this one has never run against a real
server. It is included so the working group can see the shape of the third
adapter and judge the effort, not because it is known to work.

Saying so plainly is the point: a prototype that quietly looks finished is worse
than one that states what it has not done.

## Why ILIAS is the hardest of the three

The platform research found:

  - **No core AI layer**, so nothing to inherit — same as Stud.IP.
  - **No official REST API in core.** ILIAS ships SOAP; REST comes from the
    widely used but third-party `RESTPlugin`, which an institution must install
    and configure. There is no equivalent of Moodle's web services or Stud.IP 6's
    JSON-API being simply *there*.
  - Annual major releases with real breakage, and an ecosystem that re-forks per
    major version.

So this adapter assumes `RESTPlugin` is installed and reachable. If it is not,
the realistic alternatives are the SOAP interface, or LTI — and LTI is worth
considering seriously, since a tool built once for LTI could serve all three
platforms without three adapters at all. That trade-off is an open question in
`DESIGN.md`.

## What to verify first, on a real instance

1. Is `RESTPlugin` installed, and what is its base path?
2. Which OAuth2 flow does it accept (client credentials, or user grant)?
3. Does `/v1/courses/{ref_id}/contents` — or its equivalent — return the *text*
   of learning modules and pages, or only object metadata? This is the question
   that decided how much the other two adapters could extract, and the answer for
   Moodle was disappointing.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8080")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
ILIAS_URL = os.environ.get("ILIAS_URL", "").rstrip("/")
ILIAS_CLIENT_ID = os.environ.get("ILIAS_CLIENT_ID", "")
ILIAS_CLIENT_SECRET = os.environ.get("ILIAS_CLIENT_SECRET", "")
ILIAS_TOKEN = os.environ.get("ILIAS_TOKEN", "")

_TAG = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html or ""))).strip()


def get_token() -> str:
    """Obtain a bearer token from RESTPlugin's OAuth2 endpoint.

    UNVERIFIED: the grant type and endpoint path vary with RESTPlugin
    configuration.
    """
    if ILIAS_TOKEN:
        return ILIAS_TOKEN
    if not (ILIAS_URL and ILIAS_CLIENT_ID and ILIAS_CLIENT_SECRET):
        raise RuntimeError(
            "Set ILIAS_TOKEN, or ILIAS_URL + ILIAS_CLIENT_ID + "
            "ILIAS_CLIENT_SECRET (see .env.example)."
        )
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": ILIAS_CLIENT_ID,
            "client_secret": ILIAS_CLIENT_SECRET,
        }
    ).encode()
    req = urllib.request.Request(f"{ILIAS_URL}/restplugin.php/v1/oauth2/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def ilias_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{ILIAS_URL}/restplugin.php{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {get_token()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ILIAS {e.code} on {path}: {detail}") from e


def fetch_course_documents(ref_id: str) -> tuple[str, list[dict]]:
    """Return (course_ref, documents) for an ILIAS course object.

    UNTESTED — the response shape below is inferred from RESTPlugin's published
    routes, not observed. Expect to adjust the field names on first contact with
    a real instance.
    """
    docs: list[dict] = []

    course = ilias_get(f"/v1/courses/{ref_id}")
    title = (course.get("title") or "Kurs") if isinstance(course, dict) else "Kurs"

    desc = strip_html((course or {}).get("description", ""))
    if len(desc) >= 40:
        docs.append(
            {
                "activity_ref": f"ilias:{ref_id}:description",
                "title": f"{title} — Beschreibung",
                "text": desc,
            }
        )

    contents = ilias_get(f"/v1/courses/{ref_id}/contents")
    items = contents if isinstance(contents, list) else contents.get("items", []) or []
    for item in items:
        text = strip_html(item.get("description", "") or item.get("content", ""))
        if len(text) < 40:
            continue
        docs.append(
            {
                "activity_ref": f"ilias:{ref_id}:obj:{item.get('ref_id') or item.get('id')}",
                "title": strip_html(item.get("title", "")) or "Objekt",
                "text": text,
            }
        )

    return f"ilias:{ref_id}", docs


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
    print("NOTE: this adapter has never been run against a real ILIAS instance.\n")
    if len(argv) < 3:
        print("usage:")
        print("  python3 adapters/ilias_adapter.py index <ref_id>")
        print("  python3 adapters/ilias_adapter.py ask   <ref_id> <question>")
        return 2
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
    import sys

    raise SystemExit(main(sys.argv))
