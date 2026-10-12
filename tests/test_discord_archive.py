"""Clean archive import, using real SDK objects and fake host/HTTP boundaries."""
import asyncio
import importlib.util
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace as NS, ModuleType
import discord
from discord.ext import commands
from test_discord_pcm import ROOT
from test_discord_receiver import ReceiverFixture

VoiceReceiver = ReceiverFixture  # inspect.getmodule(type(adapter)) fixture seam
class AdapterFixture:
    pass


def test_clean_source_archive_factory_works_without_installed_talk(tmp_path, monkeypatch):
    target = tmp_path / 'archive-plugin'
    target.mkdir()
    for path in [ROOT / '__init__.py', ROOT / 'language_directive.txt']:
        shutil.copy(path, target / path.name)
    shutil.copytree(ROOT / 'discord_voice', target / 'discord_voice', ignore=shutil.ignore_patterns('__pycache__'))
    home = tmp_path / 'profile-home'
    home.mkdir()
    (home / 'SOUL.md').write_text('You are Fixture Bot. Be kind.')
    (home / 'memories').mkdir()
    (home / 'memories' / 'MEMORY.md').write_text('PRIVATE MEMORY MUST NOT APPEAR')
    constants = ModuleType('hermes_constants')
    constants.get_hermes_home = lambda: home
    constants.get_process_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, 'hermes_constants', constants)
    spec = importlib.util.spec_from_file_location('archive_livevoice_fixture', target / '__init__.py', submodule_search_locations=[str(target)])
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    factories, cleanup = [], []
    plugin.register(NS(register_platform_handler=lambda p,f: factories.append(f), on_unload=cleanup.append))
    # Ambient profile changes AFTER registration must not redirect the factory's
    # persona/home scope to a Desktop-selected or unrelated profile.
    constants.get_hermes_home = lambda: tmp_path / 'unrelated-profile'
    async def scenario():
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        async def sync():
            return []  # fixture; no Discord HTTP
        native.tree.sync = sync
        adapter = AdapterFixture()
        adapter._client = native
        adapter.platform = 'discord'
        adapter.gateway_runner = NS(_owning_profile=lambda a,p: (a is adapter, None))
        adapter._get_discord_command_sync_policy = lambda: 'off'
        factories[0](native, adapter)
        await asyncio.sleep(.02)
        command = native.tree.get_command('livevoice')
        assert command is not None
        # Factory closure is actual plugin code; no installed Talk import needed.
        cleanup[0]()
        await asyncio.sleep(.02)
        assert native.tree.get_command('livevoice') is None
        await native.close()
    asyncio.run(scenario())
