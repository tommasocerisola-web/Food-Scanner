"""
Collaudo live su prodotti italiani reali di Open Food Facts.

Uso:
    python tools/italian_sample.py --limit 100
    python tools/italian_sample.py --limit 100 --seed 2026 --out out/italian_sample.json

Non è un test unitario: scarica un campione casuale riproducibile di prodotti
italiani, applica la normalizzazione e verifica le invarianti del motore.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import AdditiveTable, ScoreEngine, normalize_product  # noqa: E402
from engine.models import grade_for  # noqa: E402
from engine.scoring import ENGINE_VERSION  # noqa: E402

SEARCH_URL = "https://world.openfoodfacts.org/api/v2/search"
# OFF applica anti-bot agli User-Agent "applicativi" senza un contatto verificabile;
# per la ricerca usiamo un UA browser-like, mantenendo il nome del progetto.
SEARCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FIELDS = ",".join(
    (
        "code",
        "product_name",
        "product_name_it",
        "brands",
        "quantity",
        "product_quantity_unit",
        "nutriments",
        "additives_tags",
        "nova_group",
        "nutriscore_grade",
        "categories_tags",
        "labels_tags",
        "ingredients_text",
        "ingredients_text_it",
        "ingredients",
        "image_front_url",
        "image_nutrition_url",
    )
)


def fetch_page(page: int, page_size: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "countries_tags": "en:italy",
            "page_size": page_size,
            "page": page,
            "fields": FIELDS,
        }
    )
    request = urllib.request.Request(
        f"{SEARCH_URL}?{query}",
        headers={"User-Agent": SEARCH_USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_sample(
    limit: int, seed: int, page_size: int = 100
) -> tuple[list[dict[str, Any]], list[int]]:
    rng = random.Random(seed)
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages_used: list[int] = []
    page_size = min(100, max(1, page_size))

    first = fetch_page(1, page_size)
    page_count = max(1, int(first.get("page_count") or 1))
    pages = list(range(1, page_count + 1))
    rng.shuffle(pages)

    for page in pages:
        if len(products) >= limit:
            break
        payload = fetch_page(page, page_size)
        pages_used.append(page)
        for product in payload.get("products") or []:
            if not isinstance(product, dict):
                continue
            code = str(product.get("code") or "").strip()
            name = str(product.get("product_name_it") or product.get("product_name") or "").strip()
            if not code or not name or code in seen:
                continue
            seen.add(code)
            products.append(product)
            if len(products) >= limit:
                break
        time.sleep(0.2)

    return products, pages_used


def check_product(engine: ScoreEngine, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    record = normalize_product(raw)
    result = engine.score(record)
    problems: list[str] = []

    if not 0 <= result.score <= 100:
        problems.append(f"score fuori range: {result.score}")
    if result.grade != grade_for(result.score):
        problems.append("grade incoerente con lo score")
    if not 0.0 <= result.confidence <= 1.0:
        problems.append(f"confidence fuori range: {result.confidence}")
    if not 0.0 <= result.additive_coverage <= 1.0:
        problems.append(f"copertura additivi fuori range: {result.additive_coverage}")
    if abs(sum(component.weight for component in result.components) - 1.0) > 1e-6:
        problems.append("pesi delle componenti non sommano 1")
    if result.engine_version != ENGINE_VERSION:
        problems.append(f"versione motore inattesa: {result.engine_version}")
    if any(
        component.score is not None and not 0 <= component.score <= 100
        for component in result.components
    ):
        problems.append("componente fuori range")
    if any(not 0 <= hit.risk <= 100 for hit in result.additives):
        problems.append("rischio additivo fuori range")

    entry = {
        "barcode": record.barcode,
        "name": record.name,
        "brand": record.brand,
        "score": result.score,
        "grade": result.grade,
        "confidence": round(result.confidence, 3),
        "additives": len(result.additives),
        "is_beverage": record.is_beverage,
        "nutrition": next(
            (component.score for component in result.components if component.name == "nutrition"),
            None,
        ),
        "problems": problems,
    }
    return entry, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collauda prodotti italiani reali da Open Food Facts.")
    parser.add_argument("--limit", type=int, default=100, help="Numero di prodotti (default 100)")
    parser.add_argument("--seed", type=int, default=2026, help="Seed per la scelta delle pagine")
    parser.add_argument("--page-size", type=int, default=100, help="Prodotti per pagina OFF")
    parser.add_argument("--out", type=Path, default=Path("out/italian_sample.json"))
    args = parser.parse_args(argv)

    if args.limit <= 0:
        parser.error("--limit deve essere positivo")

    try:
        raw_products, pages = fetch_sample(args.limit, args.seed, args.page_size)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        print(f"Open Food Facts non raggiungibile: {error}", file=sys.stderr)
        return 2

    if len(raw_products) < args.limit:
        print(
            f"Trovati solo {len(raw_products)} prodotti con nome e barcode su {args.limit} richiesti.",
            file=sys.stderr,
        )
        return 2

    table = AdditiveTable.load()
    engine = ScoreEngine(table)
    entries: list[dict[str, Any]] = []
    failures: list[str] = []

    print("=" * 96)
    print(f"COLLAUDO {args.limit} PRODOTTI ITALIANI · Open Food Facts · seed {args.seed}")
    print("=" * 96)
    print(f"{'#':>3}  {'barcode':<14} {'score':>5} {'conf':>5} {'add':>4}  nome")
    print("-" * 96)

    for index, raw in enumerate(raw_products[: args.limit], start=1):
        entry, problems = check_product(engine, raw)
        entries.append(entry)
        failures.extend(f"{entry['barcode']}: {problem}" for problem in problems)
        nutrition = entry["nutrition"]
        nutrition_text = "-" if nutrition is None else f"{nutrition:.0f}"
        print(
            f"{index:>3}  {entry['barcode']:<14} {entry['score']:>5} "
            f"{entry['confidence'] * 100:>4.0f}% {entry['additives']:>4}  "
            f"{entry['name'][:52]}  [nutri {nutrition_text}]"
        )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "source": "Open Food Facts",
                    "country": "Italy",
                    "seed": args.seed,
                    "pages": pages,
                    "count": len(entries),
                    "engine_version": ENGINE_VERSION,
                    "products": entries,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    scores = [entry["score"] for entry in entries]
    missing_nutrition = sum(entry["nutrition"] is None for entry in entries)
    print()
    print(
        f"Prodotti: {len(entries)} · media {statistics.mean(scores):.0f} · "
        f"min {min(scores)} · max {max(scores)} · senza nutrienti {missing_nutrition} · "
        f"pagine OFF {', '.join(map(str, pages))}"
    )
    if failures:
        print(f"Problemi: {len(failures)}", file=sys.stderr)
        for failure in failures[:20]:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("Esito: OK · tutte le invarianti rispettate")
    if args.out:
        print(f"Report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
