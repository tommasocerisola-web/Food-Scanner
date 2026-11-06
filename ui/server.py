"""
UI di test per il motore di scoring: server HTTP minimale, solo standard library.

Uso:
    python ui/server.py
    python ui/server.py --host 0.0.0.0 --port 8000

Endpoint:
    GET  /                      pagina di test
    GET  /api/health            versione motore e statistiche tabella
    GET  /api/demo              prodotto demo offline (grezzo + score)
    GET  /api/score?barcode=... Open Food Facts + scoring
    POST /api/score             prodotto OFF-like in JSON + scoring

Non e' un'app di produzione: serve a vedere cosa fa il motore su dati reali
o di prova, senza dipendenze e senza build frontend.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    # Permette `python ui/server.py` oltre a `python -m ui.server`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import (
    AdditiveTable,
    OFFClient,
    OFFError,
    ScoreEngine,
    normalize_product,
)
from engine.off_upload import (
    DEFAULT_UPLOAD_URL,
    OFFUploadError,
    credentials_from_env,
    upload_image as off_upload_image,
    validate_image,
)
from engine.scoring import ENGINE_VERSION

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEMO_PATH = Path(__file__).resolve().parent / "demo_product.json"
DEFAULT_CACHE = BASE_DIR / "cache" / "off.sqlite"
MAX_BODY = 12 * 1024 * 1024
BARCODE_MIN, BARCODE_MAX = 8, 14


def validate_barcode(value: str) -> str:
    barcode = "".join(char for char in str(value) if char not in " \t\r\n-_")
    if not barcode:
        raise ValueError("Inserisci un barcode.")
    if not barcode.isdigit():
        raise ValueError("Il barcode deve contenere solo cifre.")
    if not BARCODE_MIN <= len(barcode) <= BARCODE_MAX:
        raise ValueError(f"Il barcode deve avere tra {BARCODE_MIN} e {BARCODE_MAX} cifre.")
    return barcode


def load_demo() -> dict[str, Any]:
    payload = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
    product = payload.get("product", payload) if isinstance(payload, dict) else payload
    if not isinstance(product, dict):
        raise ValueError("Il file demo non contiene un prodotto JSON.")
    return product


def score_payload(raw: Any, engine: ScoreEngine) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Il prodotto deve essere un oggetto JSON.")
    product = raw.get("product", raw)
    if not isinstance(product, dict):
        raise ValueError("Il campo 'product' deve essere un oggetto JSON.")
    return engine.score(normalize_product(product)).to_dict()


def _static_file(relative: str) -> Path | None:
    candidate = (STATIC_DIR / relative.lstrip("/")).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


class ScoreHandler(BaseHTTPRequestHandler):
    server_version = "generic_01-ui/0.3-beta"
    engine: ScoreEngine

    def make_client(self) -> OFFClient:
        return OFFClient(cache_path=DEFAULT_CACHE)

    def credentials(self) -> tuple[str, str] | None:
        return credentials_from_env()

    def upload_url(self) -> str:
        return os.environ.get("OFF_UPLOAD_URL", DEFAULT_UPLOAD_URL)

    def upload_photo(self, **kwargs: Any) -> dict[str, Any]:
        return off_upload_image(**kwargs)

    # ----------------------------- risposte ----------------------------- #
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    def _serve_static(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "File non trovato.")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------- routing ------------------------------ #
    def do_GET(self) -> None:  # noqa: N802 - API di BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._serve_static(STATIC_DIR / "index.html")
            return
        if route.startswith("/static/"):
            static = _static_file(route[len("/static/"):])
            if static is None:
                self._error(HTTPStatus.NOT_FOUND, "File statico non trovato.")
            else:
                self._serve_static(static)
            return
        if route == "/api/health":
            stats = self.engine.table.stats() if self.engine.table is not None else {}
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "engine_version": ENGINE_VERSION,
                    "additives": stats,
                    "demo": DEMO_PATH.exists(),
                    "contribute": {
                        "configured": self.credentials() is not None,
                        "fields": ["front", "nutrition"],
                    },
                },
            )
            return
        if route == "/api/demo":
            try:
                raw = load_demo()
                result = score_payload(raw, self.engine)
            except (OSError, ValueError) as error:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Demo non disponibile: {error}")
                return
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "source": "demo", "result": result, "product": raw},
            )
            return
        if route == "/api/score":
            query = parse_qs(parsed.query)
            try:
                barcode = validate_barcode(query.get("barcode", [""])[0])
            except ValueError as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
                return
            self._score_barcode(barcode)
            return
        self._error(HTTPStatus.NOT_FOUND, "Endpoint non trovato.")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/score", "/api/contribute"}:
            self._error(HTTPStatus.NOT_FOUND, "Endpoint non trovato.")
            return
        try:
            raw = self._read_json()
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if parsed.path == "/api/score":
            try:
                result = score_payload(raw, self.engine)
            except ValueError as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
                return
            except Exception as error:  # noqa: BLE001 - la UI deve mostrare l'errore, non cadere
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Errore nello scoring: {error}")
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "source": "manual", "result": result})
            return
        self._contribute(raw)

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length non valido.") from error
        if length <= 0:
            raise ValueError("Corpo JSON mancante.")
        if length > MAX_BODY:
            raise ValueError("Corpo troppo grande (max 12 MB).")
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"JSON non valido: {error}") from error

    def _contribute(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            self._error(HTTPStatus.BAD_REQUEST, "Payload non valido.")
            return
        try:
            barcode = validate_barcode(raw.get("barcode", ""))
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        images = raw.get("images")
        if not isinstance(images, dict) or not images:
            self._error(HTTPStatus.BAD_REQUEST, "Carica almeno una foto (front o nutrition).")
            return
        allowed = {"front", "nutrition"}
        unknown = set(images) - allowed
        if unknown:
            self._error(
                HTTPStatus.BAD_REQUEST,
                f"Campo immagine non supportato: {', '.join(sorted(unknown))}.",
            )
            return
        credentials = self.credentials()
        if credentials is None:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "code": "off_credentials_missing",
                    "error": (
                        "Caricamento non configurato: imposta OFF_USER_ID e OFF_PASSWORD "
                        "nell'ambiente del server (non nel browser)."
                    ),
                },
            )
            return

        prepared: list[tuple[str, bytes, str]] = []
        for field, image in images.items():
            if not isinstance(image, dict):
                self._error(HTTPStatus.BAD_REQUEST, f"Immagine '{field}' non valida.")
                return
            data_b64 = image.get("data")
            if not isinstance(data_b64, str) or not data_b64:
                self._error(HTTPStatus.BAD_REQUEST, f"Immagine '{field}' senza dati.")
                return
            try:
                data = base64.b64decode(data_b64, validate=True)
                content_type = validate_image(data, image.get("content_type"))
            except (binascii.Error, ValueError) as error:
                self._error(HTTPStatus.BAD_REQUEST, f"Immagine '{field}': {error}")
                return
            prepared.append((field, data, content_type))

        uploaded: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        user_id, password = credentials
        for field, data, content_type in prepared:
            try:
                result = self.upload_photo(
                    barcode=barcode,
                    imagefield=field,
                    image_bytes=data,
                    content_type=content_type,
                    user_id=user_id,
                    password=password,
                    upload_url=self.upload_url(),
                )
            except OFFUploadError as error:
                errors.append({"imagefield": field, "error": str(error)})
            except Exception:  # noqa: BLE001 - nessun dettaglio sensibile in risposta
                errors.append({"imagefield": field, "error": "Errore imprevisto durante l'upload."})
            else:
                uploaded.append(
                    {
                        "imagefield": field,
                        "status": result.get("status"),
                        "imgid": result.get("imgid"),
                    }
                )

        if errors and not uploaded:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "uploaded": [], "errors": errors, "error": "Nessuna foto è stata caricata."},
            )
            return
        if errors:
            self._send_json(
                HTTPStatus.MULTI_STATUS,
                {
                    "ok": False,
                    "uploaded": uploaded,
                    "errors": errors,
                    "error": "Alcune foto non sono state caricate.",
                },
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "uploaded": uploaded,
                "message": "Foto inviate a Open Food Facts.",
            },
        )

    # ----------------------------- scoring ------------------------------ #
    def _score_barcode(self, barcode: str) -> None:
        client: OFFClient | None = None
        try:
            client = self.make_client()
            product = client.fetch(barcode)
        except OFFError as error:
            message = str(error)
            not_found = "non trovat" in message.lower() or "non presente" in message.lower()
            self._error(HTTPStatus.NOT_FOUND if not_found else HTTPStatus.BAD_GATEWAY, message)
            return
        except Exception as error:  # noqa: BLE001
            self._error(HTTPStatus.BAD_GATEWAY, f"Errore nel recupero del prodotto: {error}")
            return
        finally:
            if client is not None:
                client.close()
        try:
            result = self.engine.score(product).to_dict()
        except Exception as error:  # noqa: BLE001
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Errore nello scoring: {error}")
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "source": "openfoodfacts", "result": result},
        )


def create_handler(
    engine: ScoreEngine,
    client_factory: Callable[[], OFFClient] | None = None,
    credentials: tuple[str, str] | None = None,
    uploader: Callable[..., dict[str, Any]] | None = None,
) -> type[ScoreHandler]:
    class Handler(ScoreHandler):
        pass

    Handler.engine = engine
    if client_factory is not None:
        Handler.make_client = lambda self: client_factory()
    if credentials is not None:
        Handler.credentials = lambda self: credentials if credentials and all(credentials) else None
    if uploader is not None:
        Handler.upload_photo = lambda self, **kwargs: uploader(**kwargs)
    return Handler


def make_server(
    host: str,
    port: int,
    engine: ScoreEngine,
    client_factory: Callable[[], OFFClient] | None = None,
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), create_handler(engine, client_factory))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="UI di test per generic_01 (solo standard library)."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Indirizzo di ascolto (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Porta (default 8000)")
    parser.add_argument("--table", type=Path, default=None, help="CSV additivi alternativo")
    parser.add_argument("--base-url", default="https://world.openfoodfacts.org")
    parser.add_argument("--rpm", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)

    table = AdditiveTable.load(args.table)
    engine = ScoreEngine(table)
    handler = create_handler(
        engine,
        client_factory=lambda: OFFClient(
            base_url=args.base_url,
            cache_path=None if args.no_cache else DEFAULT_CACHE,
            rpm=args.rpm,
            retries=args.retries,
        ),
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    stats = table.stats()
    print(
        f"UI di test: http://{args.host}:{args.port}  ·  engine {ENGINE_VERSION}  ·  "
        f"{stats.get('total', '?')} additivi",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArresto UI.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
