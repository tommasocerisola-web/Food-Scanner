# Food Scanner — food scoring engine (v0.3 beta)

> **0.3 beta** — new responsive interface with a score legend, barcode camera,
> result summary and photo contribution to Open Food Facts. Includes **100
> deterministic tests on Italian products** and a live check command on **100 real
> Italian products**.

A scoring engine with a **test UI** that reads a product from **Open Food Facts**
and returns an **explainable 0-100 score**, with one big difference from Yuka:
risk is assessed **per single additive**, not "as a package".

> Yuka's problem: if a product has 2 harmless additives and 1 very toxic
> preservative, the three are averaged out and the score stays high. Here the worst
> one weighs 65% of the additive component: **a single critical additive really
> lowers the score**.

## How it works

```
barcode ──► Open Food Facts API v2 ──► normalization ──► scoring ──► report
                                        (per 100 g/ml)     (4 components)
```

| Component | Weight | What it measures |
|---|---|---|
| Nutrition | 50% | Nutri-Score (kcal, sugars, saturated fat, salt, fiber, protein, fruit/veg), recalibrated onto A-E bands |
| Additives | 35% | risk of the single additive (editable table) with the worst one dominating |
| Processing | 10% | NOVA 1-4 |
| Organic | 5% | organic label (neutral 50 when absent: it does not penalise) |

If a value is missing, the weights of the other components are **renormalized**
and `confidence` drops below 1: the score does not pretend to be complete.

### Why it scores badly

The report explains the score in plain language: which nutrients generated
negative points (with grams per 100 g), which additive weighs the most, the NOVA
grade. Every additive carries its own preset explanation, e.g.:

```
E250  Sodium nitrite   risk 60.0 · dose ~0.6%
      Sodium nitrite: forms nitrosamines; processed meats are IARC group 1.
```

### The dose

When Open Food Facts exposes the percentage of an additive in the ingredients
(`ingredients[].percent_estimate`), the risk is reduced conservatively: 0.01% →
30% of the risk · 0.1% → 55% · 1% → 80% · ≥10% → 100%. Without the value the dose
**reduces nothing**. The same information appears in the JSON (`dose_percent`,
`base_risk`).

### Additive component

```
penalty = 0.65 · worst_risk + 0.35 · mean_risk + min(10; 2.5 · (n - 2))
additive_score = 100 - penalty
```

- `worst_risk` dominates → the single critical additive is not diluted.
- `n - 2` adds pressure only **beyond** the first two additives: it does not
  punish a necessary, harmless preservative on its own.
- Every additive with risk ≥ 70 generates a separate **critical alert**.

### Nutrient recalibration

The classic Nutri-Score is converted into 0-100 **in bands** (A → 85-100,
B → 70-85, C → 50-70, D → 25-50, E → 0-25) instead of a single straight line: a
linear mapping would reward average products too much (a Nutri-Score of 0 would
end up at 72/100). Beverages and solids use different threshold tables; salt is
derived from sodium (×2.5) when missing, and kcal from kJ when needed.

## The additive table

All **546 additives** have a 0-100 `risk_score` and an explanation (`why`):
**338 assessed by hand** (azo dyes, nitrites, parabens, glutamate, sweeteners,
phosphates, aluminium, waxes, starches...) and **208 estimated from the functional
class**, with `basis` declaring which of the two applies.

Scale used: 0-10 natural/harmless · 10-30 low (irritants, intolerances) ·
30-50 moderate (sulphites, benzoates, phosphates) · 50-70 high (nitrites,
parabens, EU-banned with toxicological evidence) · 70-100 critical (reprotoxic,
carcinogenic, formaldehyde).

Columns: `e_number, key, name_en, name_it, classes, is_preservative, risk_score,
basis, why, notes`. `basis = curated` means a manual assessment,
`basis = class` means an estimate from the function (and it is marked as such in
the report).

| File | Content |
|---|---|
| `data/additives.csv` | every Open Food Facts additive (546), preservatives already scored |
| `data/preservatives.csv` | preservatives only (71), all scored |

Fill in **only** `risk_score` (0 = harmless/natural, 100 = maximum toxicity) and,
if you want, `notes`. Then:

```bash
python tools/build_additive_table.py     # rebuilds the tables keeping the hand-written scores
```

Additives still without a score use a default by functional class
(`engine/additives.py → DEFAULT_RISK_BY_CLASS`, e.g. preservative 40) and are
marked as **estimated** in the report.

## Usage

```bash
python main.py scan 3017620422003                 # readable report
python main.py scan 5449000000996 --json          # JSON for the UI
python main.py file product.json                  # from an already downloaded OFF JSON
python main.py batch barcodes.txt --workers 6 --out results.json
python main.py additives --preservatives          # list of preservatives
python main.py additives --missing-risk           # what still needs filling
```

Weights and thresholds can be changed from the CLI (`--w-nutrition`,
`--w-additives`, `--w-processing`, `--w-organic`, `--worst-weight`, `--critical`)
without touching the code.

## Test UI

A local page to see the engine at work, with no dependencies and no build:

```bash
python ui/server.py                    # http://127.0.0.1:8000
python ui/server.py --host 0.0.0.0 --port 8000
```

- **Barcode** → Open Food Facts + scoring (with the client's cache and rate limiter).
- **Camera** → reads a barcode/QR from the photo, where the browser supports `BarcodeDetector`.
- **Offline demo** → `ui/demo_product.json`, useful without a network.
- **Manual JSON** → paste an already downloaded product and score it only.

Endpoints: `GET /api/health`, `GET /api/demo`, `GET /api/score?barcode=...`,
`POST /api/score` with the product JSON. The UI shows score, components,
confidence, alerts, additives with risk/dose and nutrients per 100 g/ml, with a
legend and a plain-language summary of the result. It is not a production app: it
is a test bench for the engine.

### Check on 100 Italian products

The unit test `tests/test_italian_products.py` generates **100 deterministic
Italian products** (fixed seed, unique EAN-13 barcodes, 20 beverages and 28
products with additives) and checks the engine invariants without a network.

The check on real products needs a connection:

```bash
python tools/italian_sample.py --limit 100
```

The command downloads a reproducible random sample of Italian products from Open
Food Facts, applies normalization and scoring, checks score, grade, confidence,
weights and additive risks. It saves the report in `out/italian_sample.json` and
exits with an error if a single invariant does not hold.

### Contributing to Open Food Facts

Top right there is **+ Add photo**: you can take or choose the general product
photo and the nutrition table photo, read a barcode/QR from the camera (where the
browser supports `BarcodeDetector`) and send them to OFF. If a scanned product has
incomplete data, the card shows a warning with the same button.

Writes to OFF **require an account** (anonymous uploads do not exist). Set the
credentials only in the server environment, never in the repo or the browser:

```bash
export OFF_USER_ID="your-username"
export OFF_PASSWORD="your-password"
python ui/server.py
```

The endpoint used is the documented CGI `product_image_upload.pl` (configurable
with `OFF_UPLOAD_URL`). Photos must be at least 640×160 px; the browser converts
them back to JPEG and the server validates magic bytes and size (max 5 MB).
`GET /api/health` exposes only `contribute.configured`, never the credentials.

## Several sources together (matching + consensus table)

Heterogeneous exports (Open Food Facts, Yuka, a third table) are compared without
writing code: `engine/sources.py` normalizes JSON, JSONL and CSV with Italian and
English aliases, `tools/cross_source.py` does the rest.

```bash
# 1. shared products: barcode, name+brand fallback, duplicates, ambiguous
python tools/cross_source.py match \
    --source off=export_off.json --source yuka=export_yuka.json \
    --source third=export_third.csv \
    --out out/match.json --csv out/match.csv

# 2. consensus table: median across sources, agreement, rows to fix
python tools/cross_source.py table \
    --table off=data/additives.csv --table yuka=yuka_additives.csv \
    --out data/additives.consensus.csv --gaps out/gaps.json
```

`match` produces: shared products with a score per source and the spread, products
present in a single source, ambiguous matches (not merged by force), duplicates,
and **additives unknown** to the table. `table` produces the median per E-number,
how many sources agree (±10 points), the missing sources and `needs_fix` with the
reason.

The official table is never modified: the consensus is a proposal to review, the
"missing pieces" end up in `out/gaps.json`.

## The three sources and the merge algorithm

Verified on 2026-11-04, all reachable without a mandatory key:

| Source | Data | Coverage |
|---|---|---|
| **Open Food Facts** | products, additives, Nutri-Score, NOVA, labels | ~3.5M products |
| **USDA FoodData Central** | full nutrition panel, ingredients, GTIN | ~1.9M foods |
| **Open Prices** | price, package, store, date | prices per barcode |

Yuka does not expose a reachable API, FoodRepo redirects to Open Food Facts and
Wger re-imports OFF data: they are not independent sources. If a Yuka/Nutritionix
key arrives in the future, an adapter producing a `SourceProduct` is enough.

Weights are **per field**, not only per source: FDC weighs more than OFF on
nutrients (1.1 vs 1.0), OFF weighs everything on additives/Nutri-Score/NOVA, Open
Prices only on price. For every field the merger takes the **weighted median** of
the sources reporting it, records the provenance and flags conflicts beyond
tolerance (e.g. sugars ±20%, salt ±25%, kcal ±15%). A missing field does not
penalise the source: the weight is renormalized.

```
confidence = 0.5·coverage + 0.3·agreement between sources + 0.2·min(1; sources/3)
```

```bash
# 1. download the three sources for a list of barcodes
python tools/merge_sources.py fetch --barcodes barcodes.txt --out out/sources

# 2. merge the data (GTIN-14 + name+brand fallback, conflicts and provenance)
python tools/merge_sources.py merge \
  --source off=out/sources/off.json \
  --source fdc=out/sources/fdc.json \
  --source openprices=out/sources/openprices.json \
  --out out/merged.json --csv out/merged.csv

# 3. consensus on the additive risk tables
python tools/merge_sources.py table \
  --table off=data/additives.csv --table yuka=yuka_additives.csv \
  --out out/additives.merge.csv --gaps out/additives.gaps.json
```

`fetch` uses `FDC_API_KEY` when present, otherwise `DEMO_KEY` (30 requests/hour:
volumes need the free FDC key). The merge never invents values: if a source is
missing, the field keeps the provenance of the other one and confidence drops.

## Performance

- Standard library only: no dependencies, instant start.
- On-disk SQLite cache (`cache/off.sqlite`, 30-day TTL, with a signature of the
  requested fields): the second scan of the same barcode does not touch the
  network, and if the field list changes the old cache is ignored.
- Requests with a selective `fields=` (minimal payload), a 60 rpm rate limiter
  that **slows itself down** when Open Food Facts replies 429, retries with
  backoff and a second pass for failed barcodes.
- Parallel batch (`--workers`) with a thread pool; per-barcode errors without
  interrupting the scan.

## Structure

```
main.py                     CLI (scan / file / batch / additives)
ui/server.py                HTTP server and API of the test UI
ui/static/                  page, CSS and JS (no dependencies)
ui/demo_product.json        offline demo product
engine/off_client.py        Open Food Facts client, cache, rate limit, normalization
engine/off_upload.py        photo upload to Open Food Facts (CGI endpoint)
engine/scoring.py           recalibrated Nutri-Score + additive aggregation
engine/additives.py         additive table and risks by class
engine/models.py            dataclasses (product, additive, components, result)
engine/report.py            text rendering
engine/merge.py             weighted median, provenance, conflicts, confidence
engine/source_online.py     fetch OFF / USDA FDC / Open Prices
engine/sources.py           multi-source export adapters (JSON/JSONL/CSV)
tools/build_additive_table.py  builds data/*.csv from the OFF taxonomy
tools/cross_source.py       product matching + consensus table + gaps
tools/merge_sources.py      fetch/merge/table CLI on the three sources
tests/test_engine.py        offline tests (unittest)
tests/test_cross_source.py  multi-source comparison tests
tests/test_merge.py         merger and adapter tests
tests/test_ui.py            UI tests (server, demo, JSON, contribution)
tests/test_off_upload.py    OFF photo upload tests
tests/test_italian_products.py  100 deterministic Italian products
tools/italian_sample.py     live check on 100 real Italian products
```

```bash
python -m unittest discover -s tests -t .   # 210 tests, no network required
python tools/italian_sample.py --limit 100  # live check, requires a network
```

## Known limits

- Open Food Facts does not report the **quantities** of individual additives: the
  risk is by presence, not by dose.
- Taxonomy coverage is not complete (some E2xx arrive without a functional class;
  in that case the E2xx family is treated as preservatives).
- Some products have `nutrition_data_per` expressed per serving and not per 100 g:
  the engine uses the `_100g` fields when present.
- The "is a beverage" recognition is based on the categories/units declared by OFF.
- Matching without a barcode uses name+brand (similarity ≥ 0.90): if two products
  share a name but have different barcodes they stay separate and end up among the
  ambiguous ones, they are not merged by force.
- USDA FDC mostly covers US products: matching a European EAN only works when the
  GTIN really matches (UPC-12 and EAN-13 normalized).
- Open Prices holds no nutrients: it adds price/store/date and confirms the
  barcode identity, not the nutritional profile.
- Without `FDC_API_KEY` the `DEMO_KEY` is used (30 requests/hour): `fetch` on long
  lists stops with an explicit error, not with invented data.
