"""Offline native startup, with real Discord command/listener construction."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import discord
from discord.ext import commands
import pytest

ROOT = Path(__file__).resolve().parents[1]


def registration(tmp_path, monkeypatch):
    constants = ModuleType('hermes_constants')
    constants.get_hermes_home = lambda: tmp_path
    constants.get_process_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)
    profiles = ModuleType('hermes_cli.profiles')
    profiles.get_profile_dir = lambda p: tmp_path / p
    monkeypatch.setitem(sys.modules, 'hermes_cli.profiles', profiles)
    spec = importlib.util.spec_from_file_location('startup_fixture', ROOT / '__init__.py',
                                                 submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    factories, cleanup = [], []
    plugin.register(NS(register_platform_handler=lambda p, f: factories.append(f),
                       on_unload=cleanup.append))
    from startup_fixture.discord_voice import native as module
    # Host receiver discovery seam only; no providers or HTTP admitted.
    monkeypatch.setattr(module.inspect, 'getmodule', lambda cls: NS(VoiceReceiver=object))
    return factories[0], cleanup[0], module


def host(native):
    registry = {}
    adapter = NS(_client=native, platform='discord', _disconnecting=False,
                 _get_discord_command_sync_policy=lambda: 'off')
    adapter.gateway_runner = NS(_owning_profile=lambda a, p:
                                (True, registry[p][1]) if p in registry and registry[p][0] is a
                                else (False, None))
    return adapter, registry


async def settle(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


def test_factory_waits_for_authoritative_registration(tmp_path, monkeypatch):
    factory, unload, module = registration(tmp_path, monkeypatch)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        try:
            factory(native, adapter)  # connect() has not yet returned to publish
            await asyncio.sleep(0)
            assert native.tree.get_command('livevoice') is None
            registry['discord'] = (adapter, None)
            await settle(lambda: native.tree.get_command('livevoice') is not None)
            assert len(native.extra_events['on_voice_state_update']) == 1
        finally:
            unload()
            await settle(lambda: native.tree.get_command('livevoice') is None)
            await native.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stop', ['disconnect_event', 'disconnect_flag', 'closed_native', 'replacement'])
def test_pending_init_is_terminal_on_native_loss(tmp_path, monkeypatch, stop):
    factory, unload, module = registration(tmp_path, monkeypatch)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        pending = module.defer_install(native, adapter, tmp_path)
        try:
            await asyncio.sleep(0)
            if stop == 'disconnect_event':
                for listener in tuple(native.extra_events.get('on_disconnect', [])):
                    await listener()
            elif stop == 'disconnect_flag':
                adapter._disconnecting = True
            elif stop == 'closed_native':
                await native.close()
            else:
                adapter._client = object()
            await asyncio.sleep(.03)
            assert pending.ready.done()
            adapter._disconnecting = False
            adapter._client = native
            registry['discord'] = (adapter, None)
            await asyncio.sleep(.03)
            assert native.tree.get_command('livevoice') is None
            assert not native.extra_events.get('on_disconnect')
        finally:
            pending.shutdown()
            await asyncio.sleep(0)
            await native.close()
    asyncio.run(scenario())


def test_unload_fences_pending_init_before_loop_can_run(tmp_path, monkeypatch):
    factory, unload, module = registration(tmp_path, monkeypatch)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        factory(native, adapter)
        unload()
        registry['discord'] = (adapter, None)
        await asyncio.sleep(.03)
        try:
            assert native.tree.get_command('livevoice') is None
            assert not native.extra_events.get('on_voice_state_update')
        finally:
            unload()
            await asyncio.sleep(0)
            await native.close()
    asyncio.run(scenario())


def test_unload_during_factory_publication_does_not_lose_pending_handle(tmp_path, monkeypatch):
    factory, unload, module = registration(tmp_path, monkeypatch)
    original = module.defer_install
    def race(*args, **kwargs):
        pending = original(*args, **kwargs)
        unload()  # synchronous host hook races the factory before handle retention
        return pending
    monkeypatch.setattr(module, 'defer_install', race)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        factory(native, adapter)
        registry['discord'] = (adapter, None)
        await asyncio.sleep(.03)
        try:
            assert native.tree.get_command('livevoice') is None
            assert not native.extra_events.get('on_disconnect')
        finally:
            unload()
            await asyncio.sleep(0)
            await native.close()
    asyncio.run(scenario())


def test_native_replacement_while_host_sync_pending_fences_initialization(tmp_path, monkeypatch):
    factory, unload, module = registration(tmp_path, monkeypatch)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        registry['discord'] = (adapter, None)
        host_sync = asyncio.create_task(asyncio.Event().wait())
        adapter._post_connect_task = host_sync
        pending = module.defer_install(native, adapter, tmp_path)
        try:
            await asyncio.sleep(.03)
            assert not native.extra_events.get('on_voice_state_update')
            adapter._client = object()
            await settle(pending.ready.done)
            assert pending.handle is None
            assert native.tree.get_command('livevoice') is None
        finally:
            pending.shutdown()
            host_sync.cancel()
            await asyncio.gather(host_sync, return_exceptions=True)
            await asyncio.sleep(0)
            await native.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('profile', [None, 'voice-owner'])
def test_already_registered_owner_keeps_exact_scope(tmp_path, monkeypatch, profile):
    home = tmp_path if profile is None else tmp_path / profile
    home.mkdir(exist_ok=True)
    factory, unload, module = registration(home, monkeypatch)
    if profile is not None:
        monkeypatch.setattr(sys.modules['hermes_cli.profiles'], 'get_profile_dir', lambda p: tmp_path / p)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        registry['discord'] = (adapter, profile)
        pending = module.defer_install(native, adapter, home)
        try:
            await pending.ready
            assert pending.handle.manager.scope.home == home
            assert pending.handle.manager.scope.profile == profile
            assert pending.handle.manager.scope_guard()
            registry['discord'] = (adapter, 'another-owner')
            with pytest.raises(ValueError):
                # Exact registered home is unavailable for the changed owner.
                module.install(native, adapter, home, object)
            assert not pending.handle.manager.scope_guard()
        finally:
            pending.shutdown()
            if pending.handle:
                await pending.handle.close()
            await native.close()
    asyncio.run(scenario())


def test_deferred_wrong_home_fails_closed_without_persona_read(tmp_path, monkeypatch, caplog):
    factory, unload, module = registration(tmp_path, monkeypatch)
    from startup_fixture.discord_voice import owner
    reads = []
    monkeypatch.setattr(owner, '_file', lambda path: reads.append(path) or b'')
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        pending = module.defer_install(native, adapter, tmp_path)
        try:
            await asyncio.sleep(0)
            assert not reads
            registry['discord'] = (adapter, 'another-owner')
            await pending.ready
            assert pending.handle is None
            assert not reads
            assert native.tree.get_command('livevoice') is None
            assert 'restart' in caplog.text.lower()
        finally:
            pending.shutdown()
            await asyncio.sleep(0)
            await native.close()
    asyncio.run(scenario())


def test_host_sync_completion_allows_initialization(tmp_path, monkeypatch):
    factory, unload, module = registration(tmp_path, monkeypatch)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        registry['discord'] = (adapter, None)
        release = asyncio.Event()
        host_sync = adapter._post_connect_task = asyncio.create_task(release.wait())
        pending = module.defer_install(native, adapter, tmp_path)
        try:
            await asyncio.sleep(.03)
            assert pending.handle is None
            release.set()
            await pending.ready
            await pending.handle.ready
            assert native.tree.get_command('livevoice') is pending.handle.command
        finally:
            pending.shutdown()
            if pending.handle:
                await pending.handle.close()
            host_sync.cancel()
            await asyncio.gather(host_sync, return_exceptions=True)
            await native.close()
    asyncio.run(scenario())


def test_offthread_unload_fences_pending_factory(tmp_path, monkeypatch):
    factory, unload, module = registration(tmp_path, monkeypatch)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        factory(native, adapter)
        await asyncio.to_thread(unload)
        registry['discord'] = (adapter, None)
        await asyncio.sleep(.03)
        assert native.tree.get_command('livevoice') is None
        assert not native.extra_events.get('on_disconnect')
        await native.close()
    asyncio.run(scenario())


def test_missing_registration_expires_without_side_effects(tmp_path, monkeypatch, caplog):
    factory, unload, module = registration(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'REGISTRATION_TIMEOUT', .02, raising=False)
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, registry = host(native)
        try:
            factory(native, adapter)
            await asyncio.sleep(.06)
            registry['discord'] = (adapter, None)
            await asyncio.sleep(.03)
            assert native.tree.get_command('livevoice') is None
            assert 'registration' in caplog.text.lower() and 'restart' in caplog.text.lower()
        finally:
            unload()
            await asyncio.sleep(0)
            await native.close()
    asyncio.run(scenario())
