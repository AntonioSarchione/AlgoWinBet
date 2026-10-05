"""Match events from GOAL `/fixtures/{id}/events` (Fase 9, goalscorers): who scored, when, penalty or own goal, assist.

Verified on a real payload (2026-10-05): a list of rows {time, timeNum, type 'GOAL', homeScorer / awayScorer (+Id), homeAssist /
awayAssist (+Id), score '0 - 1', info 'Penalty', scoreInfoTime '2nd Half'}. The scorer id is the `playerKey` of the GOAL lineups,
not their `playerId`: a lineup payload read together with the events maps one to the other (player_keys table); for matches whose
lineup was read earlier the scorer is matched by name among the players of that team's XI and bench.
Rows other than goals are kept as they come (kind = type), so a later model can use them without new requests.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

SOURCE = "goal-api"


@dataclass
class Event:
    seq: int
    minute: float | None
    team: str | None  # home / away team name
    kind: str  # GOAL, PENALTY, OWN_GOAL, or the payload type for anything else
    detail: str | None
    player_key: str | None
    player_name: str | None
    assist_key: str | None
    assist_name: str | None
    player_id: str | None = None
    assist_id: str | None = None


def _s(v) -> str | None:
    v = None if v is None else str(v).strip()
    return v or None


def _first(r: dict, *keys: str) -> str | None:
    for k in keys:
        v = _s(r.get(k))
        if v:
            return v
    return None


CARD_KEYS = {"name": ("{s}Fault", "{s}Player", "{s}PlayerName", "{s}Scorer"), "key": ("{s}FaultId", "{s}PlayerId", "{s}PlayerKey", "{s}ScorerId")}


def parse_events(data, home: str, away: str) -> list[Event]:
    """Goals verified on a real payload; cards read from the fields the underlying feed uses for them (homeFault / awayFault +
    card 'yellow card' / 'red card'), unverified until the first card rows are stored: event_reads keeps every payload, so
    `events-remap` maps them again if the names differ."""
    rows = data if isinstance(data, list) else (data or {}).get("events") if isinstance(data, dict) else None
    out: list[Event] = []
    for i, r in enumerate(rows or []):
        if not isinstance(r, dict):
            continue
        typ = str(r.get("type") or "").upper()
        info = _s(r.get("info"))
        card = _s(r.get("card")) or (typ if "CARD" in typ else None)
        try:
            minute = float(r.get("timeNum") if r.get("timeNum") is not None else str(r.get("time") or "").split("+")[0])
        except (TypeError, ValueError):
            minute = None
        if card:
            low = f"{card} {info or ''}".lower()
            side = next((x for x in ("home", "away") if any(_s(r.get(k.format(s=x))) for k in CARD_KEYS["name"] + CARD_KEYS["key"])), None)
            kind = "CARD_RED" if "red" in low or "second" in low else "CARD_YELLOW"
            out.append(Event(seq=i, minute=minute, team=home if side == "home" else away if side == "away" else None, kind=kind,
                             detail=card if not info else f"{card} | {info}",
                             player_key=_first(r, *(k.format(s=side) for k in CARD_KEYS["key"])) if side else None,
                             player_name=_first(r, *(k.format(s=side) for k in CARD_KEYS["name"])) if side else None,
                             assist_key=None, assist_name=None))
            continue
        side = "home" if _s(r.get("homeScorer")) or _s(r.get("homeScorerId")) else "away" if _s(r.get("awayScorer")) or _s(r.get("awayScorerId")) else None
        kind = typ
        if typ == "GOAL":
            low = (info or "").lower()
            kind = "OWN_GOAL" if "own" in low else "PENALTY" if "penalty" in low and "miss" not in low else "GOAL"
        out.append(Event(seq=i, minute=minute, team=home if side == "home" else away if side == "away" else None, kind=kind, detail=info,
                         player_key=_s(r.get(f"{side}ScorerId")) if side else None, player_name=_s(r.get(f"{side}Scorer")) if side else None,
                         assist_key=_s(r.get(f"{side}AssistId")) if side else None, assist_name=_s(r.get(f"{side}Assist")) if side else None))
    return out


def goals_of(events: list[Event]) -> int:
    return sum(e.kind in ("GOAL", "PENALTY", "OWN_GOAL") for e in events)


def lineup_keys(data) -> dict[str, str]:
    """playerKey -> 'goal:{playerId}' from a GOAL lineup payload."""
    out: dict[str, str] = {}
    for key in ("home", "away"):
        side = data.get(key) if isinstance(data, dict) else None
        if not isinstance(side, dict):
            continue
        for p in (side.get("startingLineups") or []) + (side.get("substitutes") or []):
            if isinstance(p, dict) and p.get("playerKey") and p.get("playerId"):
                out[str(p["playerKey"])] = f"goal:{p['playerId']}"
    return out


_LETTERS = str.maketrans({"ø": "o", "æ": "ae", "ß": "ss", "ł": "l", "đ": "d", "ı": "i", "œ": "oe", "þ": "th", "ð": "d"})


def clean(name: str) -> str:
    """'Rasmus Højlund' -> 'rasmus hojlund'; 'R. Hojlund' -> 'r. hojlund' (initials keep their dot)."""
    t = unicodedata.normalize("NFKD", name.lower().translate(_LETTERS))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9. ]+", " ", t).replace(".", ". ")
    return " ".join(t.split())


def match_name(name: str | None, candidates: dict[str, str]) -> str | None:
    """Player id whose name fits `name` ('R. Hojlund', 'Rasmus Hojlund', 'Vinicius Junior'), only when exactly one does."""
    toks = clean(name or "").split()
    words = [w for w in toks if not w.endswith(".")]
    if not words:
        return None
    initial = toks[0][0] if toks[0].endswith(".") else None
    full = " ".join(words)
    names = {pid: " ".join(w for w in clean(n).split() if not w.endswith(".")) for pid, n in candidates.items()}
    exact = [pid for pid, n in names.items() if n == full]
    if len(exact) == 1:
        return exact[0]
    hits = [pid for pid, n in names.items() if n and f" {full} " in f" {n} " and (not initial or n.startswith(initial))]
    return hits[0] if len(hits) == 1 else None


def resolve(events: list[Event], keys: dict[str, str], squads: dict[str, dict[str, str]]) -> None:
    """Fill player_id / assist_id: playerKey map first, then the name among that team's players (own goals: the other team's)."""
    teams = list(squads)
    for e in events:
        if e.team is None:
            continue
        own = e.kind == "OWN_GOAL"
        pool = squads.get(e.team, {})
        if own:  # the scorer is listed on the side that benefits; the player belongs to the other team
            other = [t for t in teams if t != e.team]
            pool = {**(squads.get(other[0], {}) if other else {}), **pool}
        e.player_id = keys.get(e.player_key or "") or match_name(e.player_name, pool)
        e.assist_id = keys.get(e.assist_key or "") or match_name(e.assist_name, squads.get(e.team, {}))


def event_rows(fid: str, events: list[Event]) -> list[tuple]:
    return [(fid, e.seq, e.minute, e.team, e.kind, e.detail, e.player_id, e.player_key, e.player_name, e.assist_id, e.assist_key,
             e.assist_name, SOURCE) for e in events]


def read_row(fid: str, events_data, n: int | None, at: datetime) -> tuple:
    return (fid, SOURCE, n, at.isoformat(), json.dumps(events_data, ensure_ascii=False, separators=(",", ":")) if events_data is not None else None)
