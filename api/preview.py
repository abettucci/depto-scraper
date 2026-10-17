from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from depto_monitor.searches import LiveSearchRequest, search_live

MAX_REQUEST_BYTES = 40_000
logger = logging.getLogger(__name__)


def preview_listings(payload: dict[str, Any]) -> dict[str, Any]:
    return search_live(LiveSearchRequest.model_validate(payload))


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
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
        self._send_json(405, {"message": "Usá POST para ejecutar una búsqueda."})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
