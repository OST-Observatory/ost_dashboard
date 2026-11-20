"""
Interface /api/v2 for OST_OCS servers (ADR 0008 of ost_ocs, description: ost_ocs/docs/schnittstellen/dashboard_v2.md).

Each OST_OCS server instance (e.g. "ost-dome", later mobile setups) pushes:
- POST /api/v2/sessions               start of a session -> sessionId
- POST /api/v2/sessions/<id>/end      end of a session (goes into the observing session log)
- POST /api/v2/status                 every 10-30 s and on changes; also the heartbeat
- POST /api/v2/preview?instanceId=..  small JPEG of the last frame (throttled by the sender)

The browser reads GET /api/v2/instances and GET /api/v2/preview/<instanceId>.jpg (dashboard login).

Unlike the old /heartbeat, a missing status does NOT end the session: the instance is shown as disturbed
("no connection since ...") and the session continues with the same sessionId when status arrives again.
Only the OST_OCS server (or later a person in the dashboard) ends a session.
"""

import os
import re
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from flask import Blueprint, abort, jsonify, request, send_from_directory

SCHEMA_VERSION = 1
INSTANCE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')
SESSION_TYPES = ('PRAKTIKUM', 'BA', 'MA', 'OUTREACH', 'ASTRO', 'TECH', 'BACKGRND')
END_REASONS = ('normal', 'timeout', 'crash', 'manual')
JPEG_MAGIC = b'\xff\xd8\xff'


@dataclass
class Context:
    """What the blueprint needs from the main module (passed in, so the module is not imported twice)."""
    state: dict
    state_lock: object
    save_state: Callable[[], None]
    require_token: Callable
    login_required: Callable
    append_log_entry: Callable[[dict], None]
    now_iso: Callable[[], str]
    utc_now: Callable
    parse_iso_utc: Callable
    stale_after_sec: int
    instance_ttl_sec: int
    preview_dir: str
    max_preview_bytes: int


def _text(value, limit):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float('inf') else None


def _bool(value):
    return value if isinstance(value, bool) else None


def _clean_mount(m):
    if not isinstance(m, dict):
        return None
    return {
        'raHours': _number(m.get('raHours')),       # J2000
        'decDeg': _number(m.get('decDeg')),         # J2000
        'altDeg': _number(m.get('altDeg')),
        'azDeg': _number(m.get('azDeg')),
        'state': _text(m.get('state'), 20),         # tracking, slewing, parked, stopped, error, unknown
        'pierSide': _text(m.get('pierSide'), 10),
        'connected': _bool(m.get('connected')),
    }


def _clean_task(t):
    if not isinstance(t, dict):
        return None
    return {
        'name': _text(t.get('name'), 40),
        'state': _text(t.get('state'), 20),         # waiting, exposing, paused, error, ...
        'detail': _text(t.get('detail'), 200),
        'exposureSeconds': _number(t.get('exposureSeconds')),
        'setPointC': _number(t.get('setPointC')),
        'sensorC': _number(t.get('sensorC')),
        'coolerPercent': _number(t.get('coolerPercent')),
        'framesSaved': _number(t.get('framesSaved')),
        'framesDiscarded': _number(t.get('framesDiscarded')),
        'lastFrameUtc': _text(t.get('lastFrameUtc'), 40),
    }


def clean_status(payload):
    """Only the documented fields, with limited lengths: no arbitrary blobs in the state file."""
    tasks = payload.get('backgroundTasks')
    dome = payload.get('dome') if isinstance(payload.get('dome'), dict) else {}
    safety = payload.get('safety') if isinstance(payload.get('safety'), dict) else {}
    return {
        'ts': _text(payload.get('ts'), 40),
        'software': _text(payload.get('software'), 40),
        'sessionId': _text(payload.get('sessionId'), 40),
        'mount': _clean_mount(payload.get('mount')),
        'dome': {'azDeg': _number(dome.get('azDeg')), 'shutter': _text(dome.get('shutter'), 20)} if dome else None,
        'activity': _text(payload.get('activity'), 200),
        'instrument': _text(payload.get('instrument'), 80),
        'backgroundTasks': [c for c in (_clean_task(t) for t in tasks[:10]) if c] if isinstance(tasks, list) else [],
        'safety': {'state': _text(safety.get('state'), 20), 'detail': _text(safety.get('detail'), 200)} if safety else None,
        'lastFrameUtc': _text(payload.get('lastFrameUtc'), 40),
        'clients': _number(payload.get('clients')),
    }


def create_blueprint(ctx: Context) -> Blueprint:
    bp = Blueprint('api_v2', __name__, url_prefix='/api/v2')

    def instances():
        v2 = ctx.state.setdefault('v2', {})
        if not isinstance(v2.get('instances'), dict):
            v2['instances'] = {}
        return v2['instances']

    def body():
        if request.content_length and request.content_length > 64 * 1024:
            abort(413)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return None, (jsonify({'ok': False, 'msg': 'JSON object expected'}), 400)
        if payload.get('schemaVersion') != SCHEMA_VERSION:
            return None, (jsonify({'ok': False, 'msg': f'schemaVersion {SCHEMA_VERSION} expected'}), 400)
        instance_id = payload.get('instanceId')
        if not isinstance(instance_id, str) or not INSTANCE_ID_RE.match(instance_id):
            return None, (jsonify({'ok': False, 'msg': 'instanceId: lower-case letters, digits and "-", at most 40'}), 400)
        return payload, None

    def instance(instance_id):
        record = instances().get(instance_id)
        if record is None:
            record = {'instanceId': instance_id, 'lastSeen': None, 'status': None, 'session': None, 'preview': None}
            instances()[instance_id] = record
        return record

    @bp.route('/sessions', methods=['POST'])
    @ctx.require_token
    def start_session():
        payload, error = body()
        if error:
            return error
        observers = payload.get('observers')
        if not isinstance(observers, list) or not observers or len(observers) > 10:
            return jsonify({'ok': False, 'msg': 'observers: 1-10 first names'}), 400
        observers = [o for o in (_text(x, 40) for x in observers) if o]
        session_type = _text(payload.get('sessionType'), 20)
        if session_type not in SESSION_TYPES:
            return jsonify({'ok': False, 'msg': f'sessionType: one of {", ".join(SESSION_TYPES)}'}), 400
        with ctx.state_lock:
            record = instance(payload['instanceId'])
            if record.get('session') and not payload.get('force'):
                return jsonify({'ok': False, 'msg': 'a session is running on this instance',
                                'sessionId': record['session']['sessionId']}), 409
            if record.get('session'):
                _end_session(record, 'manual', 'replaced by a new session')
            session = {
                'sessionId': str(uuid4()),
                'observers': observers,
                'sessionType': session_type,
                'project': _text(payload.get('project'), 80),
                'profile': _text(payload.get('profile'), 80),
                'plannedEnd': _text(payload.get('plannedEnd'), 40),
                'start': ctx.now_iso(),
            }
            record['session'] = session
            record['lastSeen'] = ctx.now_iso()
            ctx.save_state()
        return jsonify({'ok': True, 'sessionId': session['sessionId']})

    def _end_session(record, reason, note):
        session = record['session']
        start = ctx.parse_iso_utc(session.get('start'))
        end_iso = ctx.now_iso()
        end = ctx.parse_iso_utc(end_iso)
        ctx.append_log_entry({
            'id': session['sessionId'],
            'user': ', '.join(session.get('observers') or []),
            'target': session.get('project') or session.get('sessionType') or '',
            'start': session.get('start'),
            'end': end_iso,
            'durationSec': max(0, int((end - start).total_seconds())) if start and end else 0,
            'plannedEnd': session.get('plannedEnd'),
            'endReason': reason,
            'instanceId': record['instanceId'],
            'sessionType': session.get('sessionType'),
            'profile': session.get('profile'),
            'note': note,
        })
        record['session'] = None

    @bp.route('/sessions/<session_id>/end', methods=['POST'])
    @ctx.require_token
    def end_session(session_id):
        payload = request.get_json(silent=True) or {}
        reason = payload.get('reason') or 'normal'
        if reason not in END_REASONS:
            return jsonify({'ok': False, 'msg': f'reason: one of {", ".join(END_REASONS)}'}), 400
        with ctx.state_lock:
            for record in instances().values():
                if (record.get('session') or {}).get('sessionId') == session_id:
                    _end_session(record, reason, _text(payload.get('note'), 500))
                    record['lastSeen'] = ctx.now_iso()
                    ctx.save_state()
                    return jsonify({'ok': True})
        return jsonify({'ok': False, 'msg': 'unknown or already ended session'}), 404

    @bp.route('/status', methods=['POST'])
    @ctx.require_token
    def status():
        payload, error = body()
        if error:
            return error
        with ctx.state_lock:
            record = instance(payload['instanceId'])
            record['status'] = clean_status(payload)
            record['lastSeen'] = ctx.now_iso()
            ctx.save_state()
            session_id = (record.get('session') or {}).get('sessionId')
        # The sender learns whether the dashboard still knows its session (e.g. ended by hand)
        return jsonify({'ok': True, 'sessionId': session_id})

    @bp.route('/preview', methods=['POST'])
    @ctx.require_token
    def preview():
        instance_id = request.args.get('instanceId', '')
        if not INSTANCE_ID_RE.match(instance_id):
            return jsonify({'ok': False, 'msg': 'instanceId missing or invalid'}), 400
        if request.content_length and request.content_length > ctx.max_preview_bytes:
            abort(413)
        data = request.get_data(cache=False)
        if len(data) > ctx.max_preview_bytes:
            abort(413)
        if not data.startswith(JPEG_MAGIC):
            return jsonify({'ok': False, 'msg': 'JPEG expected'}), 415
        os.makedirs(ctx.preview_dir, exist_ok=True)
        path = os.path.join(ctx.preview_dir, f'{instance_id}.jpg')
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
        with ctx.state_lock:
            record = instance(instance_id)
            record['preview'] = {'ts': ctx.now_iso(), 'bytes': len(data)}
            record['lastSeen'] = ctx.now_iso()
            ctx.save_state()
        return jsonify({'ok': True})

    @bp.route('/instances')
    @ctx.login_required(api=True)
    def list_instances():
        now = ctx.utc_now()
        result = []
        with ctx.state_lock:
            for record in instances().values():
                seen = ctx.parse_iso_utc(record.get('lastSeen'))
                silent = int((now - seen).total_seconds()) if seen else None
                result.append({
                    **record,
                    'secondsSinceLastSeen': silent,
                    'disturbed': silent is None or silent > ctx.stale_after_sec,
                })
        result.sort(key=lambda r: r['instanceId'])
        return jsonify({'instances': result, 'staleAfterSec': ctx.stale_after_sec})

    @bp.route('/preview/<instance_id>.jpg')
    @ctx.login_required(api=True)
    def preview_image(instance_id):
        if not INSTANCE_ID_RE.match(instance_id):
            abort(404)
        return send_from_directory(ctx.preview_dir, f'{instance_id}.jpg', max_age=0)

    return bp


def prune(ctx: Context, now=None):
    """Forget instances without session that have been silent longer than the TTL. Caller holds the state lock."""
    if ctx.instance_ttl_sec <= 0:
        return False
    now = now or ctx.utc_now()
    entries = (ctx.state.get('v2') or {}).get('instances') or {}
    stale = [key for key, record in entries.items()
             if not record.get('session')
             and (seen := ctx.parse_iso_utc(record.get('lastSeen'))) is not None
             and (now - seen).total_seconds() > ctx.instance_ttl_sec]
    for key in stale:
        entries.pop(key, None)
        try:
            os.remove(os.path.join(ctx.preview_dir, f'{key}.jpg'))
        except OSError:
            pass
    return bool(stale)
