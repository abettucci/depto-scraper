from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from depto_monitor.models import Portal, SourceURL
from depto_monitor.sources import fetch_source

MAX_REQUEST_BYTES = 4_096
MAX_PREVIEW_RESULTS = 12


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portal: Portal
    url: str = Field(min_length=12, max_length=2_048)

    def source(self) -> SourceURL:
        return SourceURL(portal=self.portal, url=self.url)


def preview_listings(payload: dict[str, Any]) -> dict[str, Any]:
    request = PreviewRequest.model_validate(payload)
    listings = fetch_source(request.source())
    visible_listings = listings[:MAX_PREVIEW_RESULTS]
    return {
        "portal": request.portal.value,
        "found": len(listings),
        "listings": [listing.model_dump(mode="json") for listing in visible_listings],
    }


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
            self._send_json(400, {"message": "Indicá una URL HTTPS válida para el portal elegido."})
        except Exception:
            self._send_json(502, {"message": "No se pudo consultar el portal ahora. Probá de nuevo más tarde."})

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._send_json(405, {"message": "Usá POST para probar una búsqueda."})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
