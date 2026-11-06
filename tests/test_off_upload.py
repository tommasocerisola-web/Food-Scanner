"""Test offline del caricamento foto su Open Food Facts."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from engine.off_upload import (
    MAX_IMAGE_BYTES,
    OFFUploadError,
    build_multipart,
    credentials_from_env,
    upload_image,
    validate_barcode,
    validate_image,
    validate_imagefield,
)

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 200


class ValidationTests(unittest.TestCase):
    def test_barcode(self) -> None:
        self.assertEqual(validate_barcode("3017620422003"), "3017620422003")
        for value in ("", "abc", "123", "1" * 15):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_barcode(value)

    def test_imagefield(self) -> None:
        self.assertEqual(validate_imagefield("front"), "front")
        self.assertEqual(validate_imagefield("nutrition"), "nutrition")
        for value in ("", "front;rm", "../front", "image"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_imagefield(value)

    def test_image_type_and_size(self) -> None:
        self.assertEqual(validate_image(JPEG), "image/jpeg")
        self.assertEqual(validate_image(b"\x89PNG\r\n\x1a\n" + b"x" * 20), "image/png")
        with self.assertRaises(ValueError):
            validate_image(b"not an image")
        with self.assertRaises(ValueError):
            validate_image(b"\xff\xd8\xff" + b"x" * (MAX_IMAGE_BYTES + 1))

    def test_credentials_from_env(self) -> None:
        with patch.dict(os.environ, {"OFF_USER_ID": "bob", "OFF_PASSWORD": "pw"}):
            self.assertEqual(credentials_from_env(), ("bob", "pw"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(credentials_from_env())


class MultipartTests(unittest.TestCase):
    def test_build_multipart(self) -> None:
        body, boundary = build_multipart(
            [("user_id", "bob"), ("code", "3017620422003")],
            [("imgupload_front", "front.jpg", "image/jpeg", JPEG)],
        )
        self.assertIn(boundary.encode(), body)
        self.assertIn(b'name="user_id"', body)
        self.assertIn(b"3017620422003", body)
        self.assertIn(b'name="imgupload_front"; filename="front.jpg"', body)
        self.assertIn(b"Content-Type: image/jpeg", body)
        self.assertTrue(body.endswith(b"--\r\n"))


class UploadTests(unittest.TestCase):
    def test_upload_builds_expected_request(self) -> None:
        captured = {}

        def transport(request):
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["headers"] = dict(request.header_items())
            return {"status": "status ok", "imgid": 123, "imagefield": "front"}

        result = upload_image(
            barcode="3017620422003",
            imagefield="front",
            image_bytes=JPEG,
            content_type="image/jpeg",
            user_id="bob",
            password="secret",
            transport=transport,
        )
        self.assertEqual(result["imgid"], 123)
        self.assertEqual(captured["url"], "https://world.openfoodfacts.org/cgi/product_image_upload.pl")
        self.assertIn(b'name="code"', captured["body"])
        self.assertIn(b"3017620422003", captured["body"])
        self.assertIn(b'name="imgupload_front"; filename="front.jpg"', captured["body"])
        self.assertIn("multipart/form-data", captured["headers"]["Content-type"])
        self.assertNotIn("secret", repr(result))

    def test_rejected_status_does_not_leak_password(self) -> None:
        with self.assertRaises(OFFUploadError) as ctx:
            upload_image(
                barcode="3017620422003",
                imagefield="nutrition",
                image_bytes=JPEG,
                content_type="image/jpeg",
                user_id="bob",
                password="secret",
                transport=lambda request: {"status": "status not ok", "debug": "boom"},
            )
        self.assertNotIn("secret", str(ctx.exception))

    def test_missing_credentials(self) -> None:
        with self.assertRaises(OFFUploadError):
            upload_image(
                barcode="3017620422003",
                imagefield="front",
                image_bytes=JPEG,
                content_type="image/jpeg",
                user_id="",
                password="",
            )


if __name__ == "__main__":
    unittest.main()
