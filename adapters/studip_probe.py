"""Interactive probe for the Stud.IP JSON-API.

Prompts for your password (never echoed, never stored, never logged) and
reports what your account can actually see. Run it yourself:

    python3 adapters/studip_probe.py

Confirms the auth model and finds a course worth indexing, without anyone
needing admin rights or a shared credential.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get(
    "STUDIP_URL", "https://studip-test.uni-osnabrueck.de"
).rstrip("/")


def api(path: str, auth: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}/jsonapi.php/v1{path}",
        headers={"Accept": "application/vnd.api+json", "Authorization": auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode("utf-8", "replace")[:200]}


def main() -> int:
    user = os.environ.get("STUDIP_USER") or input("Stud.IP username: ").strip()
    pw = os.environ.get("STUDIP_PASSWORD") or getpass.getpass("Stud.IP password: ")
    auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    me = api("/users/me", auth)
    if me.get("_error"):
        print(f"login failed: HTTP {me['_error']} {me.get('_body','')}")
        return 1

    attrs = (me.get("data") or {}).get("attributes", {})
    uid = (me.get("data") or {}).get("id")
    print(f"\nlogged in as {attrs.get('formatted-name') or user}  (id {uid})")

    courses = api(f"/users/{uid}/courses?page[limit]=50", auth)
    data = courses.get("data", []) or []
    print(f"visible courses: {len(data)}\n")

    for c in data:
        a = c.get("attributes", {})
        cid = c.get("id")
        title = a.get("title", "")
        counts = []
        for label, path in (
            ("wiki", f"/courses/{cid}/wiki-pages?page[limit]=1"),
            ("files", f"/courses/{cid}/file-refs?page[limit]=1"),
            ("news", f"/courses/{cid}/news?page[limit]=1"),
            ("courseware", f"/courses/{cid}/courseware-units?page[limit]=1"),
        ):
            r = api(path, auth)
            if r.get("_error"):
                counts.append(f"{label}:-")
            else:
                total = (r.get("meta") or {}).get("total")
                counts.append(f"{label}:{total if total is not None else len(r.get('data', []))}")
        print(f"  {cid}  {title[:44]:<44} {'  '.join(counts)}")

    print("\nKI-Toolbox routes visible to this account:")
    for label, path in (
        ("tools", "/kitoolbox-tools"),
        ("course-tools", "/kitoolbox-course-tools"),
        ("rules", "/kitoolbox-rules"),
    ):
        r = api(path, auth)
        if r.get("_error"):
            print(f"  {label:<14} HTTP {r['_error']}")
        else:
            print(f"  {label:<14} {len(r.get('data', []))} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
