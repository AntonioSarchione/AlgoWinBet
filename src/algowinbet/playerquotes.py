"""Sisal player prices from the OddsPapi snapshots we already download (no extra request): goalscorer, first goalscorer, two
or more goals, shots and shots on target over/under. The dashboard's Giocatori tab shows them next to our probability.

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
}


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
        line = _line_key(m.get("handicap")) if code in ("SHOTS", "SHOTS_ON") else ""
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
    """Replaces the fixture's rows of this bookmaker with the snapshot's (none: left as they are, a snapshot without player
    markets is not proof that Sisal withdrew them) and drops rows older than KEEP_DAYS."""
    db = store.db
    db.execute(SCHEMA)
    db.execute("DELETE FROM player_quotes WHERE observed_at < ?", ((observed_at - timedelta(days=KEEP_DAYS)).isoformat(),))
    if quotes:
        db.execute("DELETE FROM player_quotes WHERE fixture_id = ? AND bookmaker = ?", (fixture_id, book))
        db.executemany("INSERT OR REPLACE INTO player_quotes VALUES (?,?,?,?,?,?,?,?)",
                       [(fixture_id, book, q.market, q.line_key, q.player_key, q.player_name, q.odds, observed_at.isoformat())
                        for q in quotes])
    db.commit()
    return len(quotes)
