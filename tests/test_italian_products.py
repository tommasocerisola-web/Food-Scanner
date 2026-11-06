"""
100 prodotti italiani casuali, con nomi, barcode EAN-13 e profili realistici.

Il dataset è deterministico: lo stesso seed produce gli stessi 100 prodotti, così
un fallimento è sempre riproducibile. I test non usano la rete e verificano le
invarianti del motore su categorie tipiche del mercato italiano.
"""
from __future__ import annotations

import json
import random
import unittest

from engine import AdditiveTable, ScoreEngine
from engine.models import NutrientFacts, ProductRecord, grade_for
from engine.scoring import ENGINE_VERSION

PRODUCT_SPECS: tuple[dict, ...] = (
    {
        "slug": "pasta-di-semola",
        "name": "Pasta di semola di grano duro",
        "brand": "Pastificio Italiano",
        "quantity": "500 g",
        "beverage": False,
        "kcal": (345, 375),
        "sugars": (1.5, 3.5),
        "sat": (0.3, 1.0),
        "salt": (0.01, 0.08),
        "fiber": (2.5, 4.0),
        "protein": (11, 14),
        "fat": (1.0, 2.0),
        "nova": (1, 2, 3),
        "additives": (),
    },
    {
        "slug": "passata-di-pomodoro",
        "name": "Passata di pomodoro",
        "brand": "Orti d'Italia",
        "quantity": "700 g",
        "beverage": False,
        "kcal": (25, 45),
        "sugars": (3, 6),
        "sat": (0.05, 0.3),
        "salt": (0.1, 0.7),
        "fiber": (1, 2.5),
        "protein": (1, 2),
        "fat": (0.1, 0.6),
        "fruits": (80, 100),
        "nova": (2, 3),
        "additives": ("en:e330",),
    },
    {
        "slug": "olio-extravergine",
        "name": "Olio extravergine d'oliva",
        "brand": "Frantoio del Sud",
        "quantity": "750 ml",
        "beverage": False,
        "kcal": (880, 900),
        "sugars": (0, 0.1),
        "sat": (13, 16),
        "salt": (0, 0.01),
        "fiber": (0, 0.1),
        "protein": (0, 0.1),
        "fat": (99, 100),
        "nova": (1, 2),
        "additives": (),
    },
    {
        "slug": "parmigiano-reggiano",
        "name": "Parmigiano Reggiano",
        "brand": "Caseificio Emilia",
        "quantity": "250 g",
        "beverage": False,
        "kcal": (380, 430),
        "sugars": (0, 1),
        "sat": (18, 22),
        "salt": (1.2, 1.8),
        "fiber": (0, 0.2),
        "protein": (30, 36),
        "fat": (28, 32),
        "nova": (2, 3),
        "additives": (),
    },
    {
        "slug": "prosciutto-crudo",
        "name": "Prosciutto crudo",
        "brand": "Salumeria Italiana",
        "quantity": "100 g",
        "beverage": False,
        "kcal": (230, 300),
        "sugars": (0, 1),
        "sat": (6, 10),
        "salt": (4, 6),
        "fiber": (0, 0.1),
        "protein": (25, 30),
        "fat": (12, 18),
        "nova": (3,),
        "additives": ("en:e250", "en:e252"),
    },
    {
        "slug": "mozzarella",
        "name": "Mozzarella",
        "brand": "Latticini del Borgo",
        "quantity": "125 g",
        "beverage": False,
        "kcal": (240, 300),
        "sugars": (0.5, 2),
        "sat": (12, 16),
        "salt": (0.4, 0.9),
        "fiber": (0, 0.1),
        "protein": (17, 22),
        "fat": (18, 22),
        "nova": (2, 3),
        "additives": (),
    },
    {
        "slug": "yogurt-bianco",
        "name": "Yogurt bianco naturale",
        "brand": "Cascina Italia",
        "quantity": "500 g",
        "beverage": False,
        "kcal": (45, 75),
        "sugars": (3, 6),
        "sat": (1, 3),
        "salt": (0.08, 0.2),
        "fiber": (0, 0.2),
        "protein": (3.5, 5),
        "fat": (1.5, 3.5),
        "nova": (1, 2),
        "additives": (),
    },
    {
        "slug": "gelato",
        "name": "Gelato artigianale",
        "brand": "Gelateria del Corso",
        "quantity": "500 ml",
        "beverage": False,
        "kcal": (170, 260),
        "sugars": (18, 28),
        "sat": (5, 9),
        "salt": (0.08, 0.2),
        "fiber": (0.5, 1.5),
        "protein": (3, 5),
        "fat": (7, 12),
        "nova": (4,),
        "additives": ("en:e471", "en:e412", "en:e407"),
    },
    {
        "slug": "biscotti",
        "name": "Biscotti da colazione",
        "brand": "Forno Italiano",
        "quantity": "350 g",
        "beverage": False,
        "kcal": (430, 510),
        "sugars": (20, 30),
        "sat": (8, 12),
        "salt": (0.3, 0.7),
        "fiber": (2, 4),
        "protein": (6, 9),
        "fat": (15, 22),
        "nova": (4,),
        "additives": ("en:e322", "en:e500", "en:e503"),
    },
    {
        "slug": "taralli",
        "name": "Taralli pugliesi",
        "brand": "Forno di Puglia",
        "quantity": "300 g",
        "beverage": False,
        "kcal": (430, 500),
        "sugars": (2, 5),
        "sat": (8, 12),
        "salt": (1.3, 2.5),
        "fiber": (2, 4),
        "protein": (8, 11),
        "fat": (14, 20),
        "nova": (4,),
        "additives": ("en:e322",),
    },
    {
        "slug": "pesto",
        "name": "Pesto alla genovese",
        "brand": "Liguria in Cucina",
        "quantity": "190 g",
        "beverage": False,
        "kcal": (300, 450),
        "sugars": (3, 7),
        "sat": (4, 8),
        "salt": (2, 3.5),
        "fiber": (2, 4),
        "protein": (4, 7),
        "fat": (30, 45),
        "fruits": (10, 30),
        "nova": (4,),
        "additives": ("en:e330", "en:e202"),
    },
    {
        "slug": "tonno-in-scatola",
        "name": "Tonno in scatola",
        "brand": "Mare Nostrum",
        "quantity": "160 g",
        "beverage": False,
        "kcal": (100, 190),
        "sugars": (0, 1),
        "sat": (1, 3),
        "salt": (0.7, 1.4),
        "fiber": (0, 0.2),
        "protein": (20, 26),
        "fat": (2, 8),
        "nova": (3,),
        "additives": (),
    },
    {
        "slug": "acqua-minerale",
        "name": "Acqua minerale naturale",
        "brand": "Fonte Italiana",
        "quantity": "1,5 L",
        "beverage": True,
        "kcal": (0, 1),
        "sugars": (0, 0.1),
        "sat": (0, 0.1),
        "salt": (0, 0.02),
        "fiber": (0, 0.1),
        "protein": (0, 0.1),
        "fat": (0, 0.1),
        "nova": (1,),
        "additives": (),
    },
    {
        "slug": "succo-d-arancia",
        "name": "Succo d'arancia",
        "brand": "Agrumi d'Italia",
        "quantity": "1 L",
        "beverage": True,
        "kcal": (40, 55),
        "sugars": (8, 12),
        "sat": (0, 0.2),
        "salt": (0, 0.05),
        "fiber": (0.3, 1.5),
        "protein": (0.4, 0.9),
        "fat": (0, 0.3),
        "fruits": (100, 100),
        "nova": (1, 3),
        "additives": (),
    },
    {
        "slug": "cola",
        "name": "Bevanda cola",
        "brand": "Italiana Bibite",
        "quantity": "330 ml",
        "beverage": True,
        "kcal": (40, 45),
        "sugars": (9, 11),
        "sat": (0, 0.1),
        "salt": (0.01, 0.03),
        "fiber": (0, 0.1),
        "protein": (0, 0.1),
        "fat": (0, 0.1),
        "nova": (4,),
        "additives": ("en:e150d", "en:e330"),
    },
    {
        "slug": "birra",
        "name": "Birra bionda",
        "brand": "Birrificio Artigiano",
        "quantity": "330 ml",
        "beverage": True,
        "kcal": (30, 50),
        "sugars": (0.5, 3),
        "sat": (0, 0.1),
        "salt": (0.01, 0.05),
        "fiber": (0, 0.1),
        "protein": (0.2, 0.5),
        "fat": (0, 0.1),
        "nova": (3,),
        "additives": (),
    },
    {
        "slug": "vino-rosso",
        "name": "Vino rosso",
        "brand": "Cantina del Borgo",
        "quantity": "750 ml",
        "beverage": True,
        "kcal": (65, 90),
        "sugars": (0.5, 2),
        "sat": (0, 0.1),
        "salt": (0, 0.02),
        "fiber": (0, 0.1),
        "protein": (0, 0.2),
        "fat": (0, 0.1),
        "nova": (1, 3),
        "additives": (),
    },
    {
        "slug": "snack-salati",
        "name": "Snack salati",
        "brand": "Aperitivo Italiano",
        "quantity": "150 g",
        "beverage": False,
        "kcal": (480, 540),
        "sugars": (1, 4),
        "sat": (3, 6),
        "salt": (1.4, 2.5),
        "fiber": (2, 4),
        "protein": (5, 8),
        "fat": (22, 30),
        "nova": (4,),
        "additives": ("en:e621", "en:e322"),
    },
    {
        "slug": "cioccolato-fondente",
        "name": "Cioccolato fondente",
        "brand": "Cioccolateria Italiana",
        "quantity": "100 g",
        "beverage": False,
        "kcal": (540, 610),
        "sugars": (25, 38),
        "sat": (18, 25),
        "salt": (0.01, 0.1),
        "fiber": (7, 12),
        "protein": (7, 11),
        "fat": (35, 45),
        "nova": (3,),
        "additives": ("en:e322",),
    },
    {
        "slug": "cereali-colazione",
        "name": "Cereali da colazione",
        "brand": "Mulino Italia",
        "quantity": "375 g",
        "beverage": False,
        "kcal": (370, 420),
        "sugars": (12, 26),
        "sat": (2, 5),
        "salt": (0.4, 1.0),
        "fiber": (5, 8),
        "protein": (7, 10),
        "fat": (5, 12),
        "nova": (4,),
        "additives": ("en:e322", "en:e500"),
    },
    {
        "slug": "crackers",
        "name": "Crackers salati",
        "brand": "Forno Croccante",
        "quantity": "250 g",
        "beverage": False,
        "kcal": (420, 480),
        "sugars": (2, 6),
        "sat": (7, 11),
        "salt": (1.3, 2.2),
        "fiber": (2, 4),
        "protein": (8, 11),
        "fat": (12, 18),
        "nova": (4,),
        "additives": ("en:e322",),
    },
    {
        "slug": "mortadella",
        "name": "Mortadella",
        "brand": "Salumificio Emiliano",
        "quantity": "150 g",
        "beverage": False,
        "kcal": (280, 330),
        "sugars": (0.5, 2),
        "sat": (9, 13),
        "salt": (1.8, 2.8),
        "fiber": (0, 0.2),
        "protein": (13, 17),
        "fat": (22, 28),
        "nova": (4,),
        "additives": ("en:e250", "en:e301"),
    },
    {
        "slug": "ricotta",
        "name": "Ricotta vaccina",
        "brand": "Cascina del Sole",
        "quantity": "250 g",
        "beverage": False,
        "kcal": (130, 180),
        "sugars": (2, 4),
        "sat": (6, 9),
        "salt": (0.2, 0.5),
        "fiber": (0, 0.1),
        "protein": (7, 10),
        "fat": (9, 13),
        "nova": (2,),
        "additives": (),
    },
    {
        "slug": "farina",
        "name": "Farina di grano tenero",
        "brand": "Mulino di Famiglia",
        "quantity": "1 kg",
        "beverage": False,
        "kcal": (330, 370),
        "sugars": (0.5, 2),
        "sat": (0.2, 0.6),
        "salt": (0, 0.02),
        "fiber": (2, 4),
        "protein": (9, 12),
        "fat": (1, 2),
        "nova": (1,),
        "additives": (),
    },
    {
        "slug": "riso",
        "name": "Riso Carnaroli",
        "brand": "Riseria Italiana",
        "quantity": "1 kg",
        "beverage": False,
        "kcal": (330, 360),
        "sugars": (0.1, 0.5),
        "sat": (0.1, 0.3),
        "salt": (0, 0.02),
        "fiber": (0.5, 1.5),
        "protein": (6, 8),
        "fat": (0.5, 1.5),
        "nova": (1,),
        "additives": (),
    },
    {
        "slug": "legumi-in-scatola",
        "name": "Legumi in scatola",
        "brand": "Leguminosa",
        "quantity": "400 g",
        "beverage": False,
        "kcal": (90, 130),
        "sugars": (1, 3),
        "sat": (0.1, 0.5),
        "salt": (0.3, 0.8),
        "fiber": (5, 8),
        "protein": (5, 8),
        "fat": (0.5, 2),
        "nova": (2,),
        "additives": ("en:e330",),
    },
)

SUFFIXES = ("Tradizionale", "Selezione", "Artigianale", "Biologico", "del Mulino", "Italiano")


def _ean13(body: str) -> str:
    total = sum((1 if index % 2 == 0 else 3) * int(digit) for index, digit in enumerate(body))
    return body + str((10 - total % 10) % 10)


def _value(rng: random.Random, bounds: tuple[float, float], missing: float = 0.06) -> float | None:
    if rng.random() < missing:
        return None
    return round(rng.uniform(*bounds), 2)


def _make_product(rng: random.Random, spec: dict, index: int) -> ProductRecord:
    pool = spec["additives"]
    count = rng.randint(0, min(3, len(pool)))
    additives = tuple(rng.sample(pool, count)) if count else ()
    organic = rng.random() < 0.18
    labels = ("en:organic",) if organic else (("en:no-gluten",) if rng.random() < 0.1 else ())
    nova = None if rng.random() < 0.08 else rng.choice(spec["nova"])
    body = f"80{index:04d}{rng.randint(0, 999999):06d}"
    doses = {
        key: round(rng.uniform(0.001, 0.8), 4)
        for key in additives
        if rng.random() < 0.25
    }

    facts = NutrientFacts(
        kcal=_value(rng, spec["kcal"]),
        sugars=_value(rng, spec["sugars"]),
        salt=_value(rng, spec["salt"]),
        saturated_fat=_value(rng, spec["sat"]),
        fat=_value(rng, spec["fat"]),
        fiber=_value(rng, spec["fiber"]),
        protein=_value(rng, spec["protein"]),
        fruits_veg_nuts=_value(rng, spec["fruits"]) if "fruits" in spec else None,
    )

    return ProductRecord(
        barcode=_ean13(body),
        name=f"{spec['name']} {rng.choice(SUFFIXES)}",
        brand=spec["brand"],
        quantity=spec["quantity"],
        categories=("en:italian-foods", f"en:{spec['slug']}"),
        labels=labels,
        ingredients_text=f"{spec['name']}, ingredienti selezionati.",
        additives=additives,
        nutriments=facts,
        nova_group=nova,
        nutriscore_grade=rng.choice("abcde"),
        is_beverage=spec["beverage"],
        nutrition_data_per="100ml" if spec["beverage"] else "100g",
        additive_doses=doses,
    )


def generate_products(limit: int = 100) -> list[ProductRecord]:
    rng = random.Random(20261106)
    return [
        _make_product(rng, PRODUCT_SPECS[index % len(PRODUCT_SPECS)], index + 1)
        for index in range(limit)
    ]


PRODUCTS = generate_products(100)


class ItalianProductsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.table = AdditiveTable.load()
        cls.engine = ScoreEngine(cls.table)

    def _check_product(self, product: ProductRecord) -> None:
        context = f"{product.name} ({product.barcode})"
        result = self.engine.score(product)

        self.assertGreaterEqual(result.score, 0, context)
        self.assertLessEqual(result.score, 100, context)
        self.assertEqual(result.grade, grade_for(result.score), context)
        self.assertEqual(result.engine_version, ENGINE_VERSION, context)
        self.assertEqual(ENGINE_VERSION, "0.3-beta", context)
        self.assertEqual(
            {component.name for component in result.components},
            {"nutrition", "additives", "processing", "organic"},
            context,
        )
        self.assertAlmostEqual(
            sum(component.weight for component in result.components),
            1.0,
            places=6,
            msg=context,
        )
        self.assertGreaterEqual(result.confidence, 0.0, context)
        self.assertLessEqual(result.confidence, 1.0, context)
        self.assertGreaterEqual(result.additive_coverage, 0.0, context)
        self.assertLessEqual(result.additive_coverage, 1.0, context)
        self.assertTrue(result.reasons, context)

        if product.additives:
            self.assertTrue(result.additives, context)
            self.assertEqual(
                len(result.additives),
                len({hit.e_number for hit in result.additives}),
                context,
            )
            self.assertTrue(
                all(0 <= hit.risk <= 100 for hit in result.additives),
                context,
            )

        payload = result.to_dict()
        self.assertEqual(payload["product"]["barcode"], product.barcode, context)
        self.assertEqual(payload["engine_version"], "0.3-beta", context)
        json.dumps(payload, ensure_ascii=False)
        self.assertEqual(self.engine.score(product).to_dict(), payload, context)


def _make_test(product: ProductRecord):
    def test(self: ItalianProductsTest) -> None:
        self._check_product(product)

    test.__doc__ = f"Prodotto italiano casuale: {product.name} ({product.barcode})"
    return test


for index, product in enumerate(PRODUCTS, start=1):
    slug = product.categories[-1].split(":")[-1].replace("-", "_")
    setattr(ItalianProductsTest, f"test_{index:03d}_{slug}", _make_test(product))


if __name__ == "__main__":
    unittest.main()
