"""Persistent weekly schedules. No network work occurs in the scheduling engine."""
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ACTIONS = {
    'location': '📍 الموقع الحالي', 'context': '📡 بيانات الاتصال',
    'device': '🏠 معلومات الجهاز', 'full': '📄 التقرير الكامل',
    'map_live': '🗺️ خريطة الموقع', 'history': '📅 سجل حركة اليوم',
    'health': '📡 تنبيهات الكهرباء والاتصال', 'speed': '🏎️ تنبيه السرعة',
    'battery': '🔋 تنبيه جهد البطارية', 'geofence': '🚨 تنبيه مغادرة المنطقة',
    'live_location': '📍 موقع متجدد', 'cut': '⛔ قطع الوقود', 'restore': '✅ إعادة الوقود',
}
CONTINUOUS = {'health', 'speed', 'battery', 'geofence', 'live_location'}
DAYS = ('الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد')


def parse_time(value):
    parts = value.strip().split(':')
    if len(parts) != 2 or not all(p.isdecimal() for p in parts):
        raise ValueError('استخدم HH:MM مثل 08:30.')
    hour, minute = map(int, parts)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError('الوقت خارج النطاق الصحيح.')
    return hour * 60 + minute


def time_text(minutes):
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def window(schedule, now):
    local = now.astimezone(ZoneInfo(schedule['zone']))
    for date in (local.date(), local.date() - timedelta(days=1)):
        if date.weekday() not in schedule['days']:
            continue
        start = datetime.combine(date, datetime.min.time(), local.tzinfo) + timedelta(minutes=schedule['start'])
        end = datetime.combine(date, datetime.min.time(), local.tzinfo) + timedelta(minutes=schedule['end'])
        if schedule['end'] < schedule['start']:
            end += timedelta(days=1)
        if start.timestamp() <= now.timestamp() < end.timestamp():
            return start, end
    return None


def slot(schedule, now):
    bounds = window(schedule, now)
    if bounds is None:
        return None
    start, _ = bounds
    index = 0 if schedule['action'] in {'cut', 'restore'} or schedule['interval'] == 0 else int((now.timestamp() - start.timestamp()) // (schedule['interval'] * 60))
    return f'{start.date()}:{index}'


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, spec TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, last_slot TEXT, status TEXT NOT NULL DEFAULT "لم ينفذ بعد")')
        self.db.commit()

    def add(self, owner, spec):
        with self.db:
            return self.db.execute('INSERT INTO schedules(owner,spec) VALUES (?,?)', (owner, json.dumps(spec))).lastrowid

    def rows(self, owner=None):
        query = 'SELECT id,owner,spec,enabled,last_slot,status FROM schedules'
        args = () if owner is None else (owner,)
        if owner is not None:
            query += ' WHERE owner=?'
        return [dict(json.loads(spec), id=id_, owner=user, enabled=bool(enabled), last_slot=last, status=status) for id_, user, spec, enabled, last, status in self.db.execute(query, args)]

    def claim(self, id_, key):
        with self.db:
            return self.db.execute('UPDATE schedules SET last_slot=?,status=? WHERE id=? AND enabled=1 AND (last_slot IS NULL OR last_slot<>?)', (key, 'بدأ التنفيذ', id_, key)).rowcount == 1

    def status(self, id_, value):
        with self.db:
            self.db.execute('UPDATE schedules SET status=? WHERE id=?', (value, id_))

    def change(self, owner, id_, delete=False):
        with self.db:
            if delete:
                self.db.execute('DELETE FROM schedules WHERE id=? AND owner=?', (id_, owner))
            else:
                self.db.execute('UPDATE schedules SET enabled=1-enabled WHERE id=? AND owner=?', (id_, owner))

    def disable(self, id_):
        with self.db:
            self.db.execute('UPDATE schedules SET enabled=0 WHERE id=?', (id_,))

    def close(self):
        self.db.close()
