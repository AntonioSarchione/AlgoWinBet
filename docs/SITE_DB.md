# Site database ("vetrina") — design

Status: design, step 1 of the plan agreed on 2026-10-10. Target: live from the Turso quota reset on 2026-11-01.

## Why

The Turso free plan (3 GB syncs, 500M rows read, 10M rows written, 5 GB storage per month) ran out on 2026-10-09/10:
embedded replicas in the Actions runs re-downloaded every changed page (each publish writes ~3 MB), and remote reads of
big tables (a COUNT(*) over ~1M quotes on every visit of Stato del sistema, probes on the primary) burned rows read.

## Architecture

- **Archive**: `data/offline.db`, a plain SQLite file kept in the Actions cache (`.github/actions/offline-db`). Every
  workflow reads and writes it; one writer at a time (concurrency group `offline-db`). Daily encrypted backup artifact.
- **Site database**: a new, small Turso database holding only what the dashboard reads. Written only by the push step at
  the end of the writing runs; read by the site (Vercel) and the tick route. No embedded replica anywhere: syncs ~0.
- **Push**: diff-based. The archive keeps, per site row, the hash of what was last pushed (table `site_pushed`). A run
  pushes only rows whose hash changed and deletes rows that left the site scope. `site_pushed` is updated only after
  Turso confirms the write, so a failed push is redone by the next run. A failed push never fails the run (warning only).
- **Login tables** (`login_attempts`, `login_failures`) live only on the site database: the site creates and writes them.

## What the site reads (web/lib/db.ts, web/lib/loginguard.ts, web/app/api/tick/route.ts) and the site scope

| Site table | Read by | Scope pushed | Rows (2026-10-10) |
|---|---|---|---|
| pub_runs | latestRun, Sistema (last 12) | last 12 runs | 12 |
| pub_fixtures | runFixtures, runCompetitions, fixtureDetail | last 2 runs | ~200 |
| pub_opportunities | runOpps, oppSummary, slipCandidates, fixtureCandidates, fixtureDetail | last 2 runs | ~870 |
| pub_book | fixtureBook | last 2 runs | ~120 |
| pub_players | fixtureDetail (cards) | last 2 runs | ~190 |
| pub_slips | runSlips | last 2 runs | ~10 |
| pub_quote_paths (new) | quoteMenu, quotePath, quotesAt, fixtureDetail nQuotes | fixtures of the last 2 runs, Sisal only, one row per fixture (JSON of every line's price path) | ~100 |
| fixtures | tick: matchesBetween, lineupsPending | kickoff from 2 days ago on | ~250 |
| lineups | fixtureDetail, tick lineupsPending, absencesFor (team sheets, 120 days) | all (small) | ~4,900 |
| results | teamForm, h2h, absencesFor join, Sistema per competition | all | ~6,800 |
| players | fixtureDetail names, absencesFor names | all | ~7,500 |
| player_status | absencesFor, fixtureDetail version | fixtures of the last 2 runs | ~400 |
| player_quotes | playerQuotes | fixtures of the last 2 runs, Sisal only | ~3,500 |
| api_usage | usage, manualRefreshesThisMonth | current and previous month (D and M rows) | ~40 |
| jobs | Sistema | all | ~250 |
| health | latestHealth | last row | 1 |
| quality_runs | latestQuality | last row | 1 |
| paper_legs | paperRegistry | all | ~1,000 (+~60/day) |
| paper_slips | paperRegistry, bankrollSlips | all | ~170 (+~15/day) |
| site_meta (new) | lastTick (was raw_requests MAX(id)) | key/value: last_tick | 1 |

Not on the site database: quotes (replaced by pub_quote_paths), raw_requests (replaced by site_meta), absence_history,
apif_teams, blobs, boost_*, event_reads, fixture_links, fotmob_*, match_events, match_stats, meta_models, news_items,
player_keys, player_match_stats, referees.

Columns: the site tables keep the archive's names and columns (the queries stay as they are), except the two new tables.

## Push command

`algowinbet site-push --db data/offline.db` (site from SITE_DATABASE_URL / SITE_AUTH_TOKEN; `--site <file>` for local
checks, `--full` to send everything again, `--strict` to stop on errors). It runs at the end of collect, fotmob and quality,
before the archive is saved to the cache (the push state lives in the archive, table `site_pushed`).
After restoring the archive from a backup, run once with `--full`: the backup's push state is older than the site.

## Publish consistency

A publish writes the new run's rows first and `pub_runs` last, in one transaction; the run two publishes back is deleted
after. A page that read the previous run id still finds its rows.

## Estimated monthly use (free plan limits in brackets)

- Syncs: ~0 (3 GB).
- Rows written: pub tables ~1.4k per publish x ~12/day ≈ 0.5M; quote paths ~50 fixtures changed x ~30 runs/day ≈ 45k;
  the rest a few hundred a day. Total 0.3-0.6M (10M); doubled if Turso counts index entries.
- Rows read: pages ~0.1-3k rows each; ~5-10M with the tick (500M).
- Storage: < 50 MB (5 GB).
- First load: ~30k rows, one push.

## Switch-over (2026-11-01)

1. Owner creates the new Turso database and sets its URL and token (GitHub secrets for the push, Vercel env for the site).
2. First push from the archive; checks on the local site against the new database.
3. Site live; the old 560 MB database stays untouched until the owner deletes it.
