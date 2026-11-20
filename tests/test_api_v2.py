"""Interface /api/v2 for OST_OCS servers (ADR 0008 of ost_ocs)."""
from datetime import timedelta

import observatory_presence as op
import api_v2

TOKEN = {'Authorization': 'Bearer test-secret'}
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 100


def status(**extra):
    payload = {
        'schemaVersion': 1,
        'instanceId': 'ost-dome',
        'ts': '2026-11-20T21:00:00Z',
        'software': 'OST_OCS 0.1.0',
        'mount': {'raHours': 0.7123, 'decDeg': 41.269, 'state': 'tracking', 'pierSide': 'WEST', 'connected': True},
        'activity': 'companion camera: exposing 10 s',
        'backgroundTasks': [{'name': 'companion', 'state': 'exposing', 'detail': 'exposing 10 s',
                             'exposureSeconds': 10, 'setPointC': -5, 'framesSaved': 12}],
    }
    payload.update(extra)
    return payload


def start(client, **extra):
    payload = {'schemaVersion': 1, 'instanceId': 'ost-dome', 'observers': ['Anna', 'Ben'],
               'sessionType': 'PRAKTIKUM', 'project': 'N2', 'profile': 'OST + QHY600M'}
    payload.update(extra)
    return client.post('/api/v2/sessions', json=payload, headers=TOKEN)


def instances(client):
    return {i['instanceId']: i for i in client.get('/api/v2/instances').get_json()['instances']}


def test_status_is_stored_and_listed(client):
    r = client.post('/api/v2/status', json=status(), headers=TOKEN)
    assert r.status_code == 200
    inst = instances(client)['ost-dome']
    assert inst['status']['mount']['state'] == 'tracking'
    assert inst['status']['backgroundTasks'][0]['framesSaved'] == 12
    assert inst['disturbed'] is False
    assert inst['session'] is None


def test_unknown_fields_and_long_texts_are_not_stored(client):
    client.post('/api/v2/status', json=status(blob='x' * 1000, activity='y' * 1000), headers=TOKEN)
    stored = instances(client)['ost-dome']['status']
    assert 'blob' not in stored
    assert len(stored['activity']) == 200


def test_requires_token_and_valid_payload(client, monkeypatch):
    monkeypatch.setattr(op, 'SECRET_TOKEN', 'prod-token')
    monkeypatch.setattr(op, 'ALLOW_OPEN_API', False)
    assert client.post('/api/v2/status', json=status()).status_code == 401
    assert client.post('/api/v2/status', json=status(), headers={'Authorization': 'Bearer prod-token'}).status_code == 200
    bad = {'Authorization': 'Bearer prod-token'}
    assert client.post('/api/v2/status', json=status(schemaVersion=2), headers=bad).status_code == 400
    assert client.post('/api/v2/status', json=status(instanceId='OST Dome'), headers=bad).status_code == 400


def test_instances_need_dashboard_login(anon_client):
    assert anon_client.get('/api/v2/instances').status_code == 401


def test_session_lifecycle_goes_into_the_log(client):
    r = start(client)
    assert r.status_code == 200
    session_id = r.get_json()['sessionId']
    assert instances(client)['ost-dome']['session']['observers'] == ['Anna', 'Ben']
    # A second start on the same instance is refused unless forced
    assert start(client).status_code == 409

    r = client.post(f'/api/v2/sessions/{session_id}/end', json={'reason': 'normal'}, headers=TOKEN)
    assert r.status_code == 200
    assert instances(client)['ost-dome']['session'] is None
    entry = client.get('/logbook').get_json()['entries'][0]
    assert entry['user'] == 'Anna, Ben'
    assert entry['target'] == 'N2'
    assert entry['instanceId'] == 'ost-dome'
    assert entry['endReason'] == 'normal'
    # Ending twice is an error, not a second log entry
    assert client.post(f'/api/v2/sessions/{session_id}/end', json={}, headers=TOKEN).status_code == 404
    assert client.get('/logbook').get_json()['total'] == 1


def test_session_validation(client):
    assert start(client, observers=[]).status_code == 400
    assert start(client, sessionType='PARTY').status_code == 400
    r = start(client)
    sid = r.get_json()['sessionId']
    assert client.post(f'/api/v2/sessions/{sid}/end', json={'reason': 'whatever'}, headers=TOKEN).status_code == 400


def test_silence_marks_the_instance_disturbed_but_keeps_the_session(client):
    sid = start(client).get_json()['sessionId']
    client.post('/api/v2/status', json=status(sessionId=sid), headers=TOKEN)
    with op.state_lock:
        old = (op.utc_now() - timedelta(minutes=10)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        op.state['v2']['instances']['ost-dome']['lastSeen'] = old
        # The old cleaner rules must not touch v2 sessions
        op.prune_stale_monitoring()
        api_v2.prune(op.v2_context)
    inst = instances(client)['ost-dome']
    assert inst['disturbed'] is True
    assert inst['secondsSinceLastSeen'] >= 600
    assert inst['session']['sessionId'] == sid
    # Status again: same session, no longer disturbed
    r = client.post('/api/v2/status', json=status(sessionId=sid), headers=TOKEN)
    assert r.get_json()['sessionId'] == sid
    assert instances(client)['ost-dome']['disturbed'] is False


def test_long_silent_instances_without_session_are_forgotten(client):
    client.post('/api/v2/status', json=status(instanceId='mobile-c14'), headers=TOKEN)
    with op.state_lock:
        old = (op.utc_now() - timedelta(days=8)).isoformat()
        op.state['v2']['instances']['mobile-c14']['lastSeen'] = old
        assert api_v2.prune(op.v2_context) is True
    assert 'mobile-c14' not in instances(client)


def test_several_instances_are_kept_apart(client):
    client.post('/api/v2/status', json=status(), headers=TOKEN)
    client.post('/api/v2/status', json=status(instanceId='mobile-c14', activity='mobile'), headers=TOKEN)
    both = instances(client)
    assert set(both) == {'ost-dome', 'mobile-c14'}
    assert both['mobile-c14']['status']['activity'] == 'mobile'


def test_preview_upload_and_download(client):
    r = client.post('/api/v2/preview?instanceId=ost-dome', data=JPEG, headers={**TOKEN, 'Content-Type': 'image/jpeg'})
    assert r.status_code == 200
    assert instances(client)['ost-dome']['preview']['bytes'] == len(JPEG)
    image = client.get('/api/v2/preview/ost-dome.jpg')
    assert image.status_code == 200
    assert image.data == JPEG


def test_preview_rejects_non_jpeg_and_oversize(client):
    headers = {**TOKEN, 'Content-Type': 'image/jpeg'}
    assert client.post('/api/v2/preview?instanceId=ost-dome', data=b'GIF89a', headers=headers).status_code == 415
    big = JPEG + b'\x00' * (op.MAX_PREVIEW_BYTES + 1)
    assert client.post('/api/v2/preview?instanceId=ost-dome', data=big, headers=headers).status_code == 413
    assert client.post('/api/v2/preview?instanceId=../etc', data=JPEG, headers=headers).status_code == 400
    assert client.get('/api/v2/preview/..%2Fetc.jpg').status_code == 404
