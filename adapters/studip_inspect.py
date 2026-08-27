"""Inspect one Stud.IP course: what text is extractable, and its KI-Toolbox config.

    python3 adapters/studip_inspect.py <course_id>

Prompts for the password; nothing is stored.
"""
from __future__ import annotations
import base64, getpass, json, os, re, sys, urllib.error, urllib.request
from html import unescape

BASE = os.environ.get("STUDIP_URL", "https://studip-test.uni-osnabrueck.de").rstrip("/")
_TAG = re.compile(r"<[^>]+>")

def strip_html(h): return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", h or ""))).strip()

def api(path, auth):
    req = urllib.request.Request(f"{BASE}/jsonapi.php/v1{path}",
        headers={"Accept": "application/vnd.api+json", "Authorization": auth})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode("utf-8", "replace")[:160]}

def main():
    cid = sys.argv[1] if len(sys.argv) > 1 else input("course id: ").strip()
    user = os.environ.get("STUDIP_USER") or input("Stud.IP username: ").strip()
    pw = os.environ.get("STUDIP_PASSWORD") or getpass.getpass("Stud.IP password: ")
    auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    c = api(f"/courses/{cid}", auth)
    a = (c.get("data") or {}).get("attributes", {})
    print(f"\ncourse: {a.get('title','?')}")
    for f in ("description", "sub-title"):
        t = strip_html(a.get(f, ""))
        print(f"  {f:<12} {len(t):>5} chars" + (f"  “{t[:70]}…”" if t else ""))

    print("\n-- courseware units --")
    cw = api(f"/courses/{cid}/courseware-units?page[limit]=20", auth)
    if cw.get("_error"): print("  HTTP", cw["_error"])
    else:
        for u in cw.get("data", []):
            ua = u.get("attributes", {})
            print(f"  unit {u.get('id')}  {str(ua.get('title',''))[:50]}")
            st = api(f"/courseware-units/{u.get('id')}/structural-element", auth)
            if not st.get("_error"):
                sa = (st.get("data") or {}).get("attributes", {})
                print(f"       root element: {sa.get('title','')!r}  payload={len(json.dumps(sa.get('payload') or {}))}b")

    print("\n-- files --")
    fr = api(f"/courses/{cid}/file-refs?page[limit]=20", auth)
    if fr.get("_error"): print("  HTTP", fr["_error"])
    else:
        for f in fr.get("data", []):
            fa = f.get("attributes", {})
            print(f"  {str(fa.get('name',''))[:50]:<50} {fa.get('mime-type','')}")

    print("\n-- wiki --")
    wp = api(f"/courses/{cid}/wiki-pages?page[limit]=10", auth)
    if wp.get("_error"): print("  HTTP", wp["_error"])
    else:
        for w in wp.get("data", []):
            wa = w.get("attributes", {})
            print(f"  {str(wa.get('name',''))[:40]:<40} {len(strip_html(wa.get('content','')))} chars")

    print("\n-- KI-Toolbox for this course --")
    for label, path in (("rules", f"/courses/{cid}/kitoolbox-rules"),
                        ("course-tools", f"/courses/{cid}/kitoolbox-course-tools")):
        r = api(path, auth)
        if r.get("_error"):
            print(f"  {label:<14} HTTP {r['_error']} {r.get('_body','')[:80]}")
        else:
            print(f"  {label:<14} {len(r.get('data', []))} entries")
            for e in r.get("data", [])[:5]:
                print(f"     {json.dumps(e.get('attributes', {}), ensure_ascii=False)[:150]}")

if __name__ == "__main__":
    main()
