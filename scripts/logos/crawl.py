"""Index football-logos.cc: every logo card (country, id, display name, 256px PNG). Polite: 1 request per second."""
import html, json, re, sys, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (AlgoWinBet personal dashboard; logo index)"}
CARD = re.compile(r'data-category-id="([^"]+)" data-logo-id="([^"]+)".*?<img src="(https://assets\.football-logos\.cc/logos/[^"]+/256x256/[^"]+\.png)".*?<h3[^>]*>\s*(.*?)\s*</h3>', re.S)

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")

allp = get("https://football-logos.cc/all/")
countries = sorted(set(re.findall(r'href="/([a-z0-9-]+)/"', allp)) - {"all", "collections", "dls", "license", "map", "new", "request", "tournaments"})
countries.append("national-teams")
index = {}
for k, c in enumerate(countries):
    try:
        page = get(f"https://football-logos.cc/{c}/")
    except Exception as e:
        print("ERR", c, e, file=sys.stderr); continue
    n = 0
    for cat, lid, url, name in CARD.findall(page):
        index.setdefault(f"{cat}/{lid}", {"country": cat, "id": lid, "name": html.unescape(re.sub(r"<[^>]+>", "", name)).strip(), "png256": url, "page": c})
        n += 1
    print(f"{k+1}/{len(countries)} {c}: {n}", flush=True)
    time.sleep(1.0)
json.dump(index, open("index.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print("logos:", len(index))
