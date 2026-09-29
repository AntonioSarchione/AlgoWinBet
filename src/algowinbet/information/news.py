"""News pipeline (spec 7.2/7.3): raw text -> structured, validated InformationEvent with provenance.

Default parser is deterministic and rule based (IT/EN, no API, free). An LLM parser can be plugged in through
`LLMNewsParser`: the model only classifies text into a JSON schema, every output is validated, it never produces probabilities.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from ..domain import InformationEvent, NewsItem, Player
from .sources import effective_confidence

_STATUSES = {"OUT", "SUSPENDED", "DOUBTFUL", "AVAILABLE"}

# order matters: first matching class wins (most specific first)
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AVAILABLE", re.compile(r"\b(recuperat[oa]|recupera|torna|rientra|disponibile|convocat[oa]|back in training|returns?|available|fit again)\b")),
    ("SUSPENDED", re.compile(r"\b(squalificat[oa]|squalifica|suspended|suspension|banned)\b")),
    ("DOUBTFUL", re.compile(r"\b(dubbio|in dubbio|da valutare|acciaccat[oa]|doubtful|doubt|fitness test|a rischio|gioca o no)\b")),
    ("OUT", re.compile(r"\b(infortunat[oa]|indisponibil[ei]|fuori|lesione|stop|non convocat[oa]|out|injured|injury|ruled out|will miss|sidelined)\b")),
]
_NEGATION = re.compile(r"\b(non|not|no|senza|without)\s+(?:e |è |sara |sarà |is |e' )?(?:più )?$")
_HEDGE = re.compile(r"\b(forse|probabile|pare|sembra|potrebbe|rumor|indiscrezioni|secondo alcuni|might|could|reportedly|according to|allegedly)\b")
_OTHER_TYPES = [
    ("COACH_CHANGE", re.compile(r"\b(esonerat[oa]|nuovo allenatore|sacked|new manager|dimissioni)\b")),
    ("WEATHER", re.compile(r"\b(pioggia|neve|vento|maltempo|rain|snow|storm|heavy wind)\b")),
]


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def event_id(item: NewsItem, suffix: str = "") -> str:
    h = hashlib.sha1(f"{item.source}|{item.published_at.isoformat()}|{item.text}|{suffix}".encode()).hexdigest()
    return h[:16]


def match_players(text: str, roster: list[Player]) -> list[Player]:
    """Players whose full name or unique surname appears in the text (accent/case insensitive)."""
    t = norm(text)
    found = []
    for p in roster:
        full = norm(p.name)
        parts = full.split()
        surname = parts[-1] if parts else full
        same_surname = sum(1 for q in roster if norm(q.name).split()[-1:] == [surname])
        if re.search(rf"\b{re.escape(full)}\b", t) or (same_surname == 1 and len(surname) >= 3 and re.search(rf"\b{re.escape(surname)}\b", t)):
            found.append(p)
    return found


def _classify(sentence: str) -> tuple[str | None, str]:
    s = norm(sentence)
    for status, pat in _PATTERNS:
        for m in pat.finditer(s):
            neg = _NEGATION.search(s[: m.start()])
            if neg:
                if status in ("OUT", "SUSPENDED", "DOUBTFUL"):
                    return "AVAILABLE", m.group(0)
                continue
            return status, m.group(0)
    return None, ""


class RuleBasedParser:
    """Sentence-level classifier. Players are resolved against the roster (the item's team first, else all)."""

    name = "rules-v1"

    def parse(self, item: NewsItem, roster: list[Player]) -> list[InformationEvent]:
        pool = [p for p in roster if item.team is None or p.team == item.team] or roster
        events: list[InformationEvent] = []
        sentences = [x.strip() for x in re.split(r"(?<=[.;!?])\s+|\n", item.text) if x.strip()]
        for sent in sentences:
            players = match_players(sent, pool)
            clauses = [c for c in re.split(r"[,;]|\bma\b|\bmentre\b|\bwhile\b|\bbut\b", sent) if c.strip()]
            sent_status, sent_hit = _classify(sent)
            emitted = False
            for p in players:
                # with several players in one sentence, classify the clause that names each player
                clause = next((c for c in clauses if match_players(c, [p])), sent) if len(players) > 1 else sent
                status, hit = _classify(clause)
                if not status:
                    status, hit, clause = sent_status, sent_hit, sent
                if not status:
                    continue
                hedged = bool(_HEDGE.search(norm(clause)))
                conf = effective_confidence(item.source_level, 0.55 if hedged else 0.92)
                events.append(InformationEvent(
                    id=event_id(item, p.id), fixture_id=item.fixture_id, team=p.team, player=p.id,
                    event_type="PLAYER_STATUS", source_level=item.source_level, published_at=item.published_at,
                    observed_at=item.observed_at, confidence=conf,
                    payload={"status": status, "hedged": hedged, "matched": hit, "text": sent, "source": item.source,
                             "parser": self.name}))
                emitted = True
            if emitted:
                continue
            other = next((t for t, pat in _OTHER_TYPES if pat.search(norm(sent))), None)
            if other:
                events.append(InformationEvent(
                    id=event_id(item, other), fixture_id=item.fixture_id, team=item.team, event_type=other,
                    source_level=item.source_level, published_at=item.published_at, observed_at=item.observed_at,
                    confidence=effective_confidence(item.source_level, 0.8),
                    payload={"text": sent, "source": item.source, "parser": self.name}))
        if not events:  # keep the raw item; never silently drop information (spec 42)
            events.append(InformationEvent(
                id=event_id(item, "raw"), fixture_id=item.fixture_id, team=item.team, event_type="OTHER",
                source_level=item.source_level, published_at=item.published_at, observed_at=item.observed_at,
                confidence=0.0, payload={"text": item.text, "source": item.source, "parser": self.name, "unparsed": True}))
        return events


class LLMEventSchema(BaseModel):
    event_type: str
    player_id: str | None = None
    team: str | None = None
    status: str | None = None
    confidence: float = Field(ge=0, le=1)
    hedged: bool = False


class LLMNewsParser:
    """Wraps any `complete(prompt) -> str` callable. Output must be a JSON list matching LLMEventSchema and refer to
    known players/statuses; anything else is discarded. Provenance and source reliability come from the item, not the model."""

    name = "llm-validated-v1"

    def __init__(self, complete: Callable[[str], str]):
        self.complete = complete

    def parse(self, item: NewsItem, roster: list[Player]) -> list[InformationEvent]:
        ids = {p.id: p for p in roster}
        prompt = (
            "Extract structured events from the text. Return ONLY a JSON list of objects with keys "
            "event_type (PLAYER_STATUS|COACH_CHANGE|WEATHER|OTHER), player_id (one of the roster ids or null), team, "
            f"status (OUT|SUSPENDED|DOUBTFUL|AVAILABLE or null), confidence 0-1, hedged bool.\nRoster: "
            + json.dumps({p.id: p.name for p in roster}) + f"\nText: {item.text}"
        )
        try:
            raw = json.loads(self.complete(prompt))
            rows = [LLMEventSchema.model_validate(r) for r in raw]
        except (ValueError, ValidationError, TypeError):
            return [InformationEvent(id=event_id(item, "raw"), event_type="OTHER", source_level=item.source_level,
                                     published_at=item.published_at, observed_at=item.observed_at, confidence=0.0,
                                     team=item.team, payload={"text": item.text, "unparsed": True, "parser": self.name})]
        out = []
        for k, r in enumerate(rows):
            if r.event_type == "PLAYER_STATUS" and (r.player_id not in ids or r.status not in _STATUSES):
                continue
            out.append(InformationEvent(
                id=event_id(item, f"{r.player_id}{k}"), fixture_id=item.fixture_id,
                team=ids[r.player_id].team if r.player_id in ids else (r.team or item.team), player=r.player_id,
                event_type=r.event_type, source_level=item.source_level, published_at=item.published_at,
                observed_at=item.observed_at,
                confidence=effective_confidence(item.source_level, min(r.confidence, 0.55 if r.hedged else 1.0)),
                payload={"status": r.status, "hedged": r.hedged, "text": item.text, "parser": self.name}))
        return out


def parse_lineup_names(text: str, roster: list[Player]) -> tuple[list[str], list[str]]:
    """Resolve a pasted list of names ('Maignan, Tomori, ...') to player ids. Returns (ids, unresolved_names)."""
    ids, bad = [], []
    for raw in re.split(r"[,\n;]", text):
        name = raw.strip()
        if not name:
            continue
        m = match_players(name, roster)
        if len(m) == 1 and m[0].id not in ids:
            ids.append(m[0].id)
        else:
            bad.append(name)
    return ids, bad
