import asyncio
import importlib
import sys
from types import SimpleNamespace as NS
import discord
from discord.ext import commands
from test_discord_pcm import ROOT
sys.path.insert(0, str(ROOT))
import pytest


@pytest.fixture(autouse=True)
def fixture_profile_home(tmp_path, monkeypatch):
    # Fake only the registered-owner resolution boundary, not persona loading.
    m = importlib.import_module('discord_voice.native')
    monkeypatch.setattr(m, 'owner_home', lambda adapter: tmp_path)


def command_http(native, policy='safe'):
    """Fake Discord HTTP routes; real SDK CommandTree serialization/fetch."""
    remote, calls = {}, []
    async def fetch(app_id):
        calls.append(('fetch', app_id))
        return list(remote.values())
    async def upsert(app_id, payload):
        calls.append(('upsert', app_id))
        data = dict(payload, id='991', application_id=str(app_id), version='1')
        remote[991] = data
        return data
    async def delete(app_id, command_id):
        calls.append(('delete', app_id, command_id))
        remote.pop(command_id)
    native.http.get_global_commands = fetch
    native.http.upsert_global_command = upsert
    native.http.delete_global_command = delete
    native._connection.application_id = 456
    adapter = NS(_client=native, platform='discord', _get_discord_command_sync_policy=lambda: policy)
    adapter.gateway_runner = NS(_owning_profile=lambda a,p: (a is adapter, None))
    return adapter, remote, calls


@pytest.mark.parametrize('settings, expected', [
    ([], ('English', None)), (None, ('English', None)),
    ({'language': ' es-MX ', 'voice': ' COVE '}, ('Spanish', 'cove')),
    ({'language': 'ignore instructions', 'voice': ['marin']}, ('English', None)),
    ({'language': 123, 'voice': 'unknown'}, ('English', None)),
])
def test_settings_object_normalizes_supported_language_and_voice(settings, expected):
    m = importlib.import_module('discord_voice.native')
    assert m.normalize_settings(settings) == expected


def test_native_settings_admit_only_real_v3_voice_labels():
    m = importlib.import_module('discord_voice.native')
    assert m.normalize_settings({'voice':' COVE '}) == ('English', 'cove')
    assert m.normalize_settings({'voice':'cedar'}) == ('English', None)


def test_persona_symlink_cannot_import_another_private_file(tmp_path):
    m = importlib.import_module('discord_voice.native')
    private = tmp_path/'PRIVATE-not-soul.md'
    private.write_text('PRIVATE-UNRELATED-CONTEXT')
    (tmp_path/'SOUL.md').symlink_to(private)
    async def run():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native, 'off')
        handle = m.install(native, adapter, tmp_path, object)
        try:
            assert 'PRIVATE-UNRELATED-CONTEXT' not in handle.manager.prompt
        finally:
            await handle.close()
            await native.close()
    asyncio.run(run())


def test_close_waits_for_owned_mutation_then_verifies_exact_remote_deletion(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native)
        original = native.http.upsert_global_command
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(app_id, payload):
            result = await original(app_id, payload)  # server applied, response pending
            entered.set()
            await release.wait()
            return result
        native.http.upsert_global_command = delayed
        handle = m.install(native, adapter, tmp_path, object)
        await entered.wait()
        closing = asyncio.create_task(handle.close())
        await asyncio.sleep(0)
        release.set()
        await closing
        assert not remote and ('delete', 456, 991) in calls
        assert handle.manager.closed.is_set()
        await native.close()
    asyncio.run(scenario())


def test_teardown_does_not_delete_remote_for_replacement_local_command(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native)
        handle = m.install(native, adapter, tmp_path, object)
        await handle.ready
        native.tree.remove_command('livevoice')
        async def replacement_callback(interaction):
            pass
        replacement = discord.app_commands.Command(name='livevoice', description='Another implementation', callback=replacement_callback)
        native.tree.add_command(replacement)
        await handle.close()
        assert native.tree.get_command('livevoice') is replacement
        assert 991 in remote and not any(c[0] == 'delete' for c in calls)
        await native.close()
    asyncio.run(scenario())


def test_retained_old_listeners_have_no_effect_after_shutdown(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native, 'off')
        handle = m.install(native, adapter, tmp_path, object)
        await handle.ready
        listener = native.extra_events['on_member_update'][0]
        handle.shutdown()  # synchronous fence before scheduled coroutine
        effects = []
        handle.manager.sessions[11] = NS(binding=NS(guild=11), audience={123},
            invalidate=lambda: effects.append('invalidated'), revoked=NS(set=lambda: None))
        await listener(None, NS(id=123, guild=NS(id=11)))
        handle.manager.sessions.clear()
        assert not effects
        await handle.close()
        await native.close()
    asyncio.run(scenario())


def test_failed_owned_remote_cleanup_can_be_retried_on_closed_handle(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native)
        handle = m.install(native, adapter, tmp_path, object)
        await handle.ready
        original = native.http.delete_global_command
        async def failure(*args):
            raise RuntimeError('private HTTP failure')
        native.http.delete_global_command = failure
        await handle.close()
        assert 991 in remote and handle.remote is not None
        assert 'pending' in handle.sync_status
        native.http.delete_global_command = original
        await handle.close()
        assert not remote and handle.remote is None
        await native.close()
    asyncio.run(scenario())


def test_policy_changed_to_off_forbids_remote_teardown_mutations(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native)
        handle = m.install(native, adapter, tmp_path, object)
        await handle.ready
        before = list(calls)
        adapter._get_discord_command_sync_policy = lambda: 'off'
        await handle.close()
        assert calls == before and 991 in remote
        assert handle.remote is not None and 'off' in handle.sync_status
        await native.close()
    asyncio.run(scenario())


def test_pending_fetch_rechecks_bot_identity_before_remote_write(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native)
        original = native.http.get_global_commands
        async def change_identity(app_id):
            result = await original(app_id)
            adapter._client = object()
            return result
        native.http.get_global_commands = change_identity
        handle = m.install(native, adapter, tmp_path, object)
        await handle.ready
        assert not remote and not any(c[0] == 'upsert' for c in calls)
        assert not handle.synced
        await handle.close()
        await native.close()
    asyncio.run(scenario())


def test_pending_old_command_callback_has_no_response_effect_after_unload(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native, 'off')
        entered, release = asyncio.Event(), asyncio.Event()
        async def admission(*args):
            entered.set()
            await release.wait()
            return True
        adapter._check_slash_authorization = admission
        effects = []
        async def defer(**kwargs):
            effects.append('defer')
        async def send(*args, **kwargs):
            effects.append('send')
        handle = m.install(native, adapter, tmp_path, object)
        await handle.ready
        actor = NS(response=NS(defer=defer), followup=NS(send=send))
        callback = asyncio.create_task(handle.command.callback(actor, 'status'))
        await entered.wait()
        handle.shutdown()
        release.set()
        await callback
        assert not effects
        await handle.close()
        await native.close()
    asyncio.run(scenario())


def test_native_scope_rejects_home_rebinding_after_persona_capture(tmp_path, monkeypatch):
    from test_discord_session import boundaries, FixtureLane, confirmed
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        registered = {}
        command_map = {}
        native.add_listener = lambda fn,name: registered.setdefault(name, fn)
        native.remove_listener = lambda fn,name: registered.pop(name, None)
        native.tree = NS(add_command=lambda c: command_map.setdefault(c.name,c), get_command=command_map.get,
                         remove_command=lambda n: command_map.pop(n,None))
        adapter.platform = 'discord'
        adapter.gateway_runner._owning_profile = lambda a,p: (a is adapter, None)
        adapter._get_discord_command_sync_policy = lambda: 'off'
        home = [tmp_path]
        monkeypatch.setattr(m, 'owner_home', lambda a: home[0])
        handle = m.install(native, adapter, tmp_path, ReceiverFixture)
        await handle.ready
        handle.manager.lane_factory = FixtureLane
        home[0] = tmp_path / 'different-owner-home'
        result = await confirmed(handle.manager, interaction, 'start')
        assert 'denied' in result.lower() and channel.joins == 0
        await handle.close()
    from test_discord_receiver import ReceiverFixture
    asyncio.run(scenario())


def test_persona_home_must_match_registered_adapter_owner(tmp_path, monkeypatch):
    import pytest
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native, 'off')
        # Source-owned profile resolver boundary, not ambient selected Desktop home.
        monkeypatch.setattr(m, 'owner_home', lambda adapter: tmp_path / 'other', raising=False)
        with pytest.raises(ValueError, match='profile home'):
            m.install(native, adapter, tmp_path, object)
        assert native.tree.get_command('livevoice') is None and not calls
        await native.close()
    asyncio.run(scenario())


def test_command_registration_waits_for_host_sync_without_cancelling_it(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = command_http(native)
        release = asyncio.Event()
        adapter._post_connect_task = asyncio.create_task(release.wait())
        handle = m.install(native, adapter, tmp_path, object)
        await asyncio.sleep(0)
        assert native.tree.get_command('livevoice') is None and not calls
        release.set()
        assert await handle.ready
        await handle.close()
        assert not adapter._post_connect_task.cancelled()
        await native.close()
    asyncio.run(scenario())


def test_remote_registration_respects_off_and_never_bulk_overwrites(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        for policy in ('off', 'safe', 'bulk'):
            native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
            adapter, remote, calls = command_http(native, policy)
            async def forbidden():
                raise AssertionError('plugin must never bulk-sync shared tree')
            native.tree.sync = forbidden
            handle = m.install(native, adapter, tmp_path, object)
            await handle.ready
            if policy == 'off':
                assert not calls and not remote
                assert not handle.synced
            else:
                assert handle.synced and 991 in remote
                assert [x[0] for x in calls] == ['fetch', 'upsert', 'fetch']
            await handle.close()
            assert not remote
            await native.close()
    asyncio.run(scenario())


def test_constructor_rolls_back_listener_that_mutates_before_raising(tmp_path):
    import pytest
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        original = native.add_listener
        calls = []
        def add(fn, name):
            original(fn, name)
            calls.append(name)
            if len(calls) == 2:
                raise RuntimeError('partial listener add')
        native.add_listener = add
        with pytest.raises(RuntimeError, match='partial listener'):
            m.install(native, command_http(native, 'off')[0], tmp_path, object)
        assert native.tree.get_command('livevoice') is None
        assert not any(native.extra_events.values())
        await native.close()
    asyncio.run(scenario())


def test_prompt_preserves_current_bot_identity_without_private_memory(tmp_path):
    m = importlib.import_module('discord_voice.native')
    from test_discord_session import boundaries
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        native.user.display_name = 'Owner Bot'
        (tmp_path / 'SOUL.md').write_text('A calm helpful persona.')
        (tmp_path / 'memories').mkdir()
        (tmp_path / 'memories/MEMORY.md').write_text('PRIVATE TASK SENTINEL')
        command_map = {}
        native.add_listener = lambda fn,name: None
        native.remove_listener = lambda fn,name: None
        async def sync():
            return []
        native.tree = NS(add_command=lambda c: command_map.setdefault(c.name,c), get_command=command_map.get,
                         remove_command=lambda n: command_map.pop(n,None), sync=sync)
        adapter._get_discord_command_sync_policy = lambda: 'off'
        handle = m.install(native, adapter, tmp_path, object)
        await handle.ready
        assert 'IDENTITY: You are Owner Bot.' in handle.manager.prompt
        assert 'A calm helpful persona.' in handle.manager.prompt
        assert 'PRIVATE TASK SENTINEL' not in handle.manager.prompt
        await handle.close()
    asyncio.run(scenario())


def test_unload_cancels_pending_tree_sync_and_removes_local_handlers(tmp_path):
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        entered = asyncio.Event()
        async def sync():
            entered.set()
            await asyncio.Event().wait()
        adapter, remote, calls = command_http(native)
        native.http.get_global_commands = lambda app_id: sync()
        handle = m.install(native, adapter, tmp_path, object)
        await entered.wait()
        await handle.close()
        assert handle.ready.done()
        assert native.tree.get_command('livevoice') is None
        await native.close()
    asyncio.run(scenario())


def test_membership_event_invalidates_consent_without_ending_owner_call(tmp_path):
    m = importlib.import_module('discord_voice.native')
    from test_discord_session import boundaries, FixtureLane, confirmed
    from test_discord_receiver import ReceiverFixture

    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        registered = {}
        commands_by_name = {}
        native.add_listener = lambda fn,name: registered.setdefault(name, fn)
        native.remove_listener = lambda fn,name: registered.pop(name, None)
        async def sync():
            return []
        native.tree = NS(add_command=lambda c: commands_by_name.setdefault(c.name,c),
                         get_command=commands_by_name.get, remove_command=lambda n: commands_by_name.pop(n,None), sync=sync)
        adapter._get_discord_command_sync_policy = lambda: 'off'
        handle = m.install(native, adapter, tmp_path, ReceiverFixture)
        await handle.ready
        handle.manager.lane_factory = FixtureLane
        assert await confirmed(handle.manager, interaction, 'start') == 'Voice started.'
        session = handle.manager.sessions[11]
        session.source.feed(b'\x01' * 3840)
        stranger = NS(id=789, bot=False, guild=interaction.guild)
        await registered['on_voice_state_update'](stranger, NS(channel=None), NS(channel=channel))
        assert session.state == 'waiting'
        assert not session.consents
        assert session.source.buffered_bytes == 0
        assert session.source.read() == bytes(3840)
        await asyncio.sleep(.05)
        assert handle.manager.sessions[11] is session
        assert session.lane is None and session.source.read() == b''
        await handle.close()
    asyncio.run(scenario())


def test_native_registration_uses_existing_bot_and_removes_only_own_handlers(tmp_path):
    assert (ROOT / 'discord_voice/native.py').exists(), 'Native integration missing'
    m = importlib.import_module('discord_voice.native')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        # Fixture tree synchronization, deliberately no HTTP.
        synced = []
        async def sync():
            synced.append(True)
            return []
        native.tree.sync = sync
        adapter, remote, calls = command_http(native)
        handle = m.install(native, adapter, home=tmp_path, receiver_class=object)
        await handle.ready
        assert native.tree.get_command('livevoice') is handle.command
        assert not synced and handle.synced
        assert len(handle.command.parameters) == 1
        assert [x.value for x in handle.command.parameters[0].choices] == ['start', 'pause', 'resume', 'stop', 'status', 'consent', 'revoke']
        assert native.extra_events.get('on_voice_state_update')
        await handle.close()
        assert native.tree.get_command('livevoice') is None
        assert not native.extra_events['on_voice_state_update']
        await native.close()
    asyncio.run(scenario())
