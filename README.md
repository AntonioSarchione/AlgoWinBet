# AlgoWinBet

Motore di analisi probabilistica dei mercati sportivi, **solo paper / uso personale**. Non piazza scommesse.
Specifica completa: [docs/AlgoWinBet_Specifiche_Tecniche_e_Funzionali.pdf](docs/AlgoWinBet_Specifiche_Tecniche_e_Funzionali.pdf).

## Stato: Milestone 2 — core engine + news/lineup engine (Python, SQLite, dati mock/CSV/manuali)

Pipeline: `provider → state @ cutoff → Dixon-Coles (distribuzione dei punteggi) → prezzo di mercato (devig, consenso multi-book)
→ ensemble bayesiano → fair odds / edge / EV / incertezza / data quality → status → optimizer (beam search) → risk/stake → spiegazione`.

Cosa c'è: dominio canonico, registry mercati (1X2, doppia chance, over/under, BTTS, team totals, risultato esatto),
probabilità congiunte esatte per legs della stessa partita, correlation engine, optimizer con **NO BET**, risk engine
(fractional Kelly con cap, mai inseguimento perdite), backtest walk-forward con paper betting (Brier, log loss, ECE, CLV),
store SQLite append-only, CLI.

**News/lineup engine (M2):** notizie testuali → eventi strutturati con fonte, timestamp e affidabilità (parser a regole IT/EN, parser LLM
opzionale con output validato); disponibilità giocatori (XI ufficiale > probabile > tassi di titolarità + notizie); modello di impatto dei
giocatori sui gol attesi (prior per ruolo/importanza + apprendimento dalle formazioni storiche, con incertezza); impact routing e
ricalcolo selettivo per evento; timeline T-72h…T-30m; quote "vecchie" rispetto all'informazione segnalate e mai promosse a CANDIDATE.

Non ancora (milestone successivi): modelli ML (LightGBM), player props, adapter API automatici per formazioni (oggi: JSON copiato da fonti
ufficiali), API FastAPI, dashboard Next.js, DNB/Asian handicap, learning automatico del prior di mercato.

## Uso

```bash
pip install -e ".[dev]"
python -m pytest -q

# demo su dati sintetici (mock: i book prezzano dal vero => tipicamente NO BET)
algowinbet analyze --provider mock --odds-min 2 --odds-max 15
algowinbet backtest --provider mock
# mercato con inefficienza pianificata, per vedere il motore trovare valore
algowinbet analyze --provider mock --mock-noise 0 --mock-bias-over 0.15 --market-prior-sd 0.08

# dati reali gratuiti: scarica a mano i CSV da football-data.co.uk (gratis per uso privato, niente bot)
algowinbet backtest --provider csv --csv E0.csv I1.csv
algowinbet analyze  --provider csv --csv E0.csv fixtures.csv --days 7
```

### Formazioni e notizie (M2)
```bash
# formazioni, rose e notizie copiate da siti ufficiali (formato: configs/info.example.json)
algowinbet analyze --provider csv --csv E0.csv fixtures.csv --info info.json
algowinbet news  --provider mock --team A0-Team01 --text "Colombo squalificato. Forse Testa in dubbio."   # parsing
algowinbet event --provider mock --mock-stage pre_lineup --team A0-Team03 --text "Cattaneo indisponibile per infortunio"  # ricalcolo selettivo
algowinbet timeline A0-Team09 --provider mock --mock-stage post_lineup --days 1      # info che arriva nel tempo
algowinbet info-value --provider mock --mock-effect-scale 3    # le info migliorano davvero le stime? (log loss cieco vs con info)
algowinbet stale-edge --provider mock                          # scommesse quando la quota non si è ancora mossa dopo l'XI
```

### Snapshot storage e GOAL API (M3)
Lo storico delle quote/formazioni non è ottenibile a posteriori gratis: si raccoglie **in avanti**, con timestamp reali di osservazione
(`observed_at` = momento della richiesta), in `data/snapshots.db` (payload grezzi gzip deduplicati + righe normalizzate).

```bash
export GOALAPI_KEY=...            # la chiave non va mai nel repo né come argomento
python -m algowinbet.cli goal leagues "Serie A"                      # trova l'id lega
python -m algowinbet.cli goal probe /leagues/ID/fixtures --param limit=2   # 1 richiesta: salva il grezzo e mostra i campi reali
python -m algowinbet.cli goal collect --mode fixtures --leagues ID --dry-run
python -m algowinbet.cli goal collect --mode lineups --leagues ID --window 95   # da schedulare ogni 10-15 min (Task Scheduler)
python -m algowinbet.cli snapshots                                   # statistiche e uso API
python -m algowinbet.cli analyze --provider snapshots --csv storico.csv ...
```
Modi: `fixtures`, `results`, `lineups` (solo partite entro la finestra, salta le XI già confermate), `odds`, `players`.
Budget: contatore locale giornaliero con riserva, sincronizzato con gli header `X-RateLimit-*`; `429` gestiti con `Retry-After`.

**Verificato dalla documentazione GOAL**: auth Bearer, envelope, paginazione, header rate-limit, codici d'errore, enum stati, endpoint.
**Verificato sul piano free con richieste reali (29/09/2026)**: fixtures e results (schema piatto, `kickoffUtc`, ordine decrescente per data, `from/to/status`
filtrano; ~2000 risultati storici Serie A dal 2024/25); lineups (forma `{home,away:{startingLineups,substitutes,coach,missingPlayers},homeFormation,awayFormation,hasLineups}`,
vuote finché non pubblicate). **`/odds` NON è incluso nel piano free** (402 "Feature not available in your plan"): le quote arrivano da OddsPapi o a mano.
**Non ancora visto**: righe dei titolari a partita pubblicata (assunte come le righe `coach`: `playerId`, `lineupPlayer`, `lineupPosition`) e `missingPlayers` (possibili infortuni/squalifiche).
**Assunto (non documentato)**: gli schemi delle righe lineups/odds/players. I mapper sono tolleranti e contano ciò che non riescono a
mappare (`Lacune di mapping`); i payload grezzi restano salvati, quindi dopo il primo `goal probe` reale si corregge il mapper e si rielabora senza altre richieste.
Alias nomi squadra tra fonti: `configs/team_aliases.json` (il collector segnala i nomi senza storico).

### Quote Sisal
Sisal non ha API pubbliche e lo scraping viola i suoi termini: inserisci le quote a mano in un JSON
(formato in `src/algowinbet/providers/manual.py`) e passalo con `--manual quote.json`; viene unito alle altre fonti.

## Principi già implementati
- Cutoff informativo: history/quote/eventi filtrati per `observed_at`; il backtest usa solo quote d'apertura per decidere e quelle di chiusura solo per il CLV.
- Prior di mercato: il prezzo devigged è il prior, il modello è evidenza rumorosa (`w = τ²/(τ²+σ²)`). Su mercati efficienti il risultato tipico è NO BET.
  `backtest` stampa `w_struct*` (peso ottimo osservato): se è alto in modo stabile, alza `--market-prior-sd`.
- Le combo della stessa partita usano la distribuzione congiunta, mai il prodotto delle marginali. Le slip default usano 1 leg per partita
  perché il prezzo SGP del book non è disponibile dalle fonti gratuite.
- Mock: l'errore del modello misurato è ~ `sqrt(p(1-p)/n_partite)`; con dati reali va ri-misurato.

## Cosa dicono gli esperimenti (su dati sintetici)
- Le formazioni migliorano il log loss del modello strutturale, di più quando gli effetti dei giocatori sono grandi (`info-value`), ma il
  mercato resta più accurato: il modello di squadra stima male con poche partite. Il vantaggio non sta nel "sapere più del mercato" ma nel
  sapere **prima** del mercato: dopo l'XI ufficiale, con quota non ancora aggiornata (`stale-edge`), il CLV è positivo. Con quote già
  aggiornate (`--mock-fresh-quotes`) il vantaggio sparisce e il risultato è NO BET.
- L'effetto dei singoli giocatori è quasi non apprendibile dai soli gol di squadra con poche partite: il prior per ruolo e `importance`
  (da statistiche per 90' dei giocatori) conta più dell'apprendimento. Nei dati reali fornisci `importance` e `start_rate` nel JSON.
- Sono numeri su un mondo simulato: vanno rimisurati su dati reali prima di fidarsi.

## Limiti onesti
- Le fonti gratuite non hanno player props, prezzi SGP né formazioni ufficiali strutturate: quelle parti restano da collegare.
- Nessun sistema garantisce schedine vincenti; le quote dei book incorporano già quasi tutta l'informazione pubblica.
