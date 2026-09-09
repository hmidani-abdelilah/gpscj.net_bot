"""GPSCJ relay commands, verified against JS/Map.js?v=202601519 (2026-09-09).

Only explicit supported model mappings; no arbitrary command passthrough.
"""
import json
import re
import threading
from datetime import datetime, timezone
from requests.adapters import HTTPAdapter
import gps


class CommandError(RuntimeError):
    pass


class CommandUncertain(CommandError):
    pass


_MODELS = {}
for models, pair in [
    ((12, 5033, 5011, 18, 15, 16, 17, 26, 89, 41, 43, 21, 62, 63, 64, 65, 66, 28, 68, 69, 1400), ('DYD', 'HFYD')),
    ((60, 61, 100), ('BP030', 'BP040')),
    ((25,), ('C001ON', 'C001OFF')),
    ((50, 83), ('AV010', 'AV011')),
    ((94,), ('109', '110')),
    ((51, 52), ('41141', '41140')),
    # clkShowMoreMenu explicitly enables these relay commands for model 178.
    ((178,), ('S201', 'S200')),
]:
    for model in models:
        _MODELS[model] = pair


def validate_tracking(tracking, action, now=None):
    if action not in ('cut', 'restore'):
        raise CommandError('أمر غير مدعوم.')
    # Restoration must remain available even when location/ACC is unavailable.
    if action == 'restore':
        return
    if str(tracking.status or '').lower() not in ('stop', 'move', 'speed'):
        raise CommandError('الجهاز غير متصل أو حالة اتصاله غير مؤكدة؛ لم يُرسل الأمر.')
    try:
        timestamp = datetime.fromisoformat(str(tracking.device_utc_date).replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
    except (TypeError, ValueError, OverflowError):
        raise CommandError('وقت آخر موقع مفقود أو غير صالح؛ تعذر التحقق من حداثة البيانات ولم يُرسل الأمر.') from None
    if age < 0:
        raise CommandError('وقت آخر موقع يقع في المستقبل؛ تحقق من توقيت الجهاز والخادم. لم يُرسل الأمر.')
    if age > 60:
        minutes, seconds = divmod(int(age), 60)
        raise CommandError(
            f'آخر موقع مسجل منذ {minutes} دقيقة و{seconds} ثانية '
            f'({timestamp.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC).\n'
            'لم يُرسل الأمر: يلزم موقع خلال آخر 60 ثانية للتحقق من حالة المركبة. '
            'ظهور «متوقف» وحده لا يؤكد حداثة البيانات. '
            'انتظر تحديث الجهاز في GPSCJ ثم أعد المحاولة؛ تحديث شاشة البوت يجلب البيانات المتاحة فقط.'
        )
    if action == 'cut':
        hardware = tracking.data_context
        if (tracking.speed != 0 or tracking.is_stop is not True or
                str(tracking.status).lower() != 'stop' or not hardware or hardware.acc != 0):
            raise CommandError('القطع ممنوع: يجب تأكيد توقف المركبة، سرعة صفر، وإطفاء المحرك من بيانات حديثة.')


def _post(app, path, payload):
    response = app.transport.session.post(
        gps.BASE_URL + path, data=json.dumps(payload),
        headers=app.transport.headers(referer=gps.BASE_URL + '/Monitor.aspx', ajax=True),
        timeout=(5, 15), allow_redirects=False,
    )
    if response.status_code != 200:
        raise CommandError('رفضت المنصة الطلب أو انتهت الجلسة.')
    return response.json()


def _integer(value):
    """Protocol identifiers must be integers, never rounded floats or booleans."""
    if isinstance(value, bool) or not re.fullmatch(r'-?\d+', str(value)):
        raise CommandError('استجابة المنصة تحتوي رقمًا غير صالح.')
    return int(value)


def _target(app):
    device = app.discovery.discover()
    page = app.transport.get('/Monitor.aspx')
    if page.status_code != 200:
        raise CommandError('تعذر قراءة جلسة الأوامر؛ لم يُرسل الأمر.')
    current = gps.HtmlParser.extract_device_info(page.text)
    if current.device_id != device.device_id or current.user_id != device.user_id:
        raise CommandError('تغيرت جلسة الجهاز أثناء التحقق؛ أعد المحاولة.')
    login_type = _integer(gps.HtmlParser.extract_input_value(page.text, 'hidLoginType'))
    # Monitor.aspx uses 2 for IMEI login; forward it unchanged to the API.
    if (login_type not in (0, 1, 2) or device.user_id is None or
            device.device_id is None or device.user_id <= 0 or device.device_id <= 0):
        raise CommandError('تعذر تحديد جلسة الأوامر لدى المنصة.')
    payload = gps.decode_payload(_post(app, '/Ajax/DevicesAjax.asmx/GetDevicesByUserID', {
        'UserID': device.user_id, 'isFirst': True, 'TimeZones': '0', 'DeviceID': device.device_id,
    }))
    rows = payload.get('devices', []) if isinstance(payload, dict) else []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CommandError('قائمة أجهزة المنصة غير صالحة؛ لم يُرسل الأمر.')
    matches = [row for row in rows if str(row.get('id')) == str(device.device_id) and str(row.get('sn')) == gps.DEVICE_IMEI]
    if len(matches) != 1:
        raise CommandError('تعذر مطابقة الجهاز المستهدف؛ لم يُرسل أي أمر.')
    model = _integer(matches[0].get('model'))
    if model not in _MODELS:
        raise CommandError(f'موديل الجهاز ({model}) لا يملك أوامر قطع موثقة في الربط الحالي. يلزم إضافة أوامره من المنصة.')
    return {'device_id': device.device_id, 'user_id': device.user_id, 'model': model, 'sn': gps.DEVICE_IMEI, 'login_type': login_type}


def _relay_command(action, expected=None):
    """Without expected: read-only preview. With expected: validate and send once."""
    if action not in ('cut', 'restore'):
        raise CommandError('أمر غير مدعوم.')
    with gps.GPSCJApplication(imei=gps.DEVICE_IMEI, password=gps.DEVICE_PASSWORD, email=gps.EMAIL) as app:
        # Never retry command POSTs, including HTTP status failures.
        app.transport.session.mount('https://', HTTPAdapter(max_retries=0))
        app.auth.authenticate()
        target = _target(app)
        if expected is not None and target != expected:
            raise CommandError('تغير الجهاز أو الجلسة منذ التأكيد؛ أعد فتح طلب الأمر.')
        if expected is not None:
            valid = _post(app, '/Ajax/UsersAjax.asmx/ValidPassword', {
                'UserID': target['user_id'], 'DeviceID': target['device_id'],
                'Pass': gps.DEVICE_PASSWORD, 'LoginType': target['login_type'],
            })
            if not isinstance(valid, dict) or str(valid.get('d')) != '1':
                raise CommandError('رفضت المنصة كلمة مرور الأوامر؛ لم يُرسل الأمر.')
        if action == 'cut':
            tracking = app.tracking.get_tracking(target['device_id'], '0')
            validate_tracking(tracking, action)
        if expected is None:
            return target
        command = _MODELS[target['model']][0 if action == 'cut' else 1]
        try:
            result = _post(app, '/Ajax/CommandQueueAjax.asmx/SendCommand', {
                'SN': target['sn'], 'DeviceID': target['device_id'], 'CommandType': command,
                'TrueOrFalse': '0', 'Model': 50 if target['model'] == 83 else target['model'],
                'UserID': target['user_id'], 'Pass': gps.DEVICE_PASSWORD, 'LoginType': target['login_type'],
            })
            command_id = _integer(result['d'])
        except Exception:
            raise CommandUncertain('نتيجة الإرسال غير مؤكدة. لا تكرر الأمر؛ تحقق من سجل أوامر GPSCJ أولًا.') from None
        if command_id <= 10:
            raise CommandError(f'لم تقبل المنصة الأمر (رمز {command_id}).')
        return command_id


def command_response(command_id, expected):
    """Read the platform's reply for a previously accepted command; never send."""
    command_id = _integer(command_id)
    if command_id <= 10:
        raise CommandError('رقم الأمر غير صالح.')
    with gps.GPSCJApplication(imei=gps.DEVICE_IMEI, password=gps.DEVICE_PASSWORD, email=gps.EMAIL) as app:
        app.auth.authenticate()
        if _target(app) != expected:
            raise CommandError('تغير الجهاز أو الحساب؛ تعذر قراءة رد الأمر.')
        result = _post(app, '/Ajax/CommandQueueAjax.asmx/GetResponse', {
            'CommandID': command_id, 'TimeZones': '0',
        })
        if not isinstance(result, dict) or 'd' not in result:
            raise CommandError('استجابة سجل الأوامر غير صالحة.')
        reply = result['d']
        if reply is None or (isinstance(reply, str) and reply.strip().lower() in ('', 'null')):
            return None
        if not isinstance(reply, str):
            raise CommandError('صيغة رد الجهاز غير متوقعة.')
        return reply


_COMMAND_LOCK = threading.Lock()


def relay_command(action, expected=None):
    # Retain exclusivity even if the asyncio caller is cancelled while this thread runs.
    if not _COMMAND_LOCK.acquire(blocking=False):
        raise CommandError('يوجد طلب تحكم قيد المعالجة؛ لم يُرسل طلب جديد.')
    try:
        return _relay_command(action, expected)
    finally:
        _COMMAND_LOCK.release()
