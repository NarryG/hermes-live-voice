"""Selected-owner tests execute ONLY explicitly labelled local metadata fixtures."""
import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS
import pytest
from test_discord_pcm import ROOT
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / 'tests/fixtures/fake_cli_metadata.py'


def token(claims):
    enc = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'FAKE.' + enc + '.FAKE'


def auth(account='FAKE-account', user='FAKE-user', expiry=None, rotated=False):
    identity = {'chatgpt_account_id': account, 'chatgpt_user_id': user, 'chatgpt_plan_type': 'plus'}
    return {'auth_mode': 'chatgpt', 'tokens': {'account_id': account,
        'id_token': token({'email': 'FAKE@example.invalid', 'https://api.openai.com/auth': identity}),
        'access_token': token({'exp': expiry or time.time()+3600, 'https://api.openai.com/auth': identity,
                              'nonce': 'renewed' if rotated else 'original'}), 'refresh_token': 'FAKE-REFRESH'}}


def fixture(tmp_path, *, renewal=None):
    cli = tmp_path / 'FAKE-cli'
    cli.mkdir(mode=0o700)
    (cli / 'auth.json').write_text(json.dumps(auth(expiry=time.time()-100 if renewal else None)))
    (cli / 'auth.json').chmod(0o600)
    config = {'model': 'FAKE-text-model-not-voice', 'model_provider': None,
              'cli_auth_credentials_store': None, 'realtime': None,
              'experimental_realtime_ws_model': None}
    (cli / 'config.toml').write_text('# EXPLICIT OFFLINE FAKE selected CLI config\n')
    layer = {'name': {'type': 'user', 'file': str(cli / 'config.toml'), 'profile': None},
             'version': 'FAKE-config-v1', 'config': {}}
    metadata = {'account': {'account': {'type': 'chatgpt', 'email': 'FAKE@example.invalid', 'planType': 'plus'},
                           'requiresOpenaiAuth': True, 'workspaceRouting': {
                               'chatgptAccountId': 'FAKE-account', 'backendOrigin': 'https://chatgpt.com',
                               'accountRoutingOverride': 'NO_CONSTRAINT'}},
                'config': {'config': config, 'origins': {}, 'layers': [layer]},
                'requirements': {'requirements': None, 'supportsIndependentSpeedModes': True}}
    if renewal:
        metadata['renew_auth'] = renewal
    (cli / 'FAKE-METADATA.json').write_text(json.dumps(metadata))
    home = tmp_path / 'FAKE-hermes'
    home.mkdir()
    env = {'HOME': str(tmp_path), 'CODEX_HOME': str(cli), 'PATH': os.environ['PATH']}
    scope = NS(home=home, profile=None)
    return cli, home, env, scope, metadata


def api():
    try:
        return importlib.import_module('discord_voice.owner')
    except ModuleNotFoundError:
        pytest.fail('Real selected CLI owner adapter is missing')


def owner(tmp_path, *, renewal=None):
    m = api()
    cli, home, env, scope, metadata = fixture(tmp_path, renewal=renewal)
    # Injection is a fixed labelled test child, NOT a production attestation.
    captured = m.capture_selection(home, environ=env, cwd=tmp_path)
    selected = m.SelectedCLIOwner(object(), scope, captured, metadata_command=(sys.executable, str(FIXTURE)))
    return selected, cli, scope, metadata


def test_lazy_metadata_fixture_prepares_current_same_owner_normal_defaults(tmp_path):
    async def run():
        selected, cli, scope, metadata = owner(tmp_path)
        assert not (cli / 'FAKE-REQUESTS.jsonl').exists()  # no registration credentials/CLI loading
        with pytest.raises(Exception):
            selected.snapshot()
        try:
            assert await selected.prepare(scope) is True
            snapshot = selected.snapshot()
            assert snapshot.account_id == snapshot.workspace_id == 'FAKE-account'
            assert snapshot.user_id == 'FAKE-user'
            assert snapshot.model == 'gpt-live-1-codex' and snapshot.voice == 'cove'
            assert snapshot.provider == 'openai' and snapshot.version == 'v3'
            assert 'FAKE-REFRESH' not in repr(selected) + repr(snapshot)
            requests = [json.loads(line) for line in (cli / 'FAKE-REQUESTS.jsonl').read_text().splitlines()]
            assert {r['method'] for r in requests} == {'initialize', 'initialized', 'account/read', 'config/read', 'configRequirements/read'}
            assert all(r['params'] == {'includeLayers': True, 'cwd': str(tmp_path)} for r in requests if r['method']=='config/read')
            assert all(r['params'] == {'refreshToken': False} for r in requests if r['method']=='account/read')
        finally:
            await selected.close()
    asyncio.run(run())


def test_native_factory_is_implementation_admitted_but_not_account_ready(tmp_path):
    async def run():
        m = api()
        cli, home, env, scope, _ = fixture(tmp_path)
        capture = m.capture_selection(home, environ=env, cwd=tmp_path)
        selected = m.SelectedCLIOwner(object(), scope, capture, metadata_command=(sys.executable, str(FIXTURE)))
        factory = m.SelectedLaneFactory(selected)
        try:
            assert factory.admitted is True and factory.account_ready is False
            with pytest.raises(Exception):
                factory()
            assert not (cli / 'FAKE-REQUESTS.jsonl').exists()
            assert await factory.preflight(scope) is True
            assert factory.account_ready is True
            assert await factory.preflight(NS(home=scope.home, profile=scope.profile)) is False
        finally:
            await factory.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('renewal, accepted', [(auth(rotated=True), True),
    (auth(account='FAKE-other-account', rotated=True), False),
    (auth(user='FAKE-other-user', rotated=True), False)])
def test_native_existing_login_renewal_only_preserves_same_identity(tmp_path, renewal, accepted):
    async def run():
        selected, cli, scope, _ = owner(tmp_path, renewal=renewal)
        try:
            assert await selected.prepare(scope) is accepted
            if accepted:
                assert selected.snapshot().access_token == renewal['tokens']['access_token']
                requests = [json.loads(line) for line in (cli/'FAKE-REQUESTS.jsonl').read_text().splitlines()]
                assert any(r['method']=='account/read' and r['params']=={'refreshToken':True} for r in requests)
            else:
                with pytest.raises(Exception):
                    selected.snapshot()
                assert selected._proc.returncode is not None
        finally:
            await selected.close()
    asyncio.run(run())


@pytest.mark.parametrize('target', ['auth.json', 'config.toml'])
def test_current_owner_file_generation_change_invalidates_prepared_media(tmp_path, target):
    async def run():
        selected, cli, scope, _ = owner(tmp_path)
        try:
            assert await selected.prepare(scope)
            path = cli / target
            path.write_text(path.read_text() + '\n')
            with pytest.raises(Exception, match='changed|ready'):
                selected.snapshot()
        finally:
            await selected.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['unknown-layer',  'unknown-origin', 'keyring',
    'api-key', 'managed', 'routing', 'file-permissions', 'symlink-parent'])
def test_unproven_owner_source_policy_or_store_refuses(tmp_path, change):
    async def run():
        selected, cli, scope, metadata = owner(tmp_path)
        if change == 'unknown-layer':
            metadata['config']['layers'][0]['name']['type'] = 'UNKNOWN'
        elif change == 'unknown-origin':
            metadata['config']['origins']['model_provider'] = {'name': {'type': 'user', 'file': '/UNKNOWN'}, 'version': 'UNKNOWN'}
        elif change == 'keyring':
            metadata['config']['config']['cli_auth_credentials_store'] = 'keyring'
        elif change == 'managed':
            metadata['requirements']['requirements'] = {'allowedLoginMethods': ['chatgpt']}
        elif change == 'routing':
            metadata['account']['workspaceRouting']['chatgptAccountId'] = 'FAKE-other'
        elif change == 'api-key':
            raw = auth()
            raw['OPENAI_API_KEY'] = 'FAKE-API-KEY'
            (cli / 'auth.json').write_text(json.dumps(raw))
        elif change == 'file-permissions':
            (cli / 'auth.json').chmod(0o644)
        elif change == 'symlink-parent':
            moved = cli.with_name('FAKE-moved')
            cli.rename(moved)
            cli.symlink_to(moved, target_is_directory=True)
        (cli / 'FAKE-METADATA.json').write_text(json.dumps(metadata))
        try:
            assert await selected.prepare(scope) is False
            with pytest.raises(Exception):
                selected.snapshot()
            if change in ('api-key', 'file-permissions', 'symlink-parent'):
                assert not (cli / 'FAKE-REQUESTS.jsonl').exists()
        finally:
            await selected.close()
    asyncio.run(run())

def test_prepare_accepts_the_cli_own_defaults_from_a_real_owner(tmp_path):
    """A healthy owner must prepare: the CLI's own defaults are not overrides."""
    async def run():
        selected, cli, scope, metadata = owner(tmp_path)
        settings = json.loads((cli / 'FAKE-METADATA.json').read_text())
        settings['config']['config'].update({
            'chatgpt_base_url': 'https://chatgpt.com/backend-api/',
            'experimental_thread_store_endpoint': 'http://127.0.0.1:0/thread-store',
            'js_repl_node_module_dirs': [],
            'js_repl_node_path': '/usr/bin/node',
        })
        (cli / 'FAKE-METADATA.json').write_text(json.dumps(settings))
        try:
            assert await selected.prepare(scope) is True
            assert selected.snapshot().provider == 'openai'
        finally:
            await selected.close()
    asyncio.run(run())


def test_prepare_still_refuses_a_real_non_canonical_endpoint(tmp_path):
    """The guard keeps its meaning: a third-party endpoint is not the lane's."""
    async def run():
        selected, cli, scope, metadata = owner(tmp_path)
        settings = json.loads((cli / 'FAKE-METADATA.json').read_text())
        settings['config']['config']['chatgpt_base_url'] = 'https://alternate.invalid'
        (cli / 'FAKE-METADATA.json').write_text(json.dumps(settings))
        try:
            assert await selected.prepare(scope) is False
        finally:
            await selected.close()
    asyncio.run(run())



@pytest.mark.parametrize('key', ['CODEX_HOME', 'CODEX_CONFIG_REQUIREMENTS_TOML_PATH'])
def test_changed_captured_environment_refuses_before_auth_or_cli_loading(tmp_path, monkeypatch, key):
    import dataclasses
    async def run():
        selected, cli, scope, _ = owner(tmp_path)
        selected._captured = dataclasses.replace(selected._captured, live_environment=True)
        changed = dict(selected._captured.env)
        changed[key] = str(tmp_path/'FAKE-other-cli')
        monkeypatch.setattr(api().os, 'environ', changed)
        reads = []
        original = api()._file
        def observed(path, **kwargs):
            reads.append(path)
            return original(path, **kwargs)
        monkeypatch.setattr(api(), '_file', observed)
        try:
            assert await selected.prepare(scope) is False
            assert not reads and not (cli/'FAKE-REQUESTS.jsonl').exists()
        finally:
            await selected.close()
    asyncio.run(run())


def test_selected_executable_source_generation_invalidates_capability(tmp_path, monkeypatch):
    async def run():
        selected, cli, scope, _ = owner(tmp_path)
        try:
            assert await selected.prepare(scope)
            binary = Path(sys.executable).resolve()
            original = Path.lstat
            def changed(path):
                value = original(path)
                if path == binary:
                    return NS(st_dev=value.st_dev, st_ino=value.st_ino, st_mode=value.st_mode,
                              st_uid=value.st_uid, st_size=value.st_size, st_mtime_ns=value.st_mtime_ns+1,
                              st_ctime_ns=value.st_ctime_ns+1, st_nlink=value.st_nlink)
                return value
            monkeypatch.setattr(Path, 'lstat', changed)
            with pytest.raises(Exception):
                selected.snapshot()
        finally:
            await selected.close()
    asyncio.run(run())


def test_bounded_auth_reader_rejects_symlink_in_parent_chain(tmp_path):
    m = api()
    cli, home, env, scope, _ = fixture(tmp_path)
    link = tmp_path/'FAKE-linked-cli'
    link.symlink_to(cli, target_is_directory=True)
    with pytest.raises(Exception):
        m._file(link/'auth.json', secret=True)


def test_native_legacy_file_mode_without_auth_mode_is_source_resolved_chatgpt(tmp_path):
    async def run():
        selected, cli, scope, _ = owner(tmp_path)
        raw = auth()
        raw.pop('auth_mode')  # native AuthDotJson.resolved_mode() -> Chatgpt
        (cli/'auth.json').write_text(json.dumps(raw))
        try:
            assert await selected.prepare(scope) is True
            assert selected.snapshot().auth_mode == 'chatgpt'
        finally:
            await selected.close()
    asyncio.run(run())


def test_generation_scope_guard_and_metadata_age_cannot_attest_old_media(tmp_path):
    async def run():
        selected, cli, scope, _ = owner(tmp_path)
        alive = [True]
        selected._scope_guard = lambda: alive[0]  # registered native owner boundary
        try:
            assert await selected.prepare(scope)
            alive[0] = False
            with pytest.raises(Exception):
                selected.snapshot()
        finally:
            await selected.close()
    asyncio.run(run())


def test_metadata_source_generation_change_revokes_without_spawning_each_frame(tmp_path, monkeypatch):
    async def run():
        m = api()
        monkeypatch.setattr(m, 'POLL_INTERVAL', .05)
        selected, cli, scope, metadata = owner(tmp_path)
        try:
            assert await selected.prepare(scope)
            proc = selected._proc
            for _ in range(100):
                selected.snapshot()
            metadata['config']['layers'][0]['version'] = 'FAKE-config-v2'
            (cli / 'FAKE-METADATA.json').write_text(json.dumps(metadata))
            await asyncio.sleep(.2)
            with pytest.raises(Exception):
                selected.snapshot()
            assert selected._proc is proc
            assert await selected.prepare(scope) is False
        finally:
            await selected.close()
    asyncio.run(run())
