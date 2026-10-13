"""Native handler factory: no client construction, login, token or host mutation."""
import asyncio
import inspect
import json
import logging
import re
import threading
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from .session import SessionManager, AdapterScope
from .broker import V3_VOICES


def normalize_settings(settings):
    settings = settings if isinstance(settings, dict) else {}
    language = settings.get('language')
    locale = language.strip().lower().replace('_', '-') if isinstance(language, str) else ''
    language = 'Spanish' if locale.split('-', 1)[0] == 'es' else 'English'
    voice = settings.get('voice')
    voice = voice.strip().lower() if isinstance(voice, str) else None
    if voice not in V3_VOICES:
        voice = None
    return language, voice


def owner_home(adapter):
    """Actual gateway registry ownership, never the ambient Desktop selection."""
    registered, profile = adapter.gateway_runner._owning_profile(adapter, adapter.platform)
    if not registered:
        raise ValueError('Adapter profile home ownership unavailable.')
    if profile is None:
        from hermes_constants import get_process_hermes_home
        return Path(get_process_hermes_home()).resolve()
    from hermes_cli.profiles import get_profile_dir
    return Path(get_profile_dir(profile)).resolve()


# Discord answers a create with the payload it received and a read with the
# applied defaults (``integration_types`` comes back as ``[0, 1]``). The two
# representations never compare equal, so ownership is decided on the fields
# this plugin actually specified.
_SERVER_ASSIGNED = ('id', 'version', 'application_id', 'guild_id')


def specified_fields(payload):
    """Project a command payload onto caller-specified fields.

    ``None`` and empty mappings mean "not set" in Discord's application-command
    API: a create echoes them back, a read reports the applied default, so they
    carry no ownership meaning and are dropped on both sides.
    """
    if not isinstance(payload, dict):
        return payload
    result = {}
    for key, value in payload.items():
        if key in _SERVER_ASSIGNED or value is None:
            continue
        if isinstance(value, dict) and not value:
            continue
        if key == 'options' and isinstance(value, list):
            result[key] = [specified_fields(option) for option in value]
            continue
        result[key] = value
    return result


def registration_matches(registered, observed):
    """True when every field of ``registered`` survived into ``observed``."""
    sent = specified_fields(registered)
    seen = specified_fields(observed)
    if not isinstance(sent, dict) or not isinstance(seen, dict):
        return sent == seen
    return all(seen.get(key) == value for key, value in sent.items())


REGISTRATION_TIMEOUT = 60.0


class PendingNativeHandle:
    """Wait on registry publication, never infer ownership from ambient context."""
    def __init__(self, native, adapter, home, selected_capture):
        self.native = native
        self.adapter = adapter
        self.home = home
        self.selected_capture = selected_capture
        self.loop = asyncio.get_running_loop()
        self.handle = None
        self.closed = threading.Event()
        self.lock = threading.Lock()
        async def disconnect():
            self.shutdown()
        self.disconnect = disconnect
        native.add_listener(disconnect, 'on_disconnect')
        self.ready = asyncio.create_task(self.initialize())

    async def initialize(self):
        try:
            async with asyncio.timeout(REGISTRATION_TIMEOUT):
                while True:
                    with self.lock:
                        if (self.closed.is_set() or self.adapter._client is not self.native
                                or getattr(self.adapter, '_disconnecting', False)
                                or self.native.is_closed()):
                            return
                        if self.adapter.gateway_runner._owning_profile(self.adapter, self.adapter.platform)[0]:
                            if Path(self.home).resolve() != owner_home(self.adapter):
                                raise ValueError('Adapter profile home mismatch.')
                            host_sync = getattr(self.adapter, '_post_connect_task', None)
                            if host_sync is None or host_sync.done():
                                if host_sync is not None:
                                    host_sync.result()  # fail closed if host registration failed
                                # install revalidates exact registered profile/home/native.
                                # No await between ownership validation and publication.
                                self.handle = install(self.native, self.adapter, self.home,
                                                      selected_capture=self.selected_capture)
                                return
                    await asyncio.sleep(.01)
        except Exception:
            logging.getLogger(__name__).warning(
                'Live Voice native registration failed or timed out; owning gateway restart required.')
        finally:
            self.native.remove_listener(self.disconnect, 'on_disconnect')

    def shutdown(self):
        # Fence immediately, including unload from a non-gateway thread. Serialize
        # handle publication so a concurrent unload cannot lose an installed handle.
        self.closed.set()
        with self.lock:
            if self.handle is not None:
                self.handle.shutdown()
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.cancel_pending)

    def cancel_pending(self):
        self.native.remove_listener(self.disconnect, 'on_disconnect')
        if not self.ready.done():
            self.ready.cancel()


def defer_install(native, adapter, home, *, selected_capture=None):
    return PendingNativeHandle(native, adapter, home, selected_capture)


class NativeHandle:
    def __init__(self, native, adapter, home, receiver_class, selected_capture=None):
        if adapter._client is not native or Path(home).resolve() != owner_home(adapter):
            raise ValueError('Adapter profile home mismatch.')
        registered, profile = adapter.gateway_runner._owning_profile(adapter, adapter.platform)
        if not registered:
            raise ValueError('Adapter profile registration unavailable.')
        scope = AdapterScope(Path(home).resolve(), profile)
        self.native = native
        self.adapter = adapter
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.synced = False
        self.remote = None
        self.mutating = False
        self.sync_status = 'pending'
        self.cleanup_lock = asyncio.Lock()
        settings_path = Path(__file__).resolve().parents[1] / 'settings.json'
        try:
            settings = json.loads(settings_path.read_text())
        except (OSError, ValueError):
            settings = {}
        language, voice = normalize_settings(settings)
        # Persona only, NEVER USER/MEMORY/WORKING, Desktop session or task state.
        try:
            from .owner import _file
            persona = _file(Path(home) / 'SOUL.md').decode('utf-8')[:12000]
        except (OSError, ValueError):
            persona = ''
        persona = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', persona)
        identity = getattr(native.user, 'display_name', None) or getattr(native.user, 'name', None) or 'Hermes'
        prompt = 'IDENTITY: You are ' + identity + '.\n' + persona + '\nSpeak ' + str(language) + '. Keep replies brief. '
        prompt += ('Voice conversation only. No task continuity or tools are available in this call. '
                   'If asked to execute a task or access private task state, say that this lane cannot do it; '
                   'ask the caller to use their authenticated Hermes text session. Never delegate or claim task success.')
        self.manager = SessionManager(native, adapter, receiver_class, prompt=prompt, voice=voice)
        self.manager.scope = scope
        self.manager.scope_guard = lambda: (owner_home(adapter) == scope.home
            and adapter.gateway_runner._owning_profile(adapter, adapter.platform) == (True, scope.profile))
        from .owner import SelectedCLIOwner, SelectedLaneFactory, capture_selection
        captured = selected_capture if selected_capture is not None else capture_selection(scope.home)
        owner = SelectedCLIOwner(adapter, scope, captured, scope_guard=lambda:
            adapter._client is native and self.manager.scope_guard())
        self.manager.lane_factory = SelectedLaneFactory(owner)

        async def livevoice(interaction: discord.Interaction,
                            operation: Literal['start', 'pause', 'resume', 'stop', 'status', 'consent', 'revoke']):
            if self.closed or self.manager.closed.is_set():
                return
            # Native admission BEFORE defer. Manager checks gateway/source/owner too.
            if not await adapter._check_slash_authorization(interaction, '/livevoice'):
                return
            if self.closed or self.manager.closed.is_set():
                return
            await interaction.response.defer(ephemeral=True)
            try:
                message = await self.manager.command(interaction, operation)
            except Exception:
                message = 'Voice operation failed; status is unknown. No upstream details are disclosed.'
            if self.closed or self.manager.closed.is_set():
                return
            await interaction.followup.send(message, ephemeral=True)

        self.command = app_commands.Command(name='livevoice', description='Consented live voice: start, consent, revoke and owner controls', callback=livevoice)
        self.listeners = []
        async def voice_state(member, before, after):
            if self.closed or self.manager.closed.is_set():
                return
            for session in tuple(self.manager.sessions.values()):
                if member.guild.id != session.binding.guild:
                    continue
                if member.id == native.user.id and session.state == 'starting' and after.channel is session.channel:
                    continue  # our explicit first join
                if before.channel is session.channel or after.channel is session.channel:
                    if member.id == native.user.id and after.channel is not session.channel:
                        session.revoke()
                    else:
                        session.invalidate()
        async def member_update(before, after):
            if self.closed or self.manager.closed.is_set():
                return
            for session in tuple(self.manager.sessions.values()):
                if after.guild.id == session.binding.guild and after.id in session.audience:
                    session.invalidate()
        async def guild_change(*args):
            if self.closed or self.manager.closed.is_set():
                return
            guild = getattr(args[-1], 'guild', args[-1])
            for session in tuple(self.manager.sessions.values()):
                if getattr(guild, 'id', None) == session.binding.guild:
                    session.invalidate()
        async def disconnect():
            if self.closed or self.manager.closed.is_set():
                return
            for session in tuple(self.manager.sessions.values()):
                session.revoke()
        try:
            if native.tree.get_command('livevoice') is not None:
                raise RuntimeError('Local livevoice command collision.')
            if not getattr(adapter, '_post_connect_task', None) or adapter._post_connect_task.done():
                native.tree.add_command(self.command)  # refuses collisions; never override
            for name, fn in [('on_voice_state_update', voice_state), ('on_member_update', member_update),
                             ('on_guild_channel_update', guild_change), ('on_guild_role_update', guild_change),
                             ('on_guild_remove', guild_change), ('on_disconnect', disconnect)]:
                self.listeners.append((name, fn))  # add may mutate before raising
                native.add_listener(fn, name)
            self.ready = asyncio.create_task(self.sync())
        except BaseException:
            self.manager.fence()
            self.closed = True
            self.remove_local()
            raise

    def remove_local(self):
        for name, fn in self.listeners:
            self.native.remove_listener(fn, name)
        self.listeners.clear()
        if self.native.tree.get_command('livevoice') is self.command:
            self.native.tree.remove_command('livevoice')

    async def sync(self):
        try:
            host_sync = getattr(self.adapter, '_post_connect_task', None)
            if host_sync and not host_sync.done():
                await asyncio.shield(host_sync)
            if self.closed:
                return False
            if self.native.tree.get_command('livevoice') is not self.command:
                self.native.tree.add_command(self.command)
            policy = self.adapter._get_discord_command_sync_policy()
            if policy == 'off':
                self.sync_status = 'off: local only; no remote registration verified'
                return False
            if policy not in ('safe', 'bulk') or self.adapter._client is not self.native:
                raise RuntimeError('Unsupported command policy or native identity.')
            http = self.native.http
            app_id = self.native.application_id or self.native.user.id
            existing = await http.get_global_commands(app_id)
            if any(c.get('name') == self.command.name and int(c.get('type', 1)) == 1 for c in existing):
                raise RuntimeError('Remote command collision; ownership unavailable.')
            if (self.closed or self.adapter._client is not self.native
                    or self.adapter._get_discord_command_sync_policy() not in ('safe', 'bulk')
                    or self.native.tree.get_command('livevoice') is not self.command):
                raise RuntimeError('Command ownership or policy changed.')
            payload = self.command.to_dict(self.native.tree)
            self.mutating = True
            created = await asyncio.wait_for(http.upsert_global_command(app_id, payload), 30)
            self.remote = (app_id, dict(created))
            verified = await asyncio.wait_for(http.get_global_commands(app_id), 30)
            # Verify the exact command Discord reports back, on the fields we
            # specified: a create echo and a read never match verbatim.
            created_id = str(created.get('id') or '')
            observed = next((c for c in verified
                             if created_id and str(c.get('id') or '') == created_id), None)
            if observed is None:
                observed = next((c for c in verified if registration_matches(payload, c)), None)
            if observed is None or not registration_matches(payload, observed):
                raise RuntimeError('Remote command registration unverified.')
            self.synced = not self.closed
            self.sync_status = 'verified'
        except Exception:
            # A local registration is not a successful Discord sync.
            self.sync_status = 'failed: restart required; remote ownership unverified'
            if not self.closed:
                await self.close()
            return False
        finally:
            self.mutating = False
        return True

    async def remove_remote(self):
        if not self.remote or self.adapter._client is not self.native:
            return
        if self.adapter._get_discord_command_sync_policy() not in ('safe', 'bulk'):
            self.sync_status = 'off/unsupported policy: remote cleanup pending, no mutation attempted'
            return
        local = self.native.tree.get_command('livevoice')
        if local is not None and local is not self.command:
            self.sync_status = 'local command replaced: remote not deleted'
            return
        app_id, owned = self.remote
        # Never delete by name: exact application and ID, and only while the
        # command still carries the fields we registered.
        existing = await self.native.http.get_global_commands(app_id)
        current = next((c for c in existing if str(c['id']) == str(owned['id'])), None)
        if current is None:
            self.remote = None
            return
        if not registration_matches(owned, current):
            self.sync_status = 'remote replaced: not deleted'
            return
        await self.native.http.delete_global_command(app_id, int(owned['id']))
        if any(str(c['id']) == str(owned['id']) for c in await self.native.http.get_global_commands(app_id)):
            raise RuntimeError('Remote deletion unverified.')
        self.remote = None

    def shutdown(self):
        # PluginContext.on_unload is synchronous and may run off the gateway loop.
        self.manager.fence()
        if not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.close(), self.loop)

    async def close(self):
        self.closed = True
        self.manager.fence()
        if self.ready is not asyncio.current_task() and not self.ready.done():
            # Cancellation during upsert can leave an applied, unidentified remote
            # command. Await its bounded response/readback and then delete exact ID.
            if not self.mutating:
                self.ready.cancel()
            await asyncio.gather(self.ready, return_exceptions=True)
        async with self.cleanup_lock:
            await self.manager.close()
            self.remove_local()
            try:
                await self.remove_remote()
            except Exception:
                self.sync_status = 'remote cleanup pending; restart/operator reconciliation required'
            self.synced = False
            if self.remote or self.manager.sessions:
                logging.getLogger(__name__).warning('Live Voice cleanup remains pending; ownership retained. Retry cleanup on this handle; operator reconciliation may be required.')


def install(native, adapter, home, receiver_class=None, *, selected_capture=None):
    if receiver_class is None:
        receiver_class = getattr(inspect.getmodule(type(adapter)), 'VoiceReceiver', None)
        if receiver_class is None:
            raise RuntimeError('Native voice receiver capability is unavailable.')
    return NativeHandle(native, adapter, home, receiver_class, selected_capture)
