# Fonti dati: mappa feature → sorgente e budget richieste

Stato verifiche (29/09/2026). **Verificato** = letto da documentazione/pagina ufficiale; **da testare** = serve una chiave API e una prova.

| Sorgente | Cosa è verificato | Cosa NON è verificato |
|---|---|---|
| GOAL API (`api.goal-api.com/v1`, auth `Bearer`) | Free: 1.000 req/giorno, 1 chiave, no carta. Endpoint: `/fixtures`, `/fixtures/:id/lineups`, `/statistics`, `/events`, `/odds`, `/predictions`, `/h2h/:a/:b`, player, standings | Rate limit al minuto, latenza dati sul free, copertura Serie A, se ci sono xG/player stats/infortuni, qualità delle formazioni prima del calcio d'inizio |
| API-Football | Free: 100 req/giorno, no carta (pagina ufficiale non raggiungibile: 403) | Limiti di stagione sul free (mia memoria: piano free limitato a stagioni passate → **da testare prima di dipenderne**), presenza xG nelle statistiche |
| Understat | Nessuna API ufficiale: i dati arrivano da scraping (es. `soccerdata`, che avverte: rompe se il sito cambia, rispetta i ToS) | ToS di Understat sullo scraping automatico, limiti di frequenza |
| OddsPapi | Free: 250 req/mese, 1 richiesta per chiamata a prescindere dalla dimensione; `/odds` per fixture ritorna tutti i book (216–274) e tutti i mercati; `/historical-odds` (max 3 book per chiamata, 1 richiesta); rate: ≥1 s tra chiamate stesso endpoint; Sisal risulta tra i book europei | Quali mercati Sisal per una partita di Serie A, profondità dello storico, mapping id fixture ↔ altre fonti |
| FotMob (verificato 06/10/2026) | Pagine pubbliche, senza chiave né cookie: `/match/{id}` contiene nel JSON `__NEXT_DATA__` le statistiche Opta di ogni giocatore (xG, npxG, xGOT, xA, tiri, tiri in porta, falli fatti e subiti, minuti, voto), la mappa dei tiri, i cartellini negli eventi e gli assenti (`unavailable`). `/leagues/{id}/fixtures/x?season=AAAA/AAAA` elenca ogni partita di una stagione, anche passata. Copre le 7 leghe e le coppe europee | Non è un'API ufficiale: i ToS non autorizzano lo scraping e il formato può cambiare. `/api/...` risponde 404 senza firma. Fonte facoltativa: una pagina ogni 2 s, solo partite finite |
| football-data.co.uk (già in uso) | CSV gratuiti, uso privato: risultati + quote apertura/chiusura multi-book dal 2000/01 (Bet365, Pinnacle…) | Nessuna formazione/statistica avanzata |

Fonti: [GOAL API](https://goal-api.com/what-is-goal-api), [GOAL API docs](https://goal-api.com/docs), [OddsPapi free tier](https://oddspapi.io/blog/free-odds-api-350-bookmakers/), [soccerdata](https://github.com/probberechts/soccerdata), [football-data.co.uk](https://football-data.co.uk/data.php).

## Correzioni rispetto alla proposta "4 sorgenti"
1. **Storico partite e quote non va preso dalle API a quota giornaliera**: i CSV di football-data.co.uk lo danno gratis e senza consumare richieste. Le API servono solo per il *dato in avanti* (fixture, formazioni, infortuni, quote Sisal correnti).
2. **Understat non è un'API**: è scraping di un sito privato. Va trattato come fonte fragile e facoltativa, con cache locale e frequenza bassa. Non deve essere un componente da cui il sistema dipende.
3. **Backtest reale di formazioni/quote-ferme**: le API danno l'XI finale, non "quando era noto"; assumiamo pubblicazione a kickoff−75'. Le quote con timestamp reali le abbiamo solo da OddsPapi `historical-odds` (da verificare) o raccogliendole noi da adesso. Per validare bene serve raccogliere snapshot in avanti per una stagione.
4. **"Divergenza tra fonti come feature"**: prematuro. Prima si fa riconciliazione (stessi id squadra/partita, stessi risultati) e si registrano le discrepanze. Come feature si valuta dopo, solo se il backtest mostra valore.
5. **Predictions delle API (GOAL/API-Football)**: usarle solo come benchmark da battere, non come feature: sono opache e possono contenere informazione futura.
6. **H2H e standings**: non chiamarli. Il modello non li usa (gli H2H sono rumore a campione piccolo; la classifica si ricostruisce dai risultati).

## Mappa feature → sorgente

| Feature / dato del modello | Primaria | Secondaria / verifica | Frequenza | Costo |
|---|---|---|---|---|
| Calendario, stato partita, kickoff | GOAL `/fixtures` | API-Football `/fixtures` | 1/giorno/lega | 1 req GOAL |
| Storico risultati (Dixon-Coles) | **CSV football-data.co.uk** | GOAL solo per leghe non coperte | backfill una tantum | 0 |
| Quote storiche multi-book (apertura/chiusura) | **CSV football-data.co.uk** | OddsPapi `historical-odds` | una tantum | 0 |
| Quote **Sisal** correnti, tutti i mercati | OddsPapi `/odds?fixtureId` | — | T−48h e T−60m (dopo XI) per le partite candidate | 1 req OddsPapi/chiamata |
| Storico quote Sisal (movimento, velocità) | OddsPapi `/historical-odds` | nostri snapshot salvati | 1 volta per partita | 1 req OddsPapi |
| Formazioni probabili/ufficiali | GOAL `/fixtures/:id/lineups` | API-Football lineups (solo se mancano/discordano) | 3 poll: T−90', T−75', T−60' | 3 req GOAL/partita |
| Infortuni/squalifiche | API-Football injuries (1 chiamata per lega/data) | notizie ufficiali → JSON manuale `--info` | 1/giorno/lega | 1 req API-Football |
| Statistiche partita (tiri, corner, cartellini…) | GOAL `/fixtures/:id/statistics` | API-Football (verifica a campione) | a fine partita | 1 req GOAL/partita |
| Eventi (gol, cartellini) | GOAL `/events` | — | a fine partita | 1 req GOAL/partita |
| Importanza giocatore, tassi per 90' (`importance`) | API-Football players (per squadra/stagione) | GOAL players (da testare) | mensile | ~40 req/lega/mese |
| `start_rate` (titolarità abituale) | derivato dalle formazioni storiche salvate | rosa API | continuo | 0 (calcolo) |
| xG, npxG, xGOT, xA per giocatore e partita, tiri, falli fatti/subiti, assenti | **FotMob** `/match/{id}` (`fotmobcollector.py`, tabelle `fotmob_player_stats`, `fotmob_absences`) | API-Football `/fixtures/players` (tiri, falli, cartellini) | dopo ogni partita; storico a fette ogni mattina | 0 quota, 1 pagina/partita |
| xG, xGA, npxG, xA, xGChain, shot-level | Understat via `soccerdata` (cache) | xG API-Football se presente | dopo ogni turno | 0 quota, ~11 richieste web/turno/lega |
| Meteo (facoltativo) | Open-Meteo (gratuito, senza chiave; da verificare) | — | T−3h | fuori dal budget API |
| Arbitro (cartellini/rigori) | GOAL/API-Football fixture | — | con la fixture | 0 extra |

## Budget richieste (Serie A, un solo campionato, turno da 10 partite)

Ipotesi: 10 partite per turno concentrate in ~3 giorni; 3 poll formazioni per partita.

| Sorgente | Giorno di turno | Settimana | Mese | Limite | Margine |
|---|---|---|---|---|---|
| GOAL | fixtures 1 + lineups 30 + statistiche/eventi 20 ≈ **51/giorno** (picco ~80) | ~150 | ~650 | 1.000/giorno | ampio; regge 5 leghe (~250/giorno di picco) |
| API-Football | injuries 1 + fallback lineups ≤10 ≈ **≤11/giorno** | ~40 | ~120 + 40 players | 100/giorno | ampio |
| OddsPapi | — | ~16 (2 chiamate × 8 partite candidate) | **~65–130** (32 partite/mese × 2 + refresh) | **250/mese** | stretto: 1 lega sì, 2 leghe al limite. Niente polling, solo chiamate mirate |
| Understat | ~11 richieste web dopo il turno | ~11 | ~45 | nessuna quota ufficiale | rispettare pausa ≥2 s, cache su disco |

Regola operativa OddsPapi (250/mese): una chiamata `/odds` T−48h per le sole partite che superano il filtro modello, una a T−60' dopo l'XI ufficiale, `/historical-odds` una volta a fine partita. Le chiamate a partite senza opportunità non si fanno.

## Ordine di integrazione consigliato
1. Salvataggio snapshot in avanti (quote, formazioni, notizie) nello store SQLite: senza timestamp reali non c'è backtest onesto.
2. Adapter GOAL API (fixtures, lineups, statistics) dietro il contratto `ProviderAdapter`, con budget counter e cache.
3. Adapter OddsPapi solo per Sisal + `--budget` mensile che rifiuta chiamate oltre soglia.
4. Adapter API-Football per injuries/players, dopo aver testato i limiti del free.
5. xG Understat come feature facoltativa, misurata con `info-value` prima di tenerla.

Per procedere servono le chiavi API (le crei tu: io non posso registrare account né inserire chiavi). Le si passa via variabili d'ambiente (`GOALAPI_KEY`, `APIFOOTBALL_KEY`, `ODDSPAPI_KEY`), mai nel repo.
