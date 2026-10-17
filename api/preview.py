from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from depto_monitor.searches import LiveSearchRequest, search_live

MAX_REQUEST_BYTES = 40_000
logger = logging.getLogger(__name__)
WEB_DIRECTORY = (Path(__file__).resolve().parent.parent / "web").resolve()


class StaticAssetRequest(BaseModel):
    path: Literal["/", "/index.html", "/styles.css", "/app.js", "/config.js"]


STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/config.js": ("config.js", "application/javascript; charset=utf-8"),
}


def preview_listings(payload: dict[str, Any]) -> dict[str, Any]:
    return search_live(LiveSearchRequest.model_validate(payload))


def static_asset_for(request_target: str) -> tuple[Path, str] | None:
    try:
        request_path = StaticAssetRequest.model_validate({"path": urlsplit(request_target).path}).path
    except ValidationError:
        return None

    filename, content_type = STATIC_ASSETS[request_path]
    asset_path = (WEB_DIRECTORY / filename).resolve()
    if not asset_path.is_relative_to(WEB_DIRECTORY) or not asset_path.is_file():
        return None
    return asset_path, content_type


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if urlsplit(self.path).path != "/api/preview":
            self._send_json(404, {"message": "Recurso no encontrado."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._send_json(400, {"message": "La solicitud no es válida."})
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                self._send_json(400, {"message": "La solicitud no es válida."})
                return
            self._send_json(200, preview_listings(payload))
        except (json.JSONDecodeError, ValidationError, ValueError):
            logger.info("live_search_rejected")
            self._send_json(400, {"message": "Revisá la localidad y los filtros de la búsqueda."})
        except Exception as error:
            error_id = str(uuid4())
            logger.warning("live_search_failed error_id=%s error_type=%s", error_id, type(error).__name__)
            self._send_json(502, {"message": "No se pudo ejecutar la búsqueda ahora.", "error_id": error_id})

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        asset = static_asset_for(self.path)
        if asset is not None:
            asset_path, content_type = asset
            try:
                content = asset_path.read_bytes()
            except OSError:
                logger.warning("static_asset_unavailable")
                self._send_json(500, {"message": "No se pudo cargar la aplicación."})
                return
            self._send_bytes(200, content_type, content)
            return

        if urlsplit(self.path).path == "/api/preview":
            self._send_json(405, {"message": "Usá POST para ejecutar una búsqueda."})
            return
        self._send_json(404, {"message": "Recurso no encontrado."})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", encoded)

    def _send_bytes(self, status: int, content_type: str, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
