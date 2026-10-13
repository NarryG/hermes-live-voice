"""Native boundaries below are fixtures. PCM/provider child uses real plugin code."""
import asyncio
import importlib
import sys
import pytest
from types import SimpleNamespace as NS
from test_discord_pcm import ROOT
from test_discord_receiver import ReceiverFixture, connection, packet

sys.path.insert(0, str(ROOT))

def implementation():
    assert (ROOT / 'discord_voice/session.py').exists(), 'Session implementation missing'
    return importlib.import_module('discord_voice.session')


class FixtureLane:
    """Offline echo only. Not an admitted production transport or provider."""
    admitted = True  # explicit dependency injection for native lifecycle tests
    supported_version = 'v3'
    processing_disclosure = 'Offline fixture; no external provider processing.'

    @staticmethod
    async def preflight(scope):
        return True  # Test boundary only, never a live account/capability grant.

    def __init__(self):
        self.failed = False
        self.ready = False
        self.version = None
        self.audio = asyncio.Queue(maxsize=25)
        self.process = NS(returncode=None)

    async def start(self, prompt, voice=None):
        self.version = 'v3'
        self.ready = True

    async def append(self, pcm):
        self.audio.put_nowait(pcm)

    def drain(self):
        while not self.audio.empty():
            self.audio.get_nowait()

    async def close(self):
        self.ready = False
        self.failed = True
        self.process.returncode = 0
        self.drain()

    async def stop(self):
        await self.close()
        return 'acknowledged'

async def confirmed(manager, interaction, operation):
    """One invocation is the whole step: there is no opt-in confirmation."""
    return await manager.command(interaction, operation)


class VoiceFixture:
    def __init__(self, channel):
        self.channel = channel
        self._connection = connection()
        self.played = None
        self.connected = True
    def play(self, source, after=None):
        self.played = source
    def stop(self):
        pass
    def is_connected(self):
        return self.connected
    async def disconnect(self, force=False):
        self.connected = False
        self.channel.guild.voice_client = None

class ChannelFixture:
    id = 22
    def __init__(self, guild):
        self.guild = guild
        self.members = []
        self.joins = 0
    def permissions_for(self, member):
        return NS(connect=True, speak=True, view_channel=True)
    async def connect(self):
        self.joins += 1
        vc = VoiceFixture(self)
        self.guild.voice_client = vc
        return vc


def boundaries():
    guild = NS(id=11, voice_client=None)
    channel = ChannelFixture(guild)
    owner = NS(id=123, bot=False, voice=NS(channel=channel))
    bot = NS(id=456, bot=True, voice=NS(channel=channel))
    guild.me = bot
    channel.members = [owner, bot]
    members = {123: owner, 456: bot}
    guild.get_member = members.get
    text = NS(id=33)
    guild.get_channel = lambda n: channel if n == 22 else text if n == 33 else None
    native = NS(user=bot)
    allowed = [True]
    adapter = NS(_voice_clients={}, _client=native)
    source = NS(user_id='123', guild_id='11', chat_id='33', profile=None)
    runner = NS(_transport_owner=lambda s: (adapter, None),
                _is_user_authorized=lambda s: allowed[0], _check_slash_access=lambda s,c: None)
    adapter.gateway_runner = runner
    adapter.platform = 'discord'
    runner._owning_profile = lambda a,p: (a is adapter, None)
    adapter._evaluate_slash_authorization = lambda i: (allowed[0], None)
    adapter._build_slash_event = lambda i,t: NS(source=source)
    async def admission(i, command):
        return allowed[0]
    adapter._check_slash_authorization = admission
    interaction = NS(user=owner, guild=guild, guild_id=11, channel=text, channel_id=33)
    return native, adapter, interaction, channel, allowed


def test_close_fences_a_start_already_queued_behind_command_lock():
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        manager = implementation().SessionManager(native, adapter, ReceiverFixture)
        await manager.lock.acquire()
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        starting = asyncio.create_task(manager.command(interaction, 'start'))
        await asyncio.sleep(0)
        manager.lock.release()
        await closing
        assert 'closed' in (await starting).lower()
        assert channel.joins == 0 and not manager.sessions
    asyncio.run(scenario())


def test_unauthorized_guest_after_reservation_does_not_leak_capacity(monkeypatch):
    import threading
    m = implementation()
    capacity = threading.BoundedSemaphore(1)
    monkeypatch.setattr(m, '_OVERALL_CAPACITY', capacity)
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        guest = NS(id=789, bot=False, voice=NS(channel=channel))
        channel.members.append(guest)
        interaction.guild.get_member = lambda uid: interaction.user if uid == 123 else guest
        adapter._evaluate_slash_authorization = lambda i: (i.user.id == 123, None)
        manager = m.SessionManager(native, adapter, ReceiverFixture, FixtureLane)
        try:
            result = await confirmed(manager, interaction, 'start')
            assert 'failed' in result or 'requires' in result
            assert not manager.sessions and channel.joins == 0
            assert capacity.acquire(blocking=False)
            capacity.release()
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_failed_disconnect_retains_owned_session_and_allows_stop_retry():
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        # Waiting call has no provider dependency.
        guest = NS(id=789, bot=False, voice=NS(channel=channel))
        channel.members.append(guest)
        old = interaction.guild.get_member
        interaction.guild.get_member = lambda uid: guest if uid == 789 else old(uid)
        adapter._build_slash_event = lambda i,t: NS(source=NS(user_id=str(i.user.id), guild_id='11', chat_id='33', profile=None))
        manager = implementation().SessionManager(native, adapter, ReceiverFixture, FixtureLane)
        await confirmed(manager, interaction, 'start')
        session = manager.sessions[11]
        original = session.vc.disconnect
        async def failing(force=False):
            raise RuntimeError('private disconnect detail')
        session.vc.disconnect = failing
        result = await manager.command(interaction, 'stop')
        assert 'cleanup pending' in result.lower() and 'Voice stopped' not in result
        assert manager.sessions[11] is session and session.reserved
        assert session.state == 'cleanup_pending' and session.revoked.is_set()
        assert 'cleanup_pending' in await manager.command(interaction, 'status')
        # A membership suspension queued before finish cannot downgrade terminal
        # cleanup ownership back into resumable waiting state.
        session.suspending = True
        await manager.suspend_session(session)
        assert session.state == 'cleanup_pending'
        assert 'reserved' in await confirmed(manager, interaction, 'start')
        session.vc.disconnect = original
        assert 'Voice stopped' in await manager.command(interaction, 'stop')
        assert not manager.sessions and not session.reserved
        await manager.close()
    asyncio.run(scenario())


def test_original_thread_is_resolved_and_exact_cache_identity_is_fenced():
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        thread = NS(id=33, guild=interaction.guild, parent_id=44)
        interaction.channel = thread
        interaction.guild.get_channel = lambda uid: channel if uid == 22 else None
        cached = [thread]
        interaction.guild.get_channel_or_thread = lambda uid: cached[0] if uid == 33 else None
        adapter._evaluate_slash_authorization = lambda i: (i.channel is thread, None)
        manager = implementation().SessionManager(native, adapter, ReceiverFixture)
        session = implementation().Session(manager, interaction, channel, None)
        try:
            assert session.interaction().channel is thread
            assert session.environment()
            cached[0] = NS(id=33, guild=interaction.guild, parent_id=44)
            assert not session.admitted()
        finally:
            session.reserved = False  # constructed directly, no capacity acquisition
            await manager.close()
    asyncio.run(scenario())


def test_output_pump_retains_maximum_chunk_and_leaves_ready_items_queued():
    async def scenario():
        m = implementation()
        native, adapter, interaction, channel, _ = boundaries()
        manager = m.SessionManager(native, adapter, ReceiverFixture)
        session = m.Session(manager, interaction, channel, None)
        session.reserved = False
        session.guard = lambda epoch=None: True
        session.lane = NS(failed=False, audio=asyncio.Queue(), drain=lambda: None)
        session.source = m.PCMSource(lambda: True)
        first = b'\x01\x02' * 24000
        session.lane.audio.put_nowait(first)
        session.lane.audio.put_nowait(b'\x03\x04' * 480)
        task = asyncio.create_task(session.run(0))
        try:
            await asyncio.sleep(.04)
            assert session.lane.audio.qsize() == 1
            expected = m.OutputPCM().feed(first)
            received = bytearray()
            for _ in range(50):
                assert session.source.buffered_bytes >= 3840
                received.extend(session.source.read())
                await asyncio.sleep(.012)
            assert bytes(received) == expected
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())


def test_group_input_batches_normal_ack_latency_without_revocation():
    import struct
    import time
    from discord_voice.receiver import SpeakerPCM
    async def scenario():
        m = implementation()
        native, adapter, interaction, channel, _ = boundaries()
        manager = m.SessionManager(native, adapter, ReceiverFixture)
        session = m.Session(manager, interaction, channel, None)
        session.reserved = False
        session.audience = frozenset(range(1, 17))
        session.guard = lambda epoch=None: not session.revoked.is_set()
        sent = []
        async def append(data):
            await asyncio.sleep(.1)  # ordinary 100ms transport acknowledgement
            sent.append(data)
        session.lane = NS(failed=False, audio=asyncio.Queue(), drain=lambda: None, append=append)
        session.source = m.PCMSource(lambda: True)
        task = asyncio.create_task(session.run(0))
        try:
            base = int(time.monotonic() / .02) * .02 + .001
            for index in range(30):
                now = base + index * .02
                for uid in session.audience:
                    session.incoming.put_nowait((0, SpeakerPCM(uid, now, index * 960, struct.pack('<h', 100) * 1920)))
                await asyncio.sleep(.02)
            await asyncio.sleep(.4)
            assert not session.revoked.is_set()
            assert sent and all(len(data) <= 48000 for data in sent)
            samples = b''.join(sent)
            assert len(samples) == 30 * 960  # overlaps share time, not 16 serialized streams
            assert set(struct.unpack('<' + 'h' * (len(samples)//2), samples)) == {1600}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.close()
    asyncio.run(scenario())


def test_inadmissible_default_lane_refuses_before_discord_or_provider():
    m = implementation()
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        manager = m.SessionManager(native, adapter, ReceiverFixture)
        assert manager.lane_factory is None
        assert 'unavailable' in (await confirmed(manager, interaction, 'start')).lower()
        assert channel.joins == 0 and not manager.sessions
        await manager.close()
    asyncio.run(scenario())


def test_activation_requires_ready_version_and_nonfailed_transport():
    async def scenario():
        m = implementation()
        for ready, version, failed in ((False, 'v3', False), (True, 'v1', False), (True, 'v3', True)):
            native, adapter, interaction, channel, _ = boundaries()
            class InconsistentLane(FixtureLane):
                async def start(self, prompt, voice=None):
                    self.ready, self.version, self.failed = ready, version, failed
            manager = m.SessionManager(native, adapter, ReceiverFixture, InconsistentLane)
            result = await confirmed(manager, interaction, 'start')
            assert result == 'Voice startup failed; local audio stopped.'
            assert not manager.sessions and interaction.guild.voice_client is None
            await manager.close()
    asyncio.run(scenario())


def test_a_single_invocation_starts_the_call_without_an_opt_in_step():
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        guest = NS(id=789, bot=False, voice=NS(channel=channel))
        channel.members.append(guest)
        old = interaction.guild.get_member
        interaction.guild.get_member = lambda uid: guest if uid == 789 else old(uid)
        adapter._build_slash_event = lambda i,t: NS(source=NS(user_id=str(i.user.id), guild_id='11', chat_id='33', profile=None))
        manager = implementation().SessionManager(native, adapter, ReceiverFixture, FixtureLane)
        try:
            result = await manager.command(interaction, 'start')
            assert result == 'Voice started.', result
            assert channel.joins == 1 and manager.sessions
            assert 'repeat' not in result.lower()
            guest_interaction = NS(**vars(interaction))
            guest_interaction.user = guest
            assert 'recorded' in await manager.command(guest_interaction, 'consent')
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['addition', 'replacement', 'removal'])
def test_audience_change_during_start_refuses_without_taking_voice(change, monkeypatch):
    m = implementation()
    acquisitions, capacity = [], __import__('threading').BoundedSemaphore(2)

    class CapacityFixture:
        def acquire(self, blocking=False):
            acquisitions.append(True)
            return capacity.acquire(blocking=blocking)

        def release(self):
            capacity.release()

    monkeypatch.setattr(m, '_OVERALL_CAPACITY', CapacityFixture())

    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        guest = NS(id=789, bot=False, voice=NS(channel=channel))
        previous_guest = NS(id=678, bot=False, voice=NS(channel=channel))
        if change != 'addition':
            channel.members.append(previous_guest)
        members = {member.id: member for member in channel.members}
        members[guest.id] = guest
        interaction.guild.get_member = members.get
        adapter._build_slash_event = lambda i,t: NS(source=NS(user_id=str(i.user.id), guild_id='11', chat_id='33', profile=None))
        entered, release = asyncio.Event(), asyncio.Event()
        starts = []

        class DelayedLane(FixtureLane):
            @staticmethod
            async def preflight(scope):
                entered.set()
                await release.wait()
                return True

            def __init__(self):
                starts.append(True)
                super().__init__()

        manager = m.SessionManager(native, adapter, ReceiverFixture, DelayedLane)
        try:
            starting = asyncio.create_task(manager.command(interaction, 'start'))
            await entered.wait()
            assert not manager.sessions and channel.joins == 0
            if change != 'addition':
                channel.members.remove(previous_guest)
            if change != 'removal':
                channel.members.append(guest)
            release.set()
            result = await starting
            assert result == 'Voice audience changed; run /livevoice again for the current channel.', result
            assert not acquisitions and not manager.sessions
            assert channel.joins == 0 and interaction.guild.voice_client is None
            assert not starts
            # Nothing carries over from the refused attempt: one fresh start takes voice.
            assert await manager.command(interaction, 'start') == 'Voice started.'
            assert len(starts) == 1 and channel.joins == 1
        finally:
            release.set()
            await manager.close()
    asyncio.run(scenario())


def test_scope_preflight_rejection_happens_before_discord_connect():
    async def scenario():
        m = implementation()
        native, adapter, interaction, channel, _ = boundaries()
        seen = []
        scope = ('immutable-fixture-home', None)
        class RejectedLane(FixtureLane):
            @staticmethod
            async def preflight(received_scope):
                seen.append(received_scope)
                return False
        manager = m.SessionManager(native, adapter, ReceiverFixture, RejectedLane)
        manager.scope = scope
        result = await confirmed(manager, interaction, 'start')
        assert 'unavailable' in result.lower()
        assert seen == [scope] and channel.joins == 0 and not manager.sessions
        await manager.close()
    asyncio.run(scenario())


def test_receiver_stop_failure_still_closes_provider_and_owned_connection():
    m = implementation()
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        manager = m.SessionManager(native, adapter, ReceiverFixture,
            FixtureLane)
        assert await confirmed(manager, interaction, 'start') == 'Voice started.'
        session = manager.sessions[11]
        proc = session.lane.process
        def fail():
            raise RuntimeError('fixture sensitive exception')
        session.receiver.stop = fail
        assert (await manager.command(interaction, 'stop')).startswith('Voice stopped;')
        assert proc.returncode is not None and channel.guild.voice_client is None
        assert not manager.sessions
    asyncio.run(scenario())


def test_overall_session_capacity_is_bounded_across_native_adapters():
    m = implementation()
    async def scenario():
        managers = []
        try:
            for index in range(5):
                native, adapter, interaction, channel, allowed = boundaries()
                manager = m.SessionManager(native, adapter, ReceiverFixture,
                    FixtureLane)
                managers.append(manager)
                result = await confirmed(manager, interaction, 'start')
                if index < 4:
                    assert result == 'Voice started.'
                else:
                    assert result == 'Overall voice session capacity reached.'
                    assert channel.joins == 0
        finally:
            for manager in managers:
                await manager.close()
    asyncio.run(scenario())


def test_player_thread_rechecks_authority_in_original_profile_context():
    import contextvars
    m = implementation()
    active_profile = contextvars.ContextVar('fixture_profile', default='wrong-profile')
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        active_profile.set('voice-owner-profile')
        adapter.gateway_runner._is_user_authorized = lambda s: active_profile.get() == 'voice-owner-profile'
        manager = m.SessionManager(native, adapter, ReceiverFixture,
            FixtureLane)
        assert await confirmed(manager, interaction, 'start') == 'Voice started.'
        session = manager.sessions[11]
        session.source.feed(b'\x01' * 3840)
        result = []
        import threading
        thread = threading.Thread(target=lambda: result.append(session.source.read()))
        thread.start()
        thread.join()
        assert result == [b'\x01' * 3840]
        await manager.close()
    asyncio.run(scenario())


def test_gateway_source_must_match_native_actor_before_connect():
    m = implementation()
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        adapter._build_slash_event = lambda i,t: NS(source=NS(user_id='999', guild_id='11', chat_id='33', profile=None))
        manager = m.SessionManager(native, adapter, ReceiverFixture, lambda: (_ for _ in ()).throw(RuntimeError('fixture: forbidden startup')))
        assert await confirmed(manager, interaction, 'start') == 'Voice access denied or host authority unavailable.'
        assert channel.joins == 0
    asyncio.run(scenario())


def test_group_call_starts_without_an_opt_in_step_and_revoke_still_pauses():
    m = implementation()

    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        guest = NS(id=789, bot=False, voice=NS(channel=channel))
        channel.members.append(guest)
        original_get = interaction.guild.get_member
        interaction.guild.get_member = lambda uid: guest if uid == 789 else original_get(uid)
        adapter._build_slash_event = lambda i,t: NS(source=NS(user_id=str(i.user.id), guild_id='11', chat_id='33', profile=None))
        starts = []

        def lane():
            starts.append(True)
            return FixtureLane()

        lane.admitted = True
        lane.supported_version = FixtureLane.supported_version
        lane.preflight = FixtureLane.preflight
        manager = m.SessionManager(native, adapter, ReceiverFixture, lane)
        try:
            assert await confirmed(manager, interaction, 'start') == 'Voice started.'
            assert len(starts) == 1
            guest_interaction = NS(**vars(interaction))
            guest_interaction.user = guest
            assert 'recorded' in await confirmed(manager, guest_interaction, 'consent')
            session = manager.sessions[11]
            session.receiver.map_ssrc(8, 789)
            session.receiver._on_packet(packet(8))
            for _ in range(100):
                if session.source.buffered_bytes:
                    break
                await asyncio.sleep(.01)
            assert session.source.read() == b'\x01' * 3840
            assert (await manager.command(guest_interaction, 'stop')).startswith('Only the call owner')
            assert await manager.command(guest_interaction, 'revoke') == 'Consent revoked; all call audio paused and discarded.'
            assert session.source.read() == b''
            # Revoking stops and discards that participant's audio immediately; the
            # owner can then resume the call for the audience that is present.
            assert await manager.command(interaction, 'resume') == 'Voice resumed with a fresh conversation.'
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_initial_private_channel_does_not_require_bot_to_be_prejoined():
    m = implementation()
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        channel.members = [interaction.user]
        original = channel.connect
        async def connect():
            vc = await original()
            channel.members.append(channel.guild.me)
            return vc
        channel.connect = connect
        manager = m.SessionManager(native, adapter, ReceiverFixture,
            FixtureLane)
        assert await confirmed(manager, interaction, 'start') == 'Voice started.'
        await manager.close()
    asyncio.run(scenario())


def test_explicit_owner_lane_complete_offline_pcm_pause_resume_stop():
    m = implementation()
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        factory = FixtureLane
        manager = m.SessionManager(native, adapter, ReceiverFixture, factory, prompt='Fixture Bot. English.')
        assert await confirmed(manager, interaction, 'start') == 'Voice started.'
        session = manager.sessions[11]
        session.receiver.map_ssrc(4, 123)
        session.receiver._on_packet(packet(4))
        for _ in range(100):
            if session.source.buffered_bytes:
                break
            await asyncio.sleep(.01)
        assert session.source.read() == b'\x01' * 3840
        old = session.source
        assert await manager.command(interaction, 'pause') == 'Voice paused; audio discarded.'
        assert old.read() == b''
        assert await manager.command(interaction, 'resume') == 'Voice resumed with a fresh conversation.'
        assert session.source is not old
        assert await manager.command(interaction, 'stop') == 'Voice stopped; provider stop acknowledged.'
        assert not manager.sessions and channel.guild.voice_client is None
        assert not adapter._voice_clients
    asyncio.run(scenario())


def test_a_stopped_call_does_not_block_the_next_start():
    """/livevoice stop said "Voice stopped." and the next start was refused as reserved."""
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        manager = implementation().SessionManager(native, adapter, ReceiverFixture, FixtureLane)
        try:
            assert 'started' in await confirmed(manager, interaction, 'start')
            assert manager.sessions
            assert 'stopped' in (await manager.command(interaction, 'stop')).lower()
            assert manager.sessions == {}, manager.sessions
            again = await confirmed(manager, interaction, 'start')
            assert 'already reserved' not in again, again
            assert 'started' in again, again
            await manager.command(interaction, 'stop')
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_a_session_left_under_another_key_is_still_released():
    async def scenario():
        native, adapter, interaction, channel, _ = boundaries()
        manager = implementation().SessionManager(native, adapter, ReceiverFixture, FixtureLane)
        try:
            assert 'started' in await confirmed(manager, interaction, 'start')
            session = next(iter(manager.sessions.values()))
            manager.sessions.clear()
            manager.sessions['some-other-key'] = session        # stale, differently-keyed
            assert 'stopped' in (await manager.command(interaction, 'stop')).lower()
            assert manager.sessions == {}, manager.sessions
        finally:
            await manager.close()
    asyncio.run(scenario())
