from __future__ import annotations

import base64
import hmac
import json
import logging
import os
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from depto_monitor.run import run_monitor
from depto_monitor.searches import LiveSearchRequest, search_live
from depto_monitor.sources import MercadoLibreCredentials

MAX_REQUEST_BYTES = 40_000
logger = logging.getLogger(__name__)


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supabase_url: str = Field(min_length=12, max_length=2048)
    supabase_service_role_key: str = Field(min_length=20, max_length=4096)
    preview_shared_secret: str = Field(min_length=32, max_length=4096)
    whatsapp_sidecar_url: str | None = Field(default=None, max_length=2048)
    waweb_shared_secret: str | None = Field(default=None, max_length=4096)
    default_whatsapp_chat_id: str | None = Field(default=None, pattern=r"^\d{8,20}$")
    meli_client_id: str | None = Field(default=None, min_length=1, max_length=256)
    meli_client_secret: str | None = Field(default=None, min_length=8, max_length=4096)
    meli_refresh_token: str | None = Field(default=None, min_length=16, max_length=4096)
    meli_access_token: str | None = Field(default=None, min_length=16, max_length=4096)
    meli_access_token_expires_at: int | None = Field(default=None, ge=0, le=4_102_444_800)

    @field_validator(
        "whatsapp_sidecar_url",
        "waweb_shared_secret",
        "default_whatsapp_chat_id",
        "meli_client_id",
        "meli_client_secret",
        "meli_refresh_token",
        "meli_access_token",
        mode="before",
    )
    @classmethod
    def blank_optional_values_are_absent(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    def mercado_libre_credentials(self) -> MercadoLibreCredentials | None:
        values = (self.meli_client_id, self.meli_client_secret, self.meli_refresh_token)
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise ValueError("Las credenciales de Mercado Libre están incompletas")
        return MercadoLibreCredentials(
            client_id=self.meli_client_id,
            client_secret=self.meli_client_secret,
            refresh_token=self.meli_refresh_token,
            access_token=self.meli_access_token,
            access_token_expires_at=self.meli_access_token_expires_at,
        )


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if event.get("source") == "aws.events" and event.get("detail-type") == "Scheduled Event":
        return _run_scheduled_monitor()
    if isinstance(event.get("requestContext"), dict):
        return _handle_preview(event)
    return _response(404, {"message": "Recurso no encontrado."})


def _run_scheduled_monitor() -> dict[str, Any]:
    try:
        settings = load_runtime_settings()
        mercado_libre_credentials = settings.mercado_libre_credentials()
        result = run_monitor(
            supabase_url=settings.supabase_url,
            supabase_service_role_key=settings.supabase_service_role_key,
            whatsapp_sidecar_url=settings.whatsapp_sidecar_url,
            waweb_shared_secret=settings.waweb_shared_secret,
            default_whatsapp_chat_id=settings.default_whatsapp_chat_id,
            mercado_libre_credentials=mercado_libre_credentials,
        )
        _persist_mercado_libre_tokens(settings, mercado_libre_credentials)
        logger.warning("scheduled_monitor_finished searches=%s", result["searches"])
        return _response(200, {"message": "Monitor ejecutado."})
    except Exception as error:
        error_id = str(uuid4())
        logger.warning("scheduled_monitor_failed error_id=%s error_type=%s", error_id, type(error).__name__)
        raise


def _handle_preview(event: dict[str, Any]) -> dict[str, Any]:
    try:
        settings = load_runtime_settings()
        if not _has_valid_bearer(event.get("headers"), settings.preview_shared_secret):
            return _response(401, {"message": "No autorizado."})
        payload = _parse_body(event)
        mercado_libre_credentials = settings.mercado_libre_credentials()
        result = search_live(
            LiveSearchRequest.model_validate(payload),
            mercado_libre_credentials=mercado_libre_credentials,
        )
        _persist_mercado_libre_tokens(settings, mercado_libre_credentials)
        return _response(
            200,
            result,
        )
    except (json.JSONDecodeError, ValidationError, ValueError):
        logger.info("lambda_preview_rejected")
        return _response(400, {"message": "Revisá la localidad y los filtros de la búsqueda."})
    except Exception as error:
        error_id = str(uuid4())
        logger.warning("lambda_preview_failed error_id=%s error_type=%s", error_id, type(error).__name__)
        return _response(502, {"message": "No se pudo ejecutar la búsqueda ahora.", "error_id": error_id})


def load_runtime_settings() -> RuntimeSettings:
    secret_arn = os.getenv("RUNTIME_SECRET_ARN", "")
    if not secret_arn:
        raise ValueError("RUNTIME_SECRET_ARN es obligatorio")
    import boto3

    raw_secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
    return RuntimeSettings.model_validate_json(raw_secret)


def _persist_mercado_libre_tokens(settings: RuntimeSettings, credentials: MercadoLibreCredentials | None) -> None:
    if credentials is None or not credentials.token_refreshed:
        return
    secret_arn = os.getenv("RUNTIME_SECRET_ARN", "")
    if not secret_arn:
        raise ValueError("RUNTIME_SECRET_ARN es obligatorio")
    settings.meli_refresh_token = credentials.refresh_token.get_secret_value()
    settings.meli_access_token = credentials.access_token.get_secret_value() if credentials.access_token else None
    settings.meli_access_token_expires_at = credentials.access_token_expires_at
    import boto3

    boto3.client("secretsmanager").put_secret_value(SecretId=secret_arn, SecretString=settings.model_dump_json())


def _has_valid_bearer(headers: Any, expected_secret: str) -> bool:
    if not isinstance(headers, dict):
        return False
    authorization = next((str(value) for key, value in headers.items() if key.lower() == "authorization"), "")
    return authorization.startswith("Bearer ") and hmac.compare_digest(authorization.removeprefix("Bearer "), expected_secret)


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body") or ""
    if not isinstance(body, str):
        raise ValueError("La solicitud no es válida.")
    raw_body = base64.b64decode(body, validate=True) if event.get("isBase64Encoded") else body.encode("utf-8")
    if not raw_body or len(raw_body) > MAX_REQUEST_BYTES:
        raise ValueError("La solicitud no es válida.")
    payload = json.loads(raw_body)
    if not isinstance(payload, dict):
        raise ValueError("La solicitud no es válida.")
    return payload


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False, default=str),
    }
