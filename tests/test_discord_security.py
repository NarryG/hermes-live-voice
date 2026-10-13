"""Offline adversarial native boundaries; never native network/real provider."""
import asyncio
from types import SimpleNamespace as NS
import sys
import pytest
from test_discord_session import boundaries, implementation, FixtureLane, confirmed
from test_discord_receiver import ReceiverFixture, packet
from test_discord_pcm import ROOT


def manager_for(native, adapter, receiver=ReceiverFixture):
    return implementation().SessionManager(native, adapter, receiver, FixtureLane)


@pytest.mark.parametrize('gate', ['native', 'gateway', 'slash', 'provenance', 'profile', 'permission', 'bot', 'dm', 'malformed'])
def test_denial_never_connects_or_starts_a_provider(gate):
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        if gate == 'native':
            allowed[0] = False
        elif gate == 'gateway':
            adapter.gateway_runner._is_user_authorized = lambda s: False
        elif gate == 'slash':
            adapter.gateway_runner._check_slash_access = lambda s,c: 'fixture denial'
        elif gate == 'provenance':
            adapter.gateway_runner._transport_owner = lambda s: (object(), None)
        elif gate == 'profile':
            adapter._build_slash_event = lambda i,t: NS(source=NS(user_id='123', guild_id='11', chat_id='33', profile='other'))
        elif gate == 'permission':
            channel.permissions_for = lambda member: NS(connect=True, speak=False, view_channel=True)
        elif gate == 'bot':
            interaction.user.bot = True
        elif gate == 'dm':
            interaction.guild = None
        elif gate == 'malformed':
            interaction.user.id = True
        manager = manager_for(native, adapter)
        await confirmed(manager, interaction, 'start')
        assert channel.joins == 0 and not manager.sessions
        await manager.close()
    asyncio.run(scenario())


def test_concurrent_starts_reserve_exactly_one_connection_and_do_not_touch_other_guild():
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        other_voice = object()
        adapter._voice_clients[99] = other_voice
        manager = manager_for(native, adapter)
        results = await asyncio.gather(*(manager.command(interaction, 'start') for _ in range(8)))
        assert results.count('Voice started.') == 1 and channel.joins == 1
        assert adapter._voice_clients[99] is other_voice
        old = manager.sessions[11]
        await manager.command(interaction, 'stop')
        assert await confirmed(manager, interaction, 'start') == 'Voice started.'
        old.receiver = None
        assert not old.guard(old.generation)
        await manager.close()
        assert adapter._voice_clients == {99: other_voice}
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['human_join', 'bot_join', 'owner_move', 'role_revoked', 'permissions', 'disconnect', 'guild_connection_replaced'])
def test_every_player_read_fences_changed_audience_authority_or_connection(change):
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        manager = manager_for(native, adapter)
        assert await confirmed(manager, interaction, 'start') == 'Voice started.'
        session = manager.sessions[11]
        source = session.source
        source.feed(b'\x01' * 3840)
        if change in ('human_join', 'bot_join'):
            channel.members.append(NS(id=789, bot=change == 'bot_join'))
        elif change == 'owner_move':
            interaction.user.voice.channel = NS(id=999)
        elif change == 'role_revoked':
            allowed[0] = False
        elif change == 'permissions':
            channel.permissions_for = lambda m: NS(connect=True, speak=False, view_channel=True)
        elif change == 'disconnect':
            session.vc.connected = False
        elif change == 'guild_connection_replaced':
            interaction.guild.voice_client = object()
        assert source.read() == bytes(3840)
        assert source.buffered_bytes == 0
        session.receiver.map_ssrc(4, 123)
        session.receiver._on_packet(packet(4))
        assert session.incoming.empty()
        replacement = interaction.guild.voice_client if change == 'guild_connection_replaced' else None
        await manager.close()
        if replacement is not None:
            assert interaction.guild.voice_client is replacement
            assert manager.sessions[11] is session and session.reserved
            assert session.state == 'cleanup_pending'
            # Fixture-only restoration of the original registration permits retry;
            # production must never unregister/disconnect a replacement voice client.
            interaction.guild.voice_client = session.vc
            await manager.close()
            assert not manager.sessions and not session.reserved
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['connect', 'provider', 'receiver'])
def test_partial_startup_failure_rolls_back_without_exception_disclosure(phase):
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        manager = manager_for(native, adapter)
        if phase == 'connect':
            async def connect():
                raise RuntimeError('fixture PRIVATE SECRET')
            channel.connect = connect
        elif phase == 'provider':
            class FailingLane(FixtureLane):
                async def start(self, *args):
                    raise RuntimeError('fixture PRIVATE SECRET')
            manager.lane_factory = FailingLane
        else:
            class FailingReceiver(ReceiverFixture):
                def start(self):
                    super().start()
                    raise RuntimeError('fixture PRIVATE SECRET')
            manager.receiver_class = FailingReceiver
        assert await confirmed(manager, interaction, 'start') == 'Voice startup failed; local audio stopped.'
        assert not manager.sessions and interaction.guild.voice_client is None
        await manager.close()
    asyncio.run(scenario())


def test_membership_change_fences_audio_and_a_future_call_inherits_nothing():
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        guest = NS(id=789, bot=False, voice=NS(channel=channel))
        channel.members.append(guest)
        old_get = interaction.guild.get_member
        interaction.guild.get_member = lambda uid: guest if uid == 789 else old_get(uid)
        adapter._build_slash_event = lambda i,t: NS(source=NS(user_id=str(i.user.id), guild_id='11', chat_id='33', profile=None))
        guest_interaction = NS(**vars(interaction))
        guest_interaction.user = guest
        manager = manager_for(native, adapter)
        try:
            assert 'started' in await confirmed(manager, interaction, 'start')
            session = manager.sessions[11]
            assert 'recorded' in await confirmed(manager, guest_interaction, 'consent')
            channel.members.remove(guest)
            guest.voice.channel = None
            assert session.source.read() == bytes(3840)  # already-buffered audio is fenced
            assert 'resumed' in await manager.command(interaction, 'resume')
            await manager.command(interaction, 'stop')
            channel.members.append(guest)
            guest.voice.channel = channel
            assert 'started' in await confirmed(manager, interaction, 'start')
            assert manager.sessions[11].binding != session.binding  # nothing carries into a future call
        finally:
            await manager.close()
    asyncio.run(scenario())
def test_existing_guild_voice_is_refused_not_stolen():
    async def scenario():
        native, adapter, interaction, channel, allowed = boundaries()
        host_vc = object()
        interaction.guild.voice_client = host_vc
        manager = manager_for(native, adapter)
        assert 'already owned' in await confirmed(manager, interaction, 'start')
        assert channel.joins == 0 and interaction.guild.voice_client is host_vc
    asyncio.run(scenario())
