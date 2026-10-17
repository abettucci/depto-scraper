from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from depto_monitor.searches import LiveSearchRequest, search_live

MAX_REQUEST_BYTES = 40_000


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
            self._send_json(400, {"message": "Revisá la localidad y los filtros de la búsqueda."})
        except Exception:
            self._send_json(502, {"message": "No se pudo consultar el portal ahora. Probá de nuevo más tarde."})

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._send_json(405, {"message": "Usá POST para ejecutar una búsqueda."})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
