from __future__ import annotations

import json
import logging
import math
from logging.handlers import RotatingFileHandler
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import os
from dotenv import load_dotenv

# Charge les variables du fichier .env dans l'environnement système
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")
 

# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_URL = "https://gpscj.net"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

EMAIL = os.getenv("EMAIL","")


DEVICE_IMEI = os.getenv("DEVICE_IMEI", "")
DEVICE_PASSWORD = os.getenv("DEVICE_PASSWORD", "")

OUTPUT_DIR = Path(os.getenv("GPS_OUTPUT_DIR", str(PROJECT_DIR / "gps_output1")))

HTTP_TIMEOUT = (5, 15)  # Separate connection and response timeouts.
GEOCODING_TIMEOUT = 10

# Nominatim asks clients to identify themselves.
APPLICATION_NAME = "GPSCJClient/2.0"


# =============================================================================
# LOGGING
# =============================================================================

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = OUTPUT_DIR / "gps_client.log"


def configure_logging() -> logging.Logger:
    """
    Configure application logging.
    """
    logger = logging.getLogger("gpscj")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


LOGGER = configure_logging()


# =============================================================================
# ENUMS
# =============================================================================

class PowerSource(IntEnum):
    INTERNAL = 0
    EXTERNAL_STABLE = 1
    EXTERNAL_LOW = 2
    CHARGING = 3


class PowerMode(IntEnum):
    ACTIVE = 1
    STANDBY = 4


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class DataContext:
    """
    Decoded GPSCJ dataContext protocol.

    Protocol definition supplied for this project:

        0 = Protocol Version
        1 = RSSI
        2 = Satellites
        3 = HDOP
        4 = Voltage
        5 = ACC
        6 = Relay
        7 = Power Source
        8 = Power Mode
    """

    protocol_version: int
    rssi: int
    satellites: int
    hdop: float
    voltage: float
    acc: int
    relay: int
    power_source: int
    power_mode: int

    raw: str

    @property
    def acc_on(self) -> bool:
        return self.acc == 1

    @property
    def relay_cutoff(self) -> bool:
        return self.relay == 1

    @property
    def power_source_name(self) -> str:
        names = {
            0: "Internal",
            1: "External Stable",
            2: "External Low",
            3: "Charging",
        }
        return names.get(self.power_source, f"Unknown ({self.power_source})")

    @property
    def power_mode_name(self) -> str:
        names = {
            1: "Active",
            4: "Standby",
        }
        return names.get(self.power_mode, f"Unknown ({self.power_mode})")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)

        data["acc_on"] = self.acc_on
        data["relay_cutoff"] = self.relay_cutoff
        data["power_source_name"] = self.power_source_name
        data["power_mode_name"] = self.power_mode_name

        return data


@dataclass
class H02Status:
    """
    Status response obtained from the device through SMS/H02.

    Example:

        BAT:9,GPRS:1,GSM:5,GPS:1,ACC:0,oil:1,Power:1,S:0
    """

    battery: Optional[int] = None
    gprs: Optional[int] = None
    gsm: Optional[int] = None
    gps: Optional[int] = None
    acc: Optional[int] = None
    oil: Optional[int] = None
    power: Optional[int] = None
    state: Optional[int] = None

    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrackingData:
    """
    Normalized GPSCJ GetTracking response.
    """

    location_id: Optional[int]
    device_utc_date: Optional[str]
    server_utc_date: Optional[str]

    latitude: Optional[float]
    longitude: Optional[float]

    baidu_lat: Optional[float]
    baidu_lng: Optional[float]

    original_lat: Optional[float]
    original_lng: Optional[float]

    speed: Optional[float]
    course: Optional[float]

    is_stop: Optional[bool]
    data_type: Optional[int]

    data_context_raw: Optional[str]
    distance: Optional[float]

    status: Optional[str]
    stop_time_minute: Optional[int]

    # Legacy attribute name retained: API field ofl is a duration in minutes,
    # used by the platform when status == "Offline", not a connection flag.
    offline: Optional[int]
    gzms: Optional[str]

    address: Optional[str] = None

    data_context: Optional[DataContext] = None

    raw: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)

        if self.data_context:
            result["data_context"] = self.data_context.to_dict()

        return result


@dataclass
class DeviceInfo:
    """
    Device information discovered from Monitor.aspx.
    """

    device_id: Optional[int]
    user_id: Optional[int]
    timezone: Optional[str]

    guid: Optional[str] = None

    raw_monitor_html_size: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplicationResult:
    """
    Complete application result.
    """

    collected_at: str

    device: dict[str, Any]

    tracking: Optional[dict[str, Any]]

    h02_status: Optional[dict[str, Any]]

    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =============================================================================
# HTTP TRANSPORT
# =============================================================================

class HttpTransport:
    """
    Responsible only for HTTP communication.

    It does not know anything about GPSCJ business logic.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float | tuple[float, float] = HTTP_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.session = self._create_session()

    @staticmethod
    def _create_session() -> Session:
        session = requests.Session()

        retry = Retry(
            total=1,
            connect=0,
            read=0,
            status=1,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(
                ["GET", "POST"]
            ),
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=10,
            pool_maxsize=10,
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def headers(
        self,
        referer: Optional[str] = None,
        ajax: bool = False,
    ) -> dict[str, str]:

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }

        if ajax:
            headers.update(
                {
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/json",
                }
            )
        else:
            headers["Accept"] = (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "*/*;q=0.8"
            )

        if referer:
            headers["Referer"] = referer
            headers["Origin"] = self.base_url

        return headers

    def get(
        self,
        path_or_url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Response:

        url = self._make_url(path_or_url)

        LOGGER.info("GET %s", url)

        response = self.session.get(
            url,
            headers=headers,
            params=params,
            timeout=self.timeout,
        )

        LOGGER.info(
            "HTTP %s | %d bytes",
            response.status_code,
            len(response.content),
        )

        return response

    def post(
        self,
        path_or_url: str,
        *,
        data: Any = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Response:

        url = self._make_url(path_or_url)

        LOGGER.info("POST %s", url)

        response = self.session.post(
            url,
            data=data,
            headers=headers,
            timeout=self.timeout,
        )

        LOGGER.info(
            "HTTP %s | %d bytes",
            response.status_code,
            len(response.content),
        )

        return response

    def _make_url(self, value: str) -> str:
        if value.startswith("http://"):
            return value

        if value.startswith("https://"):
            return value

        return f"{self.base_url}/{value.lstrip('/')}"


# =============================================================================
# HTML PARSING
# =============================================================================

class HtmlParser:
    """
    Responsible only for extracting values from GPSCJ HTML.
    """

    @staticmethod
    def extract_input_value(
        html: str,
        element_id: str,
    ) -> Optional[str]:

        patterns = [
            rf'id="{re.escape(element_id)}"\s+value="([^"]*)"',
            rf'value="([^"]*)"\s+id="{re.escape(element_id)}"',
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                html,
                re.IGNORECASE,
            )

            if match:
                return match.group(1)

        return None

    @classmethod
    def extract_security_tokens(
        cls,
        html: str,
    ) -> dict[str, str]:

        names = [
            "__VIEWSTATE",
            "__VIEWSTATEGENERATOR",
            "__EVENTVALIDATION",
        ]

        return {
            name: cls.extract_input_value(html, name) or ""
            for name in names
        }

    @classmethod
    def extract_device_info(
        cls,
        html: str,
    ) -> DeviceInfo:

        device_id_raw = cls.extract_input_value(
            html,
            "hidDeviceID",
        )

        user_id_raw = cls.extract_input_value(
            html,
            "hidUserID",
        )

        timezone = cls.extract_input_value(
            html,
            "hidTimeZone",
        )

        guid = cls.extract_input_value(
            html,
            "hidYiwenGUID",
        )

        device_id = safe_int(device_id_raw)
        user_id = safe_int(user_id_raw)

        return DeviceInfo(
            device_id=device_id,
            user_id=user_id,
            timezone=timezone,
            guid=guid,
            raw_monitor_html_size=len(html),
        )


# =============================================================================
# GPSCJ AUTHENTICATION SERVICE
# =============================================================================

class AuthenticationService:
    """
    Responsible only for GPSCJ authentication.
    """

    def __init__(
        self,
        transport: HttpTransport,
        imei: str,
        password: str,
    ):
        self.transport = transport
        self.imei = imei
        self.password = password

    def authenticate(self) -> bool:
        LOGGER.info("Starting authentication...")

        login_url = "/logincj.aspx?language=en-us"

        response = self.transport.get(
            login_url,
            headers=self.transport.headers(),
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Login page failed: HTTP {response.status_code}"
            )

        tokens = HtmlParser.extract_security_tokens(
            response.text
        )

        if not tokens["__VIEWSTATE"]:
            raise RuntimeError(
                "Could not extract __VIEWSTATE."
            )

        guid = self._get_guid()

        if not guid:
            raise RuntimeError(
                "Could not determine hidYiwenGUID."
            )

        timezone = self._get_local_timezone()

        payload = {
            "__VIEWSTATE": tokens["__VIEWSTATE"],
            "__VIEWSTATEGENERATOR": tokens[
                "__VIEWSTATEGENERATOR"
            ],
            "__EVENTVALIDATION": tokens[
                "__EVENTVALIDATION"
            ],
            "hidGMT": timezone,
            "hidYiwenGUID2": guid,
            "txtImeiNo": self.imei,
            "txtImeiPassword": self.password,
            "btnLoginImei": "",
        }

        headers = self.transport.headers(
            referer=f"{BASE_URL}/",
        )

        headers["Content-Type"] = (
            "application/x-www-form-urlencoded; charset=UTF-8"
        )

        response = self.transport.post(
            login_url,
            data=payload,
            headers=headers,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Authentication failed: "
                f"HTTP {response.status_code}"
            )

        monitor = self.transport.get(
            "/Monitor.aspx",
            headers=self.transport.headers(
                referer=f"{BASE_URL}/",
            ),
        )

        if monitor.status_code != 200:
            raise RuntimeError(
                "Authentication could not be verified "
                "through Monitor.aspx."
            )

        LOGGER.info("Authentication successful.")

        return True

    def _get_guid(self) -> Optional[str]:
        response = self.transport.get(
            "/",
            headers=self.transport.headers(),
        )

        if response.status_code != 200:
            return None

        return HtmlParser.extract_input_value(
            response.text,
            "hidYiwenGUID",
        )

    @staticmethod
    def _get_local_timezone() -> str:
        offset = (
            datetime.now()
            .astimezone()
            .utcoffset()
        )

        if offset is None:
            return "0"

        hours = offset.total_seconds() / 3600

        if hours.is_integer():
            return str(int(hours))

        return str(hours).rstrip("0").rstrip(".")


# =============================================================================
# DEVICE DISCOVERY SERVICE
# =============================================================================

class DeviceDiscoveryService:
    """
    Discovers DeviceID, UserID and TimeZone.
    """

    def __init__(
        self,
        transport: HttpTransport,
    ):
        self.transport = transport

    def discover(self) -> DeviceInfo:
        LOGGER.info(
            "Inspecting Monitor.aspx for device information..."
        )

        response = self.transport.get(
            "/Monitor.aspx",
            headers=self.transport.headers(
                referer=f"{BASE_URL}/",
            ),
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Monitor.aspx failed: "
                f"HTTP {response.status_code}"
            )

        LOGGER.info(
            "Monitor final URL: %s",
            response.url,
        )

        LOGGER.info(
            "Monitor response size: %d bytes",
            len(response.content),
        )

        device = HtmlParser.extract_device_info(
            response.text
        )

        if device.device_id is None:
            raise RuntimeError(
                "Could not determine DeviceID."
            )

        LOGGER.info(
            "DeviceID = %s",
            device.device_id,
        )

        if device.user_id is not None:
            LOGGER.info(
                "USER_ID = %s",
                device.user_id,
            )

        LOGGER.info(
            "TimeZone = %s",
            device.timezone,
        )

        return device


# =============================================================================
# DATACONTEXT DECODER
# =============================================================================

class DataContextDecoder:
    """
    Decoder for dataContext protocol version 1.

    Definition supplied with the project:

        index 0 = Protocol Version
        index 1 = RSSI
        index 2 = Satellites
        index 3 = HDOP
        index 4 = Voltage
        index 5 = ACC
        index 6 = Relay
        index 7 = Power Source
        index 8 = Power Mode
    """

    EXPECTED_FIELDS = 9

    def decode(
        self,
        raw: str,
    ) -> DataContext:

        if not raw:
            raise ValueError(
                "dataContext is empty."
            )

        parts = [
            item.strip()
            for item in raw.split("-")
        ]

        if len(parts) != self.EXPECTED_FIELDS:
            raise ValueError(
                "Unsupported dataContext structure: "
                f"expected {self.EXPECTED_FIELDS} "
                f"fields, received {len(parts)}. "
                f"Raw={raw!r}"
            )

        protocol_version = safe_int_required(parts[0])

        if protocol_version != 1:
            raise ValueError(
                f"Unsupported dataContext protocol version: "
                f"{protocol_version}"
            )

        rssi = safe_int_required(parts[1])

        if not 0 <= rssi <= 31:
            raise ValueError(
                f"RSSI outside expected range 0..31: {rssi}"
            )

        satellites = safe_int_required(parts[2])

        if satellites < 0:
            raise ValueError(
                f"Invalid satellite count: {satellites}"
            )

        hdop = safe_float_required(parts[3])

        voltage = safe_float_required(parts[4])

        acc = safe_int_required(parts[5])

        if acc not in (0, 1):
            raise ValueError(
                f"Invalid ACC value: {acc}"
            )

        relay = safe_int_required(parts[6])

        if relay not in (0, 1):
            raise ValueError(
                f"Invalid relay value: {relay}"
            )

        power_source = safe_int_required(parts[7])

        # Firmware can introduce additional enum values. Preserve them without
        # discarding otherwise valid hardware readings or guessing their meaning.
        if power_source < 0:
            raise ValueError(
                f"Invalid power source: {power_source}"
            )

        power_mode = safe_int_required(parts[8])

        if power_mode < 0:
            raise ValueError(
                f"Invalid power mode: {power_mode}"
            )

        return DataContext(
            protocol_version=protocol_version,
            rssi=rssi,
            satellites=satellites,
            hdop=hdop,
            voltage=voltage,
            acc=acc,
            relay=relay,
            power_source=power_source,
            power_mode=power_mode,
            raw=raw,
        )


# =============================================================================
# H02 STATUS PARSER
# =============================================================================

class H02StatusParser:
    """
    Parses H02 SMS status messages.

    Example:

        BAT:9,GPRS:1,GSM:5,GPS:1,ACC:0,oil:1,Power:1,S:0
    """

    FIELD_MAP = {
        "BAT": "battery",
        "GPRS": "gprs",
        "GSM": "gsm",
        "GPS": "gps",
        "ACC": "acc",
        "AAC": "acc",  # tolerate observed typo
        "oil": "oil",
        "Power": "power",
        "S": "state",
    }

    def parse(
        self,
        raw: str,
    ) -> H02Status:

        result = H02Status(raw=raw)

        for part in raw.split(","):

            if ":" not in part:
                continue

            key, value = part.split(
                ":",
                1,
            )

            key = key.strip()
            value = value.strip()

            attribute = self.FIELD_MAP.get(key)

            if not attribute:
                continue

            setattr(
                result,
                attribute,
                safe_int(value),
            )

        return result


# =============================================================================
# TRACKING RESPONSE PARSER
# =============================================================================

class TrackingResponseParser:
    """
    Converts GPSCJ GetTracking response into TrackingData.
    """

    def __init__(
        self,
        data_context_decoder: DataContextDecoder,
    ):
        self.data_context_decoder = data_context_decoder

    def parse(
        self,
        response: Response,
    ) -> TrackingData:

        response.raise_for_status()

        outer = response.json()

        payload = decode_payload(outer)

        if not isinstance(payload, dict):
            raise ValueError(
                "Unexpected GetTracking response structure."
            )

        data_context_raw = payload.get(
            "dataContext"
        )

        decoded_context = None

        if data_context_raw:
            try:
                decoded_context = (
                    self.data_context_decoder.decode(
                        str(data_context_raw)
                    )
                )
            except ValueError as exc:
                LOGGER.warning(
                    "dataContext decoding failed: %s",
                    exc,
                )

        latitude = safe_float(
            payload.get("latitude")
        )

        longitude = safe_float(
            payload.get("longitude")
        )

        if not valid_coordinates(latitude, longitude):
            latitude = longitude = None

        return TrackingData(
            location_id=safe_int(
                payload.get("locationID")
            ),

            device_utc_date=payload.get(
                "deviceUtcDate"
            ),

            server_utc_date=payload.get(
                "serverUtcDate"
            ),

            latitude=latitude,
            longitude=longitude,

            baidu_lat=safe_float(
                payload.get("baiduLat")
            ),

            baidu_lng=safe_float(
                payload.get("baiduLng")
            ),

            original_lat=safe_float(
                payload.get("oLat")
            ),

            original_lng=safe_float(
                payload.get("oLng")
            ),

            speed=safe_float(
                payload.get("speed")
            ),

            course=safe_float(
                payload.get("course")
            ),

            is_stop=safe_bool(payload.get("isStop")),

            data_type=safe_int(
                payload.get("dataType")
            ),

            data_context_raw=(
                str(data_context_raw)
                if data_context_raw is not None
                else None
            ),

            distance=safe_float(
                payload.get("distance")
            ),

            status=payload.get("status"),

            stop_time_minute=safe_int(
                payload.get("stopTimeMinute")
            ),

            offline=safe_int(
                payload.get("ofl")
            ),

            gzms=payload.get("gzms"),

            data_context=decoded_context,

            raw=payload,
        )


# =============================================================================
# TRACKING SERVICE
# =============================================================================

class TrackingService:
    """
    Responsible for calling GetTracking.
    """

    def __init__(
        self,
        transport: HttpTransport,
        parser: TrackingResponseParser,
    ):
        self.transport = transport
        self.parser = parser

    def get_tracking(
        self,
        device_id: int,
        timezone: str,
    ) -> TrackingData:

        payload = {
            "DeviceID": device_id,
            "TimeZone": timezone,
        }

        response = self.transport.post(
            "/Ajax/DevicesAjax.asmx/GetTracking",
            data=json.dumps(payload),
            headers=self.transport.headers(
                referer=f"{BASE_URL}/Monitor.aspx",
                ajax=True,
            ),
        )

        return self.parser.parse(response)


@dataclass
class TrackPoint:
    """
    Single point in a historical movement record.
    """

    latitude: Optional[float]
    longitude: Optional[float]
    speed: Optional[float] = None
    timestamp: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =============================================================================
# TRACKING HISTORY SERVICE
# =============================================================================

class HistoryError(RuntimeError):
    """A history failure with a fixed, safe message suitable for the bot."""


class TrackingHistoryService:
    """Read the paginated endpoint used by GPSCJ's JS/Playback.js."""

    ENDPOINT = "/Ajax/DevicesAjax.asmx/GetDevicesHistory"
    MAX_PAGES = 100

    LAT_KEYS = ("lat", "latitude", "Lat", "Latitude", "wgsLat", "oLat", "baiduLat")
    LNG_KEYS = ("lng", "lon", "longitude", "Lng", "Lon", "Longitude", "wgsLng", "oLng", "baiduLng")
    SPEED_KEYS = ("speed", "Speed", "sp")
    TIME_KEYS = ("time", "Time", "t", "gpstime", "deviceUtcDate", "serverUtcDate")

    def __init__(
        self,
        transport: HttpTransport,
        context_decoder: DataContextDecoder,
    ):
        self.transport = transport
        self.context_decoder = context_decoder

    def get_history(
        self,
        device_id: int,
        timezone: str,
        start_time: str,
        end_time: str,
    ) -> list[TrackPoint]:

        start = datetime.fromisoformat(start_time)
        end = datetime.fromisoformat(end_time)
        if start >= end:
            raise ValueError("History start must precede end.")

        cursor = start_time
        last_location_id = None
        points: list[TrackPoint] = []
        seen = set()

        for _ in range(self.MAX_PAGES):
            payload = {
                "DeviceID": device_id,
                "Start": cursor,
                "End": end_time,
                "TimeZone": timezone,
                "ShowLBS": 2,
            }
            try:
                response = self.transport.post(
                    self.ENDPOINT,
                    data=json.dumps(payload),
                    headers=self.transport.headers(
                        referer=f"{BASE_URL}/Playback.aspx", ajax=True,
                    ),
                )
                if response.status_code in (401, 403):
                    raise HistoryError("رفض الخادم الوصول إلى سجل الحركة. أعد المحاولة وتحقق من صلاحية الحساب للسجل.")
                response.raise_for_status()
                page = decode_payload(response.json())
                if not isinstance(page, dict):
                    raise ValueError("Unexpected history page.")
                state = safe_int(page.get("state"))
                if state == -9:
                    raise HistoryError("بلغت حد طلبات سجل الحركة لدى المنصة. يرجى المحاولة لاحقاً.")
                if state != 0:
                    raise HistoryError("لم يوافق الخادم على طلب سجل الحركة. تحقق من إتاحة السجل في المنصة.")
                items = page.get("devices")
                if not isinstance(items, list):
                    raise ValueError("Missing history devices array.")
                if not items:
                    return points
                page_id = safe_int(page.get("lastLocationID"))
                if page_id is None:
                    raise ValueError("Missing history cursor ID.")
                if page_id == last_location_id:
                    return points
                for point in self._parse(items):
                    key = (point.timestamp, point.latitude, point.longitude, point.speed)
                    if key not in seen:
                        seen.add(key)
                        points.append(point)
                next_cursor = page.get("lastDeviceUtcDate")
                next_time = datetime.fromisoformat(next_cursor)
                if next_time >= end:
                    return points
                if next_time <= datetime.fromisoformat(cursor):
                    raise HistoryError("تعذّر استكمال صفحات سجل الحركة. أعد المحاولة لاحقاً.")
                cursor = next_cursor
                last_location_id = page_id
            except HistoryError:
                raise
            except requests.RequestException as exc:
                raise HistoryError("تعذّر الاتصال بخادم سجل الحركة. يرجى إعادة المحاولة.") from exc
            except (ValueError, TypeError) as exc:
                raise HistoryError("أعاد الخادم سجل حركة بصيغة غير متوقعة. يرجى إعادة المحاولة.") from exc

        raise HistoryError("سجل الحركة أكبر من حد التحميل الحالي؛ لم يكتمل تحميله.")

    def _parse(self, outer: Any) -> list[TrackPoint]:
        payload = decode_payload(outer, wrappers=("d", "data", "points", "devices"))

        if not isinstance(payload, list):
            raise ValueError("Unexpected movement history response structure.")

        points: list[TrackPoint] = []

        for item in payload:
            if not isinstance(item, dict):
                continue

            lat = self._first(item, self.LAT_KEYS)
            lng = self._first(item, self.LNG_KEYS)

            lat = safe_float(lat)
            lng = safe_float(lng)

            if not valid_coordinates(lat, lng):
                continue

            points.append(
                TrackPoint(
                    latitude=lat,
                    longitude=lng,
                    speed=safe_float(
                        self._first(item, self.SPEED_KEYS)
                    ),
                    timestamp=str(
                        self._first(item, self.TIME_KEYS) or ""
                    ),
                )
            )

        return points

    @staticmethod
    def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in item and item[key] not in (None, ""):
                return item[key]
        return None


# =============================================================================
# REVERSE GEOCODING SERVICE
# =============================================================================

class GeocodingService:
    """
    Responsible only for converting coordinates to an address.
    """

    def __init__(
        self,
        transport: HttpTransport,
        email: str,
    ):
        self.transport = transport
        self.email = email

    def reverse(
        self,
        latitude: float,
        longitude: float,
    ) -> Optional[str]:

        if latitude is None or longitude is None:
            return None

        user_agent = (
            f"{APPLICATION_NAME} "
            f"(contact: {self.email})"
        )

        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }

        params = {
            "format": "json",
            "lat": latitude,
            "lon": longitude,
            "zoom": 18,
            "addressdetails": 1,
        }

        # Respectful spacing between Nominatim calls.
        time.sleep(1)

        try:
            response = self.transport.get(
                NOMINATIM_URL,
                headers=headers,
                params=params,
            )

            response.raise_for_status()

            data = response.json()

            return data.get("display_name")

        except requests.RequestException as exc:
            LOGGER.warning(
                "Reverse geocoding failed: %s",
                exc,
            )

            return None


# =============================================================================
# DIRECTION SERVICE
# =============================================================================

class DirectionService:
    """
    Converts course degrees into a cardinal direction.
    """

    DIRECTIONS = (
        "N",
        "NE",
        "E",
        "SE",
        "S",
        "SW",
        "W",
        "NW",
    )

    @classmethod
    def from_course(
        cls,
        course: Optional[float],
    ) -> str:

        if course is None:
            return "N/A"

        normalized = float(course) % 360

        index = int(
            (normalized + 22.5) // 45
        ) % 8

        return cls.DIRECTIONS[index]


# =============================================================================
# REPORTING
# =============================================================================

class Reporter:
    """
    Responsible only for console and JSON reports.
    """

    def __init__(
        self,
        output_dir: Path,
    ):
        self.output_dir = output_dir

    def print_summary(
        self,
        device: DeviceInfo,
        tracking: TrackingData,
    ):

        context = tracking.data_context

        direction = DirectionService.from_course(
            tracking.course
        )

        print()
        print("=" * 80)
        print("GPS TRACKING SUMMARY")
        print("=" * 80)

        print(
            f"Device ID  : {device.device_id}"
        )

        print(
            f"User ID    : {device.user_id}"
        )

        print(
            f"IMEI       : {DEVICE_IMEI}"
        )

        print(
            f"Time Zone  : {device.timezone}"
        )

        print(
            f"Location   : "
            f"{tracking.latitude}, "
            f"{tracking.longitude}"
        )

        print(
            f"Address    : "
            f"{tracking.address or 'N/A'}"
        )

        print(
            f"Speed      : "
            f"{tracking.speed} km/h"
        )

        print(
            f"Course     : "
            f"{tracking.course}°"
        )

        print(
            f"Direction  : "
            f"{direction}"
        )

        print(
            f"Status     : "
            f"{tracking.status}"
        )

        print(
            f"Stopped    : "
            f"{tracking.is_stop}"
        )

        print(
            f"Stop time  : "
            f"{tracking.stop_time_minute} min"
        )

        print(
            f"Device time: "
            f"{tracking.device_utc_date}"
        )

        print(
            f"Server time: "
            f"{tracking.server_utc_date}"
        )

        distance_text = (
            f"{tracking.distance / 1000:.2f} km"
            f" ({tracking.distance:,.0f} m)"
            if tracking.distance is not None and tracking.distance >= 0
            else "N/A"
        )
        print(f"Distance   : {distance_text}")

        print(
            f"Data type  : "
            f"{tracking.data_type}"
        )

        print(
            f"Data ctx   : "
            f"{tracking.data_context_raw}"
        )

        print(
            f"ofl        : "
            f"{tracking.offline} (minutes; interpreted only when status is Offline)"
        )

        if (
            tracking.baidu_lat is not None
            and tracking.baidu_lng is not None
        ):
            print(
                f"Baidu      : "
                f"{tracking.baidu_lat}, "
                f"{tracking.baidu_lng}"
            )

        if (
            tracking.original_lat is not None
            and tracking.original_lng is not None
        ):
            print(
                f"Original   : "
                f"{tracking.original_lat}, "
                f"{tracking.original_lng}"
            )

        if context:
            self.print_data_context(
                context
            )

        print("=" * 80)

    def print_data_context(
        self,
        context: DataContext,
    ):

        print()
        print("=" * 80)
        print("DATA CONTEXT - DECODED")
        print("=" * 80)

        print(
            f"Protocol Version : "
            f"{context.protocol_version}"
        )

        print(
            f"RSSI             : "
            f"{context.rssi} / 31"
        )

        print(
            f"Satellites       : "
            f"{context.satellites}"
        )

        print(
            f"HDOP             : "
            f"{context.hdop}"
        )

        print(
            f"Voltage          : "
            f"{context.voltage:.2f} V"
        )

        print(
            f"ACC              : "
            f"{'ON' if context.acc_on else 'OFF'} "
            f"({context.acc})"
        )

        print(
            f"Relay            : "
            f"{'CUT-OFF' if context.relay_cutoff else 'CONNECTED'} "
            f"({context.relay})"
        )

        print(
            f"Power Source     : "
            f"{context.power_source_name} "
            f"({context.power_source})"
        )

        print(
            f"Power Mode       : "
            f"{context.power_mode_name} "
            f"({context.power_mode})"
        )

        print(
            f"Raw               : "
            f"{context.raw}"
        )

        print("=" * 80)

    def print_server_fields(
        self,
        raw: Optional[dict[str, Any]],
    ):

        if not raw:
            return

        print()
        print("=" * 80)
        print("SERVER FIELDS")
        print("=" * 80)

        print(
            f"Total fields: {len(raw)}"
        )

        for index, (key, value) in enumerate(
            raw.items(),
            start=1,
        ):

            print()
            print(
                f"[{index}] {key}"
            )

            print(
                f"    Type : "
                f"{type(value).__name__}"
            )

            print(
                f"    Value:"
            )

            print(
                f"    {value}"
            )

        print("=" * 80)

    def save_json(
        self,
        filename: str,
        data: Any,
    ) -> Path:

        path = self.output_dir / filename

        with path.open(
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )

        return path


# =============================================================================
# APPLICATION
# =============================================================================

class GPSCJApplication:
    """
    Main application orchestrator.

    Responsibilities are intentionally limited to:
        authentication
        discovery
        tracking
        geocoding
        reporting
    """

    def __init__(
        self,
        imei: str,
        password: str,
        email: str,
    ):

        self.transport = HttpTransport(
            BASE_URL
        )

        self.auth = AuthenticationService(
            self.transport,
            imei,
            password,
        )

        self.discovery = DeviceDiscoveryService(
            self.transport
        )

        self.context_decoder = (
            DataContextDecoder()
        )

        self.tracking_parser = (
            TrackingResponseParser(
                self.context_decoder
            )
        )

        self.tracking = TrackingService(
            self.transport,
            self.tracking_parser,
        )

        self.geocoder = GeocodingService(
            self.transport,
            email,
        )

        self.reporter = Reporter(
            OUTPUT_DIR
        )

    def __enter__(self) -> GPSCJApplication:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.transport.session.close()

    def run(self) -> ApplicationResult:

        errors: list[str] = []

        print("=" * 80)
        print("GPSCJ SOFTWARE CLIENT")
        print("=" * 80)

        print(
            "Architecture: "
            "Transport -> Services -> "
            "Parsers -> Models -> Reporting"
        )

        print(
            f"Time: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        print("=" * 80)

        # ---------------------------------------------------------------------
        # STEP 1
        # ---------------------------------------------------------------------

        LOGGER.info(
            "Step 1/5 - Authentication"
        )

        self.auth.authenticate()

        # ---------------------------------------------------------------------
        # STEP 2
        # ---------------------------------------------------------------------

        LOGGER.info(
            "Step 2/5 - Device discovery"
        )

        device = self.discovery.discover()

        # ---------------------------------------------------------------------
        # STEP 3
        # ---------------------------------------------------------------------

        LOGGER.info(
            "Step 3/5 - Tracking request"
        )

        tracking = self.tracking.get_tracking(
            device.device_id,
            device.timezone or "0",
        )

        # ---------------------------------------------------------------------
        # STEP 4
        # ---------------------------------------------------------------------

        LOGGER.info(
            "Step 4/5 - Normalizing tracking data"
        )

        # Reverse geocoding is deliberately separate.
        # It can be disabled later without changing tracking logic.

        # ---------------------------------------------------------------------
        # STEP 5
        # ---------------------------------------------------------------------

        LOGGER.info(
            "Step 5/5 - Reverse geocoding"
        )

        if (
            tracking.latitude is not None
            and tracking.longitude is not None
        ):

            tracking.address = (
                self.geocoder.reverse(
                    tracking.latitude,
                    tracking.longitude,
                )
            )

        # ---------------------------------------------------------------------
        # REPORT
        # ---------------------------------------------------------------------

        self.reporter.print_summary(
            device,
            tracking,
        )

        self.reporter.print_server_fields(
            tracking.raw
        )

        result = ApplicationResult(
            collected_at=datetime.utcnow().isoformat()
            + "Z",

            device=device.to_dict(),

            tracking=tracking.to_dict(),

            h02_status=None,

            errors=errors,
        )

        # Full structured report
        full_path = self.reporter.save_json(
            "gps_tracking_full_response.json",
            result.to_dict(),
        )

        # Raw server payload
        raw_path = self.reporter.save_json(
            "gps_raw_server_response.json",
            tracking.raw or {},
        )

        print()
        print("=" * 80)
        print("FILES")
        print("=" * 80)

        print(
            f"Full report : {full_path}"
        )

        print(
            f"Raw server  : {raw_path}"
        )

        print(
            f"Output dir  : {OUTPUT_DIR.resolve()}"
        )

        print("=" * 80)

        print()
        print("=" * 80)
        print("PROCESS FINISHED")
        print("=" * 80)

        return result


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def safe_int(
    value: Any,
) -> Optional[int]:

    if value is None:
        return None

    try:
        if isinstance(value, bool):
            return int(value)
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            return int(float(text))
    except (
        ValueError,
        TypeError,
        OverflowError,
    ):
        return None


def safe_int_required(
    value: Any,
) -> int:

    result = safe_int(value)

    if result is None:
        raise ValueError(
            f"Expected integer, received {value!r}"
        )

    return result


def safe_float(
    value: Any,
) -> Optional[float]:

    if value is None:
        return None

    try:
        result = float(str(value).strip())
        return result if math.isfinite(result) else None
    except (
        ValueError,
        TypeError,
    ):
        return None


def safe_float_required(
    value: Any,
) -> float:

    result = safe_float(value)

    if result is None:
        raise ValueError(
            f"Expected float, received {value!r}"
        )

    return result


def safe_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() == "true":
        return True
    if str(value).strip().lower() == "false":
        return False
    number = safe_float(value)
    if number in (0, 1):
        return bool(number)
    return None


def valid_coordinates(latitude: Optional[float], longitude: Optional[float]) -> bool:
    """Accept finite WGS84 coordinates, including the valid origin (0, 0)."""
    return (
        latitude is not None
        and longitude is not None
        and math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def decode_payload(value: Any, wrappers: tuple[str, ...] = ("d",)) -> Any:
    """Unwrap bounded ASP.NET/JSON layers without modifying valid JSON strings."""
    for _ in range(8):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = json.loads(normalize_js_object_literal(value))
        elif isinstance(value, dict):
            key = next((key for key in wrappers if key in value), None)
            if key is None:
                return value
            value = value[key]
        else:
            return value
    raise ValueError("Response payload has too many nested layers.")


def normalize_js_object_literal(
    raw_text: str,
) -> str:

    """
    Normalizes simple JavaScript-style object literals
    returned by legacy ASP.NET endpoints.

    This intentionally does not attempt to implement
    a complete JavaScript parser.
    """

    text = raw_text.strip()

    text = re.sub(
        r"([{,]\s*)([A-Za-z_]\w*)\s*:",
        r'\1"\2":',
        text,
    )

    # Convert simple single-quoted strings.
    text = re.sub(
        r":\s*'([^']*)'",
        lambda match: (
            ': "' +
            match.group(1).replace(
                '"',
                '\\"',
            ) +
            '"'
        ),
        text,
    )

    return text


# =============================================================================
# OPTIONAL H02 SMS PARSING DEMO
# =============================================================================

def parse_h02_status_message(
    sms_text: str,
) -> dict[str, Any]:

    """
    Convenience function.

    This does NOT send SMS.
    It only parses an SMS response already received.
    """

    parser = H02StatusParser()

    return parser.parse(
        sms_text
    ).to_dict()


# =============================================================================
# MAIN
# =============================================================================

def validate_configuration() -> None:

    if not EMAIL or EMAIL == "....":
        raise RuntimeError(
            "Set EMAIL in configuration."
        )

    if not DEVICE_IMEI or DEVICE_IMEI == "YOUR_IMEI":
        raise RuntimeError(
            "Set DEVICE_IMEI in configuration."
        )

    if (
        not DEVICE_PASSWORD
        or DEVICE_PASSWORD == "YOUR_PASSWORD"
    ):
        raise RuntimeError(
            "Set DEVICE_PASSWORD in configuration."
        )


def main() -> None:

    try:

        validate_configuration()

        with GPSCJApplication(
            imei=DEVICE_IMEI,
            password=DEVICE_PASSWORD,
            email=EMAIL,
        ) as application:
            application.run()

    except KeyboardInterrupt:

        print()
        print(
            "Interrupted by user."
        )

    except Exception as exc:

        LOGGER.exception(
            "Application failed."
        )

        print()
        print("=" * 80)
        print("ERROR")
        print("=" * 80)

        print(
            f"{type(exc).__name__}: {exc}"
        )

        print()
        print(
            f"Check log: {LOG_FILE}"
        )

        print("=" * 80)


if __name__ == "__main__":
    main()
