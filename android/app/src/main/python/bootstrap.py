"""Bootstrap of the Food Scanner server inside the Android APK.

The Python project is copied from the assets into filesDir at app startup.
Here we add that directory to sys.path and start the same HTTP server used by
the desktop UI, in a separate thread.
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
    """Start the server once and return the port."""
    global _server, _thread

    if _server is not None:
        return port

    root = Path(app_dir).resolve()
    if not (root / "ui" / "server.py").is_file():
        raise RuntimeError(f"Python UI not found in {root}")

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
