from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Portal(StrEnum):
    ARGENPROP = "argenprop"
    ZONAPROP = "zonaprop"
    MERCADOLIBRE = "mercadolibre"
    REMAX = "remax"


PORTAL_HOSTS: dict[Portal, tuple[str, ...]] = {
    Portal.ARGENPROP: ("argenprop.com",),
    Portal.ZONAPROP: ("zonaprop.com.ar",),
    Portal.MERCADOLIBRE: ("mercadolibre.com.ar",),
    Portal.REMAX: ("remax.com.ar", "redremax.com"),
}


def is_allowed_host(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    hostname = hostname.lower().rstrip(".")
    return any(hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts)


def validate_portal_url(value: str, portal: Portal) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("la URL debe usar HTTPS, tener host y no incluir credenciales")
    if not is_allowed_host(parsed.hostname, PORTAL_HOSTS[portal]):
        raise ValueError(f"la URL no pertenece a {portal.value}")
    return value


class SourceURL(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portal: Portal
    url: str = Field(min_length=12, max_length=2048)

    @model_validator(mode="after")
    def validate_portal_destination(self) -> "SourceURL":
        self.url = validate_portal_url(self.url, self.portal)
        return self


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = Field(default="alquiler", pattern=r"^(alquiler|venta)$")
    locations: list[str] = Field(default_factory=list, max_length=30)
    min_price: Decimal | None = Field(default=None, ge=0, le=99_999_999_999)
    max_price: Decimal | None = Field(default=None, ge=0, le=99_999_999_999)
    currency: str = Field(default="ARS", pattern=r"^(ARS|USD)$")
    min_rooms: int | None = Field(default=None, ge=1, le=20)
    max_rooms: int | None = Field(default=None, ge=1, le=20)
    min_m2: Decimal | None = Field(default=None, ge=1, le=10_000)
    max_m2: Decimal | None = Field(default=None, ge=1, le=10_000)
    max_expenses: Decimal | None = Field(default=None, ge=0, le=99_999_999_999)
    pets_allowed: bool | None = None
    parking: bool | None = None
    radius_km: Decimal | None = Field(default=None, gt=0, le=100)
    center_lat: Decimal | None = Field(default=None, ge=-90, le=90)
    center_lng: Decimal | None = Field(default=None, ge=-180, le=180)
    polygon_geojson: dict[str, Any] | None = None

    @field_validator("locations")
    @classmethod
    def clean_locations(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            value = value.strip()
            if not value or len(value) > 100:
                raise ValueError("cada ubicacion debe tener entre 1 y 100 caracteres")
            cleaned.append(value)
        return cleaned

    @field_validator("polygon_geojson")
    @classmethod
    def validate_polygon(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        geometry_type = value.get("type") if isinstance(value, dict) else None
        coordinates = value.get("coordinates") if isinstance(value, dict) else None
        polygons = [coordinates] if geometry_type == "Polygon" else coordinates if geometry_type == "MultiPolygon" else None
        if not isinstance(polygons, list) or len(polygons) > 10:
            raise ValueError("la zona debe ser un polígono GeoJSON válido")
        points = 0
        for polygon in polygons:
            if not isinstance(polygon, list) or not polygon:
                raise ValueError("la zona debe ser un polígono GeoJSON válido")
            for ring in polygon:
                if not isinstance(ring, list) or len(ring) < 4:
                    raise ValueError("la zona debe tener al menos tres vértices")
                points += len(ring)
                for point in ring:
                    if not isinstance(point, list) or len(point) < 2 or not isinstance(point[0], (int, float)) or not isinstance(point[1], (int, float)) or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90:
                        raise ValueError("la zona tiene coordenadas inválidas")
        if points > 300:
            raise ValueError("la zona tiene demasiados vértices")
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> "SearchFilters":
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price no puede superar max_price")
        if self.min_rooms is not None and self.max_rooms is not None and self.min_rooms > self.max_rooms:
            raise ValueError("min_rooms no puede superar max_rooms")
        if self.min_m2 is not None and self.max_m2 is not None and self.min_m2 > self.max_m2:
            raise ValueError("min_m2 no puede superar max_m2")
        if (self.center_lat is None) != (self.center_lng is None):
            raise ValueError("center_lat y center_lng deben indicarse juntos")
        if self.radius_km is not None and self.center_lat is None:
            raise ValueError("un radio requiere coordenadas de centro")
        return self


class SavedSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    filters: SearchFilters
    source_urls: list[SourceURL] = Field(min_length=1, max_length=12)
    notify_chat_id: str | None = Field(default=None, pattern=r"^\d{8,20}$")
    active: bool = True
    baseline_completed: bool = False

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        return value.strip()


class Listing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source: Portal
    source_url: str = Field(min_length=12, max_length=2048)
    url: str = Field(min_length=12, max_length=2048)
    title: str = Field(min_length=1, max_length=500)
    address: str | None = Field(default=None, max_length=500)
    price: Decimal | None = Field(default=None, ge=0, le=99_999_999_999)
    currency: str | None = Field(default=None, pattern=r"^(ARS|USD)$")
    expenses: Decimal | None = Field(default=None, ge=0, le=99_999_999_999)
    rooms: Decimal | None = Field(default=None, ge=0, le=30)
    bedrooms: Decimal | None = Field(default=None, ge=0, le=30)
    covered_m2: Decimal | None = Field(default=None, ge=0, le=10_000)
    total_m2: Decimal | None = Field(default=None, ge=0, le=10_000)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    image_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_destination(self) -> "Listing":
        self.url = validate_portal_url(self.url, self.source)
        self.source_url = validate_portal_url(self.source_url, self.source)
        return self

    @property
    def fingerprint(self) -> str:
        normalized_address = normalize_text(self.address or self.title)
        size = int(self.covered_m2 or self.total_m2 or 0)
        rooms = str(self.rooms or "")
        return hashlib.sha256(f"{normalized_address}|{size}|{rooms}".encode("utf-8")).hexdigest()

    @property
    def source_key(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value)
