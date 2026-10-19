from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .browser_search import BrowserAutomationUnavailableError, BrowserSearchBusyError
from .models import SavedSearch, SearchFilters, SourceURL
from .run import _mercado_libre_credentials_from_environment
from .searches import LiveSearchRequest, search_live
from .store import SQLiteStore

MAX_REQUEST_BYTES = 40_000
WEB_DIRECTORY = (Path(__file__).resolve().parent.parent / "web").resolve()
logger = logging.getLogger(__name__)


class StaticAssetRequest(BaseModel):
    path: Literal["/", "/index.html", "/styles.css", "/app.js"]


class SaveSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    filters: SearchFilters
    source_urls: list[SourceURL] = Field(min_length=1, max_length=20)
    notify_chat_id: str | None = Field(default=None, pattern=r"^\d{8,20}$")


STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


def static_asset_for(request_target: str) -> tuple[Path, str] | None:
    try:
        request_path = StaticAssetRequest.model_validate({"path": urlsplit(request_target).path}).path
    except ValidationError:
        return None
    filename, content_type = STATIC_ASSETS[request_path]
    asset_path = (WEB_DIRECTORY / filename).resolve()
    return (asset_path, content_type) if asset_path.is_relative_to(WEB_DIRECTORY) and asset_path.is_file() else None


def preview_listings(payload: dict[str, Any]) -> dict[str, Any]:
    request = LiveSearchRequest.model_validate(payload)
    return search_live(request, mercado_libre_credentials=_mercado_libre_credentials_from_environment())


class LocalHandler(BaseHTTPRequestHandler):
    server: "LocalHTTPServer"

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/searches":
            self._send_json(200, {"searches": SQLiteStore(self.server.database_path).list_search_summaries()})
            return
        if path == "/api/preview":
            self._send_json(405, {"message": "Usá POST para ejecutar una búsqueda."})
            return
        asset = static_asset_for(self.path)
        if asset is None:
            self._send_json(404, {"message": "Recurso no encontrado."})
            return
        asset_path, content_type = asset
        try:
            self._send_bytes(200, content_type, asset_path.read_bytes())
        except OSError:
            logger.warning("local_static_asset_unavailable")
            self._send_json(500, {"message": "No se pudo cargar la aplicación."})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            payload = self._read_payload()
            if path == "/api/preview":
                self._send_json(200, preview_listings(payload))
                return
            if path == "/api/searches":
                request = SaveSearchRequest.model_validate(payload)
                search = SavedSearch(
                    id=str(uuid4()),
                    user_id="local",
                    name=request.name,
                    filters=request.filters,
                    source_urls=request.source_urls,
                    notify_chat_id=request.notify_chat_id,
                )
                SQLiteStore(self.server.database_path).save_search(search)
                self._send_json(201, {"id": search.id})
                return
            self._send_json(404, {"message": "Recurso no encontrado."})
        except BrowserSearchBusyError:
            self._send_json(409, {"message": "Ya hay una búsqueda con navegador en curso. Cerrala o esperá a que termine."})
        except BrowserAutomationUnavailableError:
            self._send_json(503, {"message": "Falta instalar Chromium para el modo navegador. Ejecutá: python -m playwright install chromium"})
        except (json.JSONDecodeError, ValidationError, ValueError):
            logger.info("local_request_rejected")
            self._send_json(400, {"message": "Revisá los datos ingresados."})
        except Exception as error:
            error_id = str(uuid4())
            logger.warning("local_request_failed error_id=%s error_type=%s", error_id, type(error).__name__)
            self._send_json(502, {"message": "No se pudo completar la operación.", "error_id": error_id})

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("invalid request length")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send_bytes(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _send_bytes(self, status: int, content_type: str, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LocalHTTPServer(ThreadingHTTPServer):
    def __init__(self, database_path: str | Path | None = None):
        self.database_path = str(database_path or os.getenv("DEPTOS_DATABASE_PATH") or "")
        super().__init__(("127.0.0.1", 8000), LocalHandler)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = LocalHTTPServer()
    print("Deptos monitor local: http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
