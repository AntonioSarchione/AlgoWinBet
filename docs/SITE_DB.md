# Site database ("vetrina") — design

Status: steps 1-3 done (2026-10-10): design, push (`sitepush.py`), dashboard reads (`web/lib/db.ts`). Target: live from the Turso quota reset on 2026-11-01.

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
- **Login tables** (`login_attempts`, `login_failures`) live in their own database (`LOGIN_DB_URL` / `LOGIN_DB_TOKEN`, set on
  Vercel): the site creates and writes them, the push never touches them.

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

## Switch-over runbook (from 2026-11-01, after the Turso quota reset)

The workflows stay on the archive for good: the switch-over only adds the site database and points the dashboard at it.

Owner (secrets are never handled by Claude):
1. Check on the Turso dashboard that the account is unblocked (new billing cycle).
2. Create a new database, e.g. `algowinbet-site`, in the region closest to Vercel `fra1` (Frankfurt), and a token for it
   (read and write: the push writes, the site writes nothing there).
3. GitHub secrets: `SITE_DATABASE_URL` (libsql://...) and `SITE_AUTH_TOKEN`.

Claude:
4. Start a collect run (workflow_dispatch). Its "site push" step does the first load: the log must show
   `vetrina: invio completo (prima volta)` and about 25-30k rows written. A second run must write only a few rows.

Owner:
5. Vercel, Production: `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` set to the site database. Check that `LOGIN_DB_URL`
   works again (its database is unblocked by the same reset).

Claude:
6. One push: `DB_OFFLINE = false` in web/app/api/tick/route.ts (the tick reads fixtures and lineups from the site
   database again) and `DB_PAUSED = false` in web/proxy.ts (the pages stop showing the pause notice). The deployment also
   picks up the new Vercel variables.
7. Checks online: every page, the odds-trend tab, Stato del sistema (Turso meters), the tick's answers in the Vercel logs.
8. First three days: Turso meters every day. Expected: syncs ~0, rows written < 30k/day, rows read < 1M/day.

Later:
9. The old 560 MB database is no longer read or written. The owner deletes it after a week of stable operation.
10. The GitHub secrets `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` become unused by the workflows.
11. Done on 2026-10-10: probe, analyze and fotmob-backfill work on the archive; the morning size check measures it.
