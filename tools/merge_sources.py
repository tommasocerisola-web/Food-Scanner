"""
Merge the three sources from the command line.

    python tools/merge_sources.py fetch --barcodes barcodes.txt --out out/sources
    python tools/merge_sources.py merge \\
        --source off=out/sources/off.json \\
        --source fdc=out/sources/fdc.json \\
        --source openprices=out/sources/openprices.json \\
        --out out/merged.json --csv out/merged.csv
    python tools/merge_sources.py table \\
        --table off=data/additives.csv --table yuka=yuka_additives.csv \\
        --out out/additives.merge.csv --gaps out/additives.gaps.json

`fetch` downloads the three sources for a list of barcodes; `merge` combines the
data with the algorithm in `engine/merge.py`; `table` builds the consensus on the
risk tables.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import AdditiveTable  # noqa: E402
from engine.merge import merge_products, merge_risk_tables  # noqa: E402
from engine.source_online import FetchError, fetch_source  # noqa: E402
from engine.sources import load_source  # noqa: E402
from tools.cross_source import cluster_products, load_risk_table  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TABLE = ROOT / "data" / "additives.csv"

CSV_FIELDS = (
    "barcode",
    "name",
    "brand",
    "quantity",
    "kcal",
    "sugars",
    "salt",
    "saturated_fat",
    "fiber",
    "protein",
    "additives",
    "additive_score",
    "grade",
    "nova",
    "price",
    "price_basis",
    "confidence",
    "sources",
    "flags",
)


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def _read_barcodes(path: Path) -> list[str]:
    codes: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        token = token.split(";", 1)[0].split(",", 1)[0].split("\t", 1)[0].strip()
        if token and token not in codes:
            codes.append(token)
    return codes


def cmd_fetch(args: argparse.Namespace) -> int:
    barcodes = _read_barcodes(Path(args.barcodes))
    if args.limit and args.limit > 0:
        barcodes = barcodes[: args.limit]
    if not barcodes:
        print("no barcode in the file", file=sys.stderr)
        return 2

    sources = [name.strip() for name in args.sources.split(",") if name.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    collected: dict[str, list[dict]] = {source: [] for source in sources}
    errors: list[dict] = []
    disabled: set[str] = set()

    def flush() -> None:
        # Incremental writing: a timeout must not lose the products already fetched.
        for source, products in collected.items():
            (out / f"{source}.json").write_text(
                json.dumps(products, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        (out / "fetch_errors.json").write_text(
            json.dumps(errors, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    for index, barcode in enumerate(barcodes, 1):
        seed = None
        if "off" in sources and "off" not in disabled:
            try:
                seed = fetch_source("off", barcode)
            except FetchError as error:
                errors.append({"barcode": barcode, "source": "off", "error": str(error)})
                if _quota_error(error):
                    disabled.add("off")
            if seed is not None:
                collected["off"].append(seed.to_dict())

        for source in sources:
            if source == "off" or source in disabled:
                continue
            try:
                product = fetch_source(
                    source,
                    barcode,
                    name=seed.name if seed else "",
                    brand=seed.brand if seed else "",
                )
            except FetchError as error:
                errors.append({"barcode": barcode, "source": source, "error": str(error)})
                if _quota_error(error):
                    disabled.add(source)
                continue
            if product is not None:
                collected[source].append(product.to_dict())

        flush()
        found = ", ".join(
            f"{source}={len(collected[source])}" + (" (stop)" if source in disabled else "")
            for source in sources
        )
        print(f"[{index}/{len(barcodes)}] {barcode}: {found}")

    flush()
    print(f"wrote {out} · errors: {len(errors)}")
    return 0


def _quota_error(error: Exception) -> bool:
    """Hourly/key limit: there is no point asking the same source again."""
    text = str(error).lower()
    return "demo_key" in text or "limit" in text or "limite" in text or "429" in text


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def _split_spec(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise ValueError(f"spec without '=': {spec} (use name=file)")
    name, raw = spec.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError(f"empty source name: {spec}")
    path = Path(raw.strip())
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    return name, path


def cmd_merge(args: argparse.Namespace) -> int:
    products: dict[str, list] = {}
    files: dict[str, str] = {}
    for spec in args.source:
        name, path = _split_spec(spec)
        products[name] = load_source(name, path)
        files[name] = str(path)

    table = AdditiveTable.load(args.additives) if args.additives else None
    clusters, ambiguous = cluster_products(products)
    merged = merge_products([cluster.entries for cluster in clusters.values()], table=table)

    payload = {
        "generator": "generic_01 merge_sources",
        "sources": {
            name: {"file": files[name], "products": len(entries)}
            for name, entries in products.items()
        },
        "total_products": sum(len(entries) for entries in products.values()),
        "clusters": len(clusters),
        "products": merged,
        "ambiguous": ambiguous,
        "unmatched": [
            {
                "barcode": cluster.barcode,
                "name": cluster.name,
                "source": next(iter(cluster.entries)),
            }
            for cluster in clusters.values()
            if len(cluster.entries) == 1
        ],
        "summary": {
            "shared": sum(1 for cluster in clusters.values() if len(cluster.entries) > 1),
            "conflicts": sum(
                1
                for item in merged
                if item["conflicts"] or "scores_conflict" in item["flags"]
            ),
            "without_price": sum(1 for item in merged if item["price"] is None),
            "mean_confidence": (
                round(sum(item["confidence"] for item in merged) / len(merged), 3) if merged else 0.0
            ),
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for item in merged:
                nutrients = item["nutrients"]
                writer.writerow(
                    {
                        "barcode": item["barcode"],
                        "name": item["name"],
                        "brand": item["brand"],
                        "quantity": item["quantity"],
                        "kcal": nutrients.get("kcal", ""),
                        "sugars": nutrients.get("sugars", ""),
                        "salt": nutrients.get("salt", ""),
                        "saturated_fat": nutrients.get("saturated_fat", ""),
                        "fiber": nutrients.get("fiber", ""),
                        "protein": nutrients.get("protein", ""),
                        "additives": "|".join(row["tag"] for row in item["additives"]),
                        "additive_score": item["additive_score"] if item["additive_score"] is not None else "",
                        "grade": item["grade"] or "",
                        "nova": item["nova"] if item["nova"] is not None else "",
                        "price": item["price"] if item["price"] is not None else "",
                        "price_basis": item["price_basis"],
                        "confidence": item["confidence"],
                        "sources": "|".join(item["sources"]),
                        "flags": "|".join(item["flags"]),
                    }
                )

    print(
        f"{len(merged)} products · {payload['summary']['shared']} shared · "
        f"{payload['summary']['conflicts']} with conflicts -> {out}"
    )
    return 0


# --------------------------------------------------------------------------- #
# table
# --------------------------------------------------------------------------- #
def cmd_table(args: argparse.Namespace) -> int:
    tables: dict[str, dict[str, float | None]] = {}
    for spec in args.table:
        name, path = _split_spec(spec)
        rows = load_risk_table(path)
        tables[name] = {key: row.risk for key, row in rows.items()}

    merged = merge_risk_tables(tables)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "e_number",
                "risk_consensus",
                "n_sources",
                "sources",
                "missing_in",
                "spread",
                "agreement",
                "confidence",
                "needs_review",
                "reasons",
            ),
        )
        writer.writeheader()
        for row in merged:
            writer.writerow(
                {
                    **row,
                    "sources": "|".join(row["sources"]),
                    "missing_in": "|".join(row["missing_in"]),
                    "reasons": "; ".join(row["reasons"]),
                }
            )

    gaps = [row for row in merged if row["needs_review"]]
    gaps_path = Path(args.gaps)
    gaps_path.parent.mkdir(parents=True, exist_ok=True)
    gaps_path.write_text(
        json.dumps(
            {"generator": "generic_01 merge_sources", "gaps": gaps},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    print(
        f"{len(merged)} additives · {len(merged) - len(gaps)} agreeing · "
        f"{len(gaps)} to review -> {out}"
    )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merge_sources",
        description="Merge Open Food Facts, USDA FDC and Open Prices into a single record.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download the sources for a list of barcodes")
    fetch.add_argument("--barcodes", required=True, help="file with one barcode per line")
    fetch.add_argument("--out", default="out/sources", help="output folder")
    fetch.add_argument("--sources", default="off,fdc,openprices", help="comma-separated sources")
    fetch.add_argument("--limit", type=int, default=0, help="max barcodes (0 = all)")
    fetch.set_defaults(func=cmd_fetch)

    merge = sub.add_parser("merge", help="merge the source exports")
    merge.add_argument("--source", action="append", required=True, help="name=file.json")
    merge.add_argument("--out", default="out/merged.json")
    merge.add_argument("--csv", default="out/merged.csv")
    merge.add_argument("--additives", default=str(DEFAULT_TABLE), help="additive table")
    merge.set_defaults(func=cmd_merge)

    table = sub.add_parser("table", help="consensus across additive risk tables")
    table.add_argument("--table", action="append", required=True, help="name=file.csv")
    table.add_argument("--out", default="out/additives.merge.csv")
    table.add_argument("--gaps", default="out/additives.gaps.json")
    table.set_defaults(func=cmd_table)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
