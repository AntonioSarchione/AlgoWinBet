"""Team names seen in every source next to the GOAL name they belong to (no request), so that aliases come from what the
sources actually wrote on matches we linked, never from a guess.

GOAL is the reference: its fixtures and results name the clubs our model, our history and the dashboard use. For each
other source the pairs (its spelling -> GOAL name) are read off matches linked to a GOAL match:
  - OddsPapi: the stored /fixtures pages and the latest /odds-by-tournaments rows, through fixture_links;
  - FotMob: the stored season pages, through fixture_links (and fotmob_prematch for coming matches);
  - API-Football: apif_teams (its name and the GOAL name it was linked to);
  - football-data: the stored season files, the file name -> our name mapping learnt the way the collector does;
  - international results (national teams): the GOAL names that file does not know.
A spelling that TeamNames.canon does not already turn into the GOAL name is an alias to add; one source spelling linked
to two GOAL names, or a club whose results sit under two names, is a link or history problem to look at."""
from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .names import TeamNames, normalize
from .snapshots import SnapshotStore

SOURCES = ("oddspapi", "fotmob", "api-football", "football-data")
GOAL_SOURCES = ("goal-api",)


def _data(x):
    return x.get("data", x) if isinstance(x, dict) else x


def _sim(a: str, b: str) -> float:
    import difflib
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


class TeamMap:
    def __init__(self, store: SnapshotStore, names: TeamNames):
        self.store, self.names = store, names
        self.matches: dict[str, tuple[str, str, str]] = {}  # fixture id -> (competition, home, away) as stored
        self.goal: dict[str, Counter] = defaultdict(Counter)  # competition -> GOAL name -> matches
        self.other_results: dict[str, Counter] = defaultdict(Counter)  # competition -> name in results of another source
        self.pairs: dict[str, Counter] = defaultdict(Counter)  # source -> (its spelling, GOAL name, competition) -> matches
        self.unlinked: dict[str, Counter] = defaultdict(Counter)  # source -> spelling seen on no linked match
        self.notes: list[str] = []

    def _has(self, table: str) -> bool:
        return bool(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def _links(self, source: str) -> dict[str, str]:
        if not self._has("fixture_links"):
            return {}
        return {str(e): f for e, f in self.store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source=?", (source,)).fetchall()}

    def _pair(self, source: str, spelled: str | None, fid: str, side: int) -> None:
        m = self.matches.get(fid)
        if not spelled or not m:
            return
        self.pairs[source][(spelled.strip(), m[side], m[0])] += 1

    # ------------------------------------------------------------------ reference
    def load_goal(self) -> None:
        for fid, src, comp, h, a in self.store.db.execute(
                "SELECT fixture_id, source, competition, home, away FROM results UNION ALL "
                "SELECT fixture_id, source, competition, home, away FROM fixtures").fetchall():
            self.matches.setdefault(fid, (comp, h, a))
            if src in GOAL_SOURCES:
                self.goal[comp][h] += 1
                self.goal[comp][a] += 1
            else:
                self.other_results[comp][h] += 1
                self.other_results[comp][a] += 1

    # ------------------------------------------------------------------ sources
    def load_oddspapi(self, days: int = 3) -> None:
        links = self._links("oddspapi")
        def latest(endpoint):
            r = self.store.db.execute("SELECT id FROM raw_requests WHERE source='oddspapi' AND endpoint=? AND status=200 ORDER BY id DESC LIMIT 1",
                                      (endpoint,)).fetchone()
            return _data(json.loads(self.store.raw_body(r[0]))) if r else None
        parts = {str(p.get("participantId")): p.get("participantName") for p in (latest("/participants") or []) if isinstance(p, dict)}
        rows = []
        for (rid,) in self.store.db.execute("SELECT MAX(id) FROM raw_requests WHERE source='oddspapi' AND endpoint='/fixtures' AND status=200 "
                                            "GROUP BY params").fetchall():
            rows += _data(json.loads(self.store.raw_body(rid))) or []
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        for (rid,) in self.store.db.execute("SELECT id FROM raw_requests WHERE source='oddspapi' AND endpoint='/odds-by-tournaments' AND status=200 "
                                            "AND fetched_at >= ?", (since,)).fetchall():
            body = _data(json.loads(self.store.raw_body(rid)))
            rows += body if isinstance(body, list) else [body]
        seen = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            ext = str(r.get("fixtureId"))
            sides = [r.get(f"participant{i}Name") or parts.get(str(r.get(f"participant{i}Id"))) for i in (1, 2)]
            if (ext, *sides) in seen:
                continue
            seen.add((ext, *sides))
            if ext in links:
                self._pair("oddspapi", sides[0], links[ext], 1)
                self._pair("oddspapi", sides[1], links[ext], 2)
            elif (r.get("startTime") or "") >= since[:10]:
                for s in sides:
                    if s:
                        self.unlinked["oddspapi"][s] += 1

    def load_fotmob(self, collector) -> None:
        links = self._links("fotmob")
        if self._has("fotmob_prematch"):
            links.update({str(e): f for f, e in self.store.db.execute("SELECT fixture_id, ext_id FROM fotmob_prematch WHERE ext_id IS NOT NULL").fetchall()})
        for lg in collector.leagues:
            for _, (_, page) in collector._stored_pages(lg).items():
                for m in page.get("matches") or []:
                    ext = str(m.get("id"))
                    h, a = (m.get("home") or {}).get("name"), (m.get("away") or {}).get("name")
                    if ext in links:
                        self._pair("fotmob", h, links[ext], 1)
                        self._pair("fotmob", a, links[ext], 2)

    def load_apif(self) -> None:
        if not self._has("apif_teams"):
            return
        comp_of: dict[str, Counter] = defaultdict(Counter)
        for comp, names in self.goal.items():
            for n in names:
                comp_of[n][comp] += 1
        for name, api_name in self.store.db.execute("SELECT name, apif_name FROM apif_teams WHERE name IS NOT NULL").fetchall():
            comp = comp_of[name].most_common(1)[0][0] if comp_of.get(name) else "?"
            self.pairs["api-football"][(api_name or "", name, comp)] += 1

    def load_football_data(self, collector, seasons: int = 2) -> None:
        from .fdcollector import _kickoff
        by_div: dict[str, list] = defaultdict(list)
        for rid, endpoint in self.store.db.execute("SELECT MAX(id), endpoint FROM raw_requests WHERE source='football-data' AND status=200 "
                                                   "AND endpoint LIKE '/mmz4281/%' GROUP BY endpoint").fetchall():
            div = endpoint.rsplit("/", 1)[-1].removesuffix(".csv")
            by_div[div].append((endpoint, rid))
        cal = collector._calendar()
        for div, files in sorted(by_div.items()):
            comp = collector.divisions.get(div)
            if not comp:
                continue
            for _, rid in sorted(files, reverse=True)[:seasons]:
                body = self.store.raw_body(rid)
                rows = []
                for row in csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace"))):
                    ko = _kickoff(row) if row.get("HomeTeam") and row.get("AwayTeam") else None
                    if ko is not None:
                        rows.append((row["HomeTeam"].strip(), row["AwayTeam"].strip(), ko, collector._near(cal, ko)))
                mapping = collector._learn(rows)
                counts = Counter(t for h, a, _, _ in rows for t in (h, a))
                for spelled, n in counts.items():
                    if spelled in mapping:
                        self.pairs["football-data"][(spelled, mapping[spelled], comp)] += n
                    else:
                        self.unlinked["football-data"][f"{spelled} ({comp})"] += n

    def national_unknown(self) -> list[str]:
        from .elo import latest_international, national_timeline
        from .meta import group_of
        body = latest_international(self.store)
        if not body:
            return []
        known = national_timeline(body, self.names).teams()
        teams = {t for comp, names in self.goal.items() if group_of(comp) == "nazionali" for t in names}
        out = []
        for t in sorted(teams - known):
            best = max(known, key=lambda k: _sim(t, k), default=None)
            out.append(f"{t} (più vicino nel file: {best})" if best else t)
        return out

    # ------------------------------------------------------------------ verdicts
    def aliases_to_add(self) -> dict[str, set[str]]:
        """GOAL name -> source spellings TeamNames.canon does not already turn into it (each seen on a linked match)."""
        out: dict[str, set[str]] = defaultdict(set)
        for src, pairs in self.pairs.items():
            for (spelled, goal, _), _ in pairs.items():
                if spelled and self.names.canon(spelled) != goal:
                    out[goal].add(spelled)
        return out

    def conflicts(self) -> list[str]:
        """One source spelling linked to two GOAL names (a wrong link or two clubs with one name), and alias clashes: a
        spelling whose normal form is already the alias or the name of another club."""
        out = []
        for src, pairs in self.pairs.items():
            by_spelling: dict[str, Counter] = defaultdict(Counter)
            for (spelled, goal, _), n in pairs.items():
                by_spelling[normalize(spelled)][goal] += n
            for sp, goals in by_spelling.items():
                if len(goals) > 1:
                    out.append(f"{src}: '{sp}' collegato a " + ", ".join(f"{g} ({n})" for g, n in goals.most_common()))
        for goal, spellings in self.aliases_to_add().items():
            for s in spellings:
                c = self.names.canon(s)
                if c != s.strip() and c != goal:
                    out.append(f"alias: '{s}' oggi diventa '{c}', ma sulle partite collegate è {goal}")
        return out

    def split_history(self) -> list[str]:
        """Names in results of another source (FotMob results, ...) that GOAL never uses in that competition but that look
        like a GOAL club of it: the same club under two names, its history split."""
        out = []
        for comp, names in self.other_results.items():
            goal = self.goal.get(comp, Counter())
            for n, k in names.items():
                if n in goal:
                    continue
                best = max(goal, key=lambda g: _sim(n, g), default=None)
                if best and _sim(n, best) >= 0.6:
                    out.append(f"{comp}: '{n}' ({k} presenze) e '{best}' di GOAL")
        return out


def print_team_map(tm: TeamMap, show_all: bool = False) -> dict[str, list[str]]:
    by_comp: dict[str, dict[str, dict[str, Counter]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(Counter)))
    for src, pairs in tm.pairs.items():
        for (spelled, goal, comp), n in pairs.items():
            by_comp[comp][goal][src][spelled] += n
    add = tm.aliases_to_add()
    for comp in sorted(set(tm.goal) | set(by_comp)):
        teams = sorted(set(tm.goal.get(comp, {})) | set(by_comp.get(comp, {})))
        if not teams:
            continue
        lines = []
        for t in teams:
            per = by_comp.get(comp, {}).get(t, {})
            cells = []
            for src in SOURCES:
                sp = per.get(src)
                if not sp:
                    cells.append(f"{src}: –")
                    continue
                cells.append(f"{src}: " + " / ".join(f"{s}{'' if tm.names.canon(s) == t else ' [ALIAS]'}" for s, _ in sp.most_common()))
            if show_all or any(s in add.get(t, ()) for src in per for s in per[src]) or t not in tm.goal.get(comp, {}):
                lines.append(f"  {t}{'' if t in tm.goal.get(comp, {}) else ' (non in GOAL)'} | " + " | ".join(cells))
        missing = {src: sum(1 for t in tm.goal.get(comp, {}) if not by_comp.get(comp, {}).get(t, {}).get(src)) for src in SOURCES}
        print(f"\n[{comp}] {len(tm.goal.get(comp, {}))} squadre GOAL; senza nome visto: " + ", ".join(f"{s} {n}" for s, n in missing.items()))
        for ln in lines:
            print(ln)
    for src, names in tm.unlinked.items():
        if names:
            print(f"\n{src}: nomi mai visti su una partita collegata ({len(names)}): " + ", ".join(f"{n} ({k})" for n, k in names.most_common(40)))
    conf = tm.conflicts()
    print(f"\nconflitti ({len(conf)}):" + ("".join(f"\n  {c}" for c in conf) if conf else " nessuno"))
    split = tm.split_history()
    print(f"storico diviso tra due nomi ({len(split)}):" + ("".join(f"\n  {s}" for s in split) if split else " nessuno"))
    nat = tm.national_unknown()
    print(f"nazionali che il file dei risultati internazionali non conosce ({len(nat)}):" + ("".join(f"\n  {n}" for n in nat) if nat else " nessuna"))
    out = {goal: sorted(s) for goal, s in sorted(add.items())}
    print(f"\nalias da aggiungere: {sum(len(v) for v in out.values())} nomi per {len(out)} squadre")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out
