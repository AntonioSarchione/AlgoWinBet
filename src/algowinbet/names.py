"""Team-name canonicalisation across sources (GOAL, football-data.co.uk, manual JSON...). Sources spell clubs differently
('Inter', 'Internazionale', 'FC Internazionale Milano'); the model needs ONE name per club or history and fixtures never meet."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

_STOP = {"fc", "ac", "as", "ssc", "us", "cf", "afc", "sc", "bc", "calcio", "club", "football", "the", "1907", "1909", "1913"}


def normalize(name: str) -> str:
    t = unicodedata.normalize("NFKD", name)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(w for w in t.split() if w not in _STOP)


class TeamNames:
    """canon(name) -> canonical display name. Explicit aliases first, then the normalised form itself."""

    def __init__(self, aliases: dict[str, list[str]] | None = None):
        self._map: dict[str, str] = {}
        for canonical, alts in (aliases or {}).items():
            self._map[normalize(canonical)] = canonical
            for a in alts:
                self._map[normalize(a)] = canonical

    @classmethod
    def load(cls, path: str | Path | None) -> "TeamNames":
        if path and Path(path).exists():
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        return cls()

    def canon(self, name: str) -> str:
        return self._map.get(normalize(name), name.strip())

    def unmatched(self, names: set[str], known: set[str]) -> set[str]:
        """Names (already canonicalised) that do not appear in the known history: candidates for a new alias."""
        kn = {normalize(k) for k in known}
        return {n for n in names if normalize(n) not in kn}
