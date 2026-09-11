"""Telegram schedule editor and isolated execution of existing bot features."""
import asyncio
import logging
import math
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import Forbidden, RetryAfter, TelegramError

import gps_commands
import gps
from task_scheduler import ACTIONS, CONTINUOUS, DAYS, Store, parse_time, time_text, window, slot

logger = logging.getLogger(__name__)


def controls(id_=None):
    rows = [] if id_ is None else [[Button('⏹ تعطيل هذا الجدول', callback_data=f'sched:pause:{id_}')]]
    return Markup(rows + [[Button('⏰ مجدول المهام', callback_data='sched:menu')]])


def summary(row):
    days = 'كل الأيام' if len(row['days']) == 7 else '، '.join(DAYS[d] for d in sorted(row['days']))
    mode = 'طوال الفترة' if row['action'] in CONTINUOUS else ('مرة في الفترة' if not row['interval'] else f"كل {row['interval']} دقيقة")
    params = ''
    if 'threshold' in row:
        params = f"\nالحد: {row['threshold']:g} {'كم/ساعة' if row['action'] == 'speed' else 'فولت'}"
    if 'center' in row:
        params = f"\nالمركز: {row['center']} — نصف القطر: {row['radius']:g} متر"
    return f"{ACTIONS[row['action']]}\n{time_text(row['start'])} ← {time_text(row['end'])} ({row['zone']})\n{days} — {mode}{params}"


def region_keyboard(context):
    rows = [[Button('📍 استخدام الموقع الحالي', callback_data='sched:use_current')]]
    if context.user_data.get('geofence'):
        rows.append([Button('استخدام المنطقة المفعلة حاليًا', callback_data='sched:use_region')])
    return Markup(rows + [[Button('إلغاء', callback_data='sched:menu')]])


async def handler(update, context, query, host):
    store = context.bot_data['schedule_store']
    owner = update.effective_user.id
    data = query.data.split(':')
    command = data[1]
    draft = context.user_data.get('schedule_draft')
    if command in ('toggle', 'delete', 'pause') and len(data) == 3 and data[2].isdigit():
        id_ = int(data[2])
        owned = next((r for r in store.rows(owner) if r['id'] == id_), None)
        if owned:
            if command == 'pause':
                store.disable(id_)
            else:
                store.change(owner, id_, delete=command == 'delete')
            task = context.bot_data.get('schedule_jobs', {}).get(id_)
            if task and (command != 'toggle' or owned['enabled']):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        command = 'menu'
    if command == 'menu':
        context.user_data.pop('schedule_draft', None)
        rows = store.rows(owner)
        buttons = [[Button('➕ إضافة جدول', callback_data='sched:new')]]
        lines = ['⏰ مجدول المهام', 'لا توجد جداول.' if not rows else '']
        for row in rows:
            lines.append(f"#{row['id']} {'✅' if row['enabled'] else '⏸'} {summary(row)}\nآخر حالة: {row['status']}")
            buttons.append([Button(f"#{row['id']} {'تعطيل' if row['enabled'] else 'تفعيل'}", callback_data=f"sched:toggle:{row['id']}"), Button('حذف', callback_data=f"sched:delete:{row['id']}")])
        buttons.append([Button('↩️ القائمة الرئيسية', callback_data='menu')])
        await query.edit_message_text('\n\n'.join(lines), reply_markup=Markup(buttons))
    elif command == 'new':
        if len(store.rows(owner)) >= 8:
            await query.edit_message_text('الحد الأقصى 8 جداول. احذف جدولًا لإضافة آخر.', reply_markup=controls())
            return
        context.user_data['schedule_draft'] = {'zone': context.bot_data['schedule_zone'], 'days': list(range(7))}
        await query.edit_message_text('اختر المهمة التي ستنفذ تلقائيًا:', reply_markup=Markup([[Button(label, callback_data=f'sched:action:{key}')] for key, label in ACTIONS.items()] + [[Button('إلغاء', callback_data='sched:menu')]]))
    elif command == 'action' and draft is not None and len(data) == 3 and data[2] in ACTIONS:
        draft['action'] = data[2]
        draft['step'] = 'times'
        await query.edit_message_text('أرسل وقت البداية والنهاية بنظام 24 ساعة، مثل:\n08:00 18:00\nإذا كانت النهاية أصغر من البداية تنتهي الفترة في اليوم التالي.\nالتوقيت: ' + draft['zone'], reply_markup=controls())
    elif command == 'day' and draft and draft.get('step') == 'days':
        if data[-1] == 'all':
            draft['days'] = list(range(7))
        elif data[-1].isdigit() and 0 <= int(data[-1]) <= 6:
            day = int(data[-1])
            if day in draft['days']:
                draft['days'].remove(day)
            else:
                draft['days'].append(day)
        await days_prompt(query.edit_message_text, draft)
    elif command == 'days_done' and draft and draft.get('step') == 'days':
        if not draft['days']:
            await query.edit_message_text('اختر يومًا واحدًا على الأقل.', reply_markup=days_keyboard(draft))
            return
        action = draft['action']
        if action in ('speed', 'battery', 'geofence'):
            draft['step'] = 'params'
            prompt = 'أرسل خط العرض ثم خط الطول ثم نصف القطر بالمتر، مفصولة بمسافات:\n33.573110 -7.589843 500' if action == 'geofence' else f"أرسل الحد بوحدة {'كم/ساعة (حتى 300)' if action == 'speed' else 'فولت (حتى 100)'}، أكبر من صفر."
            markup = controls()
            if action == 'geofence':
                prompt += '\nأو اضغط «📍 استخدام الموقع الحالي» لجلب موقع جهاز المركبة واختيار نصف القطر.'
                markup = region_keyboard(context)
            await query.edit_message_text(prompt, reply_markup=markup)
        else:
            await interval_prompt(query.edit_message_text, draft)
    elif command == 'use_current' and draft and draft.get('step') == 'params' and draft.get('action') == 'geofence':
        await query.edit_message_text('⏳ جاري جلب موقع المركبة...')
        try:
            lat, lng = await asyncio.to_thread(host._fetch_tracking)
        except Exception as exc:
            logger.warning('Could not fetch scheduled region center: %s', type(exc).__name__)
            await query.edit_message_text('تعذر جلب الموقع. أعد المحاولة أو أرسل الإحداثيات ونصف القطر يدويًا.', reply_markup=region_keyboard(context))
            return
        if not gps.valid_coordinates(lat, lng):
            await query.edit_message_text('لا يتوفر موقع صالح للمركبة حاليًا. أعد المحاولة أو أرسل الإحداثيات ونصف القطر يدويًا.', reply_markup=region_keyboard(context))
            return
        draft['center'] = [lat, lng]
        draft['step'] = 'radius'
        await query.edit_message_text(
            f'📍 مركز المنطقة: {lat:.6f}, {lng:.6f}\nهذا آخر موقع متاح من جهاز المركبة، وسيبقى مركزًا ثابتًا للجدول.\nاختر نصف القطر بالمتر أو أرسله كرقم أكبر من صفر وحتى 100000:',
            reply_markup=Markup([[Button(f'{radius} متر', callback_data=f'sched:radius:{radius}')] for radius in host.RADII] + [[Button('إلغاء', callback_data='sched:menu')]]),
        )
    elif command == 'radius' and draft and draft.get('step') == 'radius' and draft.get('action') == 'geofence':
        if len(data) == 3 and data[2].isdigit() and int(data[2]) in host.RADII:
            draft['radius'] = int(data[2])
            await interval_prompt(query.edit_message_text, draft)
    elif command == 'use_region' and draft and draft.get('step') == 'params' and draft.get('action') == 'geofence':
        region = context.user_data.get('geofence')
        if region:
            draft['center'], draft['radius'] = list(region['center']), region['radius']
            await interval_prompt(query.edit_message_text, draft)
        else:
            await query.edit_message_text('لا توجد منطقة مفعلة حاليًا. أرسل الإحداثيات ونصف القطر أو استخدم الموقع الحالي.', reply_markup=region_keyboard(context))
    elif command == 'save' and draft and draft.get('step') == 'confirm':
        if len(store.rows(owner)) >= 8:
            await query.edit_message_text('الحد الأقصى 8 جداول.', reply_markup=controls())
            return
        spec = {k: v for k, v in draft.items() if k != 'step'}
        id_ = store.add(owner, spec)
        context.user_data.pop('schedule_draft', None)
        await query.edit_message_text(f'✅ حُفظ الجدول #{id_}\n{summary(spec)}\nيبدأ تلقائيًا داخل الفترة المحددة، بما فيها الفترة الحالية.', reply_markup=controls(id_))


def days_keyboard(draft):
    return Markup([[Button(('✅ ' if i in draft['days'] else '▫️ ') + name, callback_data=f'sched:day:{i}')] for i, name in enumerate(DAYS)] + [[Button('كل الأيام', callback_data='sched:day:all'), Button('متابعة', callback_data='sched:days_done')], [Button('إلغاء', callback_data='sched:menu')]])


async def days_prompt(send, draft):
    await send('اختر أيام بداية الفترة (اضغط اليوم لتحديده أو إلغائه):', reply_markup=days_keyboard(draft))


async def confirm(send, draft):
    draft['step'] = 'confirm'
    note = '\nيُنفذ أمر الوقود مرة واحدة في كل فترة، ولا يُعكس عند النهاية. فشل الأمر لا يؤدي لإعادته في الفترة نفسها. القطع يتطلب تحقق التوقف وبيانات حديثة عند التنفيذ.' if draft['action'] in ('cut', 'restore') else ''
    await send('راجع الجدول قبل تفعيله:\n' + summary(draft) + note + '\nإذا كان الوقت داخل الفترة، يبدأ بعد الحفظ مباشرة.', reply_markup=Markup([[Button('✅ تأكيد الجدولة التلقائية', callback_data='sched:save')], [Button('إلغاء', callback_data='sched:menu')]]))


async def interval_prompt(send, draft):
    if draft['action'] in CONTINUOUS or draft['action'] in ('cut', 'restore'):
        draft['interval'] = 0
        await confirm(send, draft)
    else:
        draft['step'] = 'interval'
        await send('أرسل فاصل التكرار بالدقائق من 1 إلى 1440، أو 0 للتنفيذ مرة واحدة في الفترة.', reply_markup=controls())


async def text_input(update, context):
    draft = context.user_data['schedule_draft']
    send = update.message.reply_text
    text = update.message.text.strip()
    try:
        if draft.get('step') == 'times':
            start, end = text.split()
            draft['start'], draft['end'] = parse_time(start), parse_time(end)
            if draft['start'] == draft['end']:
                raise ValueError('يجب أن يختلف وقت البداية عن النهاية.')
            draft['step'] = 'days'
            await days_prompt(send, draft)
        elif draft.get('step') == 'params':
            values = [float(p.replace('٫', '.')) for p in text.split()]
            if not all(math.isfinite(v) for v in values):
                raise ValueError('أدخل أرقامًا صالحة.')
            if draft['action'] == 'geofence':
                if len(values) != 3 or not (-90 <= values[0] <= 90 and -180 <= values[1] <= 180 and 0 < values[2] <= 100000):
                    raise ValueError('أدخل إحداثيات صالحة ونصف قطر أكبر من صفر وحتى 100000 متر.')
                draft['center'], draft['radius'] = values[:2], values[2]
            else:
                maximum = 300 if draft['action'] == 'speed' else 100
                if len(values) != 1 or not 0 < values[0] <= maximum:
                    raise ValueError(f'أدخل حدًا أكبر من صفر وحتى {maximum}.')
                draft['threshold'] = values[0]
            await interval_prompt(send, draft)
        elif draft.get('step') == 'radius':
            radius = float(text.replace('٫', '.'))
            if not math.isfinite(radius) or not 0 < radius <= 100000:
                raise ValueError('أدخل نصف قطر أكبر من صفر وحتى 100000 متر.')
            draft['radius'] = radius
            await interval_prompt(send, draft)
        elif draft.get('step') == 'interval':
            if not text.isdecimal() or not 0 <= int(text) <= 1440:
                raise ValueError('أدخل عدد دقائق صحيحًا من 0 إلى 1440.')
            draft['interval'] = int(text)
            await confirm(send, draft)
    except ValueError as exc:
        await send(f'⚠️ إدخال غير صالح: {exc}', reply_markup=controls())


class ScheduledBot:
    """Give scheduled messages their own stop control, without touching manual jobs."""
    def __init__(self, bot, row):
        self.bot, self.row = bot, row

    def __getattr__(self, name):
        method = getattr(self.bot, name)
        if name not in ('send_message', 'send_location', 'send_photo', 'edit_message_live_location'):
            return method
        async def send(*args, **kwargs):
            kwargs['reply_markup'] = controls(self.row['id'])
            return await method(*args, **kwargs)
        return send


async def execute(application, row, host):
    store = application.bot_data['schedule_store']
    bot = ScheduledBot(application.bot, row)
    context = SimpleNamespace(bot=bot, user_data={}, application=application)
    chat_id = row['owner']
    action = row['action']
    async def send(text, **kwargs):
        await bot.send_message(chat_id, text, **kwargs)
        return True
    # Existing view functions need only a message editor and a private chat ID.
    query = SimpleNamespace(message=None, edit_message_text=send)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=chat_id))
    sending_relay = False
    try:
        if action in host.VIEWS:
            text = await asyncio.to_thread(host._fetch_data, action)
            await send(text, parse_mode='HTML', disable_web_page_preview=True)
        elif action == 'map_live':
            await host._map_live(update, context)
        elif action == 'history':
            await host._show_history(update, context, 'today')
        elif action in ('health', 'speed', 'battery'):
            context.user_data[action] = {'alert': None, 'threshold': row.get('threshold')}
            await host._health_monitor(context, chat_id, action)
        elif action == 'geofence':
            context.user_data['geofence'] = {'center': row['center'], 'radius': row['radius'], 'exited': False}
            await host._geofence_monitor(context, chat_id)
        elif action == 'live_location':
            while True:
                await host._live_location_handler(update, context, 'live_location_start')
                state = context.user_data.get('live_location')
                if state:
                    await state['task']
                else:
                    await asyncio.sleep(60)
        elif action in ('cut', 'restore'):
            target = await asyncio.to_thread(gps_commands.relay_command, action)
            # Preview can take time: recheck window and authorization before sending.
            current = next((r for r in store.rows(chat_id) if r['id'] == row['id']), None)
            if not current or not current['enabled'] or chat_id not in application.bot_data['allowed_user_ids'] or not window(row, datetime.now(timezone.utc)):
                return
            sending_relay = True
            command_id = await asyncio.to_thread(gps_commands.relay_command, action, target)
            sending_relay = False
            await send(f"الجدول #{row['id']}: قبلت المنصة أمر {ACTIONS[action]} برقم {command_id}. هذا لا يؤكد تنفيذ الجهاز؛ راجع سجل أوامر المنصة.")
        store.status(row['id'], 'اكتمل التنفيذ')
    except asyncio.CancelledError:
        if sending_relay:
            store.disable(row['id'])
            store.status(row['id'], 'توقف الانتظار أثناء إرسال الأمر؛ تحقق من المنصة قبل إعادة التفعيل')
        raise
    except gps_commands.CommandUncertain as exc:
        store.disable(row['id'])
        store.status(row['id'], 'نتيجة أمر الوقود غير مؤكدة؛ عُطّل الجدول')
        with suppress(TelegramError):
            await send(f"⚠️ الجدول #{row['id']}: {exc}\nعُطّل الجدول؛ تحقق من سجل المنصة قبل إعادة تفعيله.")
    except Forbidden:
        store.disable(row['id'])
        store.status(row['id'], 'عُطّل: تعذر الوصول للمحادثة')
    except RetryAfter as exc:
        store.status(row['id'], 'تأجيل بسبب حد رسائل تيليغرام؛ لن يعاد التنفيذ الحالي')
        delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after, 'total_seconds') else exc.retry_after
        application.bot_data['schedule_cooldown'] = asyncio.get_running_loop().time() + delay
        await asyncio.sleep(delay)
    except Exception as exc:
        store.status(row['id'], 'فشل التنفيذ: ' + type(exc).__name__)
        logger.warning('Scheduled task %s failed: %s', row['id'], type(exc).__name__)
        with suppress(TelegramError):
            await send(f"⚠️ تعذر تنفيذ الجدول #{row['id']}. " + (str(exc) if isinstance(exc, gps_commands.CommandError) else 'ستتم المحاولة في الموعد التالي.'))
    finally:
        state = context.user_data.get('live_location')
        if state:
            state['task'].cancel()
            with suppress(asyncio.CancelledError):
                await state['task']
            if not state.get('stopped'):
                with suppress(TelegramError):
                    await application.bot.stop_message_live_location(chat_id=chat_id, message_id=state['message_id'])


async def tick(application, host, now=None):
    now = now or datetime.now(timezone.utc)
    store = application.bot_data['schedule_store']
    jobs = application.bot_data.setdefault('schedule_jobs', {})
    occurrences = application.bot_data.setdefault('schedule_occurrences', {})
    rows = {row['id']: row for row in store.rows()}
    for id_, task in list(jobs.items()):
        row = rows.get(id_)
        bounds = window(row, now) if row else None
        changed = bounds and occurrences.get(id_) != str(bounds[0].date())
        if not row or not row['enabled'] or row['owner'] not in application.bot_data['allowed_user_ids'] or not bounds or changed:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            jobs.pop(id_, None)
            occurrences.pop(id_, None)
    if asyncio.get_running_loop().time() < application.bot_data.get('schedule_cooldown', 0):
        return
    for id_, row in rows.items():
        if not row['enabled'] or row['owner'] not in application.bot_data['allowed_user_ids']:
            continue
        key = slot(row, now)
        if key is None:
            continue
        task = jobs.get(id_)
        if task and not task.done():
            continue
        if row['action'] in CONTINUOUS:
            # Resume monitors after process restart; never spin on a completed job.
            if task is not None:
                continue
        elif not store.claim(id_, key):
            continue
        if row['action'] in CONTINUOUS:
            store.status(id_, 'المراقبة قيد التشغيل')
        occurrences[id_] = str(window(row, now)[0].date())
        jobs[id_] = asyncio.create_task(execute(application, row, host))


async def startup(application, host):
    zone = os.getenv('SCHEDULER_TIMEZONE', 'Africa/Casablanca')
    ZoneInfo(zone)  # Fail visibly on an invalid configuration.
    application.bot_data['schedule_zone'] = zone
    application.bot_data['schedule_store'] = Store(os.getenv('SCHEDULER_DB', str(Path(__file__).with_name('schedules.sqlite3'))))
    async def loop():
        while True:
            try:
                await tick(application, host)
            except Exception as exc:
                logger.error('Scheduler tick failed: %s', type(exc).__name__)
            await asyncio.sleep(1)
    application.bot_data['schedule_loop'] = asyncio.create_task(loop())


async def shutdown(application):
    task = application.bot_data.pop('schedule_loop', None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    jobs = list(application.bot_data.get('schedule_jobs', {}).values())
    for task in jobs:
        task.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    store = application.bot_data.pop('schedule_store', None)
    if store:
        store.close()
