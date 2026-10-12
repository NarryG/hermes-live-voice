"""Home/profile resolution without installed services or provider access."""
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_plugin_api import PLUGIN_API, load_unit


def path_namespace(name, home):
    ns = load_unit(PLUGIN_API, name)
    resolver = lambda: home
    ns.update(Path=Path, re=re, talk_config=SimpleNamespace(get_hermes_home=resolver),
              _HOST_HERMES_HOME=resolver)
    tree = ast.parse(PLUGIN_API.read_text())
    root = next((n for n in tree.body if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], 'id', '') == '_PROFILES_ROOT'), None)
    if root is not None:
        exec(compile(ast.Module(body=[root], type_ignores=[]), str(PLUGIN_API), 'exec'), ns)
    if any(isinstance(n, ast.FunctionDef) and n.name == '_profiles_root' for n in tree.body):
        helper = load_unit(PLUGIN_API, '_profiles_root')
        helper.update(ns)
        ns['_profiles_root'] = helper['_profiles_root']
    return ns


@pytest.mark.parametrize('active_named_profile', [False, True])
def test_profiles_follow_host_home_and_do_not_nest_profiles(tmp_path, active_named_profile):
    root = tmp_path / 'owner-home'
    for name in ['atlas', 'zephyr']:
        bot = root / 'profiles' / name
        bot.mkdir(parents=True)
        (bot / 'SOUL.md').write_text('# ' + name)
    (root / 'profiles' / 'not-a-bot').mkdir()
    home = root / 'profiles' / 'atlas' if active_named_profile else root
    ns = path_namespace('_list_bots', home)
    assert ns['_list_bots']() == ['atlas', 'zephyr']
    ns = path_namespace('_profile_home', home)
    assert ns['_profile_home']('atlas') == (root / 'profiles' / 'atlas').resolve()
    assert ns['_profile_home']('missing') is None
    assert ns['_profile_home']('default') is None  # no guessed host/default alias
    assert ns['_profile_home']('../zephyr') is None


def test_profile_symlink_stays_supported_during_identity_override(tmp_path):
    root = tmp_path / 'owner-home'
    (root / 'profiles').mkdir(parents=True)
    target = tmp_path / 'external-profile'
    target.mkdir()
    (root / 'profiles' / 'atlas').symlink_to(target, target_is_directory=True)
    ns = path_namespace('_profile_home', root)
    ns['talk_config'].get_hermes_home = lambda: target
    assert ns['_profile_home']('atlas') == target.resolve()


def test_full_adapter_discovery_uses_hermes_home(tmp_path):
    home = tmp_path / 'owner-home'
    adapter = home / 'plugins' / 'hermes-talk'
    adapter.mkdir(parents=True)
    (adapter / 'talk_auth.py').write_text('SOURCE = "owner adapter"\n')
    script = f'''
import importlib.util, json, sys, types
sys.modules['httpx'] = types.ModuleType('httpx')
spec = importlib.util.spec_from_file_location('voice_backend', {str(PLUGIN_API)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps({{'adapter': module.talk_auth.__file__}}))
'''
    env = dict(os.environ, HOME=str(tmp_path / 'empty-home'), HERMES_HOME=str(home), PYTHONPATH='')
    result = subprocess.run([sys.executable, '-S', '-c', script],
                            capture_output=True, text=True, timeout=20, env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['adapter'] == str(adapter / 'talk_auth.py')


@pytest.mark.parametrize('home_source', ['environment', 'host'])
@pytest.mark.parametrize('full_adapter', [False, True])
def test_bundled_resolver_ignores_preloaded_dashboard_on_repeated_loads(tmp_path, home_source, full_adapter):
    unrelated = tmp_path / 'unrelated'
    dashboard = unrelated / 'dashboard'
    dashboard.mkdir(parents=True)
    (dashboard / '__init__.py').write_text('SOURCE = "unrelated dashboard"\n')
    home = tmp_path / 'owner-home' / 'profiles' / 'atlas'
    adapter = home / 'plugins' / 'hermes-talk'
    if full_adapter:
        adapter.mkdir(parents=True)
        (adapter / 'talk_auth.py').write_text('SOURCE = "owner adapter"\n')
    script = f'''
import importlib.util, json, os, sys, types
from pathlib import Path
sys.path.insert(0, {str(unrelated)!r})
import dashboard
original_dashboard = dashboard
sys.modules['httpx'] = types.ModuleType('httpx')
home = Path({str(home)!r})
if {home_source == 'host'!r}:
    host = types.ModuleType('hermes_constants')
    host.get_hermes_home = lambda: home
    sys.modules['hermes_constants'] = host
results = []
for index in range(2):
    spec = importlib.util.spec_from_file_location('voice_backend', {str(PLUGIN_API)!r})
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolver = module._get_hermes_home
    assert sys.modules['dashboard'] is original_dashboard
    assert 'dashboard.talk_vendor' not in sys.modules
    assert resolver.__module__ not in ('talk_config', 'dashboard.talk_vendor.talk_config')
    assert sys.modules[resolver.__module__].get_hermes_home is resolver
    assert resolver is not module.talk_config.get_hermes_home
    results.append({{'home': str(resolver()), 'adapter_root': str(module._HERMES_TALK_ROOT),
                    'resolver_source': resolver.__code__.co_filename,
                    'adapter_source': module.talk_auth.__file__}})
print(json.dumps(results))
'''
    env_home = home if home_source == 'environment' else tmp_path / 'ignored-env-home'
    env = dict(os.environ, HOME=str(tmp_path / 'empty-home'), HERMES_HOME=str(env_home), PYTHONPATH='')
    result = subprocess.run([sys.executable, '-S', '-c', script],
                            capture_output=True, text=True, timeout=20, env=env)
    assert result.returncode == 0, result.stderr
    vendor = PLUGIN_API.parent / 'talk_vendor'
    expected = {'home': str(home), 'adapter_root': str(adapter),
                'resolver_source': str(vendor / 'talk_config.py'),
                'adapter_source': str((adapter if full_adapter else vendor) / 'talk_auth.py')}
    assert json.loads(result.stdout) == [expected, expected]


def test_profile_session_captures_home_before_override_and_restores_it(tmp_path):
    import asyncio
    import threading
    ns = load_unit(PLUGIN_API, 'create_session')
    home = tmp_path / 'active-host'
    target = tmp_path / 'requested-profile'
    calls = []
    config = SimpleNamespace(get_hermes_home=lambda: home)
    original_resolver = config.get_hermes_home

    def profile_home(profile):
        # Catch recursive lookup under the temporary mint-time override.
        assert config.get_hermes_home() == home
        calls.append(profile)
        return target

    def mint(profile, voice, allow_chat, language):
        assert config.get_hermes_home() == target
        raise ValueError('intentional mint failure')

    ns.update(_MINT_LOCK=threading.Lock(), _profile_home=profile_home, talk_config=config,
              _resolve_voice=lambda value: 'marin', _mint_for=mint)
    class Request:
        async def json(self):
            return {'profile': 'atlas'}
    with pytest.raises(ValueError, match='intentional mint failure'):
        asyncio.run(ns['create_session'](Request()))
    assert calls == ['atlas']
    assert config.get_hermes_home is original_resolver
