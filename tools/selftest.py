"""
End-to-end self-test: Open Food Facts API + engine invariants + real products.

Usage:
    python tools/selftest.py            # uses the cache
    python tools/selftest.py --no-cache # forces the network

This is not a unit test: it is a diagnostic check that prints what does not add up.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import (  # noqa: E402
    AdditiveTable,
    OFFClient,
    OFFError,
    ScoreEngine,
    normalize_product,
)
from engine.off_client import DEFAULT_FIELDS, DEFAULT_USER_AGENT  # noqa: E402

# Products chosen to cover different categories (soft drinks, cold cuts, sweets, dairy, snacks).
CURATED = [
    ("5449000000996", "soft drink"),
    ("5449000011527", "soft drink"),
    ("9002490100070", "energy drink"),
    ("3017620422003", "spread"),
    ("7622210449283", "biscuits"),
    ("5000159484695", "chocolate snack"),
    ("4008400402222", "chocolate"),
    ("3095758049019", "cooked cold cut"),
    ("8480000057105", "turkey cold cut"),
    ("6111246721261", "dairy product"),
    ("3033490003175", "yoghurt"),
    ("3229820129488", "cereals/organic"),
    ("3560070464290", "Carrefour product"),
    ("3168930010265", "unknown product"),
]

ANOMALIES: list[tuple[str, str]] = []


def flag(severity: str, message: str) -> None:
    ANOMALIES.append((severity, message))


# --------------------------------------------------------------------------- #
# A) API
# --------------------------------------------------------------------------- #
def check_api(client: OFFClient) -> dict:
    report: dict[str, object] = {}
    url = client.product_url("5449000000996")
    report["url"] = url

    request = urllib.request.Request(
        url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
        report["status_code"] = response.status
        report["content_type"] = response.headers.get("content-type")
    report["latency_ms"] = round((time.perf_counter() - started) * 1000)

    product = payload.get("product") or {}
    report["keys_returned"] = len(product)
    # `ingredients` is requested on purpose now (needed for the doses): the check
    # verifies that heavy, unrequested payloads do not arrive.
    report["fields_honoured"] = "images" not in product and "ingredients_original" not in product
    report["has_status"] = payload.get("status")
    report["code_matches"] = str(product.get("code")) == "5449000000996"

    required = {"code", "nutriments", "additives_tags", "categories_tags", "labels_tags"}
    missing = required - set(product)
    if missing:
        flag("API", f"expected fields missing from the response: {sorted(missing)}")
    report["required_fields_ok"] = not missing

    # barcode inesistente -> OFFError pulito
    started = time.perf_counter()
    try:
        client.fetch("0000000000000", use_cache=False)
        flag("API", "unknown barcode did not raise OFFError")
        report["not_found_handled"] = False
    except OFFError as error:
        report["not_found_handled"] = True
        report["not_found_message"] = str(error)[:80]
    report["not_found_ms"] = round((time.perf_counter() - started) * 1000)

    # cache: dedicated client with a clean temporary cache (the shared client
    # above may have the cache disabled by --no-cache).
    cache_path = Path("cache/selftest-cache.sqlite")
    cache_path.unlink(missing_ok=True)
    cache_client = OFFClient(cache_path=cache_path)
    t0 = time.perf_counter()
    first = cache_client.fetch("3017620422003", use_cache=True)
    t_first = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    second = cache_client.fetch("3017620422003", use_cache=True)
    t_second = (time.perf_counter() - t0) * 1000
    cache_client.close()
    cache_path.unlink(missing_ok=True)
    report["fresh_ms"] = round(t_first)
    report["cache_ms"] = round(t_second, 2)
    report["cache_same_payload"] = first == second
    report["cache_faster"] = t_second < t_first

    # batch parallelo
    t0 = time.perf_counter()
    batch = client.fetch_many(["5449000000996", "3017620422003", "0000000000000"], workers=3)
    report["batch_s"] = round(time.perf_counter() - t0, 2)
    report["batch_partial_failure_isolated"] = (
        "0000000000000" in batch and isinstance(batch["0000000000000"], Exception)
        and not isinstance(batch["5449000000996"], Exception)
    )

    report["fields_param_size"] = len(DEFAULT_FIELDS.split(","))
    return report


# --------------------------------------------------------------------------- #
# B) Motore
# --------------------------------------------------------------------------- #
def check_result(code: str, result, record) -> list[str]:
    problems: list[str] = []
    if not (0 <= result.score <= 100):
        problems.append(f"score out of range: {result.score}")
    from engine.models import grade_for

    if result.grade != grade_for(result.score):
        problems.append("grade inconsistent with the score")
    if not (0.0 < result.confidence <= 1.0001):
        problems.append(f"confidence out of range: {result.confidence}")
    if abs(sum(c.weight for c in result.components) - 1.0) > 1e-6:
        problems.append("component weights do not add up to 1")
    for component in result.components:
        if component.score is not None and not (0 <= component.score <= 100):
            problems.append(f"component {component.name} out of range: {component.score}")
    if result.additives:
        worst = max(hit.risk for hit in result.additives)
        if abs((result.worst_additive.risk if result.worst_additive else 0) - worst) > 1e-6:
            problems.append("worst_additive does not match the maximum risk")
        for hit in result.additives:
            if not (0 <= hit.risk <= 100):
                problems.append(f"{hit.e_number} risk out of range: {hit.risk}")
    if not (0.0 <= result.additive_coverage <= 1.0):
        problems.append(f"additive_coverage out of range: {result.additive_coverage}")
    nutrition = next(c for c in result.components if c.name == "nutrition")
    if nutrition.score is not None:
        facts = record.nutriments
        if record.is_beverage:
            present, minimum = sum(v is not None for v in (facts.kcal, facts.sugars)), 2
        else:
            salt = facts.salt if facts.salt is not None else facts.sodium
            present = sum(v is not None for v in (facts.kcal, facts.sugars, facts.saturated_fat, salt))
            minimum = 3
        if present < minimum:
            problems.append(
                f"nutrients scored with {present} of 4 key values (missing data treated as 0)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Engine self-test")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--table", type=Path, default=None)
    args = parser.parse_args(argv)

    table = AdditiveTable.load(args.table)
    engine = ScoreEngine(table)
    client = OFFClient(cache_path=None if args.no_cache else Path("cache/off.sqlite"))

    print("=" * 78)
    print("A) OPEN FOOD FACTS API")
    print("=" * 78)
    api = check_api(client)
    for key, value in api.items():
        print(f"  {key:<32} {value}")

    print()
    print("=" * 78)
    print("B) REAL PRODUCTS")
    print("=" * 78)
    print(f"{'barcode':<14}{'score':>6} {'conf':>5} {'nut':>5} {'add':>5} {'n':>3}  name / notes")
    print("-" * 78)

    details: list[dict] = []
    for code, label in CURATED:
        try:
            raw = client.fetch_raw(code)
            record = normalize_product(raw)
        except OFFError as error:
            print(f"{code:<14}{'--':>6}  unavailable: {str(error)[:44]}")
            continue

        result = engine.score(record)
        unknown_tags = [
            tag for tag in record.additives if table.get(tag) is None
        ]
        nutrition = next(c for c in result.components if c.name == "nutrition")
        additives = next(c for c in result.components if c.name == "additives")
        note_bits = [label]
        if record.is_beverage:
            note_bits.append("beverage")
        if unknown_tags:
            note_bits.append(f"unknown OFF tags: {unknown_tags}")
        if nutrition.details.get("salt_from_sodium"):
            note_bits.append("salt from sodium")
        if record.additives and additives.details.get("estimated"):
            note_bits.append(f"{additives.details['estimated']} estimated additives")
        if result.additive_coverage < 1.0:
            note_bits.append(f"risk coverage {result.additive_coverage * 100:.0f}%")
        problems = check_result(code, result, record)
        for problem in problems:
            flag("ENGINE", f"{code}: {problem}")
        if unknown_tags:
            flag("TABLE", f"{code}: unresolved OFF additives {unknown_tags}")

        print(
            f"{code:<14}{result.score:>6} {result.confidence * 100:>4.0f}% "
            f"{(nutrition.score if nutrition.score is not None else -1):>5.0f} "
            f"{(additives.score if additives.score is not None else -1):>5.0f} "
            f"{len(result.additives):>3}  {' · '.join(note_bits)[:44]}"
        )

        details.append(
            {
                "code": code,
                "name": record.name,
                "score": result.score,
                "grade": result.grade,
                "nutriscore_off": record.nutriscore_grade,
                "nutrition": nutrition.score,
                "beverage": record.is_beverage,
                "nutriments": record.nutriments.to_dict(),
                "additives": [hit.e_number for hit in result.additives],
                "confidence": round(result.confidence, 3),
                "alerts": [alert.code for alert in result.alerts],
            }
        )

    print()
    print("=" * 78)
    print("C) AGREEMENT WITH THE OPEN FOOD FACTS NUTRI-SCORE")
    print("=" * 78)
    grade_to_ns = {"a": 95, "b": 78, "c": 60, "d": 38, "e": 15}
    for item in details:
        off_grade = item["nutriscore_off"]
        if off_grade in grade_to_ns and item["nutrition"] is not None:
            delta = item["nutrition"] - grade_to_ns[off_grade]
            if abs(delta) > 25:
                flag(
                    "CALIBRATION",
                    f"{item['code']}: nutrition {item['nutrition']:.0f} vs OFF "
                    f"Nutri-Score {off_grade.upper()} (~{grade_to_ns[off_grade]})",
                )

    print()
    print("=" * 78)
    print("D) ANOMALIES")
    print("=" * 78)
    if not ANOMALIES:
        print("  no anomaly detected")
    for severity, message in ANOMALIES:
        print(f"  [{severity}] {message}")

    print()
    scores = [item["score"] for item in details]
    if scores:
        print(
            f"Products scored: {len(scores)} · mean {statistics.mean(scores):.0f} · "
            f"min {min(scores)} · max {max(scores)}"
        )
    api_failures = sum(
        1
        for key in ("fields_honoured", "has_status", "code_matches", "not_found_handled", "cache_faster")
        if not api.get(key)
    )
    client.close()
    return 1 if ANOMALIES or api_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
