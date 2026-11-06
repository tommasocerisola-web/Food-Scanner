# Food Scanner — motore di scoring alimentare (v0.3 beta)

> **0.3 beta** — nuova interfaccia responsive con legenda del punteggio,
> fotocamera per il barcode, riepilogo del risultato e contributo foto a Open
> Food Facts. Include **100 test deterministici su prodotti italiani** e un
> comando di collaudo live su **100 prodotti italiani reali**.

Motore di scoring con **UI di test** che legge un prodotto da **Open Food Facts** e
restituisce uno **score 0-100 spiegabile**, con una differenza sostanziale rispetto
a Yuka: il rischio viene valutato **per singolo additivo**, non "a pacchetto".

> Il problema di Yuka: se un prodotto ha 2 additivi innocui e 1 conservante molto
> tossico, i tre vengono mediati e il punteggio resta alto. Qui il peggiore pesa il
> 65% della componente additivi: **un solo additivo critico abbassa davvero lo score**.

## Come funziona

```
barcode ──► Open Food Facts API v2 ──► normalizzazione ──► scoring ──► report
                                        (per 100 g/ml)      (4 componenti)
```

| Componente | Peso | Cosa misura |
|---|---|---|
| Nutrienti | 50% | Nutri-Score (kcal, zuccheri, grassi saturi, sale, fibre, proteine, frutta/verdura), ricalibrato su fasce A-E |
| Additivi | 35% | rischio del singolo additivo (tabella compilabile) con il peggiore dominante |
| Lavorazione | 10% | NOVA 1-4 |
| Biologico | 5% | etichetta bio (neutro 50 se assente: non penalizza) |

Se un dato manca, i pesi delle altre componenti vengono **rinormalizzati** e la
`confidence` scende sotto 1: lo score non finge di essere completo.

### Perché fa male

Il report spiega il punteggio in chiaro: quali nutrienti hanno generato punti
negativi (con i grammi per 100 g), quale additivo pesa di più, il grado NOVA.
Ogni additivo porta la propria spiegazione preimpostata, es:

```
E250  Nitrito di sodio   rischio 60.0 · dose ~0,6%
      Nitrito di sodio: forma nitrosammine; le carni lavorate sono IARC gruppo 1.
```

### La dose

Quando Open Food Facts espone la percentuale di un additivo negli ingredienti
(`ingredients[].percent_estimate`), il rischio viene ridotto in modo
conservativo: 0,01% → 30% del rischio · 0,1% → 55% · 1% → 80% · ≥10% → 100%.
Senza il dato la dose **non riduce nulla**. La stessa informazione appare nel
JSON (`dose_percent`, `base_risk`).

### Componente additivi

```
penalità = 0.65 · rischio_peggiore + 0.35 · rischio_medio + min(10; 2.5 · (n - 2))
score_additivi = 100 - penalità
```

- `rischio_peggiore` domina → il singolo additivo critico non si diluisce.
- `n - 2` aggiunge pressione solo **oltre** i primi due additivi: non punisce un
  conservante necessario e innocuo da solo.
- Ogni additivo con rischio ≥ 70 genera un'**allerta critica** a parte.

### Ricalibraggio dei nutrienti

Il Nutri-Score classico viene convertito in 0-100 **a fasce** (A → 85-100,
B → 70-85, C → 50-70, D → 25-50, E → 0-25) invece che con una retta unica: una
mappatura lineare premierebbe troppo i prodotti medi (un Nutri-Score 0 finirebbe a
72/100). Bevande e solidi usano tabelle di soglie diverse; il sale viene ricavato
dal sodio (×2.5) quando manca, e le kcal dai kJ quando serve.

## La tabella additivi

Tutti i **546 additivi** hanno un `risk_score` 0-100 e una spiegazione (`why`):
**338 valutati a mano** (coloranti azoici, nitriti, parabeni, glutammato,
dolcificanti, fosfati, alluminio, cere, amidi...) e **208 stimati dalla classe
funzionale**, con `basis` che dichiara quale dei due casi è.

Scala usata: 0-10 naturale/innocuo · 10-30 basso (irritanti, intolleranze) ·
30-50 moderato (solfiti, benzoati, fosfati) · 50-70 alto (nitriti, parabeni,
vietati UE con evidenza tossicologica) · 70-100 critico (reprotossici,
cancerogeni, formaldeide).

Colonne: `e_number, key, name_en, name_it, classes, is_preservative, risk_score,
basis, why, notes`. `basis = curata` significa valutazione manuale,
`basis = classe` significa stima dalla funzione (ed è marcata come tale nel report).

| File | Contenuto |
|---|---|
| `data/additives.csv` | tutti gli additivi di Open Food Facts (546), conservanti già valutati |
| `data/preservatives.csv` | solo i conservanti (71), tutti con punteggio |

Compila **solo** `risk_score` (0 = innocuo/naturale, 100 = tossicità massima) e,
se vuoi, `notes`. Poi:

```bash
python tools/build_additive_table.py     # rigenera le tabelle conservando i punteggi scritti a mano
```

Gli additivi ancora senza punteggio usano un default per classe funzionale
(`engine/additives.py → DEFAULT_RISK_BY_CLASS`, es. conservante 40) e vengono
marcati come **stimati** nel report.

## Uso

```bash
python main.py scan 3017620422003                 # report leggibile
python main.py scan 5449000000996 --json          # JSON per la UI
python main.py file prodotto.json                 # da un JSON OFF già scaricato
python main.py batch barcodes.txt --workers 6 --out risultati.json
python main.py additives --preservatives          # elenco conservanti
python main.py additives --missing-risk           # cosa manca da compilare
```

Pesi e soglie si possono cambiare da CLI (`--w-nutrition`, `--w-additives`,
`--w-processing`, `--w-organic`, `--worst-weight`, `--critical`) senza toccare il codice.

## UI di test

Una pagina locale per vedere il motore all'opera, senza dipendenze e senza build:

```bash
python ui/server.py                    # http://127.0.0.1:8000
python ui/server.py --host 0.0.0.0 --port 8000
```

- **Barcode** → Open Food Facts + scoring (con cache e rate limiter del client).
- **Fotocamera** → legge un barcode/QR dalla foto, dove il browser supporta `BarcodeDetector`.
- **Demo offline** → `ui/demo_product.json`, utile senza rete.
- **JSON manuale** → incolla un prodotto già scaricato e valuta solo lo scoring.

Endpoint: `GET /api/health`, `GET /api/demo`, `GET /api/score?barcode=...`,
`POST /api/score` con il JSON del prodotto. La UI mostra score, componenti,
confidenza, allerte, additivi con rischio/dose e nutrienti per 100 g/ml, con una
legenda e un riepilogo in chiaro del risultato. Non è un'app di produzione: è un
banco di prova per il motore.

### Collaudo su 100 prodotti italiani

Il test unitario `tests/test_italian_products.py` genera **100 prodotti italiani**
deterministici (seed fisso, barcode EAN-13 unici, 20 bevande e 28 prodotti con
additivi) e verifica le invarianti del motore senza rete.

Per il collaudo su prodotti reali serve la connessione:

```bash
python tools/italian_sample.py --limit 100
```

Il comando scarica un campione casuale riproducibile di prodotti italiani da
Open Food Facts, applica normalizzazione e scoring, controlla score, grade,
confidenza, pesi e rischi degli additivi. Salva il report in
`out/italian_sample.json` e termina con errore se una sola invariante non torna.

### Contribuire a Open Food Facts

In alto a destra c'è **+ Aggiungi foto**: puoi scattare o scegliere la foto
generale del prodotto e quella della tabella nutrizionale, leggere un barcode/QR
dalla fotocamera (dove il browser supporta `BarcodeDetector`) e inviarle a OFF.
Se un prodotto scansionato ha dati incompleti, la scheda mostra un avviso con lo
stesso pulsante.

Le scritture su OFF **richiedono un account** (non esistono upload anonimi).
Imposta le credenziali solo nell'ambiente del server, mai nel repo o nel browser:

```bash
export OFF_USER_ID="il-tuo-username"
export OFF_PASSWORD="la-tua-password"
python ui/server.py
```

L'endpoint usato è il CGI documentato `product_image_upload.pl` (modificabile con
`OFF_UPLOAD_URL`). Le foto devono essere almeno 640×160 px; il browser le
riconverte in JPEG e il server valida magic bytes e dimensione (max 5 MB).
`GET /api/health` espone solo `contribute.configured`, mai le credenziali.

## Più fonti insieme (matching + tabella di consenso)

Gli export eterogenei (Open Food Facts, Yuka, una terza tabella) si confrontano
senza scrivere codice: `engine/sources.py` normalizza JSON, JSONL e CSV con alias
italiani/inglesi, `tools/cross_source.py` fa il resto.

```bash
# 1. prodotti in comune: barcode, fallback nome+marca, doppioni, ambigui
python tools/cross_source.py match \
    --source off=export_off.json --source yuka=export_yuka.json \
    --source terza=export_terza.csv \
    --out out/match.json --csv out/match.csv

# 2. tabella di consenso: mediana tra fonti, accordo, righe da fixare
python tools/cross_source.py table \
    --table off=data/additives.csv --table yuka=yuka_additivi.csv \
    --out data/additives.consensus.csv --gaps out/gaps.json
```

`match` produce: prodotti comuni con score per fonte e spread, prodotti presenti
in una sola fonte, match ambigui (non uniti a forza), doppioni, e **additivi
sconosciuti** alla tabella. `table` produce la mediana per E-number, quante fonti
sono d'accordo (±10 punti), le fonti mancanti e `needs_fix` con il motivo.

La tabella ufficiale non viene mai modificata: il consenso è una proposta da
rivedere, i "pezzi mancanti" finiscono in `out/gaps.json`.

## Le tre fonti e l'algoritmo di unione

Verificate il 4/11/2026, tutte raggiungibili senza chiave obbligatoria:

| Fonte | Dati | Copertura |
|---|---|---|
| **Open Food Facts** | prodotti, additivi, Nutri-Score, NOVA, etichette | ~3,5M prodotti |
| **USDA FoodData Central** | pannello nutrizionale completo, ingredienti, GTIN | ~1,9M alimenti |
| **Open Prices** | prezzo, confezione, negozio, data | prezzi per barcode |

Yuka non espone un'API raggiungibile, FoodRepo reindirizza su Open Food Facts e
Wger re-importa i dati di OFF: non sono fonti indipendenti. Se in futuro arriva
una chiave Yuka/Nutritionix, basta un adapter che produca `SourceProduct`.

I pesi sono **per campo**, non solo per fonte: FDC pesa più di OFF sui nutrienti
(1,1 vs 1,0), OFF pesa tutto su additivi/Nutri-Score/NOVA, Open Prices solo su
prezzo. Per ogni campo il merger prende la **mediana pesata** delle fonti che lo
riportano, registra la provenienza e segnala i conflitti oltre tolleranza
(es. zuccheri ±20%, sale ±25%, kcal ±15%). Un campo assente non penalizza la
fonte: il peso viene rinormalizzato.

```
confidenza = 0,5·copertura + 0,3·accordo tra fonti + 0,2·min(1; fonti/3)
```

```bash
# 1. scarica le tre fonti per una lista di barcode
python tools/merge_sources.py fetch --barcodes barcodes.txt --out out/sources

# 2. unisci i dati (GTIN-14 + fallback nome+marca, conflitti e provenienza)
python tools/merge_sources.py merge \
  --source off=out/sources/off.json \
  --source fdc=out/sources/fdc.json \
  --source openprices=out/sources/openprices.json \
  --out out/merged.json --csv out/merged.csv

# 3. consenso sulle tabelle di rischio additivi
python tools/merge_sources.py table \
  --table off=data/additives.csv --table yuka=yuka_additivi.csv \
  --out out/additives.merge.csv --gaps out/additives.gaps.json
```

`fetch` usa `FDC_API_KEY` se presente, altrimenti `DEMO_KEY` (30 richieste/ora:
per volumi serve la chiave gratuita di FDC). Il merge non inventa valori: se una
fonte manca, il campo resta con la provenienza dell'altra e la confidenza scende.

## Prestazioni

- Solo standard library: nessuna dipendenza, avvio immediato.
- Cache SQLite su disco (`cache/off.sqlite`, TTL 30 giorni, con firma dei campi
  richiesti): la seconda scansione dello stesso barcode non tocca la rete, e se
  cambia la lista dei campi la cache vecchia viene ignorata.
- Richieste con `fields=` selettivo (payload minimo), rate limiter a 60 rpm che
  **rallenta da solo** quando Open Food Facts risponde 429, retry con backoff e
  secondo giro per i barcode falliti.
- Batch parallelo (`--workers`) con thread pool; errori per singolo barcode senza
  interrompere la scansione.

## Struttura

```
main.py                     CLI (scan / file / batch / additives)
ui/server.py                server HTTP e API della UI di test
ui/static/                  pagina, CSS e JS (nessuna dipendenza)
ui/demo_product.json        prodotto demo offline
engine/off_client.py        client Open Food Facts, cache, rate limit, normalizzazione
engine/off_upload.py        upload foto su Open Food Facts (endpoint CGI)
engine/scoring.py           Nutri-Score ricalibrato + aggregazione additivi
engine/additives.py         tabella additivi e rischi per classe
engine/models.py            dataclass (prodotto, additivo, componenti, risultato)
engine/report.py            rendering testuale
engine/merge.py             mediana pesata, provenienza, conflitti, confidenza
engine/source_online.py     fetch OFF / USDA FDC / Open Prices
engine/sources.py           adattatori export multi-fonte (JSON/JSONL/CSV)
tools/build_additive_table.py  genera data/*.csv dalla tassonomia OFF
tools/cross_source.py       matching prodotti + tabella di consenso + buchi
tools/merge_sources.py      CLI fetch/merge/table sulle tre fonti
tests/test_engine.py        test offline (unittest)
tests/test_cross_source.py  test del confronto multi-fonte
tests/test_merge.py         test del merger e degli adapter
tests/test_ui.py            test della UI (server, demo, JSON, contributo)
tests/test_off_upload.py    test dell'upload foto OFF
tests/test_italian_products.py  100 prodotti italiani deterministici
tools/italian_sample.py     collaudo live su 100 prodotti italiani reali
```

```bash
python -m unittest discover -s tests -t .   # 210 test, nessuna rete richiesta
python tools/italian_sample.py --limit 100  # collaudo live, richiede rete
```

## Limiti noti

- Open Food Facts non riporta le **quantità** dei singoli additivi: il rischio è per
  presenza, non per dose.
- La copertura della tassonomia non è completa (alcuni E2xx arrivano senza classe
  funzionale; in quel caso la famiglia E2xx viene trattata come conservanti).
- Alcuni prodotti hanno `nutrition_data_per` espresso per porzione e non per 100 g:
  il motore usa i campi `_100g` quando presenti.
- Il riconoscimento "è una bevanda" si basa sulle categorie/unità dichiarate da OFF.
- Il matching senza barcode usa nome+marca (somiglianza ≥ 0,90): se due prodotti
  hanno lo stesso nome ma barcode diversi restano separati e finiscono tra gli
  ambigui, non vengono uniti a forza.
- USDA FDC copre soprattutto prodotti statunitensi: il match con un EAN europeo
  riesce solo quando il GTIN coincide davvero (UPC-12 ed EAN-13 normalizzati).
- Open Prices non contiene nutrienti: aggiunge prezzo/negozio/data e conferma
  l'identità del barcode, non il profilo nutrizionale.
- Senza `FDC_API_KEY` si usa `DEMO_KEY` (30 richieste/ora): `fetch` su liste
  lunghe si ferma con un errore esplicito, non con dati inventati.
