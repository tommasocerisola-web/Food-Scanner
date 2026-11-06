"""
Caricamento foto su Open Food Facts.

Usa l'endpoint CGI documentato (`product_image_upload.pl`): e' ancora supportato
e ha campi multipart espliciti. Le credenziali sono richieste da OFF per ogni
scrittura e vanno passate dal server, mai dal browser.
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
    """Errore di caricamento foto su Open Food Facts."""


def detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, content_type: str | None = None) -> str:
    """Valida bytes e ritorna il MIME rilevato dai magic bytes."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("Immagine vuota.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Immagine troppo grande (max 5 MB).")
    detected = detect_image_type(bytes(data))
    if detected is None:
        raise ValueError("Formato immagine non supportato (usa JPEG, PNG o WebP).")
    return detected


def validate_imagefield(value: str) -> str:
    field = str(value).strip().lower()
    if not _FIELD_RE.match(field):
        raise ValueError("Campo immagine non valido.")
    return field


def validate_barcode(value: str) -> str:
    barcode = str(value).strip()
    if not _BARCODE_RE.match(barcode):
        raise ValueError("Barcode non valido (8-14 cifre).")
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
        raise OFFUploadError(f"Open Food Facts ha risposto HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise OFFUploadError("Open Food Facts non raggiungibile.") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OFFUploadError("Risposta non JSON da Open Food Facts.") from error
    if not isinstance(payload, dict):
        raise OFFUploadError("Risposta inattesa da Open Food Facts.")
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
    """Carica una foto e ritorna la risposta JSON di OFF.

    `transport` e' iniettabile nei test: riceve la Request e ritorna il payload.
    Le credenziali non compaiono mai nei messaggi di errore.
    """
    if not user_id or not password:
        raise OFFUploadError("Credenziali Open Food Facts mancanti.")
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
    except Exception as error:  # noqa: BLE001 - rete/trasporto: messaggio pulito
        raise OFFUploadError("Errore di rete durante il caricamento.") from error

    status = str(payload.get("status", "")).strip().lower()
    if status not in {"status ok", "ok", "success"}:
        raise OFFUploadError("Open Food Facts ha rifiutato la foto.")
    return payload
