"""
بوت تيليغرام يعرض بيانات برنامج gps.py عبر قائمة أزرار تفاعلية.

الربط مع gps.py:
    - نستورد كلاسات gps.py مباشرة (GPSCJApplication, DirectionService ...).
    - كل زر في القائمة يستدعي دالة جلب بيانات تستخدم خدمات
      GPSCJApplication (authentication -> discovery -> tracking -> geocoding).
    - التوكن والمفاتيح تُقرأ من ملف .env.

التشغيل:
    .venv/bin/python telegram_bot.py
"""

from __future__ import annotations

import asyncio
import csv
import io
import xml.etree.ElementTree as ET
import html
import logging
import os
import re
import secrets
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from math import isfinite, asin, cos, radians, sin, sqrt
from pathlib import Path

from dotenv import load_dotenv
from staticmap import CircleMarker, Line, StaticMap
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import gps
import gps_commands

load_dotenv(Path(__file__).resolve().parent / ".env")

# =============================================================================
# CONFIGURATION
# =============================================================================

# ضع التوكن هنا مباشرة، أو الأفضل في ملف .env
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")
# HTTP request URLs can contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

# =============================================================================
# CONSTANTS
# =============================================================================

WELCOME_TEXT = (
    "🚗 <b>مرحباً بك في بوت تتبع المركبات</b>\n\n"
    "اختر أحد الخيارات من القائمة أدناه:"
)

_ERROR_TEXT = (
    "⚠️ <b>تعذر الوصول إلى البيانات حالياً.</b>\n\n"
    "يرجى إعادة المحاولة لاحقاً."
)

_DIRECTION_AR = {
    "N": "شمال",
    "NE": "شمال شرق",
    "E": "شرق",
    "SE": "جنوب شرق",
    "S": "جنوب",
    "SW": "جنوب غرب",
    "W": "غرب",
    "NW": "شمال غرب",
    "N/A": "غير متوفر",
}

# مفاتيح الأزرار التي تطلب بيانات من gps.py
VIEWS = ("location", "context", "device", "full")

# خيارات هامش الخطأ (نصف القطر) لتنبيه مغادرة المنطقة بالمتر
RADII = (50, 100, 250, 500, 1000, 5000)

# الفاصل الزمني بين فحوصات الموقع أثناء مراقبة المنطقة (بالثواني)
GEOFENCE_INTERVAL = 60
GEOFENCE_ALERT_INTERVAL = 3
LIVE_LOCATION_INTERVAL = 15
LIVE_LOCATION_PERIOD = 3600

# إعدادات خرائط OSM
_MAP_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_MAP_SIZE = (800, 600)


# =============================================================================
# KEYBOARDS
# =============================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """القائمة الرئيسية التي تظهر بعد أمر /start."""
    keyboard = [
        [
            InlineKeyboardButton(
                "📍 الموقع الحالي",
                callback_data="location",
            ),
        ],
        [
            InlineKeyboardButton(
                "📡 بيانات الاتصال",
                callback_data="context",
            ),
            InlineKeyboardButton(
                "🏠 معلومات الجهاز",
                callback_data="device",
            ),
        ],
        [
            InlineKeyboardButton(
                "📄 التقرير الكامل",
                callback_data="full",
            ),
        ],
        [
            InlineKeyboardButton(
                "🗺️ تتبع مباشر على الخريطة",
                callback_data="map_live",
            ),
        ],
        [
            InlineKeyboardButton(
                "📅 سجل حركة المركبة",
                callback_data="history",
            ),
        ],
        [
            InlineKeyboardButton(
                "🚨 تنبيه مغادرة المنطقة",
                callback_data="geofence",
            ),
        ],
        [InlineKeyboardButton("📡 تنبيهات الكهرباء والاتصال", callback_data="health")],
        [InlineKeyboardButton("📍 موقع مباشر متجدد", callback_data="live_location")],
        [InlineKeyboardButton("🔧 التحكم بالوقود عبر GPSCJ", callback_data="relay_menu")],
        [InlineKeyboardButton("🏎️ تنبيه تخطي السرعة", callback_data="limit_speed")],
        [InlineKeyboardButton("🔋 تنبيه انخفاض جهد البطارية", callback_data="limit_battery")],
    ]
    return InlineKeyboardMarkup(keyboard)


def history_menu_keyboard() -> InlineKeyboardMarkup:
    """خيارات اختيار تاريخ سجل الحركة."""
    keyboard = [
        [
            InlineKeyboardButton(
                "🗓️ اليوم",
                callback_data="history_day:today",
            ),
            InlineKeyboardButton(
                "🗓️ أمس",
                callback_data="history_day:yesterday",
            ),
        ],
        [
            InlineKeyboardButton(
                "✍️ تاريخ مخصص (YYYY-MM-DD)",
                callback_data="history_custom",
            ),
        ],
        [
            InlineKeyboardButton(
                "↩️ القائمة الرئيسية",
                callback_data="menu",
            ),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def geofence_menu_keyboard() -> InlineKeyboardMarkup:
    """خيارات تحديد مركز المنطقة المراقبة."""
    keyboard = [
        [
            InlineKeyboardButton(
                "📍 استخدم موقع الجهاز الحالي",
                callback_data="geofence_current",
            ),
        ],
        [
            InlineKeyboardButton(
                "✍️ إدخال الإحداثيات يدوياً",
                callback_data="geofence_manual",
            ),
        ],
        [
            InlineKeyboardButton(
                "↩️ القائمة الرئيسية",
                callback_data="menu",
            ),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def radius_keyboard() -> InlineKeyboardMarkup:
    """خيارات هامش الخطأ (نصف القطر) بالمتر."""
    keyboard = [
        [
            InlineKeyboardButton(
                f"{value} م",
                callback_data=f"geofence_radius:{value}",
            )
            for value in RADII[index : index + 3]
        ]
        for index in range(0, len(RADII), 3)
    ]
    return InlineKeyboardMarkup(keyboard)


def geofence_stop_keyboard() -> InlineKeyboardMarkup:
    """زر إيقاف التنبيه (يُرافق التنبيهات ورسالة التفعيل)."""
    keyboard = [
        [
            InlineKeyboardButton(
                "⏹️ إيقاف التنبيه",
                callback_data="geofence_stop",
            ),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def result_keyboard() -> InlineKeyboardMarkup:
    """أزرار التنقل التي تظهر مع النتائج."""
    keyboard = [
        [
            InlineKeyboardButton(
                "🔄 تحديث البيانات",
                callback_data="refresh",
            ),
            InlineKeyboardButton(
                "↩️ القائمة الرئيسية",
                callback_data="menu",
            ),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# =============================================================================
# DATA FETCHING (الربط مع gps.py)
# =============================================================================

def _collect(application: "gps.GPSCJApplication"):
    """تنفيذ نفس تسلسل run() في gps.py: مصادقة، اكتشاف، تتبع، ترميز العنوان."""
    application.auth.authenticate()
    device = application.discovery.discover()
    tracking = application.tracking.get_tracking(
        device.device_id,
        device.timezone or "0",
    )
    if tracking.latitude is not None and tracking.longitude is not None:
        tracking.address = application.geocoder.reverse(
            tracking.latitude,
            tracking.longitude,
        )
    return device, tracking


def _fetch_data(view_key: str) -> str:
    """دالة متزامنة تُنفَّذ داخل thread حتى لا تحجب حلقة الأحداث.

    إنشاء نسخة جديدة من التطبيق لكل طلب يعكس سلوك gps.py.run()
    ويتجنب تعارض الجلسات بين المستخدمين.
    """
    with gps.GPSCJApplication(
        imei=gps.DEVICE_IMEI,
        password=gps.DEVICE_PASSWORD,
        email=gps.EMAIL,
    ) as application:
        device, tracking = _collect(application)
        collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return _format_view(view_key, device, tracking, collected_at)


def _fetch_tracking() -> tuple[float | None, float | None]:
    """جلب الإحداثيات فقط (بدون ترميز العنوان) لفحص تنبيه المنطقة.

    يستدعي نفس خدمات gps.py: مصادقة، اكتشاف الجهاز، تتبع.
    """
    with gps.GPSCJApplication(
        imei=gps.DEVICE_IMEI,
        password=gps.DEVICE_PASSWORD,
        email=gps.EMAIL,
    ) as application:
        application.auth.authenticate()
        device = application.discovery.discover()
        tracking = application.tracking.get_tracking(
            device.device_id,
            device.timezone or "0",
        )
        return tracking.latitude, tracking.longitude


def _fetch_health_tracking():
    """Fetch hardware and connectivity with UTC timestamps."""
    with gps.GPSCJApplication(
        imei=gps.DEVICE_IMEI, password=gps.DEVICE_PASSWORD, email=gps.EMAIL,
    ) as application:
        application.auth.authenticate()
        device = application.discovery.discover()
        return application.tracking.get_tracking(device.device_id, "0")


def _health_alert_text(tracking, previous_power_source=None, last_location=None, kind="health", threshold=None):
    reasons = []
    if kind == "health" and str(tracking.status or "").strip().lower() in ("offline", "loggedoff"): 
        reasons.append("📡 الجهاز غير متصل بحسب حالة الخادم.")
    hardware = tracking.data_context
    if kind == "health" and hardware and previous_power_source in (1, 2, 3) and hardware.power_source == 0:
        reasons.append("⚡ انقطاع التغذية الخارجية: انتقل الجهاز إلى البطارية الداخلية بحسب بيانات الطاقة.")
    if kind == "speed":
        speed = tracking.speed
        if speed is not None and isfinite(speed) and speed > threshold:
            reasons.append(f"🏎️ تجاوز السرعة: {speed:g} كم/ساعة (الحد {threshold:g} كم/ساعة).")
    if kind == "battery" and hardware:
        voltage = hardware.voltage
        if voltage is not None and isfinite(voltage) and 0 <= voltage <= threshold:
            reasons.append(f"🔋 انخفاض جهد البطارية/التغذية: {voltage:g} فولت (الحد {threshold:g} فولت).")
    if not reasons:
        return None
    text = "🚨 <b>تنبيه حالة الجهاز عند اكتشاف المشكلة</b>\n" + "\n".join(reasons)
    if last_location is None and tracking.latitude is not None and tracking.longitude is not None:
        last_location = (tracking.latitude, tracking.longitude, tracking.device_utc_date)
    if last_location:
        lat, lng, timestamp = last_location
        text += f"\nآخر وقت للموقع (UTC): {_fmt_time(timestamp)}"
        text += f"\n📍 آخر موقع معروف: <code>{lat:.6f}, {lng:.6f}</code>"
        text += f'\n<a href="https://www.google.com/maps?q={lat},{lng}">عرض الموقع على الخريطة</a>'
    else:
        text += "\n📍 آخر موقع غير متوفر."
    return text + "\nتتكرر التنبيهات حتى تضغط إيقاف، حتى لو زالت المشكلة."


def _health_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹️ إيقاف تنبيهات الكهرباء والاتصال", callback_data="health_stop")],
        [InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="menu")],
    ])


async def _cancel_health_task(context, kind="health"):
    data = context.user_data.get(kind)
    if data and data.get("task"):
        data["task"].cancel()
        with suppress(asyncio.CancelledError):
            await data["task"]
    context.user_data[kind] = None


async def _health_monitor(context, chat_id, kind="health"):
    while True:
        data = context.user_data.get(kind)
        if not data:
            return
        if not data.get("alert"):
            try:
                tracking = await asyncio.to_thread(_fetch_health_tracking)
                if tracking.latitude is not None and tracking.longitude is not None:
                    data["last_location"] = (tracking.latitude, tracking.longitude, tracking.device_utc_date)
                data["alert"] = _health_alert_text(
                    tracking, data.get("previous_power_source"), data.get("last_location"),
                    kind, data.get("threshold"),
                )
                if tracking.data_context:
                    source = tracking.data_context.power_source
                    data["previous_power_source"] = source if source in (0, 1, 2, 3) else None
                else:
                    data["previous_power_source"] = None
            except Exception as exc:
                logger.warning(
                    "تعذر الوصول إلى خادم التتبع (%s). ستعاد المحاولة في الفحص التالي؛ "
                    "هذا لا يؤكد انقطاع الجهاز.", type(exc).__name__,
                )
        if not data.get("alert"):
            await asyncio.sleep(GEOFENCE_INTERVAL)
            continue
        delay = GEOFENCE_ALERT_INTERVAL
        try:
            if not data.get("location_sent"):
                location = data.get("last_location")
                if location:
                    await context.bot.send_location(
                        chat_id=chat_id, latitude=location[0], longitude=location[1],
                        reply_markup=_health_keyboard() if kind == "health" else _limit_keyboard(kind),
                    )
                else:
                    await context.bot.send_message(chat_id, "📍 لا يوجد موقع معروف محفوظ للجهاز.")
                data["location_sent"] = True
            await context.bot.send_message(
                chat_id, data["alert"], parse_mode=ParseMode.HTML,
                reply_markup=_health_keyboard() if kind == "health" else _limit_keyboard(kind), disable_web_page_preview=True,
            )
        except Forbidden:
            context.user_data[kind] = None
            return
        except RetryAfter as exc:
            retry = exc.retry_after
            delay = max(delay, retry.total_seconds() if hasattr(retry, "total_seconds") else retry)
        except TelegramError as exc:
            logger.warning("تعذر إرسال تنبيه حالة الجهاز: %s", type(exc).__name__)
        await asyncio.sleep(delay)


async def _health_handler(query, context, chat_id, action):
    if action == "health_stop":
        await _cancel_health_task(context)
        await query.edit_message_text("⏹️ تم إيقاف تنبيهات الكهرباء والاتصال.", reply_markup=main_menu_keyboard())
        return
    if action == "health_start":
        await _cancel_health_task(context)
        context.user_data["health"] = {"alert": None, "previous_power_source": None}
        task = asyncio.create_task(_health_monitor(context, chat_id))
        context.user_data["health"]["task"] = task
        tasks = context.application.bot_data.setdefault("geofence_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    active = context.user_data.get("health")
    if active:
        await query.edit_message_text(
            "✅ تنبيهات الكهرباء والاتصال مفعلة.\n"
            "يُفحص الجهاز كل دقيقة. عند اكتشاف انقطاع الاتصال أو انتقال التغذية الخارجية إلى البطارية الداخلية يُرسل آخر موقع معروف أولًا، ثم تتكرر الإشعارات كل 3 ثوانٍ حتى الإيقاف اليدوي.",
            reply_markup=_health_keyboard(),
        )
    else:
        await query.edit_message_text(
            "📡 تنبيهات الكهرباء والاتصال\nينبه عند انقطاع الاتصال أو انتقال الجهاز من التغذية الخارجية إلى البطارية الداخلية.\nيحتاج تنبيه الكهرباء إلى قراءة تغذية خارجية أولًا؛ مصدر الطاقة غير المعروف لا يؤكد انقطاعها.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تفعيل التنبيهين", callback_data="health_start")],
                [InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="menu")],
            ]),
        )


_LIMIT_LABELS = {
    "speed": ("تخطي السرعة", "كم/ساعة", 300),
    "battery": ("انخفاض جهد البطارية/التغذية", "فولت", 100),
}


def _limit_keyboard(kind):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ تحديد القيمة وتفعيل التنبيه", callback_data=f"limit_set_{kind}")],
        [InlineKeyboardButton("⏹️ إيقاف هذا التنبيه", callback_data=f"limit_stop_{kind}")],
        [InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="menu")],
    ])


async def _limit_handler(query, context, action):
    kind = action.rsplit("_", 1)[-1]
    if kind not in _LIMIT_LABELS:
        return
    label, unit, maximum = _LIMIT_LABELS[kind]
    if action == f"limit_stop_{kind}":
        await _cancel_health_task(context, kind)
        await query.edit_message_text(f"⏹️ تم إيقاف تنبيه {label}.", reply_markup=_limit_keyboard(kind))
    elif action == f"limit_set_{kind}":
        context.user_data["limit_input"] = kind
        await query.edit_message_text(
            f"أرسل حد {label} بوحدة {unit}، أكبر من صفر وحتى {maximum}. تقبل القيم العشرية."
            + ("\nهذا حد للجهد المبلّغ عنه، وليس نسبة شحن البطارية الداخلية." if kind == "battery" else ""),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data=f"limit_{kind}")]]),
        )
    else:
        active = context.user_data.get(kind)
        state = f"مفعل — الحد: {active['threshold']:g} {unit}" if active else "غير مفعل"
        await query.edit_message_text(
            f"تنبيه {label}: {state}\nيُفحص كل دقيقة، ويُرسل آخر موقع أولًا ثم تنبيهات كل 3 ثوانٍ حتى الإيقاف."
            + ("\nيعتمد على جهد dataContext المتاح؛ لا يحسب نسبة الشحن." if kind == "battery" else ""),
            reply_markup=_limit_keyboard(kind),
        )


async def _limit_input(update, context, kind):
    label, unit, maximum = _LIMIT_LABELS[kind]
    try:
        value = float(update.message.text.strip().replace("٫", ".").replace(",", "."))
        if not isfinite(value) or not 0 < value <= maximum:
            raise ValueError()
    except (ValueError, TypeError):
        await update.message.reply_text(f"أدخل رقمًا أكبر من صفر وحتى {maximum} {unit}.")
        return
    await _cancel_health_task(context, kind)
    context.user_data[kind] = {"threshold": value, "alert": None}
    task = asyncio.create_task(_health_monitor(context, update.effective_chat.id, kind))
    context.user_data[kind]["task"] = task
    tasks = context.application.bot_data.setdefault("geofence_tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    context.user_data["limit_input"] = None
    await update.message.reply_text(f"✅ تم تفعيل تنبيه {label} عند {'تجاوز' if kind == 'speed' else 'بلوغ أو انخفاض عن'} {value:g} {unit}.", reply_markup=_limit_keyboard(kind))


def _haversine(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """المسافة بالمتّر بين نقطتين إحداثيتين (Haversine)."""
    earth_radius = 6_371_000.0

    phi1 = radians(lat1)
    phi2 = radians(lat2)
    delta_phi = radians(lat2 - lat1)
    delta_lambda = radians(lon2 - lon1)

    a = (
        sin(delta_phi / 2) ** 2
        + cos(phi1) * cos(phi2) * sin(delta_lambda / 2) ** 2
    )
    return 2 * earth_radius * asin(sqrt(min(1.0, max(0.0, a))))


# =============================================================================
# MAP RENDERING (خرائط OSM)
# =============================================================================

def _render_point_map(
    lat: float,
    lng: float,
    output_path: str,
) -> None:
    """رسم خريطة بموقع واحد (نقطة حمراء)."""
    mapp = StaticMap(*_MAP_SIZE, url_template=_MAP_TILE_URL)
    mapp.add_marker(CircleMarker((lng, lat), "#ff3b30", 14))
    mapp.render().save(output_path)


def _render_route_map(
    points: list["gps.TrackPoint"],
    output_path: str,
) -> None:
    """رسم خريطة بمسار حركة المركبة (خط + نقاط)."""
    coords = [(p.longitude, p.latitude) for p in points]
    if not coords:
        raise ValueError("Cannot render an empty route.")

    mapp = StaticMap(*_MAP_SIZE, url_template=_MAP_TILE_URL)

    if len(coords) > 1:
        mapp.add_line(Line(coords, "#0066ff", 4))

    for index, coordinate in enumerate(coords):
        if index == 0:
            color = "#00cc00"  # البداية
        elif index == len(coords) - 1:
            color = "#ff3b30"  # النهاية
        else:
            color = "#ffd500"  # النقاط الوسيطة
        mapp.add_marker(CircleMarker(coordinate, color, 9))

    mapp.render().save(output_path)


# =============================================================================
# TRACKING HISTORY (سجل حركة المركبة)
# =============================================================================

def _device_offset_hours(timezone_value: str) -> float:
    match = re.search(
        r"([-+]?\d+)(?::(\d+))?",
        timezone_value or "0",
    )
    if not match:
        return 0.0

    hours = float(match.group(1))
    minutes = float(match.group(2) or 0)
    sign = -1 if match.group(1).startswith("-") else 1

    return hours + sign * minutes / 60


def _history_work(
    request: str,
) -> tuple["gps.DeviceInfo", str, list["gps.TrackPoint"]]:
    """يجلب سجل حركة الجهاز ليوم محدد.

    request: "today" أو "yesterday" أو "YYYY-MM-DD".
    """
    with gps.GPSCJApplication(
        imei=gps.DEVICE_IMEI,
        password=gps.DEVICE_PASSWORD,
        email=gps.EMAIL,
    ) as application:
        application.auth.authenticate()
        device = application.discovery.discover()

        timezone_value = device.timezone or "0"

        if request in ("today", "yesterday"):
            device_now = (
                datetime.now(timezone.utc)
                + timedelta(hours=_device_offset_hours(timezone_value))
            )
            if request == "yesterday":
                device_now -= timedelta(days=1)
            date_str = device_now.strftime("%Y-%m-%d")
        else:
            date_str = request

        start_time = f"{date_str} 00:00:00"
        end_time = f"{date_str} 23:59:59"

        service = gps.TrackingHistoryService(
            application.transport,
            application.context_decoder,
        )

        points = service.get_history(
            device.device_id,
            timezone_value,
            start_time,
            end_time,
        )

        return device, date_str, points


def _history_caption(
    device: "gps.DeviceInfo",
    date_str: str,
    points: list["gps.TrackPoint"],
) -> str:
    total_distance = 0.0
    max_speed = 0.0

    for a, b in zip(points, points[1:]):
        total_distance += _haversine(
            a.latitude,
            a.longitude,
            b.latitude,
            b.longitude,
        )

    for point in points:
        if point.speed and point.speed > max_speed:
            max_speed = point.speed

    return (
        "🗺️ <b>سجل حركة المركبة</b>\n"
        f"{_SEPARATOR}\n"
        f"📅 التاريخ: {_esc(date_str)}\n"
        f"📍 عدد النقاط: {_esc(len(points))}\n"
        f"📏 المسافة الإجمالية: {_format_distance(total_distance)}\n"
        f"🚀 السرعة القصوى: {_esc(max_speed)} كم/س\n"
        f"🕒 البداية: {_fmt_time(points[0].timestamp)}\n"
        f"🕒 النهاية: {_fmt_time(points[-1].timestamp)}\n"
        f"{_SEPARATOR}\n"
        "🟢 نقطة البداية • 🟡 النقاط الوسيطة • 🔴 نقطة النهاية"
    )


def _map_links(lat: float, lng: float) -> str:
    return (
        f'🗺️ <a href="https://maps.google.com/?q={lat:.6f},{lng:.6f}">'
        "افتح في خرائط جوجل</a>\n"
        f'🌍 <a href="https://www.openstreetmap.org/?mlat={lat:.6f}'
        f'&amp;mlon={lng:.6f}#map=16/{lat:.6f}/{lng:.6f}">'
        "افتح في OpenStreetMap</a>"
    )


def _live_map_caption(lat: float, lng: float) -> str:
    return (
        "📍 <b>تتبع مباشر على الخريطة</b>\n"
        f"{_SEPARATOR}\n"
        f"🌐 الإحداثيات: <code>{lat:.6f}, {lng:.6f}</code>\n"
        f"{_map_links(lat, lng)}"
    )


# =============================================================================
# FORMATTING
# =============================================================================

def _esc(value: object) -> str:
    """تهريب رموز HTML حتى تظهر البيانات النصية بشكل آمن."""
    return html.escape(str(value))


def _fmt_time(value: object) -> str:
    if not value:
        return "غير متوفر"
    return _esc(str(value).replace("T", " ")[:19])


# =============================================================================
# INTERPRETATION HELPERS (عرض ذو معنى تقني)
# =============================================================================

_DATATYPE_AR = {
    1: "إرسال دوري (تحديث عادي)",
}

_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━"


def _rssi_quality(rssi: int | None) -> str:
    if rssi is None:
        return "غير متوفر"
    if rssi < 10:
        return "ضعيفة (خطر فقدان الاتصال)"
    if rssi < 20:
        return "متوسطة"
    return "جيدة"


def _satellites_note(satellites: int | None) -> str:
    if satellites is None:
        return "غير متوفر"
    if satellites >= 3:
        return "كافٍ لتحديد الموقع (3+ أقمار)"
    return "غير كافٍ لتحديد الموقع"


def _hdop_quality(hdop: float | None) -> str:
    if hdop is None:
        return "غير متوفر"
    if hdop < 1:
        return "ممتازة"
    if hdop < 2:
        return "ممتازة (دقة عالية جداً)"
    if hdop < 5:
        return "جيدة (دقة عالية)"
    if hdop < 10:
        return "مقبولة (دقة متوسطة)"
    if hdop < 20:
        return "ضعيفة (دقة منخفضة)"
    return "سيئة جداً (دقة غير موثوقة)"


def _format_distance(distance: float | None) -> str:
    """عرض المسافة بالمتر والكيلومتر معاً."""
    if distance is None:
        return "غير متوفر"
    meters = float(distance)
    kilometers = meters / 1000
    if meters >= 1000:
        return f"{kilometers:.2f} كم ({meters:,.0f} م)"
    return f"{meters:.0f} م ({kilometers:.3f} كم)"


def _voltage_note(voltage: float | None) -> str:
    if voltage is None:
        return "غير متوفر"
    if voltage < 11.5:
        return "منخفض — خطر على البطارية"
    if voltage <= 14.5:
        return "طبيعي"
    return "مرتفع (قد يكون في وضع الشحن)"


def _power_mode_text(ctx: "gps.DataContext") -> str:
    if ctx.power_mode == 1:
        return "نشط"
    if ctx.power_mode == 4:
        return "وضع السكون المتقدم (توفير الطاقة)"
    return f"غير معروف (القيمة: {ctx.power_mode})"


def _connection_text(tracking: "gps.TrackingData") -> str:
    """Match GPSCJ's JS/Map.js: status determines connectivity, ofl is minutes."""
    status = str(tracking.status or "").strip().lower()
    if status in ("move", "speed", "stop"):
        return "متصل بحسب تصنيف الخادم"
    if status == "offline":
        text = "غير متصل بحسب تصنيف الخادم"
        if tracking.offline is not None and tracking.offline >= 0:
            text += f" (مدة الانقطاع المبلّغ عنها: {tracking.offline} دقيقة)"
        return text
    if status == "loggedoff":
        return "غير متصل: مسجل خروج بحسب الخادم"
    if status == "arrears":
        return "الخدمة متأخرة السداد بحسب الخادم"
    return "غير متوفر: حالة الخادم غير معروفة"


def _movement_text(tracking: "gps.TrackingData") -> str:
    if tracking.is_stop is None:
        return "غير متوفر"
    if tracking.is_stop:
        text = "متوقف بحسب آخر بيانات الخادم"
        if tracking.stop_time_minute is not None and tracking.stop_time_minute >= 0:
            text += f" (المدة المبلّغ عنها: {tracking.stop_time_minute} دقيقة)"
        if (tracking.speed is not None and tracking.speed > 0) or str(tracking.status).lower() in ("move", "speed"):
            text += " ⚠️ يتعارض مع السرعة أو حالة الخادم"
        return text
    if tracking.speed == 0 or str(tracking.status).lower() == "stop":
        return "علامة التوقف غير مفعلة؛ لا تكفي لتأكيد الحركة مع سرعة صفر أو حالة Stop"
    return "علامة التوقف غير مفعلة بحسب آخر بيانات الخادم"


def _server_distance_text(tracking: "gps.TrackingData") -> str:
    """Display the API distance in both kilometres and metres."""
    if tracking.distance is None or tracking.distance < 0:
        return "غير متوفر"
    return _format_distance(tracking.distance)


def _format_location(tracking: "gps.TrackingData") -> str:
    lat, lng = tracking.latitude, tracking.longitude

    if lat is None or lng is None:
        coords_line = "🌐 الإحداثيات (WGS84): غير متوفر"
        maps_link = ""
    else:
        coords_line = f"🌐 الإحداثيات (WGS84): {lat:.6f}, {lng:.6f}"
        maps_link = _map_links(lat, lng)

    direction_en = gps.DirectionService.from_course(tracking.course)
    direction_ar = _DIRECTION_AR.get(direction_en, direction_en)

    stop_text = _movement_text(tracking)
    conn_text = _connection_text(tracking)

    datatype_line = f"📡 سبب الإرسال (dataType): {_esc(tracking.data_type)}"
    if tracking.data_type in _DATATYPE_AR:
        datatype_line += f" — {_DATATYPE_AR[tracking.data_type]}"

    return "\n".join(
        part
        for part in [
            "📍 <b>آخر موقع مسجّل لدى الخادم</b>",
            _SEPARATOR,
            f"🗺️ العنوان: {_esc(tracking.address or 'غير متوفر')}",
            coords_line,
            maps_link,
            _SEPARATOR,
            f"🆔 معرف السجل (locationID): {_esc(tracking.location_id)}",
            datatype_line,
            f"🚗 السرعة: {_esc(tracking.speed)} كم/س",
            f"🧭 الاتجاه: {direction_ar} ({_esc(tracking.course)}°)",
            f"⏹️ حالة الحركة (isStop): {stop_text}",
            f"📏 المسافة: {_server_distance_text(tracking)}",
            f"🛰️ الاتصال (status): {conn_text}",
            f"📊 حالة الخادم: {_esc(tracking.status or 'غير متوفر')}",
            _SEPARATOR,
            f"🕒 وقت الجهاز: {_fmt_time(tracking.device_utc_date)}",
            f"🕒 وقت الخادم: {_fmt_time(tracking.server_utc_date)}",
            "ℹ️ هذه آخر بيانات مسجلة؛ نجاح جلبها لا يؤكد أن الموقع أو حالة الحركة حديثان.",
        ]
        if part
    )


def _format_context(tracking: "gps.TrackingData") -> str:
    ctx = tracking.data_context

    if ctx is None:
        if tracking.data_context_raw and tracking.data_context_raw.strip():
            details = (
                "⚠️ وصلت بيانات العتاد، لكن تعذّر تفسير صيغتها.\n"
                f"🔢 السلسلة الخام: <code>{_esc(tracking.data_context_raw)}</code>"
            )
        else:
            details = "⚠️ لم يُرجع الخادم بيانات العتاد في هذه الاستجابة."
        return (
            "📡 <b>بيانات العتاد (dataContext)</b>\n"
            f"{_SEPARATOR}\n"
            f"{details}"
        )

    power_source = (
        ctx.power_source_name
        if ctx.power_source in (0, 1, 2, 3)
        else f"غير معروف (القيمة: {ctx.power_source})"
    )
    return (
        "📡 <b>بيانات العتاد (dataContext)</b>\n"
        f"{_SEPARATOR}\n"
        f"📶 إشارة GSM (RSSI): {_esc(ctx.rssi)} / 31 — "
        f"{_rssi_quality(ctx.rssi)}\n"
        f"🛰️ الأقمار الصناعية: {_esc(ctx.satellites)} — "
        f"{_satellites_note(ctx.satellites)}\n"
        f"📐 الدقة الأفقية (HDOP): {_esc(ctx.hdop)} — "
        f"{_hdop_quality(ctx.hdop)}\n"
        f"🔋 الجهد الكهربائي: {_esc(ctx.voltage)} فولت — "
        f"{_voltage_note(ctx.voltage)}\n"
        f"🚘 التشغيل (ACC): "
        f"{'تشغيل (المركبة تعمل)' if ctx.acc_on else 'إيقاف (المركبة متوقفة)'}\n"
        f"🔌 المرحّل: {'مفصول (قطع التيار)' if ctx.relay_cutoff else 'متصل'}\n"
        f"⚡ مصدر الطاقة: {power_source}\n"
        f"🎛️ وضع الطاقة: {_power_mode_text(ctx)}\n"
        f"{_SEPARATOR}\n"
        f"🔢 السلسلة الخام: <code>{_esc(ctx.raw)}</code>\n"
        "💡 <i>سلسلة مضغوطة لتقليل حجم النقل: "
        "الفهرس 1 = GSM، 2 = الأقمار، 4 = الجهد، "
        "5 = ACC، 8 = وضع الطاقة.</i>"
    )


def _format_device(device: "gps.DeviceInfo", collected_at: str) -> str:
    return (
        "🏠 <b>معلومات الجهاز</b>\n"
        "──────────────────\n"
        f"🆔 معرف الجهاز: {_esc(device.device_id)}\n"
        f"👤 معرف المستخدم: {_esc(device.user_id or 'غير متوفر')}\n"
        f"🌍 المنطقة الزمنية: {_esc(device.timezone or 'غير متوفر')}\n"
        f"📱 رقم IMEI: <code>{_esc(gps.DEVICE_IMEI)}</code>\n"
        "──────────────────\n"
        f"🕒 آخر تحديث: {_esc(collected_at)}"
    )


def _format_full(
    device: "gps.DeviceInfo",
    tracking: "gps.TrackingData",
    collected_at: str,
) -> str:
    return "\n\n".join(
        [
            f"📄 <b>التقرير الكامل</b>\n🕒 وقت الجمع: {_esc(collected_at)}",
            _format_location(tracking),
            _format_context(tracking),
            _format_device(device, collected_at),
        ]
    )


def _format_view(
    view_key: str,
    device: "gps.DeviceInfo",
    tracking: "gps.TrackingData",
    collected_at: str,
) -> str:
    if view_key == "location":
        return _format_location(tracking)
    if view_key == "context":
        return _format_context(tracking)
    if view_key == "device":
        return _format_device(device, collected_at)
    if view_key == "full":
        return _format_full(device, tracking, collected_at)
    raise ValueError(f"Unknown view: {view_key}")


# =============================================================================
# GEOFENCE MONITOR (تنبيه مغادرة المنطقة)
# =============================================================================

def _geofence_alert_text(
    lat: float,
    lng: float,
    distance: float,
    radius: int,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "🚨 <b>تنبيه: الجهاز خرج من المنطقة المحددة!</b>\n"
        "──────────────────\n"
        f"📍 الموقع عند اكتشاف الخروج: <code>{lat:.6f}, {lng:.6f}</code>\n"
        f"📏 البُعد عن المركز: {distance:.0f} م "
        f"(الحد المسموح: {radius} م)\n"
        f"🕒 الوقت: {now}\n"
        "──────────────────\n"
        "لن تتوقف هذه التنبيهات حتى تضغط زر الإيقاف."
    )


async def _cancel_geofence_task(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """إيقاف مهمة المراقبة ومسح حالة التنبيه الخاصة بالمستخدم."""
    data = context.user_data.get("geofence")

    if data:
        task = data.get("task")
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    context.user_data["geofence"] = None
    context.user_data["geofence_state"] = None
    context.user_data["geofence_center"] = None


async def _geofence_monitor(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """افحص الموقع حتى أول خروج، ثم كرر الإنذار حتى الإيقاف اليدوي."""
    while True:
        data = context.user_data.get("geofence")
        if not data:
            return

        if not data.get("exited"):
            try:
                lat, lng = await asyncio.to_thread(_fetch_tracking)
                if lat is not None and lng is not None:
                    distance = _haversine(*data["center"], lat, lng)
                    if distance > data["radius"]:
                        data["exit_location"] = (lat, lng, distance)
                        data["exited"] = True
            except Exception as exc:
                logger.warning("فشل فحص موقع الجهاز لتنبيه المنطقة: %s", exc)

        if not data.get("exited"):
            await asyncio.sleep(GEOFENCE_INTERVAL)
            continue

        # احتفظ بأول خروج حتى لا يوقف الإنذار رجوع الجهاز أو تعذر جلب موقعه.
        lat, lng, distance = data["exit_location"]
        delay = GEOFENCE_ALERT_INTERVAL
        try:
            await context.bot.send_message(
                chat_id,
                _geofence_alert_text(lat, lng, distance, data["radius"]),
                parse_mode=ParseMode.HTML,
                reply_markup=geofence_stop_keyboard(),
                disable_web_page_preview=True,
            )
        except Forbidden:
            context.user_data["geofence"] = None
            return
        except RetryAfter as exc:
            retry_after = exc.retry_after
            if hasattr(retry_after, "total_seconds"):
                retry_after = retry_after.total_seconds()
            delay = max(delay, retry_after)
        except TelegramError as exc:
            logger.warning("تعذر إرسال تنبيه المنطقة: %s", type(exc).__name__)
        await asyncio.sleep(delay)


# =============================================================================
# HANDLERS
# =============================================================================

class _CallbackEditor:
    """Follow the replacement text message when navigating from media."""

    def __init__(self, query):
        self.query = query
        self.message = query.message

    @property
    def data(self):
        return self.query.data

    async def answer(self, *args, **kwargs):
        return await self.query.answer(*args, **kwargs)

    async def edit_message_text(self, text, **kwargs):
        if self.message is not None and self.message.text is None:
            self.message = await self.query.get_bot().send_message(
                chat_id=self.message.chat_id, text=text, **kwargs
            )
            return self.message
        try:
            if self.message is not None:
                result = await self.message.edit_text(text, **kwargs)
            else:
                result = await self.query.edit_message_text(text, **kwargs)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return self.message
            raise
        if result is not True:
            self.message = result
        return result


def _allowed_user_ids() -> set[int]:
    try:
        values = {
            int(value.strip())
            for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
            if value.strip()
        }
    except ValueError as exc:
        raise RuntimeError("ALLOWED_USER_IDS must contain comma-separated numeric user IDs.") from exc
    if any(value <= 0 for value in values):
        raise RuntimeError("ضع معرف مستخدم تيليغرام في ALLOWED_USER_IDS داخل ملف .env.")
    return values


async def show_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show only the sender's ID; this command never grants access to GPS data."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if user is None or chat is None or message is None:
        return
    if chat.type != "private":
        await message.reply_text("أرسل /id في المحادثة الخاصة مع البوت.")
        return
    await message.reply_text(
        f"معرّف حسابك: {user.id}\n\n"
        "للسماح لهذا الحساب، أضف السطر التالي إلى ملف .env على جهاز تشغيل البوت "
        "(أو أضف المعرّف إلى القائمة الموجودة)، ثم أعد تشغيل البوت:\n"
        f"ALLOWED_USER_IDS={user.id}",
    )


async def _authorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if (
        user is not None
        and user.id in context.bot_data.get("allowed_user_ids", set())
        and chat is not None
        and chat.type == "private"
    ):
        return True
    if not context.bot_data.get("allowed_user_ids"):
        text = (
            "البوت في وضع الإعداد. أرسل /id في المحادثة الخاصة لمعرفة معرّف حسابك، "
            "ثم أضفه إلى ALLOWED_USER_IDS في ملف .env وأعد تشغيل البوت. "
            "عرض بيانات المركبة معطل حتى اكتمال الإعداد."
        )
    else:
        text = "غير مصرح. استخدم المحادثة الخاصة بحساب مسموح. يمكنك معرفة معرّفك بإرسال /id."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)
    return False


async def _shutdown_geofences(application: Application) -> None:
    tasks = list(application.bot_data.get("geofence_tasks", set()))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Avoid logging the Update, which includes messages and location data.
    logger.error("فشل معالجة تحديث: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        with suppress(TelegramError):
            await update.effective_message.reply_text(
                _ERROR_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard()
            )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _authorize(update, context):
        return
    context.user_data.pop("relay_pending", None)
    context.user_data["limit_input"] = None
    context.user_data["history_state"] = None
    context.user_data["geofence_state"] = None
    context.user_data["geofence_center"] = None
    context.user_data["current_view"] = None
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def _show_view(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    view_key: str,
) -> None:
    query = _CallbackEditor(update.callback_query)

    await query.edit_message_text("⏳ جاري التحديث، يرجى الانتظار...")

    try:
        text = await asyncio.to_thread(_fetch_data, view_key)
    except Exception as exc:
        logger.exception("فشل جلب البيانات للعرض %s", view_key)
        text = f"{_ERROR_TEXT}\n\n({type(exc).__name__})"

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(),
        disable_web_page_preview=True,
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _authorize(update, context):
        return
    query = _CallbackEditor(update.callback_query)
    await query.answer()

    data = query.data
    if not isinstance(data, str):
        return
    context.user_data["limit_input"] = None
    context.user_data["history_state"] = None
    context.user_data["geofence_state"] = None
    if not data.startswith("geofence_radius:"):
        context.user_data["geofence_center"] = None

    if not data.startswith("relay_"):
        context.user_data.pop("relay_pending", None)

    if data == "menu":
        context.user_data["current_view"] = None
        await query.edit_message_text(
            WELCOME_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "refresh":
        view_key = context.user_data.get("current_view")
        if view_key == "map_live":
            await _map_live(update, context)
        elif view_key == "history":
            await _show_history(update, context, context.user_data.get("history_request", "today"))
        elif view_key in VIEWS:
            await _show_view(update, context, view_key)
        else:
            context.user_data["current_view"] = None
            await query.edit_message_text(
                WELCOME_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(),
            )
        return

    if data.startswith("relay_"):
        await _relay_handler(query, context, data)
        return
    context.user_data.pop("relay_pending", None)

    if data.startswith("limit_"):
        await _limit_handler(query, context, data)
        return

    if data in ("health", "health_stop", "health_start"):
        await _health_handler(query, context, update.effective_chat.id, data)
        return

    if data == "geofence":
        await _geofence_menu(update, context)
        return

    if data == "geofence_current":
        await _geofence_current(update, context)
        return

    if data == "geofence_manual":
        await _geofence_manual(update, context)
        return

    if data.startswith("geofence_radius:"):
        await _geofence_radius(update, context)
        return

    if data == "geofence_stop":
        await _geofence_stop(update, context)
        return

    if data in ("live_location", "live_location_start", "live_location_stop"):
        await _live_location_handler(update, context, data)
        return

    if data == "map_live":
        context.user_data["current_view"] = "map_live"
        await _map_live(update, context)
        return

    if data == "history":
        await _history_menu(update, context)
        return

    if data.startswith("history_export:"):
        await _export_history(update, context, data)
        return

    if data.startswith("history_day:"):
        await _history_day(update, context)
        return

    if data == "history_custom":
        await _history_custom(update, context)
        return

    if data in VIEWS:
        context.user_data["current_view"] = data
        await _show_view(update, context, data)


# =============================================================================
# GEOFENCE HANDLERS
# =============================================================================

async def _geofence_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)

    active = context.user_data.get("geofence")

    if active:
        center_lat, center_lng = active["center"]
        text = (
            "🚨 <b>تنبيه مغادرة المنطقة قيد العمل</b>\n\n"
            f"📍 المركز: <code>{center_lat:.6f}, "
            f"{center_lng:.6f}</code>\n"
            f"📏 هامش الخطأ: {active['radius']} م\n\n"
            "يُرسل التنبيه عند اكتشاف خروج الجهاز من المنطقة، "
            "ويتكرر كل 3 ثوانٍ حتى تضغط زر الإيقاف، حتى لو عاد الجهاز للمنطقة."
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=geofence_stop_keyboard(),
        )
        return

    await query.edit_message_text(
        "🚨 <b>تنبيه مغادرة المنطقة</b>\n\n"
        "حدّد مركز المنطقة المراد مراقبتها، ثم اختر هامش الخطأ:",
        parse_mode=ParseMode.HTML,
        reply_markup=geofence_menu_keyboard(),
    )


async def _geofence_current(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)

    await query.edit_message_text(
        "⏳ جاري جلب موقع الجهاز الحالي..."
    )

    try:
        lat, lng = await asyncio.to_thread(_fetch_tracking)
    except Exception as exc:
        logger.exception("فشل جلب الموقع الحالي لتنبيه المنطقة")
        await query.edit_message_text(
            f"{_ERROR_TEXT}\n\n({type(exc).__name__})",
            parse_mode=ParseMode.HTML,
            reply_markup=result_keyboard(),
        )
        return

    if lat is None or lng is None:
        await query.edit_message_text(
            "⚠️ لا تتوفر إحداثيات حالية للجهاز.\n"
            "جرب الإدخال اليدوي بدلاً من ذلك.",
            parse_mode=ParseMode.HTML,
            reply_markup=geofence_menu_keyboard(),
        )
        return

    context.user_data["geofence_center"] = (lat, lng)
    context.user_data["geofence_state"] = "awaiting_radius"

    text = (
        "📍 مركز المنطقة:\n"
        f"<code>{lat:.6f}, {lng:.6f}</code>\n\n"
        "اختر هامش الخطأ (نصف القطر) بالمتر:"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=radius_keyboard(),
    )


async def _geofence_manual(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)

    context.user_data["geofence_state"] = "awaiting_coords"

    await query.edit_message_text(
        "✍️ <b>إدخال الإحداثيات يدوياً</b>\n\n"
        "أرسل الآن رسالة نصية تحتوي الإحداثيات بهذه الصيغة:\n"
        "<code>خط_العرض, خط_الطول</code>\n\n"
        "مثال:\n<code>33.573110, -7.589843</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=geofence_menu_keyboard(),
    )


async def _geofence_radius(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)

    center = context.user_data.get("geofence_center")

    if not center:
        await query.edit_message_text(
            "⚠️ لم يُحدَّد مركز المنطقة بعد. ابدأ من جديد.",
            parse_mode=ParseMode.HTML,
            reply_markup=geofence_menu_keyboard(),
        )
        return

    try:
        radius = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return

    if radius not in RADII:
        await query.edit_message_text("⚠️ اختر نصف القطر من الأزرار.", reply_markup=radius_keyboard())
        return

    context.user_data["geofence_center"] = None
    context.user_data["geofence_state"] = None

    await _start_geofence(
        update,
        context,
        center,
        radius,
    )


async def _start_geofence(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    center: tuple[float, float],
    radius: int,
) -> None:
    query = _CallbackEditor(update.callback_query)

    await _cancel_geofence_task(context)

    context.user_data["geofence"] = {
        "center": center,
        "radius": radius,
        "exited": False,
        "task": None,
    }

    task = asyncio.create_task(
        _geofence_monitor(context, update.effective_chat.id)
    )
    context.user_data["geofence"]["task"] = task
    tasks = context.application.bot_data.setdefault("geofence_tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)

    text = (
        "✅ <b>تم تفعيل تنبيه مغادرة المنطقة</b>\n\n"
        f"📍 المركز: <code>{center[0]:.6f}, {center[1]:.6f}</code>\n"
        f"📏 هامش الخطأ: {radius} متر\n\n"
        "سأراقب موقع الجهاز كل دقيقة.\n"
        "عند اكتشاف الخروج ستصلك إشعارات متتالية كل 3 ثوانٍ، "
        "ولن تتوقف حتى تضغط زر الإيقاف، حتى لو عاد الجهاز للمنطقة."
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "⏹️ إيقاف التنبيه",
                callback_data="geofence_stop",
            ),
        ],
        [
            InlineKeyboardButton(
                "↩️ القائمة الرئيسية",
                callback_data="menu",
            ),
        ],
    ]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _geofence_stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)

    await _cancel_geofence_task(context)

    await query.edit_message_text(
        "⏹️ <b>تم إيقاف تنبيه مغادرة المنطقة.</b>\n\n"
        "لن يصلك المزيد من التنبيهات.",
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(),
    )


# =============================================================================
# MAP & HISTORY HANDLERS (تتبع مباشر + سجل الحركة)
# =============================================================================

def _relay_keyboard(command_id=None):
    rows = [
        [InlineKeyboardButton("⛔ قطع الوقود", callback_data="relay_cut"),
         InlineKeyboardButton("✅ إعادة الوقود", callback_data="relay_restore")],
        [InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="menu")],
    ]
    if command_id is not None:
        rows.insert(0, [InlineKeyboardButton("📨 قراءة رد الجهاز", callback_data=f"relay_status:{command_id}")])
    return InlineKeyboardMarkup(rows)


async def _relay_handler(query, context, action):
    loop = asyncio.get_running_loop()
    if action.startswith('relay_status:'):
        context.user_data.pop('relay_pending', None)
        last = context.user_data.get('relay_last_command')
        if not last or action != f"relay_status:{last['id']}":
            await query.edit_message_text('هذا الأمر غير متاح في جلسة البوت الحالية.', reply_markup=_relay_keyboard())
            return
        await query.edit_message_text('⏳ جارٍ قراءة رد الجهاز...')
        try:
            reply = await asyncio.to_thread(gps_commands.command_response, last['id'], last['target'])
            text = (f"رد المنصة للأمر {last['id']}:\n{reply[:2500]}\n\nتحقق من حالة المركبة فعليًا؛ الرد وحده لا يثبت عمل الريليه."
                    if reply else f"لم يظهر رد الجهاز للأمر {last['id']} بعد. يمكنك قراءة الرد مجددًا؛ لم يُعَد إرسال الأمر.")
        except Exception as exc:
            text = str(exc) if isinstance(exc, gps_commands.CommandError) else 'تعذرت قراءة رد الجهاز؛ حاول قراءة الرد لاحقًا.'
        await query.edit_message_text(text, reply_markup=_relay_keyboard(last['id']))
        return
    if action in ("relay_menu", "relay_cancel"):
        context.user_data.pop("relay_pending", None)
        await query.edit_message_text(
            "🔧 التحكم بالوقود عبر GPSCJ\nيلزم ريليه فصل مركب ومتوافق. القطع متاح فقط عند تأكيد توقف المركبة وإطفاء المحرك ببيانات حديثة. لا تحرك المركبة حتى التأكد من نتيجة الأمر؛ قد تتأخر المنصة في تنفيذه.",
            reply_markup=_relay_keyboard(context.user_data.get('relay_last_command', {}).get('id')),
        )
        return
    if action in ("relay_cut", "relay_restore"):
        context.user_data.pop("relay_pending", None)
        kind = action.removeprefix("relay_")
        await query.edit_message_text("⏳ جارٍ التحقق من الجهاز وحالته...")
        try:
            target = await asyncio.to_thread(gps_commands.relay_command, kind)
        except Exception as exc:
            text = str(exc) if isinstance(exc, gps_commands.CommandError) else "تعذر التحقق من المنصة؛ لم يُرسل أمر."
            await query.edit_message_text(text, reply_markup=_relay_keyboard())
            return
        nonce = secrets.token_hex(8)
        context.user_data["relay_pending"] = dict(kind=kind, target=target, nonce=nonce, expires=loop.time()+60)
        label = "قطع الوقود" if kind == "cut" else "إعادة الوقود"
        details = (
            "تأكد من أن المركبة متوقفة بأمان وأن ريليه الفصل مركب. يُعاد فحص الحالة قبل الإرسال. "
            if kind == "cut" else
            "ستُرسل إعادة الوقود دون اشتراط موقع حديث أو حالة توقف. قبول المنصة لا يضمن وصول الأمر للجهاز فورًا. "
        )
        await query.edit_message_text(
            f"تأكيد {label} للجهاز المنتهي بـ {target['sn'][-4:]} (موديل {target['model']}).\n"
            + details + "التأكيد صالح لمدة دقيقة.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"تأكيد {label}", callback_data=f"relay_confirm:{nonce}")],
                [InlineKeyboardButton("إلغاء", callback_data="relay_cancel")],
            ]),
        )
        return
    if action.startswith("relay_confirm:"):
        pending = context.user_data.pop("relay_pending", None)
        if not pending or action != f"relay_confirm:{pending['nonce']}" or loop.time() > pending['expires']:
            await query.edit_message_text("التأكيد منتهي أو مستخدم. افتح طلبًا جديدًا.", reply_markup=_relay_keyboard())
            return
        lock = context.application.bot_data.setdefault("relay_lock", asyncio.Lock())
        if lock.locked():
            await query.edit_message_text("يوجد أمر قيد المعالجة؛ لم يُرسل طلبك.", reply_markup=_relay_keyboard())
            return
        async with lock:
            await query.edit_message_text("⏳ جارٍ إعادة التحقق وإرسال الأمر مرة واحدة...")
            try:
                command_id = await asyncio.to_thread(gps_commands.relay_command, pending['kind'], pending['target'])
                context.user_data['relay_last_command'] = {'id': command_id, 'target': pending['target']}
                text = f"استلمت GPSCJ الأمر رقم {command_id}. هذا قبول للإرسال وليس تأكيدًا لتنفيذ القطع أو الإعادة. تحقق من نتيجة الأمر في المنصة قبل تحريك المركبة أو تكراره."
            except Exception as exc:
                text = str(exc) if isinstance(exc, gps_commands.CommandError) else "تعذر إتمام الطلب. تحقق من سجل أوامر GPSCJ قبل إعادة المحاولة."
            await query.edit_message_text(text, reply_markup=_relay_keyboard(context.user_data.get('relay_last_command', {}).get('id')))


def _live_location_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹️ إيقاف الموقع المتجدد", callback_data="live_location_stop")],
        [InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="menu")],
    ])


async def _live_location_monitor(context, state):
    loop = asyncio.get_running_loop()
    delay = LIVE_LOCATION_INTERVAL
    try:
        while True:
            remaining = state["deadline"] - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(delay, remaining))
            if loop.time() >= state["deadline"]:
                return
            delay = LIVE_LOCATION_INTERVAL
            try:
                tracking = await asyncio.to_thread(_fetch_health_tracking)
                if loop.time() >= state["deadline"]:
                    return
                lat, lng = tracking.latitude, tracking.longitude
                if not gps.valid_coordinates(lat, lng):
                    continue
                if (lat, lng) == state["coordinates"]:
                    continue
                await context.bot.edit_message_live_location(
                    chat_id=state["chat_id"], message_id=state["message_id"],
                    latitude=lat, longitude=lng, reply_markup=_live_location_keyboard(),
                )
                state["coordinates"] = (lat, lng)
            except Forbidden:
                return
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.warning("تعذر تحديث رسالة الموقع المباشر: %s", type(exc).__name__)
                    return
            except RetryAfter as exc:
                retry = exc.retry_after
                delay = max(delay, retry.total_seconds() if hasattr(retry, "total_seconds") else retry)
            except Exception as exc:
                logger.warning("تعذر تحديث الموقع المتجدد؛ ستعاد المحاولة: %s", type(exc).__name__)
    finally:
        with suppress(TelegramError):
            await context.bot.stop_message_live_location(
                chat_id=state["chat_id"], message_id=state["message_id"],
            )
            state["stopped"] = True
        if context.user_data.get("live_location") is state:
            context.user_data["live_location"] = None


async def _live_location_handler(update, context, action):
    query = _CallbackEditor(update.callback_query)
    active = context.user_data.get("live_location")
    if action == "live_location_stop":
        if active:
            task = active["task"]
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            # Also handles cancellation before the monitor's first execution.
            try:
                if not active.get("stopped"):
                    await context.bot.stop_message_live_location(chat_id=active["chat_id"], message_id=active["message_id"])
            except BadRequest:
                pass  # A finished live location is already stopped.
            except TelegramError:
                await query.edit_message_text("توقفت التحديثات محليًا، لكن تعذر تأكيد الإيقاف في تيليغرام. تنتهي المشاركة تلقائيًا بعد ساعة.", reply_markup=main_menu_keyboard())
                context.user_data["live_location"] = None
                return
            context.user_data["live_location"] = None
        await query.edit_message_text("⏹️ تم إيقاف الموقع المتجدد.", reply_markup=main_menu_keyboard())
        return
    if active:
        await query.edit_message_text("📍 مشاركة الموقع المتجدد قيد العمل. يُفحص الموقع كل 15 ثانية وتستمر المشاركة ساعة من بدء تشغيلها.", reply_markup=_live_location_keyboard())
        return
    if action != "live_location_start":
        await query.edit_message_text(
            "📍 موقع مباشر متجدد لمدة ساعة\nيُفحص موقع الجهاز كل 15 ثانية وتُحدّث نفس رسالة الموقع عند تغير الإحداثيات. دقة التتبع تعتمد على تحديثات جهاز GPS للخادم.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ بدء المشاركة لمدة ساعة", callback_data="live_location_start")],
                [InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="menu")],
            ]),
        )
        return
    await query.edit_message_text("⏳ جاري بدء الموقع المتجدد...")
    try:
        tracking = await asyncio.to_thread(_fetch_health_tracking)
        lat, lng = tracking.latitude, tracking.longitude
        if not gps.valid_coordinates(lat, lng):
            await query.edit_message_text("لا تتوفر إحداثيات صالحة حاليًا.", reply_markup=main_menu_keyboard())
            return
        deadline = asyncio.get_running_loop().time() + LIVE_LOCATION_PERIOD
        message = await context.bot.send_location(
            chat_id=update.effective_chat.id, latitude=lat, longitude=lng,
            live_period=LIVE_LOCATION_PERIOD, reply_markup=_live_location_keyboard(),
        )
    except Exception as exc:
        logger.warning("تعذر بدء الموقع المتجدد: %s", type(exc).__name__)
        await query.edit_message_text(_ERROR_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
        return
    state = {"chat_id": update.effective_chat.id, "message_id": message.message_id,
             "coordinates": (lat, lng), "deadline": deadline}
    context.user_data["live_location"] = state
    task = asyncio.create_task(_live_location_monitor(context, state))
    state["task"] = task
    tasks = context.application.bot_data.setdefault("geofence_tasks", set())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    await query.edit_message_text(
        f"✅ بدأت مشاركة الموقع لمدة ساعة، مع فحص كل 15 ثانية.\nوقت آخر موقع عند البدء (UTC): {_fmt_time(tracking.device_utc_date)}",
        parse_mode=ParseMode.HTML, reply_markup=_live_location_keyboard(),
    )


async def _map_live(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)

    await query.edit_message_text(
        "⏳ جاري جلب الموقع وجمع بيانات الخريطة..."
    )

    try:
        lat, lng = await asyncio.to_thread(_fetch_tracking)
    except Exception as exc:
        logger.exception("فشل جلب الموقع للتتبع المباشر")
        await query.edit_message_text(
            f"{_ERROR_TEXT}\n\n({type(exc).__name__})",
            parse_mode=ParseMode.HTML,
            reply_markup=result_keyboard(),
        )
        return

    if lat is None or lng is None:
        await query.edit_message_text(
            "⚠️ لا تتوفر إحداثيات حالية للجهاز.",
            parse_mode=ParseMode.HTML,
            reply_markup=result_keyboard(),
        )
        return

    chat_id = update.effective_chat.id

    await context.bot.send_location(
        chat_id,
        latitude=lat,
        longitude=lng,
    )

    temporary = tempfile.TemporaryDirectory(prefix="gps_live_")
    path = Path(temporary.name) / "map.png"

    try:
        await asyncio.to_thread(
            _render_point_map,
            lat,
            lng,
            str(path),
        )

        with path.open("rb") as photo:
            await context.bot.send_photo(
                chat_id,
                photo,
                caption=_live_map_caption(lat, lng),
                parse_mode=ParseMode.HTML,
                reply_markup=result_keyboard(),
            )
    except Exception as exc:
        logger.warning("فشل رسم خريطة الموقع المباشر: %s", exc)
        await context.bot.send_message(
            chat_id,
            _live_map_caption(lat, lng),
            parse_mode=ParseMode.HTML,
            reply_markup=result_keyboard(),
        )
    finally:
        temporary.cleanup()
    await query.edit_message_text("✅ تم عرض الموقع الحالي.", reply_markup=result_keyboard())


def _history_export_keyboard(date_str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 تصدير CSV", callback_data=f"history_export:csv:{date_str}"),
         InlineKeyboardButton("🗺️ تصدير GPX", callback_data=f"history_export:gpx:{date_str}")],
        [InlineKeyboardButton("📅 اختيار يوم آخر", callback_data="history")],
        [InlineKeyboardButton("↩️ القائمة الرئيسية", callback_data="menu")],
    ])


def _export_timestamp(value):
    if not value:
        return ""
    try:
        # Preserve supplied timezone; never label an unspecified zone as UTC.
        if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", str(value)):
            return ""
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except (ValueError, TypeError):
        return ""


def _serialize_trip(points, date_str, file_format):
    points = [p for p in points if gps.valid_coordinates(p.latitude, p.longitude)]
    if not points:
        raise gps.HistoryError("لا توجد نقاط صالحة للتصدير.")
    if file_format == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["timestamp", "latitude", "longitude", "speed_kmh"])
        for point in points:
            speed = point.speed
            writer.writerow([_export_timestamp(point.timestamp), point.latitude, point.longitude,
                             speed if speed is not None and isfinite(speed) and speed >= 0 else ""])
        return output.getvalue().encode("utf-8-sig")
    if file_format != "gpx":
        raise ValueError("Unsupported export format")
    root = ET.Element("gpx", xmlns="http://www.topografix.com/GPX/1/1", version="1.1", creator="GPS Tracker")
    track = ET.SubElement(root, "trk")
    ET.SubElement(track, "name").text = f"Trip {date_str}"
    segment = ET.SubElement(track, "trkseg")
    for point in points:
        node = ET.SubElement(segment, "trkpt", lat=str(point.latitude), lon=str(point.longitude))
        timestamp = _export_timestamp(point.timestamp)
        if timestamp:
            ET.SubElement(node, "time").text = timestamp
        if point.speed is not None and isfinite(point.speed) and point.speed >= 0:
            ET.SubElement(node, "desc").text = f"Speed: {point.speed:g} km/h"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


async def _export_history(update, context, action):
    query = _CallbackEditor(update.callback_query)
    match = re.fullmatch(r"history_export:(csv|gpx):(\d{4}-\d{2}-\d{2})", action)
    if not match:
        await query.edit_message_text("طلب تصدير غير صالح.", reply_markup=history_menu_keyboard())
        return
    file_format, date_str = match.groups()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await query.edit_message_text("تاريخ غير صالح.", reply_markup=history_menu_keyboard())
        return
    markup = _history_export_keyboard(date_str)
    await query.edit_message_text("⏳ جاري تجهيز ملف الرحلة...", reply_markup=markup)
    try:
        _, _, points = await asyncio.to_thread(_history_work, date_str)
        payload = await asyncio.to_thread(_serialize_trip, points, date_str, file_format)
        with io.BytesIO(payload) as document:
            await context.bot.send_document(
                chat_id=update.effective_chat.id, document=document,
                filename=f"trip_{date_str}.{file_format}",
                caption=f"سجل الحركة ليوم {date_str} — {file_format.upper()}",
            )
    except Exception as exc:
        logger.warning("تعذر تصدير الرحلة: %s", type(exc).__name__)
        message = str(exc) if isinstance(exc, gps.HistoryError) else "تعذر تصدير الرحلة. أعد المحاولة لاحقًا."
        await query.edit_message_text(message, reply_markup=markup)
        return
    await query.edit_message_text("✅ تم إرسال ملف الرحلة.", reply_markup=markup)


async def _history_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)
    context.user_data["history_state"] = None

    await query.edit_message_text(
        "📅 <b>سجل حركة المركبة</b>\n\n"
        "اختر اليوم المطلوب، أو أدخل تاريخاً مخصصاً بالصيغة "
        "<code>YYYY-MM-DD</code>:",
        parse_mode=ParseMode.HTML,
        reply_markup=history_menu_keyboard(),
    )


async def _history_day(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)
    request = query.data.split(":", 1)[1]
    await _show_history(update, context, request)


async def _history_custom(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = _CallbackEditor(update.callback_query)
    context.user_data["history_state"] = "awaiting_date"

    await query.edit_message_text(
        "✍️ <b>تاريخ مخصص</b>\n\n"
        "أرسل الآن التاريخ المطلوب بالصيغة:\n"
        "<code>YYYY-MM-DD</code>\n\n"
        "مثال: <code>2026-08-13</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=history_menu_keyboard(),
    )


async def _show_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request: str,
    *,
    from_message: bool = False,
) -> None:
    context.user_data["current_view"] = "history"
    context.user_data["history_request"] = request
    chat_id = update.effective_chat.id
    query = _CallbackEditor(update.callback_query) if not from_message else None

    if from_message:
        loading_message = await update.message.reply_text(
            "⏳ جاري تحميل سجل الحركة من الخادم..."
        )
    else:
        await query.edit_message_text(
            "⏳ جاري تحميل سجل الحركة من الخادم..."
        )

    async def _send_or_edit(text: str, markup: InlineKeyboardMarkup) -> None:
        if from_message:
            await loading_message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        else:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )

    try:
        device, date_str, points = await asyncio.to_thread(
            _history_work,
            request,
        )
    except Exception as exc:
        logger.exception("فشل جلب سجل الحركة")
        text = f"⚠️ {_esc(str(exc))}" if isinstance(exc, gps.HistoryError) else _ERROR_TEXT
        await _send_or_edit(text, result_keyboard())
        return

    if not points:
        await _send_or_edit(
            "🗓️ <b>لا توجد بيانات حركة</b> "
            f"في تاريخ {_esc(date_str)}.\n\n"
            "قد لا يكون الجهاز قد أرسل بيانات في ذلك اليوم.",
            history_menu_keyboard(),
        )
        return

    temporary = tempfile.TemporaryDirectory(prefix="gps_history_")
    path = Path(temporary.name) / "map.png"

    try:
        await asyncio.to_thread(
            _render_route_map,
            points,
            str(path),
        )

        caption = _history_caption(device, date_str, points)

        with path.open("rb") as photo:
            await context.bot.send_photo(
                chat_id,
                photo,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=_history_export_keyboard(date_str),
            )
        await _send_or_edit("✅ تم عرض سجل الحركة.", _history_export_keyboard(date_str))
    except Exception as exc:
        logger.exception("فشل رسم خريطة سجل الحركة")
        await _send_or_edit(
            f"{_ERROR_TEXT}\n\n({type(exc).__name__})",
            _history_export_keyboard(date_str),
        )
    finally:
        temporary.cleanup()


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """يعالج الإحداثيات أو التاريخ المدخل يدوياً أثناء انتظارها فقط."""
    if not await _authorize(update, context):
        return
    kind = context.user_data.get("limit_input")
    if kind in _LIMIT_LABELS:
        await _limit_input(update, context, kind)
        return
    if context.user_data.get("history_state") == "awaiting_date":
        text = update.message.text.strip()

        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text(
                "⚠️ صيغة التاريخ غير صحيحة.\n"
                "استخدم الصيغة التالية:\n"
                "<code>YYYY-MM-DD</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        context.user_data["history_state"] = None
        await _show_history(
            update,
            context,
            parsed.strftime("%Y-%m-%d"),
            from_message=True,
        )
        return

    if context.user_data.get("geofence_state") != "awaiting_coords":
        return

    text = update.message.text

    match = re.fullmatch(
        r"\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*[,،]\s*"
        r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*",
        text,
    )

    if match is None:
        await update.message.reply_text(
            "⚠️ الصيغة غير صحيحة.\n"
            "أرسل الإحداثيات هكذا:\n"
            "<code>33.573110, -7.589843</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    lat = float(match.group(1))
    lng = float(match.group(2))

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        await update.message.reply_text(
            "⚠️ الإحداثيات خارج النطاق الصحيح.\n"
            "خط العرض بين -90 و 90، وخط الطول بين -180 و 180.",
            parse_mode=ParseMode.HTML,
        )
        return

    context.user_data["geofence_center"] = (lat, lng)
    context.user_data["geofence_state"] = "awaiting_radius"

    await update.message.reply_text(
        "📍 مركز المنطقة:\n"
        f"<code>{lat:.6f}, {lng:.6f}</code>\n\n"
        "اختر هامش الخطأ (نصف القطر) بالمتر:",
        parse_mode=ParseMode.HTML,
        reply_markup=radius_keyboard(),
    )


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    gps.validate_configuration()
    allowed_user_ids = _allowed_user_ids()
    if not allowed_user_ids:
        logger.warning(
            "ALLOWED_USER_IDS غير مضبوط: وضع الإعداد فقط. أرسل /id إلى البوت في الخاص، "
            "ثم أضف معرّفك إلى .env وأعد التشغيل. بيانات المركبة محجوبة."
        )

    if not BOT_TOKEN:
        raise RuntimeError(
            "ضع BOT_TOKEN في ملف .env أو في متغير BOT_TOKEN أعلى الكود."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_stop(_shutdown_geofences)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", show_user_id))
    application.bot_data["allowed_user_ids"] = allowed_user_ids
    application.add_error_handler(_error_handler)
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    logger.info("البوت يعمل الآن... اضغط Ctrl+C للإيقاف.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
