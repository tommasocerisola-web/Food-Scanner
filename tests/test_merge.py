"""Test del merger multi-fonte: GTIN, mediana pesata, conflitti, consenso, CLI."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from engine.merge import (
    gtin14,
    merge_additives,
    merge_group,
    merge_nutrients,
    merge_products,
    merge_risk_tables,
    weighted_median,
)
from engine.source_online import _fdc_nutrients, _from_fdc
from engine.sources import SourceProduct, load_source
from tests.test_engine import make_table
from tools import merge_sources


def product(source: str, **kwargs) -> SourceProduct:
    base = {"barcode": "3017620422003", "name": "Nutella", "brand": "Ferrero"}
    base.update(kwargs)
    return SourceProduct(source=source, **base)


class GtinTests(unittest.TestCase):
    def test_varianti(self) -> None:
        self.assertEqual(gtin14("009800800124"), "00009800800124")
        self.assertEqual(gtin14("0009800800124"), "00009800800124")
        self.assertEqual(gtin14("3017620422003"), "03017620422003")
        self.assertEqual(gtin14("96385074"), "00000096385074")
        self.assertEqual(gtin14("123"), "")
        self.assertEqual(gtin14("abc"), "")


class WeightedMedianTests(unittest.TestCase):
    def test_dispari_e_pesi(self) -> None:
        self.assertEqual(weighted_median([(10, 1), (20, 1), (30, 1)]), 20)
        self.assertEqual(weighted_median([(10, 2), (20, 1)]), 10)
        self.assertIsNone(weighted_median([]))
        self.assertIsNone(weighted_median([(None, 1)]))  # type: ignore[list-item]


class NutrientMergeTests(unittest.TestCase):
    def test_mediana_pesata_e_provenienza(self) -> None:
        merged = merge_nutrients(
            {
                "off": product("off", nutrients={"kcal": 539, "sugars": 56.3, "salt": 0.107}),
                "fdc": product("fdc", nutrients={"kcal": 520, "sugars": 55.0}),
            }
        )
        self.assertEqual(merged["values"]["kcal"], 539)
        self.assertEqual(merged["provenance"]["kcal"], "off")
        self.assertEqual(merged["values"]["salt"], 0.107)
        self.assertEqual(merged["contributors"]["sugars"], 2)
        self.assertEqual(merged["conflicts"], [])

    def test_conflitto_oltre_tolleranza(self) -> None:
        merged = merge_nutrients(
            {
                "off": product("off", nutrients={"sugars": 10}),
                "fdc": product("fdc", nutrients={"sugars": 20}),
            }
        )
        self.assertEqual(merged["values"]["sugars"], 10)
        self.assertEqual(len(merged["conflicts"]), 1)
        self.assertEqual(merged["conflicts"][0]["campo"], "sugars")
        self.assertEqual(merged["agreement"], 0.0)

    def test_campo_mancante_usa_l_altra_fonte(self) -> None:
        merged = merge_nutrients(
            {
                "off": product("off", nutrients={"kcal": 539}),
                "fdc": product("fdc", nutrients={"salt": 0.5}),
            }
        )
        self.assertEqual(merged["values"]["salt"], 0.5)
        self.assertEqual(merged["provenance"]["salt"], "fdc")
        self.assertNotIn("salt", merged["conflicts"])

    def test_pesi_per_campo(self) -> None:
        # FDC pesa 1.1 sui nutrienti: con lo stesso valore il pareggio va a FDC.
        profiles = {
            "off": {"weight": 1.0, "fields": {"nutrients": 1.0}},
            "fdc": {"weight": 1.0, "fields": {"nutrients": 1.1}},
        }
        merged = merge_nutrients(
            {
                "off": product("off", nutrients={"protein": 6.0}),
                "fdc": product("fdc", nutrients={"protein": 6.0}),
            },
            profiles,
        )
        self.assertEqual(merged["provenance"]["protein"], "fdc")


class AdditiveMergeTests(unittest.TestCase):
    def test_unione_sconosciuti_e_punteggio(self) -> None:
        table = make_table([("E322", "emulsifier", "Lecitine", "20")])
        merged = merge_additives(
            {
                "off": product("off", additives=("e322", "e999")),
                "fdc": product("fdc", additives=("e322",)),
            },
            table,
        )
        self.assertEqual(merged["punteggio"], 80.0)
        self.assertEqual([row["tag"] for row in merged["sconosciuti"]], ["e999"])
        self.assertEqual(merged["additivi"][0]["fonti"], ["fdc", "off"])

    def test_senza_additivi(self) -> None:
        merged = merge_additives({"off": product("off")}, None)
        self.assertIsNone(merged["punteggio"])
        self.assertFalse(merged["dichiarati"])


class GroupMergeTests(unittest.TestCase):
    def test_confidenza_cresce_con_le_fonti(self) -> None:
        table = make_table([("E322", "emulsifier", "Lecitine", "20")])
        single = merge_group(
            {
                "off": product(
                    "off",
                    additives=("e322",),
                    grade="E",
                    nutrients={"kcal": 539, "sugars": 56.3, "salt": 0.107},
                )
            },
            table,
        )
        triple = merge_group(
            {
                "off": product(
                    "off",
                    additives=("e322",),
                    grade="E",
                    nutrients={"kcal": 539, "sugars": 56.3, "salt": 0.107},
                ),
                "fdc": product("fdc", nutrients={"kcal": 520, "sugars": 55.0}),
                "openprices": product(
                    "openprices",
                    price=3.21,
                    price_per_100g=0.8,
                    price_observations=5,
                    stores=("Carrefour",),
                ),
            },
            table,
        )
        self.assertGreater(triple["confidenza"], single["confidenza"])
        self.assertIn("una_sola_fonte", single["flag"])
        self.assertNotIn("una_sola_fonte", triple["flag"])
        self.assertEqual(triple["prezzo"], 0.8)
        self.assertEqual(triple["prezzo_base"], "100g")
        self.assertEqual(triple["prezzo_osservazioni"], 5)

    def test_grade_in_conflitto(self) -> None:
        profiles = {
            "off": {"weight": 1.0, "fields": {"grade": 1.0}},
            "fdc": {"weight": 1.0, "fields": {"grade": 1.0}},
        }
        merged = merge_group(
            {
                "off": product("off", grade="E"),
                "fdc": product("fdc", grade="D"),
            },
            None,
            profiles,
        )
        self.assertIn("grade_discordante", merged["flag"])
        self.assertEqual(merged["grade"], "E")

    def test_merge_products_ordina(self) -> None:
        merged = merge_products(
            [
                {"off": product("off", barcode="1111111111111", name="Zeta")},
                {"off": product("off", barcode="2222222222222", name="Alfa")},
            ]
        )
        self.assertEqual([item["nome"] for item in merged], ["Alfa", "Zeta"])


class RiskTableTests(unittest.TestCase):
    def test_mediana_pesata_e_buchi(self) -> None:
        rows = {
            row["e_number"]: row
            for row in merge_risk_tables(
                {
                    "off": {"e322": 20, "e621": 30},
                    "fdc": {"e322": 30},
                    "yuka": {"e322": 32, "e102": 10},
                }
            )
        }
        self.assertEqual(rows["e322"]["risk_consensus"], 30)
        self.assertEqual(rows["e322"]["n_sources"], 3)
        self.assertFalse(rows["e322"]["needs_review"])
        self.assertEqual(rows["e621"]["missing_in"], ["fdc", "yuka"])
        self.assertTrue(rows["e621"]["needs_review"])

    def test_fonti_discordi(self) -> None:
        rows = {row["e_number"]: row for row in merge_risk_tables({"off": {"e102": 10}, "yuka": {"e102": 90}})}
        self.assertTrue(rows["e102"]["needs_review"])
        self.assertIn("fonti discordi", " ".join(rows["e102"]["reasons"]))


class SourceParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_off_nutrients_labels_categorie(self) -> None:
        path = self.write(
            "off.json",
            json.dumps(
                {
                    "products": [
                        {
                            "code": "3017620422003",
                            "product_name": "Nutella",
                            "brands": "Ferrero",
                            "quantity": "400 g",
                            "categories_tags": ["en:spreads"],
                            "labels_tags": ["en:organic"],
                            "nova_group": 4,
                            "nutriments": {
                                "energy-kcal_100g": 539,
                                "sugars_100g": 56.3,
                                "salt_100g": 0.107,
                            },
                        }
                    ]
                }
            ),
        )
        item = load_source("off", path)[0]
        self.assertEqual(item.nutrients["kcal"], 539)
        self.assertEqual(item.nutrients["salt"], 0.107)
        self.assertEqual(item.quantity, "400 g")
        self.assertEqual(item.categories, ("en:spreads",))
        self.assertEqual(item.labels, ("en:organic",))
        self.assertEqual(item.nova_group, 4)

    def test_csv_nutrienti_piatti(self) -> None:
        path = self.write(
            "t.csv",
            "ean,nome,marca,kcal,zuccheri,sale\n3017620422003,Nutella,Ferrero,539,56.3,0.107\n",
        )
        item = load_source("t", path)[0]
        self.assertEqual(item.nutrients["kcal"], 539)
        self.assertEqual(item.nutrients["sugars"], 56.3)
        self.assertEqual(item.nutrients["salt"], 0.107)

    def test_roundtrip_del_formato_normalizzato(self) -> None:
        original = product(
            "off",
            nutrients={"kcal": 539, "salt": 0.107},
            additives=("e322",),
            labels=("en:organic",),
            price=1.2,
            price_per_100g=0.3,
            price_observations=2,
            stores=("Carrefour",),
        )
        path = self.write("x.json", json.dumps([original.to_dict()]))
        loaded = load_source("off", path)[0]
        self.assertEqual(loaded.nutrients["kcal"], 539)
        self.assertEqual(loaded.nutrients["salt"], 0.107)
        self.assertEqual(loaded.additives, ("e322",))
        self.assertEqual(loaded.labels, ("en:organic",))
        self.assertEqual(loaded.price, 1.2)
        self.assertEqual(loaded.price_per_100g, 0.3)
        self.assertEqual(loaded.price_observations, 2)
        self.assertEqual(loaded.stores, ("Carrefour",))


class FdcParsingTests(unittest.TestCase):
    def test_nutrienti_e_sale_da_sodio(self) -> None:
        food = {
            "description": "NUTELLA FERRERO",
            "brandName": "FERRERO",
            "gtinUpc": "009800800124",
            "foodCategory": "Crackers & Biscotti",
            "foodNutrients": [
                {"nutrientName": "Energy", "unitName": "KCAL", "value": 519},
                {"nutrientName": "Total Sugars", "unitName": "G", "value": 44.2},
                {"nutrientName": "Sodium, Na", "unitName": "MG", "value": 250},
                {"nutrientName": "Protein", "unitName": "G", "value": 7.69},
            ],
        }
        nutrients = _fdc_nutrients(food)
        self.assertEqual(nutrients["kcal"], 519)
        self.assertEqual(nutrients["sugars"], 44.2)
        self.assertEqual(nutrients["sodium"], 0.25)

        product = _from_fdc(food, "0009800800124")
        self.assertEqual(product.barcode, "009800800124")
        self.assertEqual(product.nutrients["salt"], 0.625)
        self.assertEqual(product.categories, ("Crackers & Biscotti",))

    def test_energia_da_kj(self) -> None:
        food = {"foodNutrients": [{"nutrientName": "Energy", "unitName": "kJ", "value": 1000}]}
        self.assertAlmostEqual(_fdc_nutrients(food)["kcal"], 1000 / 4.184, places=3)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return merge_sources.main(list(args))

    def test_merge_end_to_end(self) -> None:
        off_path = self.dir / "off.json"
        fdc_path = self.dir / "fdc.json"
        off_path.write_text(
            json.dumps(
                [
                    {
                        "source": "off",
                        "barcode": "3017620422003",
                        "name": "Nutella",
                        "brand": "Ferrero",
                        "additives": ["e322"],
                        "nutrients": {"kcal": 539, "sugars": 56.3, "salt": 0.107},
                        "grade": "E",
                        "nova_group": 4,
                    }
                ]
            ),
            encoding="utf-8",
        )
        fdc_path.write_text(
            json.dumps(
                [
                    {
                        "source": "fdc",
                        "barcode": "03017620422003",
                        "name": "Nutella",
                        "brand": "Ferrero",
                        "nutrients": {"kcal": 520, "sugars": 55.0},
                    }
                ]
            ),
            encoding="utf-8",
        )
        out = self.dir / "merged.json"
        csv_out = self.dir / "merged.csv"
        code = self._run(
            "merge",
            "--source",
            f"off={off_path}",
            "--source",
            f"fdc={fdc_path}",
            "--out",
            str(out),
            "--csv",
            str(csv_out),
        )
        self.assertEqual(code, 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["sintesi"]["in_comune"], 1)
        item = payload["prodotti"][0]
        self.assertEqual(item["nutrienti"]["kcal"], 539)
        self.assertEqual(item["fonti"], ["fdc", "off"])
        self.assertTrue(csv_out.exists())
        self.assertIn("barcode,nome", csv_out.read_text(encoding="utf-8").splitlines()[0])

    def test_table_end_to_end(self) -> None:
        off_path = self.dir / "t_off.csv"
        fdc_path = self.dir / "t_fdc.csv"
        off_path.write_text("e_number,risk_score\nE322,20\nE621,30\n", encoding="utf-8")
        fdc_path.write_text("e_number,risk_score\nE322,22\n", encoding="utf-8")
        out = self.dir / "consensus.csv"
        gaps = self.dir / "gaps.json"
        code = self._run(
            "table",
            "--table",
            f"off={off_path}",
            "--table",
            f"fdc={fdc_path}",
            "--out",
            str(out),
            "--gaps",
            str(gaps),
        )
        self.assertEqual(code, 0)
        payload = json.loads(gaps.read_text(encoding="utf-8"))
        self.assertEqual([row["e_number"] for row in payload["gaps"]], ["e621"])
        self.assertTrue(out.exists())

    def test_file_mancante(self) -> None:
        code = self._run(
            "merge",
            "--source",
            f"off={self.dir / 'nope.json'}",
            "--out",
            str(self.dir / "x.json"),
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
