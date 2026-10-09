"""Sisal player prices from the OddsPapi snapshots we already download (no extra request): goalscorer, first goalscorer, two
or more goals, assist, shots, shots on target and fouls committed over/under (no booking market in Sisal's feed, 2026-10-09). The dashboard's Giocatori tab shows them next to our probability.

Only the latest price is kept (no price path): each snapshot of a fixture replaces that bookmaker's rows of the fixture, and
rows not refreshed for KEEP_DAYS days (matches played, players dropped from the list) are deleted. OddsPapi names players
"Surname, Name"; they are matched to ours on the dashboard, within the two teams of the match.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

SCHEMA = """CREATE TABLE IF NOT EXISTS player_quotes(fixture_id TEXT, bookmaker TEXT, market TEXT, line_key TEXT, player_key TEXT,
  player_name TEXT, odds REAL, observed_at TEXT, PRIMARY KEY(fixture_id, bookmaker, market, line_key, player_key));"""
KEEP_DAYS = 3

# OddsPapi marketType -> our market code; the outcome kept per type (lowercase name); totals keep only "over"
PLAYER_TYPES = {
    "players-anytimegoalscorer": ("SCORER", None),
    "players-firstgoalscorer": ("FIRST_SCORER", None),
    "players-goals": ("TWO_PLUS", "2+"),
    "playertotals-shots": ("SHOTS", "over"),
    "playertotals-shotsongoal": ("SHOTS_ON", "over"),
    "players-assists": ("ASSIST", "1+"),
    "playertotals-foulscommitted": ("FOULS", "over"),
}
LINED = ("SHOTS", "SHOTS_ON", "FOULS")  # over/under markets: one row per line


@dataclass(frozen=True)
class PlayerQuote:
    market: str
    line_key: str  # "" or the over line ("0.5", "1.5"...)
    player_key: str
    player_name: str
    odds: float


def _line_key(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{x:g}"


def player_odds(payload: dict, markets: dict[int, dict], book: str) -> list[PlayerQuote]:
    """Player prices of one bookmaker in one fixture object of /odds-by-tournaments. markets: the OddsPapi catalogue by id with
    "_out" = {outcome id: outcome name} (OddsPapiMapper.markets). Inactive prices and markets are skipped."""
    bdata = (payload.get("bookmakerOdds") or {}).get(book)
    if not isinstance(bdata, dict) or bdata.get("suspended"):
        return []
    out: list[PlayerQuote] = []
    for mid_s, mdata in (bdata.get("markets") or {}).items():
        try:
            m = markets.get(int(mid_s))
        except ValueError:
            continue
        if not m or not m.get("playerProp") or not isinstance(mdata, dict) or mdata.get("marketActive") is False:
            continue
        kind = PLAYER_TYPES.get(str(m.get("marketType") or "").lower())
        if kind is None:
            continue
        code, want = kind
        line = _line_key(m.get("handicap")) if code in LINED else ""
        for oid_s, odata in (mdata.get("outcomes") or {}).items():
            try:
                name = m["_out"].get(int(oid_s), "").strip().lower()
            except (KeyError, ValueError):
                continue
            if want is not None and name != want:
                continue
            for pkey, p in ((odata or {}).get("players") or {}).items():
                if pkey == "0" or not isinstance(p, dict) or p.get("active") is False:
                    continue
                price, pname = p.get("price"), str(p.get("playerName") or "").strip()
                if not isinstance(price, (int, float)) or price <= 1.0 or not pname:
                    continue
                out.append(PlayerQuote(code, line, str(pkey), pname, float(price)))
    return out


def save_player_quotes(store, fixture_id: str, book: str, quotes: list[PlayerQuote], observed_at: datetime) -> int:
    """Brings the fixture's rows of this bookmaker to the snapshot's (none: left as they are, a snapshot without player markets
    is not proof that Sisal withdrew them) and drops rows older than KEEP_DAYS. Reads are local (replica) and every write
    statement is a round trip to the primary: only changed prices are written, many rows per statement. Returns the rows
    written."""
    db = store.db
    if not getattr(store, "_player_quotes_ready", False):  # once per run: a CREATE is a write, a round trip to the primary
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'player_quotes'").fetchone():
            db.execute(SCHEMA)
        store._player_quotes_ready = True
    at = observed_at.isoformat()
    cut = (observed_at - timedelta(days=KEEP_DAYS)).isoformat()
    wrote = False
    if db.execute("SELECT 1 FROM player_quotes WHERE observed_at < ? LIMIT 1", (cut,)).fetchone():
        db.execute("DELETE FROM player_quotes WHERE observed_at < ?", (cut,))
        wrote = True
    rows: list[tuple] = []
    if quotes:
        new = {(q.market, q.line_key, q.player_key): q for q in quotes}
        old = {(m, l, k): (o, t) for m, l, k, o, t in db.execute(
            "SELECT market, line_key, player_key, odds, observed_at FROM player_quotes WHERE fixture_id = ? AND bookmaker = ?",
            (fixture_id, book)).fetchall()}
        gone = [k for k in old if k not in new]
        for i in range(0, len(gone), 100):
            part = gone[i:i + 100]
            db.execute("DELETE FROM player_quotes WHERE fixture_id = ? AND bookmaker = ? AND (" + " OR ".join(
                ["(market = ? AND line_key = ? AND player_key = ?)"] * len(part)) + ")", [fixture_id, book, *[v for k in part for v in k]])
            wrote = True
        rows = [(fixture_id, book, q.market, q.line_key, q.player_key, q.player_name, q.odds, at)
                for k, q in new.items() if old.get(k, (None, ""))[0] != q.odds]
        # unchanged prices keep their row; their time moves on (so KEEP_DAYS counts from the last snapshot) once a day at most,
        # not at every snapshot: one statement per match and snapshot would add minutes to a run
        day = (observed_at - timedelta(days=1)).isoformat()
        if any(k in new and t < day for k, (_, t) in old.items()):
            db.execute("UPDATE player_quotes SET observed_at = ? WHERE fixture_id = ? AND bookmaker = ? AND observed_at < ?",
                       (at, fixture_id, book, day))
            wrote = True
        if rows:
            store._bulk("INSERT OR REPLACE INTO player_quotes(fixture_id, bookmaker, market, line_key, player_key, player_name, odds, "
                        "observed_at)", rows)  # commits
            wrote = False
    if wrote:
        db.commit()
    return len(rows)
