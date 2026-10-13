"""Call-consented native Discord sessions, bound to the supplied adapter."""
import asyncio
import contextvars
import queue
import logging
import threading
import time
import uuid
from types import SimpleNamespace
from typing import NamedTuple, Any, Callable
from pathlib import Path

from .audio import PCMSource

from .pcm import GroupPCM, OutputPCM
from .receiver import new_receiver

_OVERALL_CAPACITY = threading.BoundedSemaphore(4)


logger = logging.getLogger(__name__)

class AdapterScope(NamedTuple):
    home: Path
    profile: object


class Binding(NamedTuple):
    owner: int
    guild: int
    voice: int
    text: int
    profile: object
    call_id: str


class Session:
    def __init__(self, manager, interaction, channel, profile):
        self.manager = manager
        self.guild = interaction.guild
        self.channel = channel
        self.text_channel = interaction.channel
        self.binding = Binding(interaction.user.id, self.guild.id, channel.id, interaction.channel_id, profile, uuid.uuid4().hex)
        self.vc = None
        self.receiver = None
        self.source = None
        self.lane = None
        self.pump = None
        self.generation = 0
        self.revoked = threading.Event()
        self.state = 'starting'
        self.incoming = queue.Queue(maxsize=256)  # 16 speakers, bounded UDP handoff
        self.loop = asyncio.get_running_loop()
        self.authority_context = contextvars.copy_context()
        self.audience = frozenset(m.id for m in channel.members if m.id != manager.native.user.id)
        self.suspending = False
        self.reserved = True
        self.rx_packets = 0
        self.rx_last = None
        self.rx_maxgap = 0.0
        self.rx_droughts = 0
        self.rx_overflows = 0
        self.rx_report = time.monotonic()
        self.pump_last = None
        self.pump_max_tick = 0.0
        self.out_min = 10 ** 9
        self.out_empty = 0

    def interaction(self, uid=None):
        b = self.binding
        resolve = getattr(self.guild, 'get_channel_or_thread', self.guild.get_channel)
        text = resolve(b.text)
        if text is not self.text_channel or text.id != b.text or getattr(text, 'guild', self.guild) is not self.guild:
            raise ValueError('original text channel unavailable')
        return SimpleNamespace(user=self.guild.get_member(b.owner if uid is None else uid), guild=self.guild, guild_id=b.guild,
                               channel=text, channel_id=b.text)

    def environment(self):
        b = self.binding
        me = self.guild.me
        if me.id != self.manager.native.user.id or self.revoked.is_set():
            return False
        members = tuple(m for m in self.channel.members if m.id != me.id)
        current = frozenset(m.id for m in members)
        if current != self.audience:
            self.invalidate()
            self.audience = current
            return False
        if b.owner not in current or len(current) > 16 or any(m.bot for m in members):
            return False
        for member in members + (me,):
            p = self.channel.permissions_for(member)
            if not (p.connect and p.speak and p.view_channel):
                return False
            if member is not me:
                if not member.voice or member.voice.channel is not self.channel:
                    return False
                if self.manager.authorized(self.interaction(member.id)) != b.profile:
                    return False
        if self.manager.adapter._voice_clients.get(b.guild) is not None:
            return False
        if self.vc is not None and (self.guild.voice_client is not self.vc or not self.vc.is_connected()):
            return False
        return True

    def admitted(self):
        """Joining the private channel admits a participant: there is no opt-in step.

        A single /livevoice start is the owner's consent and the audience check is
        membership, permissions and profile admission only.
        """
        try:
            return self.environment()
        except Exception:
            return False

    def guard(self, generation=None):
        # Discord's UDP/player threads do not inherit Hermes profile contextvars.
        return self.authority_context.copy().run(self._guard, generation)

    def _guard(self, generation=None):
        if self.manager.closed.is_set() or self.revoked.is_set() or (generation is not None and generation != self.generation):
            return False
        if self.manager.sessions.get(self.binding.guild) is not self:
            return False
        if generation is not None and self.state != 'active':
            return False
        if getattr(self.manager.lane_factory, 'media_current', True) is not True:
            self.revoke()
            return False
        if not self.admitted():
            self.invalidate()
            return False
        return True

    def invalidate(self):
        # Synchronous first: invalidate immutable snapshot before any player/UDP read.
        if self.revoked.is_set() or self.manager.closed.is_set():
            return
        if not self.suspending:
            self.suspending = True
            self.state = 'waiting'
            self.drain()
            self.loop.call_soon_threadsafe(self.manager.schedule_suspend, self)

    def revoke(self):
        if not self.revoked.is_set():
            self.revoked.set()
            self.drain()
            self.loop.call_soon_threadsafe(self.manager.schedule_revoke, self)

    def drain(self):
        while True:
            try:
                self.incoming.get_nowait()
            except queue.Empty:
                break
        if self.source:
            self.source.drain()
        if self.lane:
            self.lane.drain()

    def _attach_delegations(self):
        """Phase 1: run what the call delegates in the chat it was started from.

        Only when the lane was told it may delegate; a client-managed call keeps the
        refusal path untouched.
        """
        if getattr(self.manager.lane_factory, 'delegation', 'client') != 'server':
            return
        attach = getattr(self.lane, 'attach_delegations', None)
        if not callable(attach):
            return
        try:
            attach(self.delegation_queue())
        except Exception:
            # A call must still start when the executor cannot be wired (older core,
            # missing session types): the lane then refuses delegated work as before.
            logger.exception('Live Voice: no delegation executor for this call')

    def delegation_queue(self):
        from gateway.session import SessionSource

        from .delegation import ChatWork, TaskBoard
        b = self.binding
        text = self.text_channel
        parent = getattr(text, 'parent', None)
        is_thread = parent is not None or type(text).__name__ == 'Thread'
        owner = getattr(b, 'owner', None) or getattr(b, 'user', None) or b[0]
        source = SessionSource(
            platform=getattr(self.manager.adapter, 'platform', None), chat_id=str(b.text),
            chat_name=getattr(text, 'name', None),
            chat_type='thread' if is_thread else 'channel',
            user_id=str(owner), user_name=getattr(getattr(text, 'guild', None), 'name', None),
            thread_id=str(b.text) if is_thread else None,
            parent_chat_id=str(getattr(parent, 'id', '') or '') or None,
            scope_id=str(b.guild), guild_id=str(b.guild), profile=b.profile)
        work = ChatWork(self.manager.adapter, source, str(b.text))
        logger.warning('Live Voice: delegations run in chat=%s (%s)', b.text,
                       'thread' if is_thread else 'channel')
        return TaskBoard(work, self.lane.report_delegation).start()

    async def activate(self):
        if getattr(self.manager.lane_factory, 'admitted', False) is not True:
            raise RuntimeError('No admitted voice transport.')
        self.generation += 1
        epoch = self.generation
        self.state = 'starting'
        self.drain()
        self.lane = self.manager.lane_factory()
        self._attach_delegations()
        await self.lane.start(self.manager.prompt, self.manager.voice)
        supported_version = getattr(self.manager.lane_factory, 'supported_version', None)
        if (not isinstance(supported_version, str) or not supported_version
                or getattr(self.lane, 'failed', True) is not False
                or getattr(self.lane, 'ready', False) is not True
                or getattr(self.lane, 'version', None) != supported_version):
            raise RuntimeError('Voice transport is not ready in the supported version.')
        if not self.guard():
            raise RuntimeError('Voice authority revoked.')
        self.state = 'active'
        self.source = PCMSource(lambda: self.guard(epoch))
        def deliver(packet):
            # The receive leg already has slack (a 256-packet inbound queue and a timed
            # mixer window), unlike the send path which had none -- so measure it rather
            # than assume: arrival gaps here are what the provider would hear as broken.
            now = time.monotonic()
            if self.rx_last is not None:
                gap = now - self.rx_last
                if gap > self.rx_maxgap:
                    self.rx_maxgap = gap
                if gap > .08:
                    self.rx_droughts += 1
            self.rx_last = now
            self.rx_packets += 1
            if packet.speaker in self.audience and self.guard(epoch):
                try:
                    self.incoming.put_nowait((epoch, packet))
                except queue.Full:
                    self.rx_overflows += 1
                    self.revoke()
        self.receiver = new_receiver(self.manager.receiver_class, self.vc, self.audience,
                                     lambda: self.guard(epoch), deliver)
        self.receiver.start()
        self.vc.play(self.source, after=lambda error: self.revoke() if error else None)
        self.pump = asyncio.create_task(self.run(epoch))

    async def run(self, epoch):
        mixer, output_pcm = GroupPCM(self.audience), OutputPCM()
        lane, source = self.lane, self.source
        pending_output = b''
        append_task = None
        try:
            while self.guard(epoch):
                if lane.failed:
                    raise RuntimeError('Voice transport closed.')
                # Consume bounded UDP handoff independently of request ACK latency.
                for _ in range(self.incoming.maxsize):
                    try:
                        generation, packet = self.incoming.get_nowait()
                    except queue.Empty:
                        break
                    if generation == epoch and self.guard(epoch):
                        mixer.feed(packet)
                if append_task is not None and append_task.done():
                    append_task.result()
                    append_task = None
                if append_task is None:
                    pcm = mixer.pop_ready(time.monotonic())
                    if pcm and self.guard(epoch):
                        append_task = asyncio.create_task(lane.append(pcm))
                # What the caller hears: the playback source holds up to 500ms, so watch
                # how deep it actually runs -- depth near zero is an audible gap, however
                # healthy the rest of the pipeline looks.
                depth = source.buffered_bytes
                if depth < self.out_min:
                    self.out_min = depth
                if depth == 0:
                    self.out_empty += 1
                if not pending_output and source.buffered_bytes < source.limit and not lane.audio.empty():
                    pcm = lane.audio.get_nowait()
                    if len(pcm) > 48000 or len(pcm) % 2:
                        raise RuntimeError('Invalid output audio geometry.')
                    pending_output = output_pcm.feed(pcm)
                if pending_output and self.guard(epoch):
                    accepted = source.feed(pending_output)
                    pending_output = pending_output[accepted:]
                now = time.monotonic()
                # How long this 10ms pump was itself held off the CPU. The voice process
                # shares its event loop with agent turns, and a starved pump is what the
                # caller hears as gaps -- so measure it rather than infer it.
                if self.pump_last is not None:
                    tick = now - self.pump_last
                    if tick > self.pump_max_tick:
                        self.pump_max_tick = tick
                self.pump_last = now
                if now - self.rx_report >= 10.0:
                    self.rx_report = now
                    logger.warning('Live Voice rx leg: packets=%d maxgap=%.3f droughts=%d '
                                   'overflows=%d inbound=%d mixer_ready=%d '
                                   'playback_min=%d playback_empty=%d provider_q=%d',
                                   self.rx_packets, self.rx_maxgap, self.rx_droughts,
                                   self.rx_overflows, self.incoming.qsize(),
                                   0 if append_task is not None else 1,
                                   self.out_min, self.out_empty, lane.audio.qsize())
                    logger.warning('Live Voice pump: max_tick=%.3fs (10ms cadence expected)',
                                   self.pump_max_tick)
                    self.pump_max_tick = 0.0
                    self.out_min = source.limit
                await asyncio.sleep(.01)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.revoke()
        finally:
            if append_task is not None:
                append_task.cancel()
                await asyncio.gather(append_task, return_exceptions=True)
            self.drain()

    async def deactivate(self):
        self.state = 'paused'
        self.generation += 1
        self.drain()
        if self.source:
            self.source.cleanup()
        if self.receiver:
            receiver, self.receiver = self.receiver, None
            try:
                receiver.stop()
            except Exception:
                pass  # local audio already invalidated; continue ALL other cleanup
        if self.vc:
            try:
                self.vc.stop()
            except Exception:
                pass
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
            self.pump = None
        result = 'unknown'
        if self.lane:
            lane, self.lane = self.lane, None
            try:
                result = await lane.stop()
            except Exception:
                await lane.close()
        return result


class SessionManager:
    """One call per supplied profile adapter, one guild owner, no automatic joins."""
    def __init__(self, native, adapter, receiver_class, lane_factory=None,
                 prompt='You are Hermes. Speak English. Voice conversation only; task execution is unavailable.', voice=None):
        self.native, self.adapter = native, adapter
        self.receiver_class = receiver_class
        self.lane_factory = lane_factory
        self.prompt, self.voice = prompt, voice
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.cleanup_tasks = set()
        self.closed = threading.Event()
        self.scope: AdapterScope | None = None  # Captured at native registration.
        self.scope_guard: Callable[[], bool] | None = None

    def refusal_message(self, tail):
        """Name the cause: a bare "preflight refused" hid a working owner."""
        reason = getattr(self.lane_factory, 'refusal_reason', None)
        detail = ' [%s]' % reason if isinstance(reason, str) and reason else ''
        return 'Voice unavailable: selected CLI owner/config preflight refused%s. %s' % (detail, tail)

    async def preflight(self):
        check: Any = getattr(self.lane_factory, 'preflight', None)
        if getattr(self.lane_factory, 'admitted', False) is not True or not callable(check):
            return False
        try:
            if await check(self.scope) is not True:
                return False
            voice_check = getattr(self.lane_factory, 'accepts_voice', None)
            return not callable(voice_check) or voice_check(self.voice) is True
        except Exception:
            return False

    def authorized(self, interaction):
        if self.scope_guard is not None and not self.scope_guard():
            raise ValueError('captured adapter scope changed')
        if self.adapter._client is not self.native:
            raise ValueError('native changed')
        if not interaction.guild or type(interaction.user.id) is not int or interaction.user.bot:
            raise ValueError('invalid actor')
        if not self.adapter._evaluate_slash_authorization(interaction)[0]:
            raise ValueError('native admission rejected')
        runner = self.adapter.gateway_runner
        source = self.adapter._build_slash_event(interaction, '/livevoice').source
        if (str(source.user_id) != str(interaction.user.id)
                or str(source.guild_id) != str(interaction.guild.id)
                or str(source.chat_id) != str(interaction.channel_id)):
            raise ValueError('native/source identity mismatch')
        if getattr(source, 'profile_route_rejected', False):
            raise ValueError('route rejected')
        transport = runner._transport_owner(source)
        if not transport or transport[0] is not self.adapter:
            raise ValueError('transport provenance missing')
        if self.scope_guard is not None and (self.scope is None or transport[1] != self.scope.profile):
            raise ValueError('captured adapter profile changed')
        if not runner._is_user_authorized(source) or runner._check_slash_access(source, 'livevoice') is not None:
            raise ValueError('gateway admission rejected')
        # A routed satellite is a different context/account scope: do not borrow it.
        if getattr(source, 'profile', None) not in (None, transport[1], 'default' if transport[1] is None else transport[1]):
            raise ValueError('routed profile unsupported')
        return transport[1]

    def schedule_revoke(self, session):
        session.drain()
        task = asyncio.create_task(self.revoke_session(session))
        self.cleanup_tasks.add(task)
        task.add_done_callback(self.cleanup_tasks.discard)

    def schedule_suspend(self, session):
        if self.closed.is_set() or session.revoked.is_set():
            return
        session.drain()
        task = asyncio.create_task(self.suspend_session(session))
        self.cleanup_tasks.add(task)
        task.add_done_callback(self.cleanup_tasks.discard)

    async def suspend_session(self, session):
        async with self.lock:
            if self.closed.is_set() or session.revoked.is_set():
                return
            if self.sessions.get(session.binding.guild) is session and session.suspending:
                await session.deactivate()
                session.state = 'waiting'
                session.suspending = False

    async def revoke_session(self, session):
        async with self.lock:
            if self.sessions.get(session.binding.guild) is session:
                await self.finish(session)

    async def finish(self, session):
        session.revoked.set()
        result = 'unknown'
        try:
            result = await session.deactivate()
        except Exception:
            pass  # Keep attempting connection cleanup even after provider failure.
        try:
            if session.vc and session.guild.voice_client is session.vc:
                await session.vc.disconnect(force=True)
            elif session.vc:
                # Discord disconnect/cleanup mutates the guild registration.
                # A replacement is not ours to disconnect or unregister.
                result = 'unknown'
        except Exception:
            session.state = 'cleanup_pending'
            return 'cleanup_pending'
        if session.vc and (session.guild.voice_client is session.vc or session.vc.is_connected()):
            session.state = 'cleanup_pending'
            return 'cleanup_pending'
        # Remove by identity: the start path stores the session under the guild id it
        # derives, which is not always the same value as binding.guild, and a leftover
        # entry makes every later /livevoice start answer "already reserved" while nothing
        # is actually running.
        for key, value in list(self.sessions.items()):
            if value is session:
                self.sessions.pop(key, None)
        session.state = 'stopped'
        if session.reserved:
            session.reserved = False
            _OVERALL_CAPACITY.release()
        return result

    async def command(self, interaction, operation):
        if self.closed.is_set():
            return 'Voice manager closed; gateway restart required.'
        if not await self.adapter._check_slash_authorization(interaction, '/livevoice'):
            return 'Voice access denied.'
        try:
            profile = self.authorized(interaction)
        except Exception:
            return 'Voice access denied or host authority unavailable.'
        async with self.lock:
            if self.closed.is_set():
                return 'Voice manager closed; gateway restart required.'
            guild_id = interaction.guild.id
            session = self.sessions.get(guild_id)
            if operation in ('consent', 'revoke'):
                if not session or interaction.channel_id != session.binding.text or profile != session.binding.profile:
                    return 'No matching call in this text channel.'
                if getattr(getattr(interaction.user, 'voice', None), 'channel', None) is not session.channel:
                    return 'Consent must come from a current participant in this voice channel.'
                if operation == 'revoke':
                    session.invalidate()
                    await session.deactivate()
                    session.state = 'waiting'
                    session.suspending = False
                    return 'Consent revoked; all call audio paused and discarded.'
                # Refresh changed membership before accepting a native, authenticated opt-in.
                session.environment()
                if session.suspending:
                    await session.deactivate()
                    session.state = 'waiting'
                    session.suspending = False
                if not session.environment():
                    return 'Call audience or participant admission is unavailable; consent was not recorded.'
                return 'Consent is recorded by joining this call; no separate opt-in step is required.'
            if session and (session.binding.owner != interaction.user.id or session.binding.text != interaction.channel_id or session.binding.profile != profile):
                return 'Only the call owner in the original text channel can control this voice session.'
            if operation == 'status':
                return 'Voice ' + (session.state if session else 'stopped') + '.'
            if operation in ('start', 'resume') and getattr(self.lane_factory, 'admitted', False) is not True:
                return 'Voice unavailable: no admitted conversation-only subscription transport. No Discord connection or provider was started.'
            if self.closed.is_set():
                return 'Voice manager closed; gateway restart required.'
            if operation == 'stop':
                # A session can be left under a differently-shaped key after a restart or a
                # half-finished cleanup; with one live session that is unambiguous, and leaving it
                # behind is what makes every later start answer 'already reserved'.
                live = [s for s in self.sessions.values() if getattr(s, 'state', None) != 'stopped']
                if len(live) == 1:
                    session = live[0]
                if not session:
                    return 'Voice stopped.'
                ack = await self.finish(session)
                if ack == 'cleanup_pending':
                    return 'Voice cleanup pending; audio fenced. Owner may retry stop; connection is still reserved.'
                return 'Voice stopped; provider stop ' + ack + '.'
            if operation == 'pause' and session and session.state == 'active':
                await session.deactivate()
                return 'Voice paused; audio discarded.'
            if operation == 'resume' and session and session.state in ('paused', 'waiting'):
                if not session.admitted():
                    return 'Call audience or participant admission is unavailable; owner must resume.'
                if not await self.preflight():
                    return self.refusal_message('No provider was started.')
                if self.closed.is_set():
                    return 'Voice manager closed; gateway restart required.'
                try:
                    await session.activate()
                    return 'Voice resumed with a fresh conversation.'
                except Exception:
                    await self.finish(session)
                    return 'Voice startup failed; local audio stopped.'
            if operation != 'start':
                return 'No matching voice operation for the current state.'
            # A stopped session is not a reservation; never let one block a fresh call.
            for key, value in list(self.sessions.items()):
                if getattr(value, 'state', None) == 'stopped':
                    self.sessions.pop(key, None)
                    if session is value:
                        session = None
            if session or self.sessions:
                return 'A voice session is already reserved for this profile.'
            if interaction.guild.voice_client is not None or self.adapter._voice_clients.get(guild_id) is not None:
                return 'Guild voice is already owned; no connection was taken over.'
            channel = getattr(getattr(interaction.user, 'voice', None), 'channel', None)
            if channel is None or channel.guild is not interaction.guild:
                return 'Join a private voice channel first.'
            audience = frozenset(m.id for m in channel.members if m.id != self.native.user.id)
            if not audience or len(audience) > 16 or any(type(uid) is not int or uid <= 0 for uid in audience):
                return 'Voice audience unavailable.'
            if not await self.preflight():
                return self.refusal_message('No Discord connection or provider was started.')
            if self.closed.is_set():
                return 'Voice manager closed; gateway restart required.'
            try:
                if self.authorized(interaction) != profile:
                    raise ValueError()
            except Exception:
                return 'Voice access denied or host authority unavailable.'
            # Preflight yields before a session exists to fence membership events.
            # Recheck the confirmed audience without yielding through reservation
            # and session publication; /start must not consent to a new audience.
            current_audience = frozenset(m.id for m in channel.members if m.id != self.native.user.id)
            if current_audience != audience:
                return 'Voice audience changed; run /livevoice again for the current channel.'
            if not _OVERALL_CAPACITY.acquire(blocking=False):
                return 'Overall voice session capacity reached.'
            session = None
            try:
                session = Session(self, interaction, channel, profile)
                self.sessions[guild_id] = session
                if not session.environment():
                    await self.finish(session)
                    return 'Voice requires admitted human participants, no extra bots, and Connect/Speak permissions.'
                session.vc = await channel.connect()
                if not session.admitted():
                    session.state = 'waiting'
                    return 'Voice admission changed while connecting; run /livevoice again.'
                await session.activate()
                return 'Voice started.'
            except BaseException as error:
                if session is not None:
                    await self.finish(session)
                else:
                    _OVERALL_CAPACITY.release()
                if not isinstance(error, Exception):
                    raise
                # Name the cause: a bare "startup failed" hid a working stack before.
                logger.warning('Live Voice: startup failed; local audio stopped', exc_info=True)
                return 'Voice startup failed; local audio stopped.'

    def fence(self):
        self.closed.set()
        for session in tuple(self.sessions.values()):
            session.revoked.set()

    async def close(self):
        self.fence()
        async with self.lock:
            for session in tuple(self.sessions.values()):
                await self.finish(session)
        if self.cleanup_tasks:
            await asyncio.gather(*tuple(self.cleanup_tasks), return_exceptions=True)
        shutdown = getattr(self.lane_factory, 'shutdown', None)
        if callable(shutdown):
            await shutdown()
