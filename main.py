"""
generic_01 — motore di scoring alimentare basato su Open Food Facts.

Uso:
    python main.py scan 3017620422003
    python main.py scan 3017620422003 5449000000996 --json
    python main.py file tests/fixtures/prodotto.json
    python main.py batch barcodes.txt --workers 6 --out risultati.json
    python main.py additives --preservatives --missing-risk
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engine import (
    AdditiveTable,
    EngineConfig,
    OFFClient,
    OFFError,
    ScoreEngine,
    Weights,
    normalize_product,
)
from engine.report import render_report
from engine.scoring import ENGINE_VERSION

DEFAULT_CACHE = Path("cache/off.sqlite")


def _build_engine(args: argparse.Namespace) -> ScoreEngine:
    table = AdditiveTable.load(args.table)
    weights = Weights(
        nutrition=args.w_nutrition,
        additives=args.w_additives,
        processing=args.w_processing,
        organic=args.w_organic,
    )
    config = EngineConfig(
        weights=weights,
        worst_additive_weight=args.worst_weight,
        critical_risk=args.critical,
    )
    return ScoreEngine(table=table, config=config)


def _client(args: argparse.Namespace) -> OFFClient:
    cache = None if args.no_cache else args.cache
    return OFFClient(
        base_url=args.base_url,
        cache_path=cache,
        rpm=args.rpm,
        retries=args.retries,
        transport=None,
    )


def cmd_scan(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    client = _client(args)
    exit_code = 0
    results = []
    try:
        for barcode in args.barcodes:
            try:
                record = client.fetch(barcode, use_cache=not args.no_cache)
            except OFFError as error:
                print(f"{barcode}: {error}", file=sys.stderr)
                exit_code = 1
                continue
            result = engine.score(record)
            results.append(result)
            if not args.json:
                print(render_report(result))
                print()
    finally:
        client.close()

    if args.json:
        print(json.dumps([result.to_dict() for result in results], indent=2, ensure_ascii=False))
    return exit_code


def cmd_file(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    try:
        raw = json.loads(Path(args.path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"JSON non valido in {args.path}: {error}", file=sys.stderr)
        return 2
    raw = raw.get("product", raw)
    result = engine.score(normalize_product(raw))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) if args.json else render_report(result))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    client = _client(args)
    codes = [
        line.strip()
        for line in Path(args.barcodes_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    payload: dict[str, dict] = {}
    errors: dict[str, str] = {}
    try:
        records = client.fetch_many(codes, workers=args.workers, use_cache=not args.no_cache)
    finally:
        client.close()

    for code, value in records.items():
        if isinstance(value, Exception):
            errors[code] = str(value)
            continue
        payload[code] = engine.score(value).to_dict()

    out = {"engine_version": ENGINE_VERSION, "results": payload, "errors": errors}
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"{len(payload)} prodotti valutati, {len(errors)} errori -> {args.out}")
    else:
        print(text)
    return 0 if not errors else 1


def cmd_additives(args: argparse.Namespace) -> int:
    table = AdditiveTable.load(args.table)
    rows = table.preserve_only() if args.preservatives else list(table)
    if args.missing_risk:
        rows = [row for row in rows if row.risk is None]
    rows.sort(key=lambda row: row.e_number.lower())

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "e_number": row.e_number,
                        "name_it": row.name_it,
                        "name_en": row.name_en,
                        "classes": list(row.classes),
                        "is_preservative": row.is_preservative,
                        "risk_score": row.risk,
                    }
                    for row in rows
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    stats = table.stats()
    print(
        f"Tabella: {stats['total']} additivi · {stats['preservatives']} conservanti · "
        f"{stats['scored']} con punteggio · {stats['unscored']} da compilare"
    )
    print()
    print(f"{'E-number':<12}{'Rischio':>8}  {'Cons.':<6}Nome")
    for row in rows:
        risk = "   -" if row.risk is None else f"{row.risk:>6.1f}"
        flag = "si" if row.is_preservative else ""
        print(f"{row.e_number:<12}{risk:>8}  {flag:<6}{row.name_it or row.name_en}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generic_01",
        description="Motore di scoring alimentare (Open Food Facts)",
    )
    parser.add_argument("--version", action="version", version=f"generic_01 engine {ENGINE_VERSION}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--table", type=Path, default=None, help="CSV additivi (default data/additives.csv)")
    common.add_argument("--json", action="store_true", help="Output JSON")
    common.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="Cache SQLite")
    common.add_argument("--no-cache", action="store_true", help="Ignora la cache")
    common.add_argument("--base-url", default="https://world.openfoodfacts.org")
    common.add_argument("--rpm", type=int, default=90, help="Richieste/minuto massime")
    common.add_argument("--retries", type=int, default=3)
    common.add_argument("--w-nutrition", type=float, default=0.50)
    common.add_argument("--w-additives", type=float, default=0.35)
    common.add_argument("--w-processing", type=float, default=0.10)
    common.add_argument("--w-organic", type=float, default=0.05)
    common.add_argument("--worst-weight", type=float, default=0.65,
                        help="Peso del peggior additivo nella componente additivi")
    common.add_argument("--critical", type=float, default=70.0,
                        help="Soglia di rischio per allerta critica")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[common], help="Scansiona uno o piu' barcode")
    scan.add_argument("barcodes", nargs="+")
    scan.set_defaults(func=cmd_scan)

    file_cmd = sub.add_parser("file", parents=[common], help="Valuta un JSON di Open Food Facts")
    file_cmd.add_argument("path", type=Path)
    file_cmd.set_defaults(func=cmd_file)

    batch = sub.add_parser("batch", parents=[common], help="Valuta una lista di barcode da file")
    batch.add_argument("barcodes_file", type=Path)
    batch.add_argument("--workers", type=int, default=4)
    batch.add_argument("--out", type=Path, default=None)
    batch.set_defaults(func=cmd_batch)

    additives = sub.add_parser("additives", parents=[common], help="Ispeziona la tabella additivi")
    additives.add_argument("--preservatives", action="store_true", help="Solo conservanti")
    additives.add_argument("--missing-risk", action="store_true", help="Solo righe senza punteggio")
    additives.set_defaults(func=cmd_additives)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2
    except BrokenPipeError:  # es. `python main.py additives | head`
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
