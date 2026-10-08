"""Match context of the player numbers: who the opponent is, where the match is played, who referees it.

A player's own history is a mix of home and away matches against opponents of every kind, so his expected counts describe
an average match. MatchContext turns them into this match's with factors learnt from the team totals of earlier matches
(the FotMob player rows summed per team):

  opponent .. what the teams facing this opponent did in its last OPP_N matches (shots, shots on target, fouls committed,
              fouls won, bookings, assists), shrunk toward the competition's average with OPP_K matches, over that average
  venue ..... the home (or away) teams' average over everyone's average
  referee ... bookings per match in his matches, shrunk toward the competition's average with REF_K matches, over it
              (bookings only)
No request; the same object serves the replay (playereval.py, fed day by day) and the publication (fed once).
"""
from __future__ import annotations

from collections import defaultdict, deque

CTX_STATS = ("shots", "shots_on", "fouls_committed", "fouls_drawn", "cards", "assists")
OPP_N = 20
OPP_K = 8.0
REF_K = 8.0
MIN_COMP = 40  # team-matches before a competition has its own average (else everyone's)


class MatchContext:
    def __init__(self, opp_n: int = OPP_N, opp_k: float = OPP_K, ref_k: float = REF_K):
        self.opp_k, self.ref_k = opp_k, ref_k
        self.against: dict[str, deque] = defaultdict(lambda: deque(maxlen=opp_n))  # team -> what its opponents did, per match
        self.comp: dict[str, list[float]] = defaultdict(lambda: [0.0] * (len(CTX_STATS) + 1))  # sums per team-match, count
        self.venue = {True: [0.0] * (len(CTX_STATS) + 1), False: [0.0] * (len(CTX_STATS) + 1)}
        self.ref: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])  # referee -> bookings, matches

    def add(self, comp: str, home: str, away: str, totals: dict[str, dict[str, float]], ref: str | None = None) -> None:
        """One finished match: totals = {team: {stat: sum over its players}} for both teams."""
        h, a = totals.get(home), totals.get(away)
        if h is None or a is None:
            return
        self.against[home].append(a)
        self.against[away].append(h)
        for side, t in ((True, h), (False, a)):
            for acc in (self.comp[comp], self.venue[side]):
                for i, k in enumerate(CTX_STATS):
                    acc[i] += t.get(k, 0.0)
                acc[-1] += 1
        if ref:
            r = self.ref[ref]
            r[0] += h.get("cards", 0.0) + a.get("cards", 0.0)
            r[1] += 1

    def _mean(self, comp: str) -> list[float]:
        c = self.comp.get(comp)
        if c is None or c[-1] < MIN_COMP:
            tot = [0.0] * (len(CTX_STATS) + 1)
            for v in self.comp.values():
                for i, x in enumerate(v):
                    tot[i] += x
            c = tot
        n = c[-1]
        return [x / n if n else 0.0 for x in c[:-1]]

    def factors(self, opp: str, comp: str, home: bool, ref: str | None = None) -> dict[str, dict[str, float]]:
        """{"opp": {stat: factor}, "venue": {...}, "ref": {"cards": factor}}; 1.0 wherever nothing is known."""
        mu = self._mean(comp)
        seen = self.against.get(opp) or []
        opp_f, venue_f = {}, {}
        allv = [self.venue[True][i] + self.venue[False][i] for i in range(len(CTX_STATS))]
        nall = self.venue[True][-1] + self.venue[False][-1]
        v = self.venue[home]
        for i, k in enumerate(CTX_STATS):
            s = sum(m.get(k, 0.0) for m in seen)
            opp_f[k] = ((s + self.opp_k * mu[i]) / (len(seen) + self.opp_k)) / mu[i] if mu[i] > 0 else 1.0
            venue_f[k] = (v[i] / v[-1]) / (allv[i] / nall) if v[-1] and allv[i] else 1.0
        ref_f = {}
        if ref and ref in self.ref and mu[CTX_STATS.index("cards")] > 0:
            per_match = 2 * mu[CTX_STATS.index("cards")]
            b, n = self.ref[ref]
            ref_f["cards"] = ((b + self.ref_k * per_match) / (n + self.ref_k)) / per_match
        return {"opp": opp_f, "venue": venue_f, "ref": ref_f}


def apply(exp: dict[str, float], f: dict[str, dict[str, float]], opp: float = 0.0, venue: float = 0.0, ref: float = 0.0) -> dict[str, float]:
    """Expected counts times the factors, each raised to its weight (0 = left out)."""
    out = {}
    for k, x in exp.items():
        m = f["opp"].get(k, 1.0) ** opp * f["venue"].get(k, 1.0) ** venue * f["ref"].get(k, 1.0) ** ref
        out[k] = x * m if x is not None else None
    return out
