# Team crests

Crests come from [football-logos.cc](https://football-logos.cc/) (logos belong to their clubs and federations; the site
allows non-commercial fan use, which is what this private dashboard is). They are stored as 96px WebP files in
`web/public/logos` and mapped by our team name in `web/lib/logos.json`. A team without a match shows a grey default shield.

Run from this folder when new teams appear (promoted clubs, European cups):

1. `algowinbet teams --db turso > teams.txt` (or the `probe` workflow with source `db`, command `teams`).
2. `python crawl.py` builds `index.json` (one request per country page, one per second).
3. `python match.py` writes `matches.json` and lists what it could not match: add those to `aliases.json`
   (site ids, `country/id` to pin a country), then run it again. Check the printed matches before building.
4. `npm install && node build.mjs ../../web` downloads only missing PNGs and writes the WebP files and the map.

`index.json`, `matches.json`, `teams.txt` and `png-cache/` are working files and are not committed.
