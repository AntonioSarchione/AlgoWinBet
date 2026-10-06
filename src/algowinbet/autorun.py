"""One scheduler tick: decide what is worth fetching NOW and do only that. Meant to run every ~15 minutes on a machine that
is not the user's PC (GitHub Actions + Turso). Every step is idempotent and budget-aware; a tick with nothing to do sends
zero requests.

Plan per tick (UTC):
  - GOAL fixtures ........ if the last successful sync is older than 20h            (~1 request/league/day)
  - GOAL results + stats . if the last successful sync is older than 20h            (~1 + 1 per finished match)
  - GOAL lineups ......... fixtures kicking off within the lineup window, until both XI are confirmed
  - GOAL backfill ........ once per league, one league per tick: multi-season results history (no manual CSV)
  - OddsPapi snapshot .... Sisal only, 1 counted request per block of 5 tournaments (2 for the 10 leagues). Automatic
                           snapshots aim at `oddspapi_plan_monthly` (200) counted requests a month, paced day by day:
                             daily ..... first tick of the UTC day with fixtures in the next 7 days (links new fixtures)
                             pre-kick .. 30-75 min before a crowded kickoff slot (>= crowded_slot matches), after the XI
                             refresh ... busy days (>= busy_day kickoffs in the next 12h): every refresh_every_h hours, only
                                         while the month is on track (midday and late afternoon in practice)
  - OddsPapi manual ...... the dashboard button: snapshot + history now, at most manual_monthly a month, on top of the plan
  - OddsPapi history ..... free /historical-odds (Sisal + Pinnacle) for linked fixtures: once a day up to 7 days ahead,
                           then at 24/12/6/3/1.5h before kickoff and right after a confirmed XI. Pinnacle comes only from here
  - OddsPapi closing ..... free /historical-odds after kickoff (closing line for CLV)
  - football-data ........ season CSVs (stats, xG, opening/closing Pinnacle, Betfair Exchange, market average) once the
                           GOAL backfill is complete: past seasons once, the current season every dataset_refresh_days
  - international ........ results of every national team (github martj42, public CSV) once a week: national-team Elo
"""
from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .collector import CollectStats, GoalCollector
from .elo import international_due, sync_international
from .fdcollector import FootballDataCollector
from .oddscollector import LINKS_SCHEMA, OddsCollector
from .providers.oddspapi import SOURCE
from .snapshots import SnapshotProvider, SnapshotStore


@dataclass
class League:
    name: str
    goal: str | None = None       # GOAL league id (goal leagues "...")
    oddspapi: int | None = None   # OddsPapi tournamentId (odds tournaments "...")
    fd: str | None = None         # football-data.co.uk division (I1, E0...): season CSVs
    apif: int | None = None       # API-Football league id (lineups, injuries, squads)


@dataclass
class AutoConfig:
    goal_leagues: list[str] = field(default_factory=list)
    oddspapi_tournaments: list[str] = field(default_factory=list)
    bookmakers: list[str] = field(default_factory=lambda: ["sisal"])  # counted snapshots
    history_bookmakers: list[str] = field(default_factory=lambda: ["sisal", "pinnacle"])  # free /historical-odds
    leagues: list[League] = field(default_factory=list)
    goal_daily_limit: int = 1000
    goal_reserve: int = 50
    oddspapi_monthly_limit: int = 250
    oddspapi_reserve: int = 20
    lineup_window_min: int = 60  # official XI from 60 minutes before kickoff (probable lineup before)
    fixtures_days: int = 14
    history_seasons: int = 2  # backfill: current season + 2 previous, never more
    prekick_min: tuple[int, int] = (30, 75)
    oddspapi_plan_monthly: int = 200  # counted requests the automatic snapshots aim at (manual ones come on top)
    manual_monthly: int = 5           # manual refreshes from the dashboard per month
    crowded_slot: int = 3             # matches in one kickoff slot that justify a pre-kick snapshot
    busy_day: int = 6                 # kickoffs in the next 12h that make a busy day
    refresh_every_h: float = 5
    history_checkpoints_h: tuple[float, ...] = (24, 12, 6, 3, 1.5)
    history_days: int = 7             # daily free price path for fixtures up to this far ahead
    history_per_tick: int = 20
    divisions: dict[str, str] = field(default_factory=dict)  # football-data division -> competition
    dataset_refresh_days: float = 3
    international: bool = False  # weekly international results (national-team Elo), public CSV, no key
    apif_daily_limit: int = 100   # API-Football free plan
    apif_reserve: int = 3
    apif_squads_per_day: int = 20

    @classmethod
    def load(cls, path: str | Path) -> "AutoConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for k in ("prekick_min", "history_checkpoints_h"):
            if k in raw:
                raw[k] = tuple(raw[k])
        cfg = cls(**{k: v for k, v in raw.items() if not k.startswith("_") and k != "leagues"})
        cfg.leagues = [League(**{k: v for k, v in l.items() if not k.startswith("_")}) for l in raw.get("leagues", [])]
        # ids saved once in the config: no lookup request is ever needed at run time
        cfg.goal_leagues = cfg.goal_leagues or [l.goal for l in cfg.leagues if l.goal]
        cfg.oddspapi_tournaments = cfg.oddspapi_tournaments or [str(l.oddspapi) for l in cfg.leagues if l.oddspapi]
        cfg.divisions = cfg.divisions or {l.fd: l.name for l in cfg.leagues if l.fd}
        return cfg


DATASETS_SECONDS = 180.0  # time a tick may spend on season CSVs (about 4 files on Turso)
MANUAL_REFRESHES = "manual-refresh"  # api_usage source: refreshes started from the dashboard (a count, not requests)
MANUAL_REQUESTS = "oddspapi-manual"  # api_usage source: counted OddsPapi requests spent by those refreshes


def manual_used(store: SnapshotStore, now: datetime) -> int:
    return store.usage(MANUAL_REFRESHES, f"M{now:%Y-%m}")


def _auto_used(store: SnapshotStore, period: str) -> int:
    return store.usage("oddspapi", period) - store.usage(MANUAL_REQUESTS, period)


def odds_allowed_today(store: SnapshotStore, cfg: AutoConfig, now: datetime, cost: int) -> bool:
    """Pace the automatic plan (oddspapi_plan_monthly): today may spend at most twice the fair share of what is left (match
    days need more than empty days, and empty days spend nothing because no snapshot is planned without fixtures). The hard
    limit minus the reserve is never crossed, manual refreshes included."""
    day, month = f"D{now:%Y-%m-%d}", f"M{now:%Y-%m}"
    used_today = _auto_used(store, day)
    left = min(cfg.oddspapi_plan_monthly - _auto_used(store, month),
               cfg.oddspapi_monthly_limit - cfg.oddspapi_reserve - store.usage("oddspapi", month))
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    allowance = 2 * (left + used_today) / days_left
    return left >= cost and used_today + cost <= max(allowance, cost)


def _on_track(store: SnapshotStore, cfg: AutoConfig, now: datetime, cost: int) -> bool:
    """Optional snapshots (busy-day refreshes) only while the month has spent no more than its share up to today."""
    share = cfg.oddspapi_plan_monthly * now.day / calendar.monthrange(now.year, now.month)[1]
    return _auto_used(store, f"M{now:%Y-%m}") + cost <= share


def snapshot_reason(store: SnapshotStore, cfg: AutoConfig, now: datetime, last_odds: datetime | None, cost: int) -> str | None:
    """Why an automatic snapshot is due now (None = not due). Pre-kick first: it is the moment stale prices exist."""
    prov = SnapshotProvider(store)
    lo, hi = cfg.prekick_min
    day = prov.list_fixtures(None, now, now + timedelta(hours=12))
    slots: dict[datetime, int] = {}
    for f in day:
        k = f.kickoff.replace(minute=f.kickoff.minute // 15 * 15, second=0, microsecond=0)
        slots[k] = slots.get(k, 0) + 1
    crowded = [k for k, n in slots.items() if n >= cfg.crowded_slot and timedelta(minutes=lo) <= k - now <= timedelta(minutes=hi)]
    if crowded and _stale(last_odds, now, timedelta(minutes=hi - lo + 5)):
        return "pre-kick"
    if (last_odds is None or last_odds.date() < now.date()) and prov.list_fixtures(None, now, now + timedelta(days=cfg.history_days)):
        return "daily"
    if len(day) >= cfg.busy_day and _stale(last_odds, now, timedelta(hours=cfg.refresh_every_h)) and _on_track(store, cfg, now, cost):
        return "refresh"
    return None


def _history_fetches(store: SnapshotStore) -> dict[str, datetime]:
    """Last /historical-odds fetch per OddsPapi fixture id."""
    out: dict[str, datetime] = {}
    for params, at in store.db.execute("SELECT params, MAX(fetched_at) FROM raw_requests WHERE source='oddspapi' AND "
                                       "endpoint='/historical-odds' AND status=200 GROUP BY params").fetchall():
        ext = str(json.loads(params or "{}").get("fixtureId", ""))
        t = datetime.fromisoformat(at)
        if ext and (ext not in out or t > out[ext]):
            out[ext] = t
    return out


# Freshness target of a Sisal price by time to kickoff (hours to kickoff, max age of the last read): the free reads that
# fill the minute a run already pays for go to the most overdue matches first.
FRESHNESS = ((6.0, 1.0), (24.0, 3.0), (72.0, 8.0), (float("inf"), 24.0))


def freshness_due(store: SnapshotStore, now: datetime, days: int = 7) -> list[str]:
    """GOAL fixture ids linked to OddsPapi whose last price read is older than its FRESHNESS target, most overdue first."""
    store.db.executescript(LINKS_SCHEMA)
    upcoming = {f.id: f for f in SnapshotProvider(store).list_fixtures(None, now, now + timedelta(days=days))}
    fetched = _history_fetches(store)
    scored = []
    for ext, fid in store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source=?", (SOURCE,)).fetchall():
        f = upcoming.get(fid)
        if f is None:
            continue
        to_ko = (f.kickoff - now).total_seconds() / 3600.0
        target = next(t for h, t in FRESHNESS if to_ko <= h)
        last = fetched.get(ext)
        age = (now - last).total_seconds() / 3600.0 if last else float("inf")
        if age >= target:
            scored.append((age / target, -to_ko, fid))
    return [fid for *_, fid in sorted(scored, reverse=True)]


def history_due(store: SnapshotStore, cfg: AutoConfig, now: datetime) -> list[str]:
    """GOAL fixture ids whose free price path is due: never fetched or older than a day (up to history_days ahead), a
    checkpoint passed since the last fetch, or a confirmed XI stored after it. Nearest kickoff first."""
    store.db.executescript(LINKS_SCHEMA)
    upcoming = {f.id: f for f in SnapshotProvider(store).list_fixtures(None, now, now + timedelta(days=cfg.history_days))}
    links = [(e, fid) for e, fid in store.db.execute("SELECT ext_id, fixture_id FROM fixture_links WHERE source=?", (SOURCE,)).fetchall()
             if fid in upcoming]
    if not links:
        return []
    fetched = _history_fetches(store)
    xi = dict(store.db.execute("SELECT fixture_id, MAX(observed_at) FROM lineups WHERE status='confirmed' GROUP BY fixture_id").fetchall())
    due: list[str] = []
    for ext, fid in links:
        f, last = upcoming[fid], fetched.get(ext)
        passed = [f.kickoff - timedelta(hours=h) for h in cfg.history_checkpoints_h if f.kickoff - timedelta(hours=h) <= now]
        lineup = xi.get(fid)
        if fid not in due and (last is None or now - last >= timedelta(hours=24) or (passed and last < max(passed))
                               or (lineup and last < datetime.fromisoformat(lineup))):
            due.append(fid)
    return sorted(due, key=lambda fid: upcoming[fid].kickoff)[:cfg.history_per_tick]


def _last_ok(store: SnapshotStore, source: str, endpoint_like: str) -> datetime | None:
    row = store.db.execute("SELECT MAX(fetched_at) FROM raw_requests WHERE source=? AND endpoint LIKE ? AND status=200 "
                           "AND (source != 'goal-api' OR params LIKE '%\"from\"%')",  # manual probes are not syncs
                           (source, endpoint_like)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


RESULT_DUE = timedelta(hours=2, minutes=30)  # kickoff + 90' + half time + stoppages, with a margin
RESULT_RETRY = timedelta(minutes=45)  # a match still missing its result is asked again at most this often (GOAL requests are plenty)


def late_result_leagues(store: SnapshotStore, cfg: AutoConfig, now: datetime) -> list[str]:
    """GOAL leagues with a match that kicked off 2.5h to 3 days ago, is not postponed or cancelled, and still has no result:
    their results are fetched on the next tick instead of waiting for the daily sync (yesterday evening's matches would
    otherwise reach the model up to 20 hours late)."""
    rows = store.db.execute(
        "SELECT DISTINCT f.competition FROM fixtures f WHERE f.source = 'goal-api' AND f.kickoff >= ? AND f.kickoff <= ? "
        "AND f.observed_at = (SELECT MAX(observed_at) FROM fixtures g WHERE g.fixture_id = f.fixture_id) "
        "AND f.status NOT IN ('POSTPONED', 'CANCELLED') "
        "AND NOT EXISTS (SELECT 1 FROM results r WHERE r.fixture_id = f.fixture_id)",
        ((now - timedelta(days=3)).isoformat(), (now - RESULT_DUE).isoformat())).fetchall()
    comps = {norm_comp(r[0]) for r in rows}
    if not comps:
        return []
    ids = [l.goal for l in cfg.leagues if l.goal and norm_comp(l.name) in comps]
    return ids or list(cfg.goal_leagues)  # a competition named differently: ask every league rather than miss it


def norm_comp(name: str) -> str:
    return " ".join(str(name).lower().split())


def _stale(last: datetime | None, now: datetime, age: timedelta) -> bool:
    return last is None or now - last >= age


def plan_tick(store: SnapshotStore, cfg: AutoConfig, now: datetime, last_odds: datetime | None, odds_cost: int | None = None,
              manual: bool = False, history: bool = False) -> list[str]:
    """Pure decision (no network): which steps this tick should run. manual (dashboard button) takes a snapshot now while the
    month has manual refreshes left, plus the price paths of the next 3 days; history = every upcoming price path."""
    steps: list[str] = []
    if cfg.goal_leagues:
        stale = lambda kind: any(_stale(_last_ok(store, "goal-api", f"/leagues/{lid}/{kind}"), now, timedelta(hours=20)) for lid in cfg.goal_leagues)
        if stale("fixtures"):
            steps.append("fixtures")
        late = late_result_leagues(store, cfg, now)
        if stale("results") or (late and _stale(_last_ok(store, "goal-api", "/leagues/%/results"), now, RESULT_RETRY)):
            steps += ["results", "stats"]
        steps.append("lineups")  # costs nothing when no fixture is inside the window
        if any(not store.job_done(f"backfill:goal:{lid}") for lid in cfg.goal_leagues):
            steps.append("backfill")  # one league per tick until every league has its multi-season history
    if any(l.apif for l in cfg.leagues):
        steps.append("apif")  # decides by itself: requests only for what is due (see apifcollector)
    if cfg.oddspapi_tournaments:
        cost = odds_cost or len(cfg.bookmakers)
        if manual:
            # a second click right after a refresh would only buy the same prices again
            if manual_used(store, now) < cfg.manual_monthly and _stale(last_odds, now, timedelta(minutes=10)):
                steps.append("odds")
        elif snapshot_reason(store, cfg, now, last_odds, cost) and odds_allowed_today(store, cfg, now, cost):
            steps.append("odds")
        if manual or history or history_due(store, cfg, now):
            steps.append("history")
        steps.append("closing")
    if datasets_due(store, cfg, now):
        steps.append("datasets")  # last: nothing time-critical; the files link to the backfilled results, so they wait for it
    return steps


def datasets_due(store: SnapshotStore, cfg: AutoConfig, now: datetime) -> bool:
    backfilled = all(store.job_done(f"backfill:goal:{lid}") for lid in cfg.goal_leagues)
    return bool((cfg.divisions and backfilled and FootballDataCollector(store, cfg.divisions, now=lambda: now).due(cfg.history_seasons, cfg.dataset_refresh_days))
                or (cfg.international and international_due(store, now)))


def run_datasets(store: SnapshotStore, cfg: AutoConfig, datasets: FootballDataCollector | None, now: datetime, budget: float,
                 clock: Callable[[], float] = time.monotonic, on_step: Callable[[CollectStats], None] | None = None) -> list[CollectStats]:
    """Season CSVs and the weekly international results, within `budget` seconds (no new file starts after it)."""
    out: list[CollectStats] = []
    if not datasets_due(store, cfg, now):
        return out
    if cfg.international:
        st = sync_international(store, now)  # one small file a week: national-team Elo
        if st:
            out.append(st)
            if on_step:
                on_step(st)
    if datasets is not None:
        st = datasets.sync(cfg.history_seasons, cfg.dataset_refresh_days, max_seconds=budget, clock=clock)
        if st.requests or st.errors or st.skipped:
            out.append(st)
            if on_step:
                on_step(st)
    return out


def transient_db_error(e: BaseException) -> bool:
    """A dropped HTTP connection to Turso, not a SQL error. libsql reports it as ValueError("Hrana: `http error: ...`") on a
    statement and as ValueError("sync error: http dispatch error: ...") on a replica sync (seen 2026-10-02 and 10-04)."""
    m = str(e)
    return (("Hrana" in m or "sync error" in m)
            and any(k in m for k in ("http error", "dispatch error", "connection", "stream", "timed out", "502", "503", "504")))


def run_tick(store: SnapshotStore, cfg: AutoConfig, goal: GoalCollector | None, odds: OddsCollector | None,
             now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), on_step: Callable[[CollectStats], None] | None = None,
             max_seconds: float | None = None, clock: Callable[[], float] = time.monotonic,
             manual: bool = False, history: bool = False, datasets: FootballDataCollector | None = None,
             skip: tuple[str, ...] = (), apif=None) -> list[CollectStats]:
    """Runs the planned steps in order. With max_seconds, no NEW step starts after that time (the CI job has a hard timeout;
    whatever is skipped is simply picked up by the next tick, every step being idempotent)."""
    t = now()
    t0 = clock()
    steps = plan_tick(store, cfg, t, odds.last_snapshot_at() if odds else None, odds.snapshot_cost() if odds else None,
                      manual=manual, history=history)
    out: list[CollectStats] = []
    if manual and odds is not None and "odds" not in steps:
        st = CollectStats("odds")
        st.skipped.append(f"aggiornamento manuale senza fotografia: {manual_used(store, t)}/{cfg.manual_monthly} usati questo mese "
                          "oppure fotografia di meno di 10 minuti fa (lo storico gratuito si aggiorna comunque)")
        out.append(st)
        if on_step:
            on_step(st)
    for s in steps:
        if s in skip:
            continue
        if max_seconds is not None and clock() - t0 > max_seconds:
            skipped = CollectStats(s)
            skipped.skipped.append("tempo del giro esaurito: rimandato al prossimo tick")
            out.append(skipped)
            if on_step:
                on_step(skipped)
            continue
        before = len(out)
        if s in ("fixtures", "results", "stats", "lineups", "backfill") and goal is None:
            continue
        if s in ("odds", "history", "closing") and odds is None:
            continue
        if s == "datasets" and datasets is None and not cfg.international:
            continue
        if s == "apif" and apif is None:
            continue
        try:
            if s == "fixtures":
                out.append(goal.sync_fixtures(cfg.fixtures_days, leagues=goal.stale_leagues("fixtures")))
            elif s == "results":
                due = goal.stale_leagues("results")
                due += [lid for lid in late_result_leagues(store, cfg, t) if lid not in due]
                out.append(goal.sync_results(leagues=due))
            elif s == "stats":
                out.append(goal.sync_stats(3))
            elif s == "lineups":
                out.append(goal.sync_lineups(cfg.lineup_window_min))
            elif s == "backfill":
                st = goal.backfill_next(cfg.history_seasons)
                if st:
                    out.append(st)
            elif s == "odds":
                st = odds.sync_odds()
                if manual and st.requests:
                    store.add_usage(MANUAL_REFRESHES, f"M{t:%Y-%m}", 1)
                    for period in (f"D{t:%Y-%m-%d}", f"M{t:%Y-%m}"):
                        store.add_usage(MANUAL_REQUESTS, period, st.requests)
                out.append(st)
                # matches the Sisal snapshot skipped: link them, so the free price path brings Pinnacle (estimated Sisal price)
                link = odds.link_unquoted({l.name: str(l.oddspapi) for l in cfg.leagues if l.oddspapi})
                if link.requests or link.errors:
                    out.append(link)
            elif s == "history":
                left = 600.0 if max_seconds is None else max(30.0, max_seconds - (clock() - t0))
                if history:
                    out.append(odds.sync_prematch_history(max_seconds=left))
                elif manual:
                    out.append(odds.sync_prematch_history(days_ahead=3, max_fixtures=40, max_seconds=left))
                else:
                    out.append(odds.sync_prematch_history(only=history_due(store, cfg, t), max_seconds=left))
            elif s == "closing":
                out.append(odds.sync_closing())
            elif s == "apif":
                out.append(apif.run())
            elif s == "datasets":
                # capped; the first load spreads over a few ticks. The CLI skips it here and runs it after the publication.
                left = DATASETS_SECONDS if max_seconds is None else max(30.0, min(DATASETS_SECONDS, max_seconds - (clock() - t0)))
                for st in run_datasets(store, cfg, datasets, t, left, clock):
                    out.append(st)
                    if on_step:
                        on_step(st)
        except Exception as e:  # noqa: BLE001 - only a dropped connection to Turso is absorbed, anything else re-raised
            if not transient_db_error(e):
                raise
            # the step's uncommitted writes are lost, its committed ones stay (every step is idempotent): a fresh connection
            # and the rest of the tick go on, the next tick redoes this step. No failed run, no e-mail for a network blip.
            store.recover()
            st = CollectStats(s)
            st.errors.append(f"connessione a Turso caduta ({e}): passo ripreso al prossimo giro")
            out.append(st)
        if on_step and len(out) > before:
            on_step(out[-1])
    return out


def should_publish(results: list[CollectStats], last_pub: datetime | None, now: datetime, max_age: timedelta = timedelta(hours=6)) -> bool:
    """Re-analyse when this tick brought prices or lineups, or when the published analysis is older than max_age. Closing
    lines belong to matches already started: they feed CLV and the quality report, not the analysis of upcoming ones."""
    fresh = any((r.saved.get("quotes") and r.mode != "closing") or r.saved.get("lineups") or r.saved.get("player status")
                for r in results)
    return fresh or last_pub is None or now - last_pub >= max_age
