"""Pretty-print a /v1/chat response for the demo script."""
import json
import sys

r = json.load(sys.stdin)
print("  " + r["answer"].replace("\n", "\n  "))
if r.get("sources"):
    print("\n  Quellen:")
    for i, s in enumerate(r["sources"], 1):
        print("    [%d] %s  (score %s)" % (i, s["title"], s.get("score")))
else:
    print("\n  (keine Quellen — nichts Passendes im Kurs gefunden)")
