"""One-off diagnostic. Shows exactly what Luma returns so the parser can be fixed.

Run it from the project folder:

    python probe_luma.py

Then paste the output back into the chat.
"""

import re
import json
import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def json_blobs(page_html):
    blobs = []
    pattern = r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>'
    for m in re.findall(pattern, page_html, re.S | re.I):
        try:
            blobs.append(json.loads(m.strip()))
        except Exception:
            pass
    for m in re.findall(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                        page_html, re.S | re.I):
        try:
            blobs.append(json.loads(m.strip()))
        except Exception:
            pass
    return blobs


def walk(node, found):
    if isinstance(node, dict):
        name = node.get("name") or node.get("title")
        start = (node.get("start_at") or node.get("startAt")
                 or node.get("start_date") or node.get("startDate")
                 or node.get("startTime"))
        if isinstance(name, str) and isinstance(start, str) and len(name) > 3:
            found.append(node)
        for v in node.values():
            walk(v, found)
    elif isinstance(node, list):
        for v in node:
            walk(v, found)


for city in ("philadelphia", "nyc"):
    url = f"https://luma.com/{city}"
    print("\n" + "=" * 70)
    print("CITY:", city, "->", url)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        print("HTTP", r.status_code, "| page size", len(r.text))
    except Exception as e:
        print("FETCH FAILED:", e)
        continue

    blobs = json_blobs(r.text)
    print("json script blocks found:", len(blobs))

    found = []
    for b in blobs:
        walk(b, found)
    print("event-shaped objects found:", len(found))

    for node in found[:3]:
        print("\n--- sample event object ---")
        print("KEYS:", sorted(node.keys())[:40])
        for k in ("name", "title", "start_at", "startAt", "url", "slug",
                  "api_id", "permalink", "event_url"):
            if k in node:
                v = node[k]
                if isinstance(v, (str, int, float)):
                    print(f"  {k} = {v}")
        loc = node.get("geo_address_info") or node.get("location")
        if isinstance(loc, dict):
            print("  location KEYS:", sorted(loc.keys())[:20])
            print("  location SAMPLE:", json.dumps(loc)[:300])
        elif loc:
            print("  location =", loc)

    if not found:
        snippet = r.text[:600].replace("\n", " ")
        print("\nNo event objects. First 600 chars of the page:")
        print(snippet)

print("\nDone. Paste everything above into the chat.")
