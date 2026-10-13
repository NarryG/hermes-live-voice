"""The lane must own its command across Discord's two representations of it.

A create answers with the payload it received (nulls included); a read answers
with the applied defaults (``integration_types`` becomes ``[0, 1]``). Comparing
those verbatim can never match: in production the lane created its command,
declared it unverified, tore its own handler down, and then refused to delete
the command it had just made - leaving a registered command with no handler,
so every invocation answered "The application did not respond".
"""
import asyncio
import importlib
import sys
from types import SimpleNamespace as NS

import discord
from discord.ext import commands
from test_discord_pcm import ROOT

sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fixture_profile_home(tmp_path, monkeypatch):
    m = importlib.import_module('discord_voice.native')
    monkeypatch.setattr(m, 'owner_home', lambda adapter: tmp_path)


# Fields Discord fills with its own default when the caller leaves them unset.
APPLIED_DEFAULTS = {'integration_types': [0, 1]}


def discord_http(native, policy='safe'):
    """Routes carrying the real create/read asymmetry; no providers, no network."""
    remote, calls = {}, []

    def as_read(data):
        out = dict(data)
        for key, default in APPLIED_DEFAULTS.items():
            if out.get(key) is None:
                out[key] = default
        return out

    async def fetch(app_id):
        calls.append(('fetch', app_id))
        return [as_read(value) for value in remote.values()]

    async def upsert(app_id, payload):
        calls.append(('upsert', app_id))
        data = dict(payload, id='991', application_id=str(app_id), version='1')
        remote[991] = data
        return dict(data)  # echoes the submission, nulls included

    async def delete(app_id, command_id):
        calls.append(('delete', app_id, command_id))
        remote.pop(command_id)

    native.http.get_global_commands = fetch
    native.http.upsert_global_command = upsert
    native.http.delete_global_command = delete
    native._connection.application_id = 456
    adapter = NS(_client=native, platform='discord',
                 _get_discord_command_sync_policy=lambda: policy)
    adapter.gateway_runner = NS(_owning_profile=lambda a, p: (a is adapter, None))
    return adapter, remote, calls


def test_registration_survives_the_read_representation(tmp_path):
    m = importlib.import_module('discord_voice.native')

    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = discord_http(native)
        handle = m.install(native, adapter, tmp_path, object)
        try:
            assert await asyncio.wait_for(handle.ready, 5) is True
            assert handle.synced and not handle.closed
            assert native.tree.get_command('livevoice') is handle.command
            assert remote and handle.remote is not None
        finally:
            await handle.close()
            await native.close()

    asyncio.run(scenario())


def test_teardown_deletes_the_command_it_registered(tmp_path):
    m = importlib.import_module('discord_voice.native')

    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = discord_http(native)
        handle = m.install(native, adapter, tmp_path, object)
        try:
            assert await asyncio.wait_for(handle.ready, 5) is True
            stored = handle.remote[1]
            assert stored['integration_types'] is None  # create echoes the null
            await handle.close()
            assert ('delete', 456, 991) in calls
            assert not remote and handle.remote is None
        finally:
            await native.close()

    asyncio.run(scenario())


def test_teardown_refuses_a_command_someone_else_changed(tmp_path):
    m = importlib.import_module('discord_voice.native')

    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        adapter, remote, calls = discord_http(native)
        handle = m.install(native, adapter, tmp_path, object)
        try:
            await asyncio.wait_for(handle.ready, 5)  # outcome is not this test's subject
            assert remote, 'registration must reach the create step to be observable'
            remote[991]['description'] = 'Replaced by another implementation'
            await handle.close()
            assert not [c for c in calls if c[0] == 'delete']
            assert handle.remote is not None
            assert 'not deleted' in handle.sync_status
        finally:
            await native.close()

    asyncio.run(scenario())
