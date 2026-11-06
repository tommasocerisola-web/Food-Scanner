"""Robustezza del motore: fuzz, casi limite, client HTTP, cache e performance."""
from __future__ import annotations

import json
import random
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from engine import OFFClient, OFFError, ScoreEngine, normalize_product
from engine.off_client import ProductCache
from engine.report import render_report
from tests.test_engine import make_table

E_POOL = [
    "en:e100",
    "en:e102",
    "en:e150d",
    "en:e211",
    "en:e250",
    "en:e322",
    "en:e322i",
    "en:e407",
    "en:e407a",
    "en:e621",
    "en:e951",
    "en:e9999",  # sconosciuto: deve essere ignorato, non far crashare
]
NUTRIENTS = {
    "energy-kcal_100g": 900,
    "sugars_100g": 100,
    "saturated-fat_100g": 60,
    "salt_100g": 8,
    "sodium_100g": 4,
    "proteins_100g": 40,
    "fiber_100g": 30,
}


def random_raw(rng: random.Random) -> dict:
    raw: dict = {"code": str(rng.randint(1, 10**12))}
    if rng.random() < 0.8:
        raw["product_name"] = f"Prodotto {rng.randint(1, 999)}"
    if rng.random() < 0.7:
        raw["brands"] = "Marca"
    if rng.random() < 0.85:
        raw["nutriments"] = {
            key: round(rng.uniform(0, maximum), 3)
            for key, maximum in NUTRIENTS.items()
            if rng.random() < 0.6
        }
    if rng.random() < 0.15:
        raw.setdefault("nutriments", {})["energy-kcal_100g"] = -20  # assurdo
    if rng.random() < 0.15:
        raw.setdefault("nutriments", {})["sugars_100g"] = "12.5"  # stringa numerica
    count = rng.randint(0, 6)
    raw["additives_tags"] = rng.sample(E_POOL, count)
    if rng.random() < 0.5:
        raw["ingredients"] = [
            {"id": tag, "percent_estimate": round(rng.uniform(0, 5), 3)}
            for tag in raw["additives_tags"]
            if tag.startswith("en:e")
        ]
    categories = ["en:plant-based-foods-and-beverages", "en:snacks", "en:spreads"]
    if rng.random() < 0.3:
        categories.append("en:beverages")
    if rng.random() < 0.15:
        categories.append("en:colas")
    raw["categories_tags"] = categories
    raw["labels_tags"] = ["en:organic"] if rng.random() < 0.2 else ["en:no-gluten"]
    raw["product_quantity_unit"] = rng.choice(["g", "g", "ml", ""])
    if rng.random() < 0.8:
        raw["nova_group"] = rng.choice([1, 2, 3, 4, 5])
    if rng.random() < 0.5:
        raw["nutrition_data_per"] = rng.choice(["100g", "serving"])
    return raw


class FuzzTests(unittest.TestCase):
    def test_random_products_never_crash_and_stay_in_range(self) -> None:
        rng = random.Random(2024)
        engine = ScoreEngine(make_table([("E250", "preservative", "Nitrito di sodio", "60")]))
        for _ in range(600):
            record = normalize_product(random_raw(rng))
            result = engine.score(record)
            self.assertGreaterEqual(result.score, 0)
            self.assertLessEqual(result.score, 100)
            self.assertGreater(result.confidence, 0.0)
            self.assertLessEqual(result.confidence, 1.0001)
            self.assertGreaterEqual(result.additive_coverage, 0.0)
            self.assertLessEqual(result.additive_coverage, 1.0)
            payload = result.to_dict()
            json.dumps(payload)  # deve essere serializzabile sempre
            text = render_report(result)
            self.assertIn("Score", text)

    def test_empty_product_is_scored_conservatively(self) -> None:
        engine = ScoreEngine(make_table([]))
        result = engine.score(normalize_product({}))
        self.assertGreaterEqual(result.score, 0)
        self.assertLessEqual(result.score, 100)
        self.assertLess(result.confidence, 1.0)
        self.assertTrue(any("Nutrienti non valutati" in reason for reason in result.reasons))

    def test_extreme_values_are_clamped(self) -> None:
        engine = ScoreEngine(make_table([]))
        result = engine.score(
            normalize_product(
                {
                    "code": "1",
                    "nutriments": {
                        "energy-kcal_100g": 1e9,
                        "sugars_100g": 1e9,
                        "saturated-fat_100g": 1e9,
                        "salt_100g": 1e9,
                    },
                }
            )
        )
        self.assertGreaterEqual(result.score, 0)
        self.assertLessEqual(result.score, 100)

    def test_non_finite_values_are_ignored(self) -> None:
        engine = ScoreEngine(make_table([]))
        for bad in (float("nan"), float("inf"), float("-inf"), "abc", None):
            record = normalize_product({"code": "1", "nutriments": {"energy-kcal_100g": bad}})
            self.assertIsNone(record.nutriments.kcal)
            result = engine.score(record)
            self.assertGreaterEqual(result.score, 0)
            self.assertLessEqual(result.score, 100)

    def test_low_coverage_does_not_claim_excellence(self) -> None:
        engine = ScoreEngine(make_table([]))
        empty = engine.score(normalize_product({}))
        self.assertLess(empty.confidence, 0.6)
        self.assertLess(empty.score, 75)
        self.assertGreaterEqual(empty.score, 40)

    def test_many_additives_do_not_break_or_dominate(self) -> None:
        table = make_table([(f"E{1000 + i}", "preservative", f"Additivo {i}", "20") for i in range(200)])
        engine = ScoreEngine(table)
        tags = [f"en:e{1000 + i}" for i in range(200)]
        result = engine.score(
            normalize_product(
                {
                    "code": "1",
                    "additives_tags": tags,
                    "nutriments": {"energy-kcal_100g": 50, "sugars_100g": 1, "salt_100g": 0.1},
                }
            )
        )
        self.assertEqual(len(result.additives), 200)
        self.assertGreaterEqual(result.score, 0)
        self.assertLessEqual(result.score, 100)


class ClientRobustnessTests(unittest.TestCase):
    def _client(self, transport, retries: int = 2) -> OFFClient:
        return OFFClient(transport=transport, cache_path=None, retries=retries, rpm=0, timeout=1)

    def test_404_is_not_retried(self) -> None:
        calls: list[str] = []

        def transport(url: str):
            calls.append(url)
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)

        with mock.patch("engine.off_client.time.sleep"):
            with self.assertRaises(OFFError):
                self._client(transport).fetch("1")
        self.assertEqual(len(calls), 1)

    def test_503_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def transport(url: str):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(url, 503, "busy", {}, None)
            return {"status": 1, "product": {"code": "1", "product_name": "ok"}}

        with mock.patch("engine.off_client.time.sleep"):
            record = self._client(transport, retries=3).fetch("1")
        self.assertEqual(record.name, "ok")
        self.assertEqual(calls["n"], 3)

    def test_retry_after_header_is_honoured(self) -> None:
        calls = {"n": 0}

        def transport(url: str):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(url, 429, "slow down", {"Retry-After": "7"}, None)
            return {"status": 1, "product": {"code": "1"}}

        with mock.patch("engine.off_client.time.sleep") as sleep:
            self._client(transport).fetch("1")
        sleep.assert_called_with(7.0)

    def test_garbage_response_raises_offerror(self) -> None:
        def transport(url: str):
            raise json.JSONDecodeError("bad", "", 0)

        with mock.patch("engine.off_client.time.sleep"):
            with self.assertRaises(OFFError):
                self._client(transport).fetch("1")

    def test_batch_isolates_single_failure(self) -> None:
        def transport(url: str):
            if "404" in url:
                raise urllib.error.HTTPError(url, 404, "nope", {}, None)
            return {"status": 1, "product": {"code": "1", "product_name": "ok"}}

        with mock.patch("engine.off_client.time.sleep"):
            results = self._client(transport).fetch_many(["1", "404", "2"], workers=3)
        self.assertEqual(len([v for v in results.values() if isinstance(v, Exception)]), 1)
        self.assertEqual(len([v for v in results.values() if not isinstance(v, Exception)]), 2)


class RateLimiterTests(unittest.TestCase):
    def test_limiter_slows_down_on_429_and_recovers(self) -> None:
        from engine.off_client import RateLimiter

        limiter = RateLimiter(rpm=600)
        base = limiter.interval
        limiter.penalize()
        self.assertGreater(limiter.interval, base)
        for _ in range(50):
            limiter.relax()
        self.assertAlmostEqual(limiter.interval, base, places=3)


class CacheConcurrencyTests(unittest.TestCase):
    def test_parallel_writes_and_reads(self) -> None:
        path = Path(tempfile.mkdtemp()) / "cache.sqlite"
        cache = ProductCache(path, signature="sig")
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                for _ in range(20):
                    cache.put(str(index), {"status": 1})
                    cache.get(str(index))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertIsNotNone(cache.get("3"))
        cache.close()


class PerformanceTests(unittest.TestCase):
    def test_scoring_is_fast(self) -> None:
        rng = random.Random(7)
        engine = ScoreEngine(
            make_table([("E250", "preservative", "Nitrito di sodio", "60"), ("E330", "acidity regulator", "Citrico", "3")])
        )
        records = [normalize_product(random_raw(rng)) for _ in range(500)]
        start = time.perf_counter()
        for record in records * 4:  # 2000 valutazioni
            engine.score(record)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0, f"2000 valutazioni in {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
