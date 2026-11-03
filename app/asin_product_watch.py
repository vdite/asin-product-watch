"""ASIN Product Watch.

Monitors ASINs through Amazon's official Creators API and sends durable
Pushover notifications when selected product data changes.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import copy
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_NAME = "ASIN Product Watch"
APP_VERSION = "1.1.0"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
PORT = int(os.environ.get("PORT", "8080"))
ICON_FILE = Path(__file__).with_name("icon.svg")

MAX_CHECK_INTERVAL_MINUTES = 1440
DEFAULT_INTERVAL = min(
    MAX_CHECK_INTERVAL_MINUTES,
    max(5, int(os.environ.get("DEFAULT_CHECK_INTERVAL_MINUTES", "60"))),
)
MAX_WATCHLIST_SIZE = max(10, int(os.environ.get("MAX_WATCHLIST_SIZE", "500")))
MAX_EVENTS = 100
MAX_PENDING = 100
PRODUCT_CONTENT_TTL_SECONDS = 24 * 60 * 60
ASIN_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{10})(?![A-Z0-9])", re.IGNORECASE)

RESOURCES = [
    "images.primary.medium",
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "itemInfo.productInfo",
    "itemInfo.classifications",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.dealDetails",
    "offersV2.listings.isBuyBoxWinner",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.price",
    "offersV2.listings.type",
    "parentASIN",
]

AVAILABILITY_LABELS = {
    "NO_FEATURED_OFFER": "kein Featured Offer in der API",
    "AVAILABLE_DATE": "ab zukünftigem Datum verfügbar",
    "AVAILABLEDATE": "ab zukünftigem Datum verfügbar",
    "IN_STOCK": "auf Lager",
    "INSTOCK": "auf Lager",
    "IN_STOCK_SCARCE": "knapper Bestand",
    "INSTOCKSCARCE": "knapper Bestand",
    "LEAD_TIME": "mit Lieferzeit",
    "LEADTIME": "mit Lieferzeit",
    "OUT_OF_STOCK": "nicht auf Lager",
    "OUTOFSTOCK": "nicht auf Lager",
    "PREORDER": "vorbestellbar",
    "UNAVAILABLE": "nicht verfügbar",
    "UNKNOWN": "unbekannt",
}

CONFIG_DEFAULTS = {
    "asins": [],
    "check_interval_minutes": DEFAULT_INTERVAL,
    "csrf_token": "",
}

STATE_DEFAULTS = {
    "products": {},
    "events": [],
    "pending_notifications": [],
    "last_check_at": None,
    "last_check_status": "Noch keine Prüfung ausgeführt.",
    "check_running": False,
}

storage_lock = threading.RLock()
check_lock = threading.Lock()
check_trigger = threading.Event()


class AppError(RuntimeError):
    """Expected configuration or external-service error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    print(f"{utc_now()} {message}", flush=True)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def load_json(path: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON root is not an object")
        return {**copy.deepcopy(defaults), **value}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return copy.deepcopy(defaults)


def load_config() -> dict[str, Any]:
    with storage_lock:
        config = load_json(CONFIG_FILE, CONFIG_DEFAULTS)
        if not config.get("csrf_token"):
            config["csrf_token"] = secrets.token_urlsafe(32)
            atomic_write_json(CONFIG_FILE, config)
        return config


def save_config(config: dict[str, Any]) -> None:
    with storage_lock:
        atomic_write_json(CONFIG_FILE, config)


def parsed_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def product_content_expired(value: Any, now: datetime | None = None) -> bool:
    parsed = parsed_timestamp(value)
    if parsed is None:
        return True
    reference = now or datetime.now(timezone.utc)
    return (reference - parsed).total_seconds() >= PRODUCT_CONTENT_TTL_SECONDS


def purge_expired_product_content(state: dict[str, Any], now: datetime | None = None) -> bool:
    """Remove Amazon Product Advertising Content once its 24-hour cache window ends."""
    reference = now or datetime.now(timezone.utc)
    changed = False
    products = state.get("products", {})
    if isinstance(products, dict):
        for entry in products.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("snapshot") and product_content_expired(entry.get("last_checked"), reference):
                entry.pop("snapshot", None)
                entry["content_expired"] = True
                entry["last_error"] = (
                    "Produktdaten wurden nach 24 Stunden entfernt; eine neue API-Prüfung steht aus."
                )
                entry["missing_offer_streak"] = 0
                entry["missing_offer_alerted"] = False
                changed = True
            confirmed_at = entry.get("last_confirmed_at")
            if entry.get("last_confirmed_snapshot") and product_content_expired(confirmed_at, reference):
                entry.pop("last_confirmed_snapshot", None)
                entry.pop("last_confirmed_at", None)
                changed = True

    for key, timestamp_key in (("events", "at"), ("pending_notifications", "created_at")):
        values = state.get(key, [])
        if not isinstance(values, list):
            state[key] = []
            changed = True
            continue
        retained = [
            item
            for item in values
            if isinstance(item, dict) and not product_content_expired(item.get(timestamp_key), reference)
        ]
        if len(retained) != len(values):
            state[key] = retained
            changed = True
    return changed


def load_state() -> dict[str, Any]:
    with storage_lock:
        state = load_json(STATE_FILE, STATE_DEFAULTS)
        if purge_expired_product_content(state):
            atomic_write_json(STATE_FILE, state)
        return state


def save_state(state: dict[str, Any]) -> None:
    purge_expired_product_content(state)
    state["events"] = list(state.get("events", []))[-MAX_EVENTS:]
    state["pending_notifications"] = list(state.get("pending_notifications", []))[-MAX_PENDING:]
    with storage_lock:
        atomic_write_json(STATE_FILE, state)


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()
    if not CONFIG_FILE.exists():
        save_config(config)
    if not STATE_FILE.exists():
        save_state(dict(STATE_DEFAULTS))


def extract_asins(raw: str) -> list[str]:
    """Extract unique 10-character ASINs from text or Amazon URLs."""
    found: list[str] = []
    seen: set[str] = set()
    for match in ASIN_PATTERN.findall(raw.upper()):
        if match not in seen:
            found.append(match)
            seen.add(match)
    return found


def export_asin_list(config: dict[str, Any]) -> bytes:
    asins = [str(asin).upper() for asin in config.get("asins", [])]
    text = "\n".join(asins)
    return (text + ("\n" if text else "")).encode("utf-8")


def batches(values: list[str], size: int = 10) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def display_value(container: Any, *path: str) -> Any:
    current = container
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, dict):
        if "displayValue" in current:
            return current.get("displayValue")
        if "displayValues" in current:
            return current.get("displayValues")
    return current


def canonical_data(value: Any) -> Any:
    """Remove presentation-only API metadata before comparing catalog data."""
    if isinstance(value, dict):
        return {
            key: canonical_data(item)
            for key, item in sorted(value.items())
            if key not in {"label", "locale"} and item is not None
        }
    if isinstance(value, list):
        return [canonical_data(item) for item in value]
    return value


def inaccessible_snapshot(asin: str, code: str, message: str) -> dict[str, Any]:
    return {
        "asin": asin,
        "accessible": False,
        "error_code": code,
        "error_message": message,
        "title": None,
        "brand": None,
        "availability_type": "UNAVAILABLE",
        "availability_message": message,
        "price_amount": None,
        "price_currency": None,
        "price_display": None,
        "seller": None,
        "condition": None,
        "image_url": None,
        "parent_asin": None,
        "features": [],
        "product_info": {},
        "classifications": {},
        "deal": {},
        "detail_url": None,
    }


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    asin = str(item.get("asin", "")).upper()
    info = item.get("itemInfo") or {}
    images = item.get("images") or {}
    offers = item.get("offersV2") or {}
    listings = offers.get("listings") or []
    listing = next((entry for entry in listings if entry.get("isBuyBoxWinner") is True), None)
    if listing is None and listings:
        listing = listings[0]
    listing = listing or {}

    availability = listing.get("availability") or {}
    price = listing.get("price") or {}
    money = price.get("money") or {}
    merchant = listing.get("merchantInfo") or {}
    condition = listing.get("condition") or {}
    primary = images.get("primary") or {}
    image = primary.get("medium") or primary.get("large") or primary.get("small") or {}

    availability_type = availability.get("type")
    availability_message = availability.get("message")
    normalized_availability = str(availability_type or "").replace("_", "").upper()
    listing_has_offer_data = money.get("amount") is not None or bool(money.get("displayAmount")) or bool(
        merchant.get("name")
    )
    if not listing:
        availability_type = "NO_FEATURED_OFFER"
        availability_message = "Die Creators API lieferte kein Featured Offer."
    elif normalized_availability == "OUTOFSTOCK" and not listing_has_offer_data:
        availability_type = "NO_FEATURED_OFFER"
        availability_message = (
            "Die Creators API lieferte nur einen Out-of-stock-Platzhalter ohne Preis und Verkäufer."
        )
    elif not availability_type:
        availability_type = "UNKNOWN"

    byline = info.get("byLineInfo") or {}
    brand = display_value(byline, "brand") or display_value(byline, "manufacturer")

    return {
        "asin": asin,
        "accessible": True,
        "error_code": None,
        "error_message": None,
        "title": display_value(info, "title"),
        "brand": brand,
        "availability_type": availability_type,
        "availability_message": availability_message,
        "price_amount": money.get("amount"),
        "price_currency": money.get("currency"),
        "price_display": money.get("displayAmount"),
        "seller": merchant.get("name"),
        "condition": condition.get("value"),
        "image_url": image.get("url"),
        "parent_asin": item.get("parentASIN"),
        "features": display_value(info, "features") or [],
        "product_info": canonical_data(info.get("productInfo") or {}),
        "classifications": canonical_data(info.get("classifications") or {}),
        "deal": canonical_data(listing.get("dealDetails") or {}),
        "detail_url": item.get("detailPageURL"),
    }


def request_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"ASINProductWatch/{APP_VERSION}",
        **(headers or {}),
    }
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:2000]
        try:
            parsed = json.loads(body)
            detail = parsed.get("error_description") or parsed.get("message") or parsed.get("error") or body
        except json.JSONDecodeError:
            detail = body
        raise AppError(f"HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise AppError(f"Netzwerkfehler: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise AppError("Dienst antwortete nicht mit gültigem JSON") from error


@dataclass(frozen=True)
class AmazonSettings:
    credential_id: str
    credential_secret: str
    credential_version: str
    partner_tag: str
    marketplace: str

    @classmethod
    def from_env(cls) -> "AmazonSettings":
        return cls(
            credential_id=os.environ.get("AMAZON_CREDENTIAL_ID", "").strip(),
            credential_secret=os.environ.get("AMAZON_CREDENTIAL_SECRET", "").strip(),
            credential_version=os.environ.get("AMAZON_CREDENTIAL_VERSION", "3.2").strip(),
            partner_tag=os.environ.get("AMAZON_PARTNER_TAG", "").strip(),
            marketplace=os.environ.get("AMAZON_MARKETPLACE", "www.amazon.de").strip(),
        )

    def missing(self) -> list[str]:
        fields = {
            "AMAZON_CREDENTIAL_ID": self.credential_id,
            "AMAZON_CREDENTIAL_SECRET": self.credential_secret,
            "AMAZON_CREDENTIAL_VERSION": self.credential_version,
            "AMAZON_PARTNER_TAG": self.partner_tag,
            "AMAZON_MARKETPLACE": self.marketplace,
        }
        return [name for name, value in fields.items() if not value]

    @property
    def token_url(self) -> str:
        override = os.environ.get("AMAZON_TOKEN_URL", "").strip()
        if override:
            return override
        endpoints = {
            "3.1": "https://api.amazon.com/auth/o2/token",
            "3.2": "https://api.amazon.co.uk/auth/o2/token",
            "3.3": "https://api.amazon.co.jp/auth/o2/token",
        }
        try:
            return endpoints[self.credential_version]
        except KeyError as error:
            raise AppError("AMAZON_CREDENTIAL_VERSION muss 3.1, 3.2 oder 3.3 sein") from error


class AmazonCreatorsClient:
    def __init__(self, settings: AmazonSettings):
        self.settings = settings
        self._access_token = ""
        self._token_valid_until = 0.0
        self._token_lock = threading.Lock()

    def access_token(self) -> str:
        missing = self.settings.missing()
        if missing:
            raise AppError("Fehlende Amazon-Umgebungsvariablen: " + ", ".join(missing))
        with self._token_lock:
            if self._access_token and time.monotonic() < self._token_valid_until:
                return self._access_token
            response = request_json(
                self.settings.token_url,
                {
                    "grant_type": "client_credentials",
                    "client_id": self.settings.credential_id,
                    "client_secret": self.settings.credential_secret,
                    "scope": "creatorsapi::default",
                },
            )
            token = response.get("access_token")
            if not token:
                raise AppError("Amazon lieferte kein OAuth-Zugriffstoken")
            expires_in = max(120, int(response.get("expires_in", 3600)))
            self._access_token = str(token)
            self._token_valid_until = time.monotonic() + expires_in - 60
            return self._access_token

    def get_items(self, asins: list[str]) -> dict[str, dict[str, Any]]:
        if not 1 <= len(asins) <= 10:
            raise ValueError("GetItems accepts between one and ten ASINs")
        response = request_json(
            "https://creatorsapi.amazon/catalog/v1/getItems",
            {
                "itemIds": asins,
                "itemIdType": "ASIN",
                "condition": "New",
                "marketplace": self.settings.marketplace,
                "partnerTag": self.settings.partner_tag,
                "resources": RESOURCES,
            },
            {
                "Authorization": f"Bearer {self.access_token()}",
                "x-marketplace": self.settings.marketplace,
            },
        )

        result_container = response.get("itemsResult") or response.get("itemResults") or {}
        returned = {
            str(item.get("asin", "")).upper(): normalize_item(item)
            for item in result_container.get("items", [])
            if item.get("asin")
        }
        errors_by_asin: dict[str, dict[str, str]] = {}
        for error in response.get("errors", []):
            message = str(error.get("message", "Produkt ist nicht zugänglich"))
            referenced = extract_asins(message)
            for asin in referenced:
                errors_by_asin[asin] = {
                    "code": str(error.get("code", "ItemNotAccessible")),
                    "message": message,
                }

        snapshots: dict[str, dict[str, Any]] = {}
        for asin in asins:
            if asin in returned:
                snapshots[asin] = returned[asin]
            else:
                error = errors_by_asin.get(asin) or {
                    "code": "ItemMissing",
                    "message": "ASIN wurde von GetItems nicht zurückgegeben.",
                }
                snapshots[asin] = inaccessible_snapshot(asin, error["code"], error["message"])
        return snapshots


def format_availability(value: Any) -> str:
    if not value:
        return "–"
    return AVAILABILITY_LABELS.get(str(value).upper(), str(value))


def format_price(snapshot: dict[str, Any]) -> str:
    if snapshot.get("price_display"):
        return str(snapshot["price_display"])
    amount = snapshot.get("price_amount")
    currency = snapshot.get("price_currency")
    if amount is None:
        return "kein Preis"
    return f"{amount} {currency or ''}".strip()


def short_value(value: Any, maximum: int = 90) -> str:
    if value is None or value == "":
        text = "–"
    elif isinstance(value, bool):
        text = "ja" if value else "nein"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def has_no_featured_offer(snapshot: dict[str, Any] | None) -> bool:
    return bool(
        snapshot
        and snapshot.get("accessible")
        and snapshot.get("availability_type") == "NO_FEATURED_OFFER"
    )


def diff_catalog_data(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Compare stable catalog fields without interpreting offer availability."""
    changes: list[str] = []
    if bool(old.get("accessible")) != bool(new.get("accessible")):
        changes.append(
            "Produktkatalog ist wieder über die API erreichbar"
            if new.get("accessible")
            else "Produktkatalog ist nicht mehr über die API erreichbar"
        )
        return changes
    scalar_fields = [
        ("title", "Titel"),
        ("brand", "Marke/Hersteller"),
        ("parent_asin", "Parent-ASIN"),
    ]
    for field, label in scalar_fields:
        if old.get(field) != new.get(field):
            changes.append(f"{label}: {short_value(old.get(field))} → {short_value(new.get(field))}")
    if old.get("image_url") != new.get("image_url"):
        changes.append("Produktbild geändert")
    if old.get("features") != new.get("features"):
        changes.append("Produktmerkmale geändert")
    if old.get("product_info") != new.get("product_info"):
        changes.append("Produktdetails geändert")
    if old.get("classifications") != new.get("classifications"):
        changes.append("Klassifizierung geändert")
    return changes


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    if bool(old.get("accessible")) != bool(new.get("accessible")):
        if new.get("accessible"):
            return [
                "Produkt ist wieder über die API verfügbar",
                f"Verfügbarkeit: {format_availability(new.get('availability_type'))}",
                f"Preis: {format_price(new)}",
            ]
        reason = new.get("error_code") or "UNAVAILABLE"
        return [f"Produkt ist nicht mehr verfügbar bzw. zugänglich ({reason})"]

    if not new.get("accessible"):
        if old.get("error_code") != new.get("error_code"):
            return [f"API-Status: {short_value(old.get('error_code'))} → {short_value(new.get('error_code'))}"]
        return []

    changes: list[str] = []
    scalar_fields = [
        ("title", "Titel", short_value),
        ("brand", "Marke/Hersteller", short_value),
        ("availability_type", "Verfügbarkeit", format_availability),
        ("availability_message", "Verfügbarkeitshinweis", short_value),
        ("seller", "Verkäufer", short_value),
        ("condition", "Zustand", short_value),
        ("parent_asin", "Parent-ASIN", short_value),
    ]
    for field, label, formatter in scalar_fields:
        if old.get(field) != new.get(field):
            changes.append(f"{label}: {formatter(old.get(field))} → {formatter(new.get(field))}")

    old_price = (old.get("price_amount"), old.get("price_currency"))
    new_price = (new.get("price_amount"), new.get("price_currency"))
    if old_price != new_price:
        changes.append(f"Preis: {format_price(old)} → {format_price(new)}")

    if old.get("image_url") != new.get("image_url"):
        changes.append("Produktbild geändert")
    if old.get("features") != new.get("features"):
        changes.append("Produktmerkmale geändert")
    if old.get("product_info") != new.get("product_info"):
        changes.append("Produktdetails geändert")
    if old.get("classifications") != new.get("classifications"):
        changes.append("Klassifizierung geändert")
    if old.get("deal") != new.get("deal"):
        changes.append("Deal-Informationen geändert")
    return changes


@dataclass(frozen=True)
class PushoverSettings:
    app_token: str
    user_key: str

    @classmethod
    def from_env(cls) -> "PushoverSettings":
        return cls(
            app_token=os.environ.get("PUSHOVER_APP_TOKEN", "").strip(),
            user_key=os.environ.get("PUSHOVER_USER_KEY", "").strip(),
        )

    def missing(self) -> list[str]:
        missing = []
        if not self.app_token:
            missing.append("PUSHOVER_APP_TOKEN")
        if not self.user_key:
            missing.append("PUSHOVER_USER_KEY")
        return missing


def send_pushover(title: str, message: str, url: str = "", url_title: str = "Produkt öffnen") -> None:
    settings = PushoverSettings.from_env()
    missing = settings.missing()
    if missing:
        raise AppError("Fehlende Pushover-Umgebungsvariablen: " + ", ".join(missing))
    payload = {
        "token": settings.app_token,
        "user": settings.user_key,
        "title": title[:250],
        "message": message[:1024],
        "priority": "0",
    }
    if url:
        payload["url"] = url[:512]
        payload["url_title"] = url_title[:100]
    request = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"User-Agent": f"ASINProductWatch/{APP_VERSION}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise AppError(f"Pushover antwortete mit HTTP {response.status}")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:1000]
        raise AppError(f"Pushover HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise AppError(f"Pushover-Netzwerkfehler: {error.reason}") from error


def add_event(state: dict[str, Any], kind: str, asin: str, message: str, **extra: Any) -> dict[str, Any]:
    event = {
        "id": secrets.token_hex(8),
        "at": utc_now(),
        "kind": kind,
        "asin": asin,
        "message": message,
        **extra,
    }
    state.setdefault("events", []).append(event)
    state["events"] = state["events"][-MAX_EVENTS:]
    return event


def product_url(asin: str, snapshot: dict[str, Any], settings: AmazonSettings) -> str:
    if snapshot.get("detail_url"):
        return str(snapshot["detail_url"])
    query = urllib.parse.urlencode({"tag": settings.partner_tag}) if settings.partner_tag else ""
    return f"https://{settings.marketplace}/dp/{asin}" + (f"?{query}" if query else "")


def queue_change_notification(
    state: dict[str, Any], asin: str, snapshot: dict[str, Any], changes: list[str], settings: AmazonSettings
) -> None:
    product_name = snapshot.get("title") or asin
    body_lines = [f"ASIN {asin}", *[f"• {change}" for change in changes]]
    body = "\n".join(body_lines)
    event = add_event(
        state,
        "change",
        asin,
        "; ".join(changes),
        title=product_name,
        notified=False,
    )
    digest = hashlib.sha256(f"{asin}|{event['at']}|{body}".encode("utf-8")).hexdigest()[:16]
    state.setdefault("pending_notifications", []).append(
        {
            "id": digest,
            "event_id": event["id"],
            "asin": asin,
            "title": f"ASIN-Änderung: {short_value(product_name, 120)}",
            "message": body,
            "url": product_url(asin, snapshot, settings),
            "created_at": event["at"],
            "attempts": 0,
            "last_error": "",
        }
    )


def deliver_pending(state: dict[str, Any]) -> tuple[int, int]:
    sent = 0
    remaining = []
    events = {event.get("id"): event for event in state.get("events", [])}
    for notification in state.get("pending_notifications", []):
        try:
            send_pushover(
                str(notification.get("title", APP_NAME)),
                str(notification.get("message", "Produktänderung")),
                str(notification.get("url", "")),
            )
            sent += 1
            event = events.get(notification.get("event_id"))
            if event is not None:
                event["notified"] = True
                event["notification_error"] = ""
        except Exception as error:  # durable queue retries on the next check
            notification["attempts"] = int(notification.get("attempts", 0)) + 1
            notification["last_error"] = str(error)
            event = events.get(notification.get("event_id"))
            if event is not None:
                event["notification_error"] = str(error)
            remaining.append(notification)
            log(f"Pushover-Versand fehlgeschlagen: {error}")
    state["pending_notifications"] = remaining[-MAX_PENDING:]
    return sent, len(remaining)


def run_check() -> None:
    if not check_lock.acquire(blocking=False):
        return
    try:
        config = load_config()
        state = load_state()
        state["check_running"] = True
        save_state(state)
        asins = list(config.get("asins", []))
        if not asins:
            state["last_check_at"] = utc_now()
            state["last_check_status"] = "Keine ASINs in der Watchlist."
            state["check_running"] = False
            save_state(state)
            return

        settings = AmazonSettings.from_env()
        missing = settings.missing()
        if missing:
            raise AppError("Fehlende Amazon-Umgebungsvariablen: " + ", ".join(missing))

        client = AmazonCreatorsClient(settings)
        now = utc_now()
        checked = 0
        changed = 0
        batch_errors: list[str] = []

        for batch_number, asin_batch in enumerate(batches(asins)):
            try:
                snapshots = client.get_items(asin_batch)
                for asin in asin_batch:
                    snapshot = snapshots[asin]
                    old_entry = state.setdefault("products", {}).get(asin) or {}
                    old_snapshot = old_entry.get("snapshot")
                    no_featured_offer = has_no_featured_offer(snapshot)
                    entry = {
                        **old_entry,
                        "snapshot": snapshot,
                        "last_checked": now,
                        "last_error": snapshot.get("error_message") if not snapshot.get("accessible") else "",
                    }
                    entry.pop("content_expired", None)
                    changes: list[str] = []
                    if old_snapshot is None:
                        entry["last_changed"] = now
                        add_event(state, "baseline", asin, "Erster Produktstand gespeichert.", title=snapshot.get("title"))
                        if no_featured_offer:
                            entry["missing_offer_streak"] = 1
                            entry["missing_offer_alerted"] = False
                        else:
                            entry["missing_offer_streak"] = 0
                            entry["missing_offer_alerted"] = False
                            entry["last_confirmed_snapshot"] = snapshot
                            entry["last_confirmed_at"] = now
                            unavailable_initially = not snapshot.get("accessible") or snapshot.get(
                                "availability_type"
                            ) in {"OUT_OF_STOCK", "OUTOFSTOCK", "UNAVAILABLE"}
                            if unavailable_initially:
                                reason = (
                                    f"Erstprüfung: Produkt ist nicht verfügbar bzw. zugänglich ({snapshot.get('error_code')})"
                                    if not snapshot.get("accessible")
                                    else f"Erstprüfung: Verfügbarkeit ist {format_availability(snapshot.get('availability_type'))}"
                                )
                                changes.append(reason)
                    elif no_featured_offer:
                        previous_streak = int(old_entry.get("missing_offer_streak", 0))
                        streak = previous_streak + 1 if has_no_featured_offer(old_snapshot) else 1
                        already_alerted = bool(old_entry.get("missing_offer_alerted", False))
                        entry["missing_offer_streak"] = streak
                        entry["missing_offer_alerted"] = already_alerted
                        if not old_entry.get("last_confirmed_snapshot") and not has_no_featured_offer(old_snapshot):
                            entry["last_confirmed_snapshot"] = old_snapshot
                            entry["last_confirmed_at"] = old_entry.get("last_checked")
                        changes.extend(diff_catalog_data(old_snapshot, snapshot))
                        if streak >= 2 and not already_alerted:
                            changes.insert(
                                0,
                                "Creators API liefert seit 2 Prüfungen kein Featured Offer; "
                                "die Amazon-Webseite kann trotzdem verfügbar sein",
                            )
                            entry["missing_offer_alerted"] = True
                    else:
                        previous_streak = int(old_entry.get("missing_offer_streak", 0))
                        was_alerted = bool(old_entry.get("missing_offer_alerted", False))
                        confirmed_snapshot = old_entry.get("last_confirmed_snapshot")
                        entry["missing_offer_streak"] = 0
                        entry["missing_offer_alerted"] = False
                        entry["last_confirmed_snapshot"] = snapshot
                        entry["last_confirmed_at"] = now
                        if was_alerted:
                            changes = [
                                "Featured Offer wird wieder über die Creators API geliefert",
                                f"Verfügbarkeit: {format_availability(snapshot.get('availability_type'))}",
                                f"Preis: {format_price(snapshot)}",
                            ]
                            if confirmed_snapshot:
                                changes.extend(diff_catalog_data(confirmed_snapshot, snapshot))
                        elif previous_streak and confirmed_snapshot:
                            changes = diff_snapshots(confirmed_snapshot, snapshot)
                        elif previous_streak:
                            changes = diff_catalog_data(old_snapshot, snapshot)
                        else:
                            changes = diff_snapshots(old_snapshot, snapshot)
                    if changes:
                        changed += 1
                        entry["last_changed"] = now
                        queue_change_notification(state, asin, snapshot, changes, settings)
                    state["products"][asin] = entry
                    checked += 1
            except Exception as error:
                batch_errors.append(f"{', '.join(asin_batch)}: {error}")
                for asin in asin_batch:
                    old_entry = state.setdefault("products", {}).get(asin) or {}
                    state["products"][asin] = {**old_entry, "last_error": str(error)}
                log(f"Amazon-Prüfung fehlgeschlagen ({', '.join(asin_batch)}): {error}")
            if batch_number < len(batches(asins)) - 1:
                time.sleep(1.05)  # initial API allowance is at most one request per second

        sent, pending = deliver_pending(state)
        state["last_check_at"] = now
        status = f"{checked}/{len(asins)} geprüft, {changed} geändert, {sent} Nachricht(en) gesendet"
        if pending:
            status += f", {pending} ausstehend"
        if batch_errors:
            status += f", {len(batch_errors)} Batch-Fehler"
        state["last_check_status"] = status + "."
        state["check_running"] = False
        save_state(state)
        log(state["last_check_status"])
    except Exception as error:
        state = load_state()
        state["last_check_at"] = utc_now()
        state["last_check_status"] = f"Fehler: {error}"
        state["check_running"] = False
        save_state(state)
        log(f"Prüfung fehlgeschlagen: {error}")
    finally:
        check_lock.release()


def scheduler() -> None:
    time.sleep(2)
    while True:
        run_check()
        config = load_config()
        interval = min(
            MAX_CHECK_INTERVAL_MINUTES,
            max(5, int(config.get("check_interval_minutes", DEFAULT_INTERVAL))),
        )
        check_trigger.wait(timeout=interval * 60)
        check_trigger.clear()


def escaped(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def human_time(value: Any) -> str:
    if not value:
        return "–"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timezone_name = os.environ.get("TZ", "Europe/Berlin").strip() or "Europe/Berlin"
        local = parsed.astimezone(ZoneInfo(timezone_name))
        return local.strftime("%d.%m.%Y %H:%M:%S %Z")
    except (ValueError, ZoneInfoNotFoundError):
        return str(value).replace("T", " ").replace("Z", " UTC")


def render_product_rows(config: dict[str, Any], state: dict[str, Any], settings: AmazonSettings) -> str:
    rows: list[str] = []
    csrf = escaped(config["csrf_token"])
    for asin in config.get("asins", []):
        entry = state.get("products", {}).get(asin) or {}
        snapshot = entry.get("snapshot") or {}
        accessible = snapshot.get("accessible")
        availability = snapshot.get("availability_type")
        if accessible is False or availability in {"OUT_OF_STOCK", "OUTOFSTOCK", "UNAVAILABLE"}:
            badge_class = "bad"
        elif availability in {"IN_STOCK", "INSTOCK"}:
            badge_class = "good"
        elif snapshot:
            badge_class = "warn"
        else:
            badge_class = "neutral"
        status = format_availability(availability) if snapshot else "warte auf Erstprüfung"
        title = snapshot.get("title") or asin
        image = (
            f'<img class="product-image" src="{escaped(snapshot.get("image_url"))}" alt="" loading="lazy">'
            if snapshot.get("image_url")
            else '<div class="product-image placeholder">📦</div>'
        )
        url = product_url(asin, snapshot, settings)
        error = entry.get("last_error")
        error_html = f'<div class="error-line">{escaped(error)}</div>' if error else ""
        rows.append(
            f"""
            <article class="product-card">
              {image}
              <div class="product-main">
                <div class="product-heading">
                  <div><h3>{escaped(title)}</h3><code>{escaped(asin)}</code></div>
                  <span class="badge {badge_class}">{escaped(status)}</span>
                </div>
                <div class="facts">
                  <span><b>Preis</b> {escaped(format_price(snapshot) if snapshot else '–')}</span>
                  <span><b>Verkäufer</b> {escaped(snapshot.get('seller') or '–')}</span>
                  <span><b>Geprüft</b> {escaped(human_time(entry.get('last_checked')))}</span>
                  <span><b>Geändert</b> {escaped(human_time(entry.get('last_changed')))}</span>
                </div>
                {error_html}
                <div class="card-actions">
                  <a href="{escaped(url)}" target="_blank" rel="noreferrer noopener">Bei Amazon öffnen ↗</a>
                  <form method="post" action="/remove" onsubmit="return confirm('ASIN {escaped(asin)} wirklich entfernen?')">
                    <input type="hidden" name="csrf" value="{csrf}">
                    <input type="hidden" name="asin" value="{escaped(asin)}">
                    <button class="link danger" type="submit">Entfernen</button>
                  </form>
                </div>
              </div>
            </article>
            """
        )
    if not rows:
        return '<div class="empty">Noch keine Produkte beobachtet. Füge oben deine erste ASIN hinzu.</div>'
    return "".join(rows)


def render_events(state: dict[str, Any]) -> str:
    entries = []
    for event in reversed(state.get("events", [])[-20:]):
        kind = event.get("kind")
        marker = "Änderung" if kind == "change" else "Startwert"
        notification = ""
        if kind == "change":
            if event.get("notified"):
                notification = '<span class="sent">Pushover gesendet</span>'
            elif event.get("notification_error"):
                notification = f'<span class="failed">Versand ausstehend: {escaped(event["notification_error"])}</span>'
            else:
                notification = '<span class="pending">Versand ausstehend</span>'
        entries.append(
            f"""
            <li>
              <div><b>{escaped(marker)} · {escaped(event.get('asin') or 'System')}</b> {notification}</div>
              <div>{escaped(event.get('message'))}</div>
              <time>{escaped(human_time(event.get('at')))}</time>
            </li>
            """
        )
    return "".join(entries) if entries else '<li class="empty">Noch keine Ereignisse.</li>'


PAGE = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>ASIN Product Watch</title>
  <link rel="icon" type="image/svg+xml" href="/icon.svg">
  <style>
    :root {{ --bg:#f5f6f8;--card:#fff;--text:#17202e;--muted:#687386;--line:#dfe3e8;--accent:#ff9900;--accent2:#232f3e;--good:#137333;--goodbg:#e6f4ea;--bad:#b3261e;--badbg:#fce8e6;--warn:#8a4b00;--warnbg:#fff4df;--shadow:0 8px 28px rgba(35,47,62,.08); }}
    @media(prefers-color-scheme:dark) {{ :root{{--bg:#101318;--card:#191e25;--text:#edf1f7;--muted:#a5afbd;--line:#303742;--accent:#ffad22;--accent2:#e8edf3;--good:#7cdb91;--goodbg:#183421;--bad:#ff918a;--badbg:#3d211f;--warn:#ffc46b;--warnbg:#3a2d18;--shadow:none}} }}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
    header{{background:#232f3e;color:#fff;border-bottom:4px solid var(--accent)}} .header-inner{{max-width:1060px;margin:auto;padding:22px 18px;display:flex;align-items:center;gap:14px}} .logo{{width:44px;height:44px}} h1{{font-size:23px;margin:0}} header p{{margin:2px 0 0;color:#cbd3dc;font-size:13px}}
    main{{max-width:1060px;margin:auto;padding:18px}} .grid{{display:grid;grid-template-columns:1.35fr .65fr;gap:16px}} @media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
    .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px;box-shadow:var(--shadow)}} h2{{font-size:17px;margin:0 0 12px}} h3{{font-size:15px;margin:0 0 2px;line-height:1.35}} .muted{{color:var(--muted)}}
    textarea,input[type=number]{{width:100%;border:1px solid var(--line);border-radius:9px;padding:10px 11px;background:var(--bg);color:var(--text);font:inherit}} textarea{{min-height:94px;resize:vertical}} label span{{display:block;font-size:13px;color:var(--muted);margin-bottom:5px}}
    button,.button{{border:0;border-radius:9px;padding:9px 14px;background:var(--accent2);color:var(--card);font:600 14px/1.3 inherit;cursor:pointer;text-decoration:none;display:inline-block}} header button{{color:#fff}} button.primary{{background:var(--accent);color:#1d2631}} button.secondary,.button.secondary{{background:transparent;color:var(--text);border:1px solid var(--line)}} .actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}} .inline{{display:flex;gap:8px;align-items:end}} .inline label{{flex:1}}
    .flash{{border-radius:10px;padding:11px 14px;margin-bottom:16px;border:1px solid var(--line)}} .flash.ok{{background:var(--goodbg);color:var(--good)}} .flash.error{{background:var(--badbg);color:var(--bad)}}
    .status-list{{display:grid;gap:9px}} .status-row{{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid var(--line);padding-bottom:8px}} .status-row:last-child{{border:0;padding:0}} .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--bad)}} .dot.ok{{background:var(--good)}}
    .product-card{{display:grid;grid-template-columns:92px 1fr;gap:14px;padding:15px 0;border-top:1px solid var(--line)}} .product-card:first-child{{border-top:0;padding-top:2px}} .product-image{{width:92px;height:92px;object-fit:contain;background:#fff;border:1px solid var(--line);border-radius:10px}} .product-image.placeholder{{display:grid;place-items:center;font-size:30px;background:var(--bg)}} .product-heading{{display:flex;justify-content:space-between;gap:12px;align-items:start}} code{{color:var(--muted);font-size:12px}} .badge{{white-space:nowrap;border-radius:999px;padding:4px 8px;font-size:12px;background:var(--line)}} .badge.good{{background:var(--goodbg);color:var(--good)}} .badge.bad{{background:var(--badbg);color:var(--bad)}} .badge.warn{{background:var(--warnbg);color:var(--warn)}}
    .facts{{display:grid;grid-template-columns:repeat(2,minmax(120px,1fr));gap:3px 16px;margin:9px 0;color:var(--muted);font-size:13px}} .facts b{{color:var(--text);font-weight:600}} .card-actions{{display:flex;gap:14px;align-items:center;font-size:13px}} a{{color:#2877c7}} .card-actions form{{margin:0}} button.link{{padding:0;background:none;color:#2877c7;font:inherit}} button.danger{{color:var(--bad)}} .error-line{{font-size:12px;color:var(--bad);margin:5px 0;overflow-wrap:anywhere}}
    .events{{list-style:none;margin:0;padding:0}} .events li{{border-top:1px solid var(--line);padding:10px 0}} .events li:first-child{{border:0;padding-top:0}} .events time{{color:var(--muted);font-size:12px}} .sent{{color:var(--good);font-size:12px;margin-left:7px}} .failed,.pending{{color:var(--warn);font-size:12px;margin-left:7px}} .empty{{padding:18px;text-align:center;color:var(--muted);background:var(--bg);border-radius:10px}} .legal-note{{margin-top:14px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:11px}} footer{{max-width:1060px;margin:auto;padding:4px 18px 28px;color:var(--muted);font-size:11px}}
    @media(max-width:560px){{.product-card{{grid-template-columns:64px 1fr}}.product-image{{width:64px;height:64px}}.product-heading{{display:block}}.badge{{display:inline-block;margin-top:6px}}.facts{{grid-template-columns:1fr}}.inline{{display:block}}.inline button{{margin-top:8px;width:100%}}}}
  </style>
</head>
<body>
<header><div class="header-inner"><img class="logo" src="/icon.svg" alt=""><div><h1>ASIN Product Watch</h1><p>Offizielle Creators API · keine Shop-Seiten werden gescrapt</p></div></div></header>
<main>
  {flash}
  <div class="grid">
    <section>
      <div class="card">
        <h2>ASINs beobachten</h2>
        <form method="post" action="/add">
          <input type="hidden" name="csrf" value="{csrf}">
          <label><span>Eine oder mehrere ASINs oder Amazon-Produktlinks (Leerzeichen, Komma oder neue Zeile)</span><textarea name="asins" placeholder="B0XXXXXXXXX&#10;https://www.amazon.de/dp/B0YYYYYYYYY" required></textarea></label>
          <div class="actions"><button class="primary" type="submit">Zur Watchlist hinzufügen</button><a class="button secondary" href="/export-asins">ASIN-Liste exportieren</a></div>
        </form>
      </div>
      <div class="card">
        <div class="product-heading"><h2>Watchlist · {count} Produkt(e)</h2>{running}</div>
        {products}
        <div class="legal-note">Product prices and availability are accurate as of the date/time indicated and are subject to change. Any price and availability information displayed on {marketplace} at the time of purchase will apply to the purchase of this product.</div>
      </div>
    </section>
    <aside>
      <div class="card">
        <h2>Status</h2>
        <div class="status-list">
          <div class="status-row"><span><i class="dot {amazon_ok}"></i>Amazon API</span><b>{amazon_status}</b></div>
          <div class="status-row"><span><i class="dot {pushover_ok}"></i>Pushover</span><b>{pushover_status}</b></div>
          <div class="status-row"><span>Marketplace</span><b>{marketplace}</b></div>
          <div class="status-row"><span>Letzte Prüfung</span><b>{last_check}</b></div>
        </div>
        <p class="muted">{last_status}</p>
        <form method="post" action="/check-now"><input type="hidden" name="csrf" value="{csrf}"><button class="secondary" type="submit">Jetzt prüfen</button></form>
      </div>
      <div class="card">
        <h2>Prüfintervall</h2>
        <form method="post" action="/settings" class="inline">
          <input type="hidden" name="csrf" value="{csrf}">
          <label><span>Minuten (5 bis 1440)</span><input type="number" name="interval" min="5" max="1440" value="{interval}" required></label>
          <button type="submit">Speichern</button>
        </form>
      </div>
      <div class="card">
        <h2>Pushover testen</h2>
        <p class="muted">Sendet eine Testnachricht an den per Umgebungsvariablen konfigurierten Empfänger.</p>
        <form method="post" action="/test-pushover"><input type="hidden" name="csrf" value="{csrf}"><button class="secondary" type="submit">Testnachricht senden</button></form>
      </div>
    </aside>
  </div>
  <section class="card"><h2>Letzte Ereignisse</h2><ul class="events">{events}</ul></section>
</main>
<footer><p>Version {version} · Zugangsdaten werden ausschließlich aus Container-Umgebungsvariablen gelesen. Die Weboberfläche hat keinen Login und gehört nur ins vertrauenswürdige Heimnetz.</p><p>CERTAIN CONTENT THAT APPEARS IN THIS APPLICATION COMES FROM AMAZON EUROPE CORE S.à r.l. THIS CONTENT IS PROVIDED "AS IS" AND IS SUBJECT TO CHANGE OR REMOVAL AT ANY TIME.</p><p>Amazon and the Amazon logo are trademarks of Amazon.com, Inc. or its affiliates.</p></footer>
</body></html>"""


def render_page(message: str = "", is_error: bool = False) -> str:
    config = load_config()
    state = load_state()
    amazon = AmazonSettings.from_env()
    pushover = PushoverSettings.from_env()
    amazon_missing = amazon.missing()
    pushover_missing = pushover.missing()
    flash = ""
    if message:
        flash = f'<div class="flash {"error" if is_error else "ok"}">{escaped(message)}</div>'
    return PAGE.format(
        flash=flash,
        csrf=escaped(config["csrf_token"]),
        count=len(config.get("asins", [])),
        running='<span class="badge warn">Prüfung läuft …</span>' if state.get("check_running") else "",
        products=render_product_rows(config, state, amazon),
        events=render_events(state),
        amazon_ok="ok" if not amazon_missing else "",
        amazon_status="bereit" if not amazon_missing else f"{len(amazon_missing)} Wert(e) fehlen",
        pushover_ok="ok" if not pushover_missing else "",
        pushover_status="bereit" if not pushover_missing else f"{len(pushover_missing)} Wert(e) fehlen",
        marketplace=escaped(amazon.marketplace or "–"),
        last_check=escaped(human_time(state.get("last_check_at"))),
        last_status=escaped(state.get("last_check_status", "–")),
        interval=escaped(config.get("check_interval_minutes", DEFAULT_INTERVAL)),
        version=APP_VERSION,
    )


class Handler(BaseHTTPRequestHandler):
    server_version = f"ASINProductWatch/{APP_VERSION}"

    def log_message(self, format_string: str, *args: Any) -> None:
        log(f"Web: {format_string % args}")

    def send_bytes(
        self, body: bytes, content_type: str, status: int = 200, extra_headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, message: str = "", error: bool = False) -> None:
        query = urllib.parse.urlencode({"message": message, "error": "1" if error else "0"}) if message else ""
        self.send_response(303)
        self.send_header("Location", "/" + (f"?{query}" if query else ""))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            raise AppError("Formular ist zu groß")
        raw = self.rfile.read(length).decode("utf-8")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def require_csrf(self, form: dict[str, list[str]]) -> None:
        supplied = form.get("csrf", [""])[0]
        expected = load_config().get("csrf_token", "")
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise AppError("Ungültiges Formular-Token. Bitte Seite neu laden.")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            query = urllib.parse.parse_qs(parsed.query)
            message = query.get("message", [""])[0]
            is_error = query.get("error", ["0"])[0] == "1"
            self.send_bytes(render_page(message, is_error).encode("utf-8"), "text/html; charset=utf-8")
        elif parsed.path == "/healthz":
            payload = json.dumps(
                {
                    "status": "ok",
                    "version": APP_VERSION,
                    "amazon_configured": not AmazonSettings.from_env().missing(),
                    "pushover_configured": not PushoverSettings.from_env().missing(),
                }
            ).encode("utf-8")
            self.send_bytes(payload, "application/json; charset=utf-8")
        elif parsed.path == "/icon.svg":
            try:
                self.send_bytes(ICON_FILE.read_bytes(), "image/svg+xml")
            except OSError:
                self.send_error(404)
        elif parsed.path == "/export-asins":
            self.send_bytes(
                export_asin_list(load_config()),
                "text/plain; charset=utf-8",
                extra_headers={"Content-Disposition": 'attachment; filename="asin-product-watch-asins.txt"'},
            )
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            form = self.read_form()
            self.require_csrf(form)
            path = urllib.parse.urlparse(self.path).path
            if path == "/add":
                raw = form.get("asins", [""])[0]
                new_asins = extract_asins(raw)
                if not new_asins:
                    raise AppError("Keine gültige 10-stellige ASIN gefunden.")
                config = load_config()
                existing = list(config.get("asins", []))
                added = [asin for asin in new_asins if asin not in existing]
                if len(existing) + len(added) > MAX_WATCHLIST_SIZE:
                    raise AppError(f"Maximal {MAX_WATCHLIST_SIZE} ASINs sind erlaubt.")
                config["asins"] = existing + added
                save_config(config)
                check_trigger.set()
                self.redirect(f"{len(added)} ASIN(s) hinzugefügt; Prüfung wurde gestartet.")
            elif path == "/remove":
                asin = form.get("asin", [""])[0].upper()
                config = load_config()
                config["asins"] = [value for value in config.get("asins", []) if value != asin]
                save_config(config)
                with check_lock:
                    state = load_state()
                    state.get("products", {}).pop(asin, None)
                    state["pending_notifications"] = [
                        item for item in state.get("pending_notifications", []) if item.get("asin") != asin
                    ]
                    save_state(state)
                self.redirect(f"{asin} wurde entfernt.")
            elif path == "/settings":
                interval = int(form.get("interval", [str(DEFAULT_INTERVAL)])[0])
                if not 5 <= interval <= MAX_CHECK_INTERVAL_MINUTES:
                    raise AppError("Das Intervall muss zwischen 5 und 1440 Minuten liegen.")
                config = load_config()
                config["check_interval_minutes"] = interval
                save_config(config)
                check_trigger.set()
                self.redirect(f"Prüfintervall auf {interval} Minuten gesetzt.")
            elif path == "/check-now":
                check_trigger.set()
                self.redirect("Prüfung wurde gestartet.")
            elif path == "/test-pushover":
                send_pushover(
                    APP_NAME,
                    "Test erfolgreich: Pushover ist korrekt eingerichtet.",
                )
                self.redirect("Pushover-Testnachricht wurde gesendet.")
            else:
                self.send_error(404)
        except (AppError, ValueError) as error:
            self.redirect(f"Fehler: {error}", error=True)
        except Exception as error:
            log(f"Unerwarteter Webfehler: {error}")
            self.redirect("Fehler: Unerwarteter interner Fehler.", error=True)


def main() -> None:
    ensure_storage()
    thread = threading.Thread(target=scheduler, name="product-checker", daemon=True)
    thread.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"{APP_NAME} {APP_VERSION} läuft auf Port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
