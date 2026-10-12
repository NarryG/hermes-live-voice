"""Backend-owned English messages; no CLI, credentials, audio or network."""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_plugin_api import PLUGIN_API, _start_ns, load_unit


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class Request:
    def __init__(self, body=None):
        self.body = body or {}

    async def json(self):
        return self.body


def test_missing_codex_message_is_english():
    ns = load_unit(PLUGIN_API, '_cl_ensure')
    ns.update(_CL={'proc': None}, _codex_binary=lambda: None)
    with pytest.raises(RuntimeError, match='Codex CLI was not found'):
        ns['_cl_ensure']()


@pytest.mark.parametrize('state,expected', [
    ('timeout', 'Sign-in timed out: the code expired without approval'),
    ('valid', 'Signed in'),
    ('missing', 'Could not sign in: original CLI output'),
])
def test_login_process_messages_are_english(state, expected):
    ns = load_unit(PLUGIN_API, '_login_check')
    proc = SimpleNamespace(poll=lambda: None if state == 'timeout' else 1)
    ns.update(_LOGIN={'status': 'pending', 'proc': proc, 'started_at': time.time() - 100,
                      'output': 'original CLI output'}, _LOGIN_TIMEOUT_S=10,
              _terminate=lambda process: None,
              talk_auth=SimpleNamespace(auth_diagnostic=lambda: {'codex_oauth': state}))
    ns['_login_check']()
    assert ns['_LOGIN']['message'] == expected


def test_login_missing_binary_message_is_english():
    import threading
    ns = load_unit(PLUGIN_API, '_start_codex_login')
    ns.update(_LOGIN_LOCK=threading.Lock(), _LOGIN={}, _login_check=lambda: None,
              _codex_binary=lambda: None)
    assert ns['_start_codex_login']() == {
        'ok': False, 'message': 'Codex CLI was not found on the server'}


def test_unavailable_voice_message_is_english():
    ns = load_unit(PLUGIN_API, '_resolve_voice')
    ns.update(talk_config=SimpleNamespace(OPENAI_REALTIME_VOICES=('marin',)), HTTPException=HTTPException)
    with pytest.raises(HTTPException) as exc:
        ns['_resolve_voice']('unknown')
    assert exc.value.detail == "voice 'unknown' is not available"


@pytest.mark.parametrize('route', ['create_session', 'codexlive_session'])
def test_missing_profile_message_is_english(route):
    ns = load_unit(PLUGIN_API, route)
    ns.update(_profile_home=lambda profile: None, HTTPException=HTTPException)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ns[route](Request({'profile': 'missing'})))
    assert exc.value.detail == "profile 'missing' does not exist"


@pytest.mark.parametrize('route,expected', [
    ('voice_usage_report', {'ok': False, 'error': 'durationMs is required'}),
    ('codexlive_interrupt', {'ok': False, 'error': 'turnId/threadId are required'}),
])
def test_missing_route_arguments_are_english(route, expected):
    ns = load_unit(PLUGIN_API, route)
    ns.update(_CL={})
    assert asyncio.run(ns[route](Request())) == expected


def test_missing_sdp_message_is_english():
    ns = load_unit(PLUGIN_API, 'codexlive_session')
    ns.update(HTTPException=HTTPException)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ns['codexlive_session'](Request()))
    assert exc.value.detail == 'SDP offer is required'


@pytest.mark.parametrize('state', ['missing', 'present', 'write-error'])
def test_logout_messages_are_english_without_touching_real_auth(tmp_path, state):
    import shutil
    ns = load_unit(PLUGIN_API, 'codex_logout')
    path = tmp_path / 'fake-auth.json'
    if state != 'missing':
        path.write_text('test fixture only')
    def copy2(source, target):
        if state == 'write-error':
            raise OSError('fixture refusal')
        shutil.copy2(source, target)
    ns.update(_codex_auth_path=lambda: path, shutil=SimpleNamespace(copy2=copy2))
    result = asyncio.run(ns['codex_logout']())
    if state == 'missing':
        assert result == {'ok': True, 'message': 'There was no session to sign out of'}
    elif state == 'write-error':
        assert result == {'ok': False, 'message': 'Could not sign out: fixture refusal'}
        assert path.exists()
    else:
        assert result['message'].startswith('Signed out (backup: auth.json.bak-voice-logout-')
        assert not path.exists()
        assert list(tmp_path.glob('*.bak-voice-logout-*'))


def test_app_server_unavailable_message_is_english():
    ns = load_unit(PLUGIN_API, '_cl_send')
    ns.update(_CL={'proc': None})
    with pytest.raises(RuntimeError, match='codex app-server is not available'):
        ns['_cl_send']({})


def test_app_server_request_timeout_message_is_english():
    ns = load_unit(PLUGIN_API, '_cl_request')
    ns.update(_CL={'seq': 0}, _cl_send=lambda obj: None)
    with pytest.raises(TimeoutError, match='codex app-server: probe did not respond'):
        ns['_cl_request']('probe', {}, timeout=0)


def test_missing_thread_message_is_english():
    ns = load_unit(PLUGIN_API, '_cl_thread_ensure')
    ns.update(_CL={}, _agent_model=lambda: None, Path=Path, _cl_request=lambda *a, **kw: {})
    with pytest.raises(RuntimeError, match='codex: could not create the thread'):
        ns['_cl_thread_ensure']()


@pytest.mark.parametrize('provider_message,expected', [
    ('unknown field `clientManagedHandoffs`',
     'LIVE_HANDOFF_NO_SOPORTADO: the Codex app-server does not support clientManagedHandoffs'),
    ('unknown field `bogus`', "codex live: the app-server rejected field 'bogus'"),
    ('unknown field', 'codex live: the app-server rejected field (no readable name)'),
    ('You hit your usage limit', 'LIVE_SIN_QUOTA: the provider reported a usage limit for Codex Live.'),
])
def test_codex_live_errors_are_english_without_automatic_billing_fallback(provider_message, expected):
    ns, sent = _start_ns('client', [provider_message])
    with pytest.raises(RuntimeError) as exc:
        ns['_codexlive_start'](None, 'cove', 'fixture')
    message = str(exc.value)
    assert message.startswith(expected)
    if message.startswith('LIVE_SIN_QUOTA:'):
        assert 'weekly' not in message.lower()
        assert 'reset' not in message.lower()
        assert 'gpt-realtime' not in message.lower()
    assert len(sent) == 1


def test_optional_field_warning_is_english():
    ns, _ = _start_ns('client', ['unknown field `delegationAckFiller`'])
    assert ns['_codexlive_start'](None, 'cove', 'fixture')['warning'] == (
        'the app-server does not support: delegationAckFiller (omitted)')


def test_missing_sdp_answer_message_is_english():
    ns, _ = _start_ns('client', [])
    ticks = iter([0, 100])
    ns['time'] = SimpleNamespace(time=lambda: next(ticks))
    # Suppress fixture notifications to exercise the timeout, never a real provider.
    ns['_cl_request'] = lambda *a, **kw: {}
    with pytest.raises(TimeoutError, match='codex live: no SDP answer received'):
        ns['_codexlive_start'](None, 'cove', 'fixture')


@pytest.mark.parametrize('minutes,expected', [
    (None, ['primary', 'secondary']), (300, ['5h']),
    (10080, ['weekly']), (8640, ['144h']), (90, ['90m']),
])
def test_provider_usage_window_labels_use_only_reported_duration(monkeypatch, tmp_path, minutes, expected):
    ns = load_unit(PLUGIN_API, 'codex_usage')
    window = {'used_percent': 25, **({'window_minutes': minutes} if minutes else {})}
    payload = {'rate_limit': {'primary_window': window, 'secondary_window': window}}
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setitem(sys.modules, 'httpx', SimpleNamespace(Client=Client))
    ns.update(talk_auth=SimpleNamespace(resolve_auth=lambda: SimpleNamespace(token='fixture')),
              _codex_auth_path=lambda: tmp_path / 'no-auth')
    result = asyncio.run(ns['codex_usage']())
    assert list(result['windows']) == expected
    assert all(w['resetsAt'] is None for w in result['windows'].values())
