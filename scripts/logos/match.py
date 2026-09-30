"""Match our team names (GOAL naming) to football-logos.cc ids. Country-restricted for league clubs, national-teams page
for Nations League, anywhere for European cups. Manual aliases for abbreviations the fuzzy match cannot guess."""
import difflib, json, re, sys, unicodedata

COMP_COUNTRY = {"Serie A": "italy", "Premier League": "england", "Bundesliga": "germany", "Ligue 1": "france", "La Liga": "spain",
                "Primeira Liga": "portugal", "Eredivisie": "netherlands"}
DROP = {"fc", "cf", "ac", "afc", "sc", "sco", "cd", "ud", "sv", "club", "calcio", "de", "of", "the", "and", "fk", "sk", "if", "bk", "vfb",
        "vfl", "tsg", "national", "team", "rc", "ss", "us", "as", "ssc", "acf", "1909", "1913", "1907", "1900", "1899", "1846", "04", "05", "fußball"}
ALIASES = json.load(open("aliases.json", encoding="utf-8"))

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("'", ""))
    return " ".join(w for w in s.split() if w not in DROP)

index = json.load(open("index.json", encoding="utf-8"))
nat = {k: v for k, v in index.items() if v["id"].endswith("national-team")}
teams = []
for line in open("teams.txt", encoding="utf-8"):
    if line.startswith("TEAM|"):
        _, name, comps = line.rstrip("\n").split("|")
        teams.append((name, comps.split(";")))

def candidates(comps):
    if comps == ["UEFA Nations League"]:
        return nat
    countries = {COMP_COUNTRY[c] for c in comps if c in COMP_COUNTRY}
    if countries:
        return {k: v for k, v in index.items() if v["country"] in countries and k not in nat}
    return {k: v for k, v in index.items() if k not in nat}

out, missing = {}, []
for name, comps in teams:
    cand = candidates(comps)
    if name in ALIASES:
        hit = next((k for want in ALIASES[name] for k in (index if "/" in want else cand) if (k == want if "/" in want else k.split("/", 1)[1] == want)), None)
        if hit:
            out[name] = hit
            continue
        print("ALIAS NOT IN INDEX", name, ALIASES[name], file=sys.stderr)
    n = norm(name)
    keys = {k: {norm(v["name"]), norm(v["id"].replace("-", " "))} for k, v in cand.items() if "--" not in v["id"]}
    exact = [k for k, ns in keys.items() if n in ns]
    if exact:
        # several clubs share a short name (Brest / AS Brest): prefer the id that is exactly the name, then the shortest id
        slug = n.replace(" ", "-")
        out[name] = sorted(exact, key=lambda k: (k.split("/", 1)[1] != slug, len(k)))[0]; continue
    scored = sorted(((max(difflib.SequenceMatcher(None, n, x).ratio() for x in ns), k) for k, ns in keys.items()), reverse=True)
    if scored and scored[0][0] >= 0.86 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05):
        out[name] = scored[0][1]
    else:
        missing.append((name, comps, [(round(s, 2), k) for s, k in scored[:3]]))
json.dump(out, open("matches.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"abbinate {len(out)}/{len(teams)}")
for m in missing:
    print("MISSING", m)
