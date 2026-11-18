"""
Photo upload to Open Food Facts.

Uses the documented CGI endpoint (`product_image_upload.pl`): it is still
supported and has explicit multipart fields. OFF requires credentials for every
write, and they must be passed by the server, never by the browser.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

DEFAULT_UPLOAD_URL = "https://world.openfoodfacts.org/cgi/product_image_upload.pl"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
USER_AGENT = "generic_01-ui/0.3-beta (food score test UI)"

_FIELD_RE = re.compile(r"^(front|nutrition|ingredients|packaging|other)(_[a-z]{2})?$")
_BARCODE_RE = re.compile(r"^\d{8,14}$")


class OFFUploadError(Exception):
    """Photo upload error on Open Food Facts."""


def detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, content_type: str | None = None) -> str:
    """Validate the bytes and return the MIME type detected from the magic bytes."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("Empty image.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large (max 5 MB).")
    detected = detect_image_type(bytes(data))
    if detected is None:
        raise ValueError("Unsupported image format (use JPEG, PNG or WebP).")
    return detected


def validate_imagefield(value: str) -> str:
    field = str(value).strip().lower()
    if not _FIELD_RE.match(field):
        raise ValueError("Invalid image field.")
    return field


def validate_barcode(value: str) -> str:
    barcode = str(value).strip()
    if not _BARCODE_RE.match(barcode):
        raise ValueError("Invalid barcode (8-14 digits).")
    return barcode


def build_multipart(
    fields: Iterable[tuple[str, str]],
    files: Iterable[tuple[str, str, str, bytes]],
) -> tuple[bytes, str]:
    boundary = f"----generic01{secrets.token_hex(12)}"
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    for field_name, filename, content_type, data in files:
        safe_name = str(filename).replace('"', "")
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'.encode(
                "utf-8"
            )
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        parts.append(bytes(data))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def credentials_from_env() -> tuple[str, str] | None:
    user_id = os.environ.get("OFF_USER_ID", "").strip()
    password = os.environ.get("OFF_PASSWORD", "")
    if not user_id or not password:
        return None
    return user_id, password


def _http_post(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise OFFUploadError(f"Open Food Facts replied HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise OFFUploadError("Open Food Facts is unreachable.") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OFFUploadError("Non-JSON response from Open Food Facts.") from error
    if not isinstance(payload, dict):
        raise OFFUploadError("Unexpected response from Open Food Facts.")
    return payload


def upload_image(
    barcode: str,
    imagefield: str,
    image_bytes: bytes,
    content_type: str | None,
    user_id: str,
    password: str,
    upload_url: str = DEFAULT_UPLOAD_URL,
    timeout: float = 30.0,
    transport: Callable[[urllib.request.Request], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Upload one photo and return the JSON response from OFF.

    `transport` is injectable in tests: it receives the Request and returns the
    payload. Credentials never appear in error messages.
    """
    if not user_id or not password:
        raise OFFUploadError("Missing Open Food Facts credentials.")
    barcode = validate_barcode(barcode)
    field = validate_imagefield(imagefield)
    detected = validate_image(image_bytes, content_type)
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[detected]
    filename = f"{field}.{extension}"

    body, boundary = build_multipart(
        fields=[
            ("user_id", user_id),
            ("password", password),
            ("code", barcode),
            ("imagefield", field),
        ],
        files=[(f"imgupload_{field}", filename, detected, image_bytes)],
    )
    request = urllib.request.Request(
        upload_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        payload = _http_post(request, timeout) if transport is None else transport(request)
    except OFFUploadError:
        raise
    except Exception as error:  # noqa: BLE001 - network/transport: clean message
        raise OFFUploadError("Network error during the upload.") from error

    status = str(payload.get("status", "")).strip().lower()
    if status not in {"status ok", "ok", "success"}:
        raise OFFUploadError("Open Food Facts rejected the photo.")
    return payload
