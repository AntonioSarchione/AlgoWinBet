"""Impact routing (spec 19): which market families an information event can move, so recalculation is selective."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain import InformationEvent, Player, Position

ALL = {"1X2", "TOTALS", "BTTS", "TEAM_TOTALS", "CORRECT_SCORE"}


@dataclass
class Route:
    high: set[str] = field(default_factory=set)
    monitor: set[str] = field(default_factory=set)
    reason: str = ""

    @property
    def affected(self) -> set[str]:
        return self.high | self.monitor


def resolve(event: InformationEvent, roster: dict[str, Player] | None = None) -> Route:
    t = event.event_type
    if t in ("LINEUP_CONFIRMED", "LINEUP_PROBABLE"):
        return Route(high={"1X2", "TOTALS", "BTTS", "TEAM_TOTALS"}, monitor={"CORRECT_SCORE"}, reason="formazione")
    if t == "PLAYER_STATUS":
        p = (roster or {}).get(event.player or "")
        if p is None:
            return Route(high={"1X2", "TOTALS"}, monitor={"BTTS", "TEAM_TOTALS"}, reason="giocatore non in rosa nota")
        if p.position == Position.GK:
            return Route(high={"1X2", "BTTS", "TOTALS"}, monitor={"CORRECT_SCORE", "TEAM_TOTALS"}, reason="portiere")
        if p.position == Position.FWD:
            return Route(high={"TEAM_TOTALS", "TOTALS"}, monitor={"1X2", "BTTS"}, reason="attaccante")
        if p.position == Position.MID:
            return Route(high={"1X2", "TEAM_TOTALS"}, monitor={"TOTALS", "BTTS"}, reason="centrocampista")
        return Route(high={"TOTALS", "BTTS"}, monitor={"1X2", "TEAM_TOTALS"}, reason="difensore")
    if t == "COACH_CHANGE":
        return Route(high={"1X2", "TOTALS"}, monitor=ALL - {"1X2", "TOTALS"}, reason="cambio allenatore")
    if t == "WEATHER":
        return Route(high={"TOTALS", "TEAM_TOTALS"}, monitor={"BTTS"}, reason="meteo")
    if t == "ODDS_MOVE":
        return Route(high=set(), monitor=set(), reason="solo rivalutazione valore/EV")
    return Route(reason="nessun impatto noto")
