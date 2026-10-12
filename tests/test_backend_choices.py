"""No implicit developer model override; explicit owner choices remain intact."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_plugin_api import PLUGIN_API, REPO, VENDOR, load_unit


def test_example_settings_does_not_pin_a_developer_model():
    example = json.loads((REPO / 'settings.example.json').read_text())
    assert not example.get('codexAgentModel')
    assert example['delegation'] == 'client'


@pytest.mark.parametrize('env_value,settings_value,expected', [
    (None, None, None), (None, 'owner-model', 'owner-model'),
    ('env-owner-model', 'owner-model', 'env-owner-model'),
])
def test_agent_model_preserves_explicit_choices(monkeypatch, tmp_path, env_value, settings_value, expected):
    import os
    ns = load_unit(PLUGIN_API, '_agent_model')
    path = tmp_path / 'settings.json'
    content = json.dumps({'codexAgentModel': settings_value, 'delegation': 'server'})
    path.write_text(content)
    settings = load_unit(PLUGIN_API, '_talk_settings')
    settings.update(_PLUGIN_ROOT=tmp_path)
    ns.update(os=os, _talk_settings=settings['_talk_settings'])
    if env_value is None:
        monkeypatch.delenv('TALK_CODEX_AGENT_MODEL', raising=False)
    else:
        monkeypatch.setenv('TALK_CODEX_AGENT_MODEL', env_value)
    assert ns['_agent_model']() == expected
    assert path.read_text() == content


@pytest.mark.parametrize('model', [None, 'owner-model'])
def test_codex_thread_sends_only_explicit_model_override(model):
    ns = load_unit(PLUGIN_API, '_cl_thread_ensure')
    sent = []
    ns.update(_CL={}, Path=Path, _agent_model=lambda: model)
    def request(method, body, **kwargs):
        sent.append((method, body))
        return {'thread': {'id': 'fixture-thread'}}
    ns['_cl_request'] = request
    assert ns['_cl_thread_ensure']() == 'fixture-thread'
    body = sent[0][1]
    assert body['modelProvider'] == 'openai'  # unchanged subscription-only Codex lane
    if model is None:
        assert 'model' not in body
    else:
        assert body['model'] == model


def test_codex_auth_path_respects_owner_home(monkeypatch, tmp_path):
    import os
    ns = load_unit(PLUGIN_API, '_codex_auth_path')
    ns.update(os=os, Path=Path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'owner-codex'))
    assert ns['_codex_auth_path']() == tmp_path / 'owner-codex' / 'auth.json'


def test_codex_process_uses_original_subscription_lane_without_touching_parent_env(monkeypatch):
    import os
    import threading
    ns = load_unit(PLUGIN_API, '_cl_ensure')
    calls = []
    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
    def popen(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return object()
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-only')
    monkeypatch.setenv('CODEX_API_KEY', 'fixture-only')
    monkeypatch.setenv('CODEX_HOME', '/owner/codex')
    ns.update(_CL={'proc': None}, _codex_binary=lambda: '/fixture/codex', os=os, Path=Path,
              subprocess=SimpleNamespace(Popen=popen, PIPE=-1, DEVNULL=-3),
              threading=SimpleNamespace(Thread=Thread), _cl_reader=lambda proc: None,
              _cl_request=lambda *a, **kw: {})
    ns['_cl_ensure']()
    arguments, kwargs = calls[0]
    assert 'model_provider=openai' in arguments
    assert not any(arg.startswith('model=') for arg in arguments)
    assert 'OPENAI_API_KEY' not in kwargs['env'] and 'CODEX_API_KEY' not in kwargs['env']
    assert kwargs['env']['CODEX_HOME'] == '/owner/codex'
    assert os.environ['OPENAI_API_KEY'] == 'fixture-only'
    assert os.environ['CODEX_API_KEY'] == 'fixture-only'
