"""Test offline della UI di test (nessuna rete)."""
from __future__ import annotations

import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from engine import AdditiveTable, ScoreEngine, normalize_product
from ui.server import create_handler, load_demo, score_payload, validate_barcode


class BarcodeTests(unittest.TestCase):
    def test_valid_barcodes(self) -> None:
        self.assertEqual(validate_barcode("3017620422003"), "3017620422003")
        self.assertEqual(validate_barcode(" 3017620422003 "), "3017620422003")
        self.assertEqual(validate_barcode("301-762-042-2003"), "3017620422003")
        self.assertEqual(validate_barcode("12345678"), "12345678")
        self.assertEqual(validate_barcode("1" * 14), "1" * 14)

    def test_invalid_barcodes(self) -> None:
        for value in ("", "abc", "123", "1" * 15, "3017x", "3017 62"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_barcode(value)


class ScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = ScoreEngine(AdditiveTable.load())

    def test_demo_produces_complete_result(self) -> None:
        result = score_payload(load_demo(), self.engine)
        self.assertEqual(result["engine_version"], "0.3-beta")
        self.assertIsInstance(result["score"], int)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertEqual(result["product"]["barcode"], "3017620422003")
        self.assertEqual(len(result["components"]), 4)
        self.assertTrue(result["reasons"])
        self.assertTrue(0 <= result["confidence"] <= 1)

    def test_demo_has_additives(self) -> None:
        result = score_payload(load_demo(), self.engine)
        e_numbers = {hit["e_number"] for hit in result["additives"]}
        self.assertIn("E322", e_numbers)
        self.assertIn("E471", e_numbers)

    def test_wrapped_product_is_accepted(self) -> None:
        result = score_payload({"product": load_demo()}, self.engine)
        self.assertEqual(result["product"]["barcode"], "3017620422003")

    def test_non_object_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            score_payload([], self.engine)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = ScoreEngine(AdditiveTable.load())
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(cls.engine),
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _get(self, path: str):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def _json(self, path: str) -> tuple[int, dict]:
        with self._get(path) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_index_served(self) -> None:
        with self._get("/") as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers.get("Content-Type", ""))
        self.assertIn("Food Scanner", body)
        self.assertIn("v0.3 beta", body)
        self.assertIn("food score lab", body)
        self.assertIn("/static/app.js", body)

    def test_static_css_served(self) -> None:
        with self._get("/static/style.css") as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("text/css", response.headers.get("Content-Type", ""))
        self.assertIn(":root", body)

    def test_health(self) -> None:
        status, payload = self._json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["engine_version"], "0.3-beta")
        self.assertGreater(payload["additives"]["total"], 400)
        self.assertIn("contribute", payload)
        self.assertIsInstance(payload["contribute"]["configured"], bool)

    def test_demo_endpoint(self) -> None:
        status, payload = self._json("/api/demo")
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "demo")
        self.assertIn("product", payload)
        self.assertEqual(payload["result"]["product"]["barcode"], "3017620422003")
        self.assertTrue(payload["result"]["additives"])

    def test_invalid_barcode_returns_400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/score?barcode=abc")
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertIn("barcode", payload["error"].lower())

    def test_post_product_scores(self) -> None:
        body = json.dumps(load_demo()).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/score",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "manual")
        self.assertEqual(payload["result"]["product"]["barcode"], "3017620422003")

    def test_post_invalid_json_returns_400(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/score",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_endpoint_returns_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)
        self.assertIn("non trovato", ctx.exception.read().decode("utf-8").lower())

    def test_barcode_endpoint_with_fake_client(self) -> None:
        class FakeClient:
            def fetch(self, barcode: str):
                return normalize_product(load_demo())

            def close(self) -> None:
                pass

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(self.engine, client_factory=FakeClient),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/score?barcode=3017620422003"
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "openfoodfacts")
        self.assertEqual(payload["result"]["product"]["barcode"], "3017620422003")


class ContributeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = ScoreEngine(AdditiveTable.load())
        cls.uploads: list[dict] = []

        def uploader(**kwargs):
            cls.uploads.append(kwargs)
            return {"status": "status ok", "imgid": len(cls.uploads)}

        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(
                cls.engine,
                credentials=("utente", "password-test"),
                uploader=uploader,
            ),
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    @staticmethod
    def _image() -> str:
        return base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * 200).decode("ascii")

    def _post(self, base: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            base + "/api/contribute",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_upload_two_images(self) -> None:
        payload = {
            "barcode": "3017620422003",
            "images": {
                "front": {"data": self._image(), "content_type": "image/jpeg"},
                "nutrition": {"data": self._image(), "content_type": "image/jpeg"},
            },
        }
        status, body = self._post(self.base, payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["uploaded"]), 2)
        self.assertNotIn("password-test", json.dumps(body))
        self.assertEqual(len(self.uploads), 2)

    def test_missing_credentials(self) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(self.engine, credentials=("", ""), uploader=lambda **kwargs: {}),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            status, body = self._post(
                base,
                {"barcode": "3017620422003", "images": {"front": {"data": self._image()}}},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "off_credentials_missing")

    def test_invalid_image(self) -> None:
        payload = {
            "barcode": "3017620422003",
            "images": {"front": {"data": base64.b64encode(b"not an image").decode()}},
        }
        status, body = self._post(self.base, payload)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_unknown_image_field(self) -> None:
        payload = {
            "barcode": "3017620422003",
            "images": {"other": {"data": self._image()}},
        }
        status, body = self._post(self.base, payload)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_invalid_base64(self) -> None:
        payload = {
            "barcode": "3017620422003",
            "images": {"front": {"data": "!!!"}},
        }
        status, body = self._post(self.base, payload)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
