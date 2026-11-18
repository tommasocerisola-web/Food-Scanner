"""Bootstrap del server Food Scanner dentro l'APK Android.

Il progetto Python viene copiato dagli asset in filesDir all'avvio dell'app.
Qui aggiungiamo quella directory a sys.path e avviamo lo stesso server HTTP
usato dalla UI desktop, in un thread separato.
"""
from __future__ import annotations

import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None


def start(app_dir: str, port: int = 8765) -> int:
    """Avvia il server una sola volta e restituisce la porta."""
    global _server, _thread

    if _server is not None:
        return port

    root = Path(app_dir).resolve()
    if not (root / "ui" / "server.py").is_file():
        raise RuntimeError(f"UI Python non trovata in {root}")

    os.chdir(root)
    sys.path.insert(0, str(root))

    from engine import AdditiveTable, ScoreEngine
    from engine.off_client import OFFClient
    from ui.server import create_handler

    table = AdditiveTable.load()
    engine = ScoreEngine(table)
    cache_path = root / "cache" / "off.sqlite"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    handler = create_handler(
        engine,
        client_factory=lambda: OFFClient(cache_path=cache_path),
    )
    _server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    _thread = threading.Thread(
        target=_server.serve_forever,
        name="foodscanner-http",
        daemon=True,
    )
    _thread.start()
    print(f"Food Scanner Android: http://127.0.0.1:{port}", flush=True)
    return port
