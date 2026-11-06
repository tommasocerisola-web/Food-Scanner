"""Test del confronto multi-fonte: caricamento, matching, consenso, buchi."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from engine.sources import SourceProduct, barcode_key, load_source, name_key, norm_text
from tests.test_engine import make_table
from tools import cross_source
from tools.cross_source import build_report, cluster_products, load_risk_table, merge_tables


def item(
    source: str,
    barcode: str = "",
    name: str = "Nutella",
    brand: str = "Ferrero",
    score: float | None = None,
    additives: tuple[str, ...] = (),
) -> SourceProduct:
    return SourceProduct(
        source=source,
        barcode=barcode,
        name=name,
        brand=brand,
        score=score,
        additives=tuple(additives),
    )


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path


class SourceLoadingTests(TempDirTest):
    def test_loads_three_dialects(self) -> None:
        off = self.write(
            "off.json",
            json.dumps(
                {
                    "products": [
                        {
                            "code": "3017620422003",
                            "product_name": "Nutella",
                            "brands": "Ferrero",
                            "additives_tags": ["en:e322"],
                            "nutriscore_grade": "e",
                        }
                    ]
                }
            ),
        )
        yuka = self.write(
            "yuka.json",
            json.dumps([{"ean": "3017620422003", "nome": "Nutella", "marca": "Ferrero", "score": 12}]),
        )
        terza = self.write(
            "terza.csv",
            "ean;prodotto;marca;punteggio;additivi\n"
            '3017620422003;Nutella;Ferrero;20;"en:e322|en:e471"\n',
        )

        products = {
            "off": load_source("off", off),
            "yuka": load_source("yuka", yuka),
            "terza": load_source("terza", terza),
        }
        for source, entries in products.items():
            self.assertEqual(len(entries), 1, source)
            self.assertEqual(entries[0].name, "Nutella")
            self.assertEqual(barcode_key(entries[0].barcode), "3017620422003")

        self.assertEqual(products["off"][0].grade, "E")
        self.assertEqual(products["off"][0].additives, ("e322",))
        self.assertEqual(products["yuka"][0].score, 12.0)
        self.assertEqual(products["terza"][0].additives, ("e322", "e471"))
        self.assertEqual(products["terza"][0].score, 20.0)

    def test_score_rescaled_to_100(self) -> None:
        path = self.write("yuka.json", json.dumps([{"ean": "3017620422003", "nome": "Nutella", "note": 12}]))
        product = load_source("yuka", path, score_max=20)[0]
        self.assertEqual(product.score, 60.0)

    def test_junk_rows_are_skipped(self) -> None:
        path = self.write(
            "junk.json",
            json.dumps([{"foo": "bar"}, {"nome": "Nutella", "ean": "3017620422003"}]),
        )
        entries = load_source("junk", path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Nutella")

    def test_jsonl_and_wrapped_product(self) -> None:
        jsonl = self.write(
            "x.jsonl",
            '{"code": "3017620422003", "product_name": "Nutella"}\n'
            '{"code": "8000500310427", "product_name": "Biscotti"}\n',
        )
        self.assertEqual(len(load_source("x", jsonl)), 2)

        wrapped = self.write(
            "w.json",
            json.dumps({"code": "3017620422003", "product": {"product_name": "Nutella"}}),
        )
        product = load_source("w", wrapped)[0]
        self.assertEqual(product.name, "Nutella")
        self.assertEqual(product.barcode, "3017620422003")

    def test_barcode_key_and_text_normalisation(self) -> None:
        self.assertEqual(barcode_key("03017620422003"), "3017620422003")
        self.assertEqual(barcode_key("01234567"), "01234567")
        self.assertEqual(barcode_key("123"), "")
        self.assertEqual(barcode_key("abc"), "")
        self.assertEqual(norm_text("Crème Brûlée & Co."), "creme brulee and co")
        self.assertEqual(name_key("Nutella", "Ferrero"), "ferrero nutella")


class MatchingTests(unittest.TestCase):
    def test_join_on_barcode_across_three_sources(self) -> None:
        clusters, ambiguous = cluster_products(
            {
                "off": [item("off", "3017620422003", score=43)],
                "yuka": [item("yuka", "3017620422003", score=38)],
                "terza": [item("terza", "03017620422003", score=50)],
            }
        )
        self.assertEqual(len(clusters), 1)
        cluster = next(iter(clusters.values()))
        self.assertEqual(set(cluster.entries), {"off", "yuka", "terza"})
        self.assertEqual(cluster.spread, 12.0)
        self.assertEqual(ambiguous, [])

    def test_name_fallback_when_barcode_missing(self) -> None:
        clusters, _ = cluster_products(
            {
                "off": [item("off", "3017620422003", score=43)],
                "yuka": [item("yuka", "", score=38)],
            }
        )
        self.assertEqual(len(clusters), 1)
        cluster = next(iter(clusters.values()))
        self.assertEqual(cluster.entries["yuka"].score, 38.0)
        self.assertEqual(cluster.how, "barcode+nome")

    def test_different_products_stay_separate(self) -> None:
        clusters, _ = cluster_products(
            {
                "off": [item("off", "1111111111111", "Pesto alla genovese", "Barilla", 70)],
                "yuka": [item("yuka", "", "Nutella", "Ferrero", 30)],
            }
        )
        self.assertEqual(len(clusters), 2)

    def test_ambiguous_candidate_not_merged(self) -> None:
        clusters, ambiguous = cluster_products(
            {
                "off": [
                    item("off", "1111111111111", "Pesto alla genovese", "Barilla", 70),
                    item("off", "2222222222222", "Pesto alla genovese", "Barilla", 70),
                ],
                "terza": [item("terza", "", "Pesto alla genovese", "Barilla", 60)],
            }
        )
        self.assertEqual(len(clusters), 2)
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(len(ambiguous[0]["candidati"]), 2)

    def test_duplicate_barcode_counted(self) -> None:
        clusters, _ = cluster_products(
            {
                "off": [
                    item("off", "1111111111111", score=10),
                    item("off", "1111111111111", score=20),
                ]
            }
        )
        cluster = next(iter(clusters.values()))
        self.assertEqual(len(cluster.entries), 1)
        self.assertEqual(cluster.duplicates["off"], 1)

    def test_products_without_barcode_group_by_name(self) -> None:
        clusters, _ = cluster_products(
            {
                "off": [item("off", "", score=43)],
                "yuka": [item("yuka", "", score=38)],
            }
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(next(iter(clusters.values())).entries), 2)


class ReportTests(unittest.TestCase):
    def test_unknown_additives_and_flags(self) -> None:
        table = make_table([("E322", "emulsifier", "Lecitine", "20")])
        products = {
            "off": [item("off", "1111111111111", "Nutella", "Ferrero", 40, ("e322", "e999"))],
            "yuka": [item("yuka", "1111111111111", "Nutella", "Ferrero", 20, ("e322",))],
        }
        clusters, ambiguous = cluster_products(products)
        report = build_report(products, clusters, ambiguous, {name: Path(name) for name in products}, table)

        self.assertEqual(report["in_comune"], 1)
        self.assertEqual([row["tag"] for row in report["additivi_sconosciuti"]], ["e999"])
        payload = report["prodotti"][0]
        self.assertEqual(payload["manca_in"], [])
        self.assertIn("additivi_discordanti", payload["flag"])
        self.assertIn("additivi_sconosciuti:e999", payload["flag"])
        self.assertEqual(payload["spread"], 20.0)


class MergeTablesTests(TempDirTest):
    def test_consensus_median_missing_and_spread(self) -> None:
        off = self.write("off.csv", "e_number,risk_score,why\nE322,20,Da soia\nE621,30,Esaltatore\n")
        yuka = self.write("yuka.json", json.dumps({"e322": 40, "e102": 90}))
        terza = self.write(
            "terza.csv",
            "codice;pericolosita;motivo\nE322;30;Lecitina\nE102;10;Colorante\n",
        )
        tables = {
            "off": load_risk_table(off),
            "yuka": load_risk_table(yuka),
            "terza": load_risk_table(terza),
        }
        rows = {row["e_number"]: row for row in merge_tables(tables)}

        self.assertEqual(rows["e322"]["risk_consensus"], 30.0)
        self.assertEqual(rows["e322"]["n_sources"], 3)
        self.assertEqual(rows["e322"]["agreement"], 1.0)
        self.assertFalse(rows["e322"]["needs_fix"])
        self.assertEqual(rows["e322"]["missing_in"], [])

        self.assertTrue(rows["e621"]["needs_fix"])
        self.assertEqual(rows["e621"]["missing_in"], ["yuka", "terza"])

        self.assertTrue(rows["e102"]["needs_fix"])
        self.assertEqual(rows["e102"]["spread"], 80.0)

    def test_missing_risk_in_one_source_flagged(self) -> None:
        off = self.write("a.csv", "e_number,risk_score\nE322,20\n")
        yuka = self.write("b.csv", "e_number,risk_score\nE322,\n")
        rows = merge_tables({"off": load_risk_table(off), "yuka": load_risk_table(yuka)})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["risk_consensus"], 20.0)
        self.assertTrue(rows[0]["needs_fix"])
        self.assertIn("punteggio mancante in una fonte", rows[0]["reasons"])

    def test_json_list_and_italian_headers(self) -> None:
        path = self.write("t.json", json.dumps([{"e": "322", "score": "55,5", "nome": "Lecitina"}]))
        table = load_risk_table(path)
        self.assertEqual(table["e322"].risk, 55.5)
        self.assertEqual(table["e322"].name, "Lecitina")

    def test_empty_and_unparsable_rows_ignored(self) -> None:
        path = self.write("t.csv", "e_number,risk_score\n,40\nE322,\nE621,150\n")
        table = load_risk_table(path)
        self.assertEqual(set(table), {"e322", "e621"})
        self.assertIsNone(table["e322"].risk)
        self.assertEqual(table["e621"].risk, 100.0)


class CliTests(TempDirTest):
    def test_match_and_table_end_to_end(self) -> None:
        off = self.write(
            "off.json",
            json.dumps(
                {
                    "products": [
                        {
                            "code": "3017620422003",
                            "product_name": "Nutella",
                            "brands": "Ferrero",
                            "additives_tags": ["en:e322"],
                        }
                    ]
                }
            ),
        )
        yuka = self.write(
            "yuka.json",
            json.dumps([{"ean": "3017620422003", "nome": "Nutella", "marca": "Ferrero", "score": 12}]),
        )
        out = self.dir / "match.json"
        csv_out = self.dir / "match.csv"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cross_source.main(
                [
                    "match",
                    "--source",
                    f"off={off}",
                    "--source",
                    f"yuka={yuka}",
                    "--out",
                    str(out),
                    "--csv",
                    str(csv_out),
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["in_comune"], 1)
        self.assertTrue(csv_out.exists())

        table_off = self.write("t_off.csv", "e_number,risk_score\nE322,20\n")
        table_yuka = self.write("t_yuka.csv", "e_number,risk_score\nE322,40\nE621,60\n")
        table_out = self.dir / "consensus.csv"
        gaps_out = self.dir / "gaps.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cross_source.main(
                [
                    "table",
                    "--table",
                    f"off={table_off}",
                    "--table",
                    f"yuka={table_yuka}",
                    "--out",
                    str(table_out),
                    "--gaps",
                    str(gaps_out),
                ]
            )
        self.assertEqual(code, 0)
        gaps = json.loads(gaps_out.read_text(encoding="utf-8"))
        self.assertEqual([row["e_number"] for row in gaps["gaps"]], ["e621"])

    def test_missing_file_reports_error(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cross_source.main(
                ["match", "--source", f"off={self.dir / 'nope.json'}", "--out", str(self.dir / "x.json")]
            )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
