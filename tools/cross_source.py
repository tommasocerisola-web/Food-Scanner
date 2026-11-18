"""
Multi-source comparison: shared products, consensus additive table, gaps.

The flow designed for several sources (Open Food Facts, Yuka, a third table):

  1. `match` takes the source exports, merges products by barcode (falling back
     to name+brand) and reports how many products are shared, where the sources
     contradict each other and which additives are still missing from the table.
  2. `table` takes a risk table per source, computes the consensus (median),
     flags the discordant or missing rows and writes the list of "missing
     pieces" to fix.

Usage:
    python tools/cross_source.py match \
        --source off=export_off.json --source yuka=export_yuka.json \
        --source third=export_third.csv --out out/match.json --csv out/match.csv

    python tools/cross_source.py table \
        --table off=data/additives.csv --table yuka=yuka_additives.csv \
        --out data/additives.consensus.csv --gaps out/gaps.json

No source is ever modified: the consensus is a proposal, the official table
(`data/additives.csv`) stays the curated one.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import AdditiveTable  # noqa: E402
from engine.additives import canonical  # noqa: E402
from engine.sources import (  # noqa: E402
    SourceProduct,
    barcode_key,
    load_source,
    name_key,
    norm_text,
)

MAX_SPREAD = 15.0   # points: beyond this, two sources contradict on the same product
SIMILARITY = 0.90   # name+brand similarity for matching without a barcode


# --------------------------------------------------------------------------- #
# Product matching
# --------------------------------------------------------------------------- #
@dataclass
class Cluster:
    """The same product seen by several sources."""

    key: str
    barcode: str = ""
    name: str = ""
    brand: str = ""
    how: str = "barcode"
    entries: dict[str, SourceProduct] = field(default_factory=dict)
    duplicates: dict[str, int] = field(default_factory=dict)

    @property
    def scores(self) -> dict[str, float]:
        return {source: entry.score for source, entry in self.entries.items() if entry.score is not None}

    @property
    def spread(self) -> float:
        values = list(self.scores.values())
        return round(max(values) - min(values), 1) if len(values) > 1 else 0.0


def cluster_products(
    products_by_source: dict[str, list[SourceProduct]],
) -> tuple[dict[str, Cluster], list[dict[str, Any]]]:
    """Merge the products of the sources; returns clusters and ambiguous matches."""
    clusters: dict[str, Cluster] = {}
    loose: list[SourceProduct] = []
    ambiguous: list[dict[str, Any]] = []

    for source, products in products_by_source.items():
        for product in products:
            key = barcode_key(product.barcode)
            if not key:
                loose.append(product)
                continue
            cluster = clusters.get(key)
            if cluster is None:
                cluster = Cluster(key=key, barcode=product.barcode, name=product.name, brand=product.brand)
                clusters[key] = cluster
            elif source in cluster.entries:
                cluster.duplicates[source] = cluster.duplicates.get(source, 0) + 1
                continue

            cluster.entries[source] = product
            if not cluster.name:
                cluster.name = product.name
            if not cluster.brand:
                cluster.brand = product.brand

    for product in loose:
        candidates = _best_candidates(product, clusters.values())
        if len(candidates) == 1:
            cluster = candidates[0]
            if product.source in cluster.entries:
                cluster.duplicates[product.source] = cluster.duplicates.get(product.source, 0) + 1
                continue
            cluster.entries[product.source] = product
            if not cluster.name:
                cluster.name = product.name
            if not cluster.brand:
                cluster.brand = product.brand
            cluster.how = "barcode+name"
        elif len(candidates) > 1:
            ambiguous.append(
                {
                    "source": product.source,
                    "name": product.name,
                    "brand": product.brand,
                    "candidates": [candidate.barcode or candidate.key for candidate in candidates],
                }
            )
        else:
            cluster = clusters.get("name:" + name_key(product.name, product.brand))
            if cluster is None:
                cluster = Cluster(
                    key="name:" + name_key(product.name, product.brand),
                    name=product.name,
                    brand=product.brand,
                    how="name",
                )
                clusters[cluster.key] = cluster
            if product.source in cluster.entries:
                cluster.duplicates[product.source] = cluster.duplicates.get(product.source, 0) + 1
                continue
            cluster.entries[product.source] = product

    return clusters, ambiguous


def _best_candidates(product: SourceProduct, clusters: Any) -> list[Cluster]:
    """Compatible clusters by name+brand, only when the best one is clear-cut."""
    target = name_key(product.name, product.brand)
    if not target:
        return []
    scored: list[tuple[float, Cluster]] = []
    for cluster in clusters:
        if cluster.key.startswith("name:"):
            continue
        if cluster.entries.get(product.source) is not None:
            continue
        if not _brand_compatible(product.brand, cluster.brand):
            continue
        candidate = name_key(cluster.name, cluster.brand)
        if not candidate:
            continue
        ratio = SequenceMatcher(None, target, candidate).ratio()
        if ratio >= SIMILARITY:
            scored.append((ratio, cluster))
    if not scored:
        return []
    best = max(ratio for ratio, _ in scored)
    return [cluster for ratio, cluster in scored if best - ratio <= 0.02]


def _brand_compatible(left: str, right: str) -> bool:
    first = set(norm_text(left).split())
    second = set(norm_text(right).split())
    if not first or not second:
        return True
    return bool(first & second)


def build_report(
    products_by_source: dict[str, list[SourceProduct]],
    clusters: dict[str, Cluster],
    ambiguous: list[dict[str, Any]],
    paths: dict[str, Path],
    table: AdditiveTable | None,
) -> dict[str, Any]:
    sources = list(products_by_source)
    stats: dict[str, dict[str, Any]] = {}
    for source, products in products_by_source.items():
        with_barcode = sum(1 for product in products if barcode_key(product.barcode))
        stats[source] = {
            "file": str(paths.get(source, "")),
            "products": len(products),
            "with_barcode": with_barcode,
            "without_barcode": len(products) - with_barcode,
            "duplicates": sum(cluster.duplicates.get(source, 0) for cluster in clusters.values()),
        }

    unknown: dict[str, set[str]] = {}
    for products in products_by_source.values():
        for product in products:
            for tag in product.additives:
                if table is not None and table.get(tag) is None:
                    unknown.setdefault(tag, set()).add(product.label)

    payloads = [_cluster_payload(cluster, sources, table) for cluster in clusters.values()]
    payloads.sort(key=lambda item: (norm_text(item["name"]), item["barcode"] or item["key"]))

    common = [cluster for cluster in clusters.values() if len(cluster.entries) > 1]
    single = {
        source: sum(1 for cluster in clusters.values() if len(cluster.entries) == 1 and source in cluster.entries)
        for source in sources
    }

    return {
        "generator": "generic_01 cross_source",
        "sources": stats,
        "total_products": sum(len(products) for products in products_by_source.values()),
        "clusters": len(clusters),
        "shared": len(common),
        "single_source": single,
        "ambiguous": ambiguous,
        "unknown_additives": [
            {"tag": tag, "products": sorted(names)} for tag, names in sorted(unknown.items())
        ],
        "products": payloads,
    }


def _cluster_payload(cluster: Cluster, sources: list[str], table: AdditiveTable | None) -> dict[str, Any]:
    tags_by_source = {source: set(entry.additives) for source, entry in cluster.entries.items()}
    union = set().union(*tags_by_source.values()) if tags_by_source else set()
    discordant = len(tags_by_source) > 1 and any(tags != union for tags in tags_by_source.values())
    missing = [source for source in sources if source not in cluster.entries]
    unknown = sorted(tag for tag in union if table is not None and table.get(tag) is None)

    flags: list[str] = []
    if missing:
        flags.append("missing_in:" + ",".join(missing))
    if cluster.spread > MAX_SPREAD:
        flags.append(f"score_divergent:{cluster.spread:g}")
    if discordant:
        flags.append("additives_conflict")
    if unknown:
        flags.append("unknown_additives:" + ",".join(unknown))

    return {
        "key": cluster.key,
        "barcode": cluster.barcode,
        "name": cluster.name or next((entry.name for entry in cluster.entries.values()), ""),
        "brand": cluster.brand or next((entry.brand for entry in cluster.entries.values()), ""),
        "match": cluster.how,
        "sources": sorted(cluster.entries),
        "scores": {source: score for source, score in cluster.scores.items()},
        "spread": cluster.spread,
        "missing_in": missing,
        "additives": {source: sorted(tags) for source, tags in tags_by_source.items()},
        "unknown_additives": unknown,
        "flags": flags,
    }


def write_match_csv(path: Path, report: dict[str, Any], sources: list[str]) -> None:
    fields = ["barcode", "name", "brand", "sources", "spread", "missing_in", "additives_conflict", "unknown_additives"]
    fields += [f"score_{_safe(source)}" for source in sources]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in report["products"]:
            row = {
                "barcode": item["barcode"],
                "name": item["name"],
                "brand": item["brand"],
                "sources": ";".join(item["sources"]),
                "spread": item["spread"],
                "missing_in": ";".join(item["missing_in"]),
                "additives_conflict": "yes" if "additives_conflict" in item["flags"] else "",
                "unknown_additives": ";".join(item["unknown_additives"]),
            }
            for source in sources:
                row[f"score_{_safe(source)}"] = item["scores"].get(source, "")
            writer.writerow(row)


# --------------------------------------------------------------------------- #
# Risk tables and consensus
# --------------------------------------------------------------------------- #
@dataclass
class RiskRow:
    e_number: str
    name: str = ""
    risk: float | None = None
    why: str = ""


E_FIELDS = ("e_number", "e", "code", "codice", "additive", "additivo")
RISK_FIELDS = ("risk_score", "risk", "score", "danger", "pericolosita", "punteggio")
NAME_FIELDS = ("name", "name_en", "name_it", "nome")
WHY_FIELDS = ("why", "reason", "motivo", "spiegazione")


def load_risk_table(path: Path | str) -> dict[str, RiskRow]:
    """Read a risk table (CSV or JSON) indexed by E-number."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    if not text.strip():
        return {}
    if text.lstrip()[:1] in "[{":
        records = _table_records(json.loads(text))
    else:
        records = _table_csv(text)

    rows: dict[str, RiskRow] = {}
    for record in records:
        entry = _risk_row(record)
        if entry is not None:
            rows.setdefault(entry.e_number, entry)
    return rows


def _table_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # A single record (not an E-number -> risk map).
        if {str(key).lower() for key in payload} & set(E_FIELDS):
            return [payload]
        for key in ("additives", "rows", "data", "items", "additivi"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        records: list[Any] = []
        for key, value in payload.items():
            if isinstance(value, dict):
                record = dict(value)
                record.setdefault("e_number", key)
                records.append(record)
            else:
                records.append({"e_number": key, "risk_score": value})
        return records
    return []


def _table_csv(text: str) -> list[dict[str, Any]]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [dict(row) for row in csv.DictReader(text.splitlines(), dialect=dialect)]


def _risk_row(record: dict[str, Any]) -> RiskRow | None:
    lowered = {str(key).lower(): value for key, value in record.items()}
    e_number = _as_e_number(_first(lowered, E_FIELDS))
    if not e_number:
        return None
    return RiskRow(
        e_number=e_number,
        name=_text(_first(lowered, NAME_FIELDS)),
        risk=_risk(_first(lowered, RISK_FIELDS)),
        why=_text(_first(lowered, WHY_FIELDS)),
    )


def merge_tables(tables: dict[str, dict[str, RiskRow]]) -> list[dict[str, Any]]:
    """Consensus per E-number: median, agreement, missing sources, fix reason."""
    keys = sorted(set().union(*(set(table) for table in tables.values())), key=_e_sort)
    merged: list[dict[str, Any]] = []
    for key in keys:
        present = {source: table[key] for source, table in tables.items() if key in table}
        risks = [row.risk for row in present.values() if row.risk is not None]
        consensus = round(statistics.median(risks), 1) if risks else None
        spread = round(max(risks) - min(risks), 1) if len(risks) > 1 else 0.0
        agreement = (
            round(sum(1 for risk in risks if abs(risk - consensus) <= 10) / len(risks), 2)
            if risks
            else 0.0
        )
        missing = [source for source in tables if source not in present]
        reasons: list[str] = []
        if consensus is None:
            reasons.append("no score")
        if missing:
            reasons.append("missing in: " + ", ".join(missing))
        if spread > 25:
            reasons.append(f"sources disagree (±{spread:g})")
        if len(risks) < len(present):
            reasons.append("score missing in one source")

        merged.append(
            {
                "e_number": key,
                "name": next((row.name for row in present.values() if row.name), ""),
                "risk_consensus": consensus,
                "agreement": agreement,
                "n_sources": len(present),
                "spread": spread,
                "missing_in": missing,
                "needs_fix": bool(reasons),
                "reasons": reasons,
                "risks": {source: row.risk for source, row in present.items()},
                "why": max((row.why for row in present.values()), key=len, default=""),
            }
        )
    return merged


def write_consensus_csv(path: Path, rows: list[dict[str, Any]], sources: list[str]) -> None:
    fields = ["e_number", "name", "risk_consensus", "agreement", "n_sources", "spread", "missing_in", "needs_fix"]
    fields += [f"risk_{_safe(source)}" for source in sources]
    fields.append("why")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            payload = {
                "e_number": row["e_number"],
                "name": row["name"],
                "risk_consensus": "" if row["risk_consensus"] is None else row["risk_consensus"],
                "agreement": row["agreement"],
                "n_sources": row["n_sources"],
                "spread": row["spread"],
                "missing_in": ";".join(row["missing_in"]),
                "needs_fix": "true" if row["needs_fix"] else "false",
                "why": row["why"],
            }
            for source in sources:
                value = row["risks"].get(source)
                payload[f"risk_{_safe(source)}"] = "" if value is None else value
            writer.writerow(payload)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_specs(values: list[str]) -> dict[str, Path]:
    specs: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        name, raw_path = name.strip(), raw_path.strip()
        if not separator or not name or not raw_path:
            raise SystemExit(f"expected name=path, received: {value}")
        specs[name] = Path(raw_path)
    if not specs:
        raise SystemExit("specify at least one source")
    return specs


def _parse_score_max(values: list[str]) -> dict[str, float]:
    limits: dict[str, float] = {}
    for value in values:
        name, separator, raw = value.partition("=")
        if not separator or not name.strip():
            raise SystemExit(f"expected name=scale, received: {value}")
        try:
            limits[name.strip()] = float(raw)
        except ValueError as error:
            raise SystemExit(f"invalid scale for {name}: {raw}") from error
    return limits


def cmd_match(args: argparse.Namespace) -> int:
    specs = _parse_specs(args.source)
    limits = _parse_score_max(args.score_max)
    products = {
        name: load_source(name, path, limits.get(name, 100.0)) for name, path in specs.items()
    }
    if not any(products.values()):
        print("no product loaded from the given sources", file=sys.stderr)
        return 2

    table = None
    if args.additives:
        table_path = Path(args.additives)
        if not table_path.exists():
            raise FileNotFoundError(f"additive table not found: {table_path}")
        table = AdditiveTable.load(table_path)

    clusters, ambiguous = cluster_products(products)
    report = build_report(products, clusters, ambiguous, specs, table)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_match_csv(csv_path, report, list(specs))

    print(
        f"{report['total_products']} products · {report['shared']} shared · "
        f"{len(report['ambiguous'])} ambiguous · "
        f"{len(report['unknown_additives'])} unknown additives -> {out}"
    )
    return 0


def cmd_table(args: argparse.Namespace) -> int:
    specs = _parse_specs(args.table)
    tables = {name: load_risk_table(path) for name, path in specs.items()}
    if not any(tables.values()):
        print("no additive row loaded from the given tables", file=sys.stderr)
        return 2

    rows = merge_tables(tables)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_consensus_csv(out, rows, list(specs))

    gaps = [row for row in rows if row["needs_fix"]]
    if args.gaps:
        gaps_path = Path(args.gaps)
        gaps_path.parent.mkdir(parents=True, exist_ok=True)
        gaps_path.write_text(
            json.dumps(
                {"sources": {name: len(table) for name, table in tables.items()}, "total": len(gaps), "gaps": gaps},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    print(
        f"{len(rows)} additives · {len(rows) - len(gaps)} agreeing · {len(gaps)} to fix -> {out}"
    )
    if args.gaps:
        print(f"list of missing pieces -> {args.gaps}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cross_source",
        description="Compare food sources: shared products and consensus tables",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    match = sub.add_parser("match", help="Find the products shared by several sources")
    match.add_argument("--source", action="append", default=[], metavar="NAME=FILE")
    match.add_argument("--additives", default="data/additives.csv", help="Additive table used to spot unknown tags")
    match.add_argument("--score-max", action="append", default=[], metavar="NAME=SCALE",
                       help="Score scale per source (default 100)")
    match.add_argument("--out", default="out/match.json")
    match.add_argument("--csv", default="out/match.csv")
    match.set_defaults(func=cmd_match)

    table = sub.add_parser("table", help="Merge the risk tables and find the gaps")
    table.add_argument("--table", action="append", default=[], metavar="NAME=FILE")
    table.add_argument("--out", default="data/additives.consensus.csv")
    table.add_argument("--gaps", default="out/gaps.json")
    table.set_defaults(func=cmd_table)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


def _safe(source: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", source).strip("_").lower() or "source"


def _first(record: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for field in fields:
        value = record.get(field)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value if item not in (None, ""))
    return str(value).strip()


def _as_e_number(value: Any) -> str:
    token = canonical(_text(value))
    if token.isdigit():
        token = "e" + token
    return token


def _risk(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    return max(0.0, min(100.0, round(number, 1)))


def _e_sort(value: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", value)[:4]
    return (int(digits) if digits else 10**6, value)


if __name__ == "__main__":
    raise SystemExit(main())
