"""Test offline del motore (nessuna rete): unittest della standard library."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from engine import AdditiveTable, EngineConfig, ScoreEngine, Weights, normalize_product
from engine.models import NutrientFacts
from engine.scoring import ScoreEngine as _ScoreEngine  # noqa: F401  (import esplicito per chiarezza)


def make_table(rows: list[tuple[str, str, str, str]]) -> AdditiveTable:
    """rows: (e_number, classes, name_it, risk_score)."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8")
    with handle as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "e_number",
                "key",
                "name_en",
                "name_it",
                "classes",
                "is_preservative",
                "risk_score",
                "basis",
                "why",
                "notes",
            ]
        )
        for e_number, classes, name_it, risk in rows:
            writer.writerow(
                [
                    e_number,
                    f"en:{e_number.lower()}",
                    name_it,
                    name_it,
                    classes,
                    "true" if "preservative" in classes.split(";") else "false",
                    risk,
                    "curata" if risk else "classe",
                    "",
                    "",
                ]
            )
    table = AdditiveTable.load(Path(handle.name))
    Path(handle.name).unlink(missing_ok=True)
    return table


class NutritionScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ScoreEngine(make_table([]))

    def test_sugary_product_scores_low(self) -> None:
        score, _ = self.engine.nutrition_score(
            NutrientFacts(kcal=539, sugars=56.3, saturated_fat=10.6, salt=0.107, fiber=0, protein=6.3)
        )
        self.assertIsNotNone(score)
        self.assertLess(score, 25)

    def test_healthy_product_scores_high(self) -> None:
        score, _ = self.engine.nutrition_score(
            NutrientFacts(kcal=60, sugars=2.0, saturated_fat=0.3, salt=0.1, fiber=4.0, protein=6.0, fruits_veg_nuts=90)
        )
        self.assertIsNotNone(score)
        self.assertGreater(score, 80)

    def test_sodium_converted_to_salt(self) -> None:
        score, details = self.engine.nutrition_score(
            NutrientFacts(kcal=400, sugars=2, saturated_fat=1, sodium=1.2, fiber=1, protein=5)
        )
        self.assertTrue(details["salt_from_sodium"])
        self.assertAlmostEqual(details["salt_g"], 3.0, places=3)
        self.assertLess(score, 60)

    def test_missing_nutrients_returns_none(self) -> None:
        score, details = self.engine.nutrition_score(NutrientFacts())
        self.assertIsNone(score)
        self.assertIn("missing", details)

    def test_beverage_profile_used(self) -> None:
        engine = self.engine
        solid_facts = NutrientFacts(kcal=180, sugars=10.6, saturated_fat=2.0, salt=0.3)
        solid, _ = engine.nutrition_score(solid_facts, is_beverage=False)
        liquid, details = engine.nutrition_score(solid_facts, is_beverage=True)
        self.assertEqual(details["profile"], "beverage")
        self.assertLess(liquid, solid)


class AdditiveDominanceTests(unittest.TestCase):
    """Il cuore del progetto: un singolo additivo tossico non deve essere diluito."""

    def setUp(self) -> None:
        self.table = make_table(
            [
                ("E330", "acidity regulator", "Acido citrico", "2"),
                ("E300", "antioxidant", "Acido ascorbico", "3"),
                ("E211", "preservative", "Benzoato di sodio", "92"),
            ]
        )
        self.engine = ScoreEngine(self.table)

    def _product(self, additives: tuple[str, ...]):
        raw = {
            "code": "0001",
            "product_name": "Test",
            "additives_tags": list(additives),
            "nutriments": {
                "energy-kcal_100g": 70,
                "sugars_100g": 3,
                "saturated-fat_100g": 0.4,
                "salt_100g": 0.2,
                "fiber_100g": 3,
                "proteins_100g": 5,
                "fruits-vegetables-nuts-estimate-from-ingredients_100g": 80,
            },
            "nova_group": 3,
        }
        return normalize_product(raw)

    def test_benign_additives_keep_high_score(self) -> None:
        result = self.engine.score(self._product(("en:e330", "en:e300")))
        self.assertGreater(result.score, 70)
        self.assertEqual(result.alerts, ())

    def test_single_toxic_additive_tanks_score(self) -> None:
        clean = self.engine.score(self._product(("en:e330", "en:e300")))
        toxic = self.engine.score(self._product(("en:e330", "en:e300", "en:e211")))
        self.assertLess(toxic.score, clean.score - 20)
        self.assertLess(toxic.score, 70)
        self.assertEqual(toxic.worst_additive.e_number, "E211")
        self.assertEqual(toxic.alerts[0].level, "critico")

    def test_additive_component_values(self) -> None:
        result = self.engine.score(self._product(("en:e211",)))
        component = next(c for c in result.components if c.name == "additives")
        self.assertAlmostEqual(component.details["worst_risk"], 92.0)
        self.assertLess(component.score, 40)

    def test_unscored_additive_uses_class_default(self) -> None:
        table = make_table([("E999", "preservative", "Ignoto", "")])
        engine = ScoreEngine(table)
        result = engine.score(self._product(("en:e999",)))
        self.assertEqual(result.unscored_additives, 1)
        self.assertTrue(result.additives[0].risk_estimated)
        self.assertGreater(result.additives[0].risk, 0)


class DataQualityTests(unittest.TestCase):
    """Regressioni trovate dal self-test sui prodotti reali."""

    def test_roman_variant_resolves_to_base_additive(self) -> None:
        table = make_table([("E322", "emulsifier", "Lecitine", "10")])
        self.assertEqual(table.get("en:e322i").e_number, "E322")
        self.assertEqual(table.get("EN:E451I"), None)

    def test_letter_variants_stay_distinct(self) -> None:
        table = make_table(
            [("E150a", "colour", "Caramello", "10"), ("E150b", "colour", "Caramello solfito", "12")]
        )
        self.assertEqual(table.get("en:e150a").e_number, "E150a")
        self.assertEqual(table.get("en:e150b").e_number, "E150b")

    def test_parent_category_is_not_a_beverage(self) -> None:
        record = normalize_product(
            {
                "code": "1",
                "categories_tags": ["en:plant-based-foods-and-beverages", "en:nuts"],
                "product_quantity_unit": "g",
                "nutriments": {"energy-kcal_100g": 462, "sugars_100g": 12},
            }
        )
        self.assertFalse(record.is_beverage)

    def test_real_beverage_still_detected(self) -> None:
        record = normalize_product(
            {
                "code": "2",
                "categories_tags": ["en:plant-based-foods-and-beverages", "en:beverages", "en:colas"],
                "product_quantity_unit": "ml",
                "nutriments": {"energy-kcal_100g": 42},
            }
        )
        self.assertTrue(record.is_beverage)

    def test_incomplete_nutrition_is_not_scored(self) -> None:
        engine = ScoreEngine(make_table([]))
        score, details = engine.nutrition_score(NutrientFacts(kcal=400, sugars=10))
        self.assertIsNone(score)
        self.assertIn("coverage", details)

    def test_complete_beverage_nutrition_is_scored(self) -> None:
        engine = ScoreEngine(make_table([]))
        score, details = engine.nutrition_score(
            NutrientFacts(kcal=42, sugars=10.6), is_beverage=True
        )
        self.assertIsNotNone(score)
        self.assertEqual(details["profile"], "beverage")

    def test_additive_coverage_is_reported(self) -> None:
        table = make_table(
            [("E211", "preservative", "Benzoato di sodio", "35"), ("E330", "acidity regulator", "Citrico", "")]
        )
        engine = ScoreEngine(table)
        result = engine.score(
            normalize_product(
                {
                    "code": "3",
                    "additives_tags": ["en:e211", "en:e330"],
                    "nutriments": {"energy-kcal_100g": 100, "sugars_100g": 2, "salt_100g": 0.1},
                }
            )
        )
        self.assertAlmostEqual(result.additive_coverage, 0.5)
        self.assertEqual(result.to_dict()["additive_coverage"], 0.5)


class ExplanationAndDoseTests(unittest.TestCase):
    def test_why_text_is_loaded_and_reported(self) -> None:
        table = make_table([("E211", "preservative", "Benzoato di sodio", "35")])
        row = table.get("en:e211")
        self.assertEqual(row.basis, "curata")
        engine = ScoreEngine(table)
        result = engine.score(
            normalize_product(
                {
                    "code": "1",
                    "additives_tags": ["en:e211"],
                    "nutriments": {"energy-kcal_100g": 100, "sugars_100g": 2, "salt_100g": 0.1},
                }
            )
        )
        self.assertIn("benzoato", result.additives[0].name.lower())
        self.assertEqual(result.additives[0].base_risk, 35)

    def test_dose_scales_risk(self) -> None:
        table = make_table([("E211", "preservative", "Benzoato di sodio", "60")])
        engine = ScoreEngine(table)
        record = normalize_product(
            {
                "code": "1",
                "additives_tags": ["en:e211"],
                "ingredients": [{"id": "en:e211", "percent_estimate": 0.005}],
                "nutriments": {"energy-kcal_100g": 100, "sugars_100g": 2, "salt_100g": 0.1},
            }
        )
        result = engine.score(record)
        hit = result.additives[0]
        self.assertAlmostEqual(hit.dose_percent, 0.005)
        self.assertAlmostEqual(hit.base_risk, 60)
        self.assertLess(hit.risk, 20)

    def test_no_dose_keeps_full_risk(self) -> None:
        table = make_table([("E211", "preservative", "Benzoato di sodio", "60")])
        engine = ScoreEngine(table)
        result = engine.score(
            normalize_product(
                {
                    "code": "1",
                    "additives_tags": ["en:e211"],
                    "nutriments": {"energy-kcal_100g": 100, "sugars_100g": 2, "salt_100g": 0.1},
                }
            )
        )
        self.assertIsNone(result.additives[0].dose_percent)
        self.assertAlmostEqual(result.additives[0].risk, 60)

    def test_reasons_explain_the_score(self) -> None:
        engine = ScoreEngine(make_table([]))
        result = engine.score(
            normalize_product(
                {
                    "code": "1",
                    "nova_group": 4,
                    "nutriments": {
                        "energy-kcal_100g": 539,
                        "sugars_100g": 56.3,
                        "saturated-fat_100g": 10.6,
                        "salt_100g": 0.1,
                    },
                }
            )
        )
        self.assertTrue(any("Zuccheri" in reason for reason in result.reasons))
        self.assertTrue(any("NOVA 4" in reason for reason in result.reasons))
        self.assertTrue(any("nessuno dichiarato" in reason for reason in result.reasons))
        self.assertEqual(len(result.to_dict()["reasons"]), len(result.reasons))


class WeightAndConfidenceTests(unittest.TestCase):
    def test_missing_nutrition_renormalizes_weights(self) -> None:
        engine = ScoreEngine(make_table([]))
        result = engine.score(
            normalize_product(
                {
                    "code": "0002",
                    "product_name": "Senza dati",
                    "additives_tags": [],
                    "labels_tags": ["en:organic"],
                    "nova_group": 2,
                }
            )
        )
        nutrition = next(c for c in result.components if c.name == "nutrition")
        self.assertIsNone(nutrition.score)
        self.assertLess(result.confidence, 1.0)
        self.assertAlmostEqual(
            sum(c.weight for c in result.components if c.score is not None), result.confidence
        )

    def test_custom_weights_are_normalized(self) -> None:
        engine = ScoreEngine(make_table([]), EngineConfig(weights=Weights(1, 1, 1, 1)))
        self.assertAlmostEqual(engine.weights.nutrition, 0.25)

    def test_organic_bonus(self) -> None:
        table = make_table([])
        engine = ScoreEngine(table)
        base = {"code": "3", "product_name": "Bio", "nova_group": 2,
                "nutriments": {"energy-kcal_100g": 100, "sugars_100g": 2, "salt_100g": 0.1,
                               "saturated-fat_100g": 0.5, "fiber_100g": 2, "proteins_100g": 4}}
        plain = engine.score(normalize_product(dict(base, labels_tags=[])))
        organic = engine.score(normalize_product(dict(base, labels_tags=["en:organic"])))
        self.assertGreater(organic.score, plain.score)


class TableTests(unittest.TestCase):
    def test_table_loading_and_stats(self) -> None:
        table = make_table(
            [
                ("E200", "preservative", "Acido sorbico", "30"),
                ("E322", "emulsifier", "Lecitine", ""),
            ]
        )
        stats = table.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["preservatives"], 1)
        self.assertEqual(stats["scored"], 1)
        self.assertEqual(len(table.preserve_only()), 1)
        self.assertEqual(table.get("en:e322").e_number, "E322")

    def test_real_table_if_present(self) -> None:
        from engine.additives import DEFAULT_TABLE_PATH

        if not DEFAULT_TABLE_PATH.exists():
            self.skipTest("data/additives.csv non ancora generato")
        table = AdditiveTable.load()
        self.assertGreater(len(table), 200)


class CurationTests(unittest.TestCase):
    def test_curation_lookup_is_case_insensitive(self) -> None:
        from data.curation import curated

        self.assertEqual(curated("E150d")[0], 40)
        self.assertEqual(curated("e150D")[0], 40)
        self.assertIsNone(curated("E9999"))


class CacheTests(unittest.TestCase):
    def test_cache_is_invalidated_when_fields_change(self) -> None:
        from engine.off_client import ProductCache

        path = Path(tempfile.mkdtemp()) / "cache.sqlite"
        first = ProductCache(path, signature="firma-a")
        first.put("123", {"status": 1, "product": {"code": "123"}})
        self.assertIsNotNone(first.get("123"))
        first.close()

        second = ProductCache(path, signature="firma-b")
        self.assertIsNone(second.get("123"))
        second.close()


class NormalizeTests(unittest.TestCase):
    def test_kj_converted_to_kcal_and_sodium_to_salt(self) -> None:
        record = normalize_product(
            {
                "code": "123",
                "units": {},
                "nutriments": {"energy_100g": 1046, "sodium_100g": 0.4, "sugars_100g": 5},
                "product_quantity_unit": "ml",
            }
        )
        self.assertAlmostEqual(record.nutriments.kcal, 250, places=0)
        self.assertAlmostEqual(record.nutriments.salt, 1.0, places=3)
        self.assertTrue(record.is_beverage)

    def test_additives_tags_deduplicated(self) -> None:
        record = normalize_product(
            {"code": "9", "additives_tags": ["en:e322", "en:e322", "en:e330"], "nutriments": {}}
        )
        self.assertEqual(record.additives, ("en:e322", "en:e330"))


if __name__ == "__main__":
    unittest.main()
