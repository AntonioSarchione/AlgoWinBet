# AlgoWinBet

Motore di analisi probabilistica dei mercati sportivi, **solo paper / uso personale**. Non piazza scommesse.
Specifica completa: [docs/AlgoWinBet_Specifiche_Tecniche_e_Funzionali.pdf](docs/AlgoWinBet_Specifiche_Tecniche_e_Funzionali.pdf).

## Stato: Milestone 1 — core engine (Python, SQLite, dati mock/CSV)

Pipeline: `provider → state @ cutoff → Dixon-Coles (distribuzione dei punteggi) → prezzo di mercato (devig, consenso multi-book)
→ ensemble bayesiano → fair odds / edge / EV / incertezza / data quality → status → optimizer (beam search) → risk/stake → spiegazione`.

Cosa c'è: dominio canonico, registry mercati (1X2, doppia chance, over/under, BTTS, team totals, risultato esatto),
probabilità congiunte esatte per legs della stessa partita, correlation engine, optimizer con **NO BET**, risk engine
(fractional Kelly con cap, mai inseguimento perdite), backtest walk-forward con paper betting (Brier, log loss, ECE, CLV),
store SQLite append-only, CLI.

Non ancora (milestone successivi): news/lineup engine e impact routing, modelli ML (LightGBM), player props,
API FastAPI, dashboard Next.js, DNB/Asian handicap, learning automatico del prior di mercato.

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

## Limiti onesti
- Le fonti gratuite non hanno player props, prezzi SGP né formazioni ufficiali strutturate: quelle parti restano da collegare.
- Nessun sistema garantisce schedine vincenti; le quote dei book incorporano già quasi tutta l'informazione pubblica.
