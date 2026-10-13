"""Integrated OFFLINE native command -> metadata owner -> HTTP -> local Opus RTP."""
import asyncio
import importlib
import json
import struct
import sys
from types import SimpleNamespace as NS

import discord
from discord.ext import commands
from aiohttp import web
import pytest
from test_discord_owner import FIXTURE, fixture
from test_discord_session import boundaries
from test_discord_receiver import ReceiverFixture
from test_discord_webrtc import eventually
from fixtures.fake_webrtc_provider import FakeProvider


@pytest.mark.parametrize('speaker_count,ending', [(1,'pause'), (2,'revoke'), (1,'config-change'), (1,'voice-mismatch')])
def test_native_command_lazy_owner_preflight_and_real_local_http_webrtc(tmp_path, monkeypatch, speaker_count, ending):
    async def run():
        owner_api = importlib.import_module('discord_voice.owner')
        native_api = importlib.import_module('discord_voice.native')
        broker_api = importlib.import_module('discord_voice.broker')
        cli, home, env, scope, metadata = fixture(tmp_path)
        (home / 'SOUL.md').write_text('Fixture Soul only.')
        (home / 'USER.md').write_text('PRIVATE MUST NOT LEAVE HOME')
        native = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        native._connection.user = discord.ClientUser(state=native._connection,
            data={'id':'456', 'username':'FixtureBot', 'discriminator':'0000', 'avatar':None})
        _, adapter, interaction, channel, allowed = boundaries()
        guest = NS(id=789, bot=False, voice=NS(channel=channel))
        if speaker_count == 2:
            channel.members.append(guest)
            get_member = interaction.guild.get_member
            interaction.guild.get_member = lambda uid: guest if uid == 789 else get_member(uid)
            adapter._build_slash_event = lambda i,t: NS(source=NS(user_id=str(i.user.id),
                guild_id='11', chat_id='33', profile=None))
        adapter._client = native
        adapter._get_discord_command_sync_policy = lambda: 'off'
        adapter.gateway_runner._owning_profile = lambda a,p: (a is adapter, None)
        monkeypatch.setattr(native_api, 'owner_home', lambda a: home)
        monkeypatch.setattr(owner_api.SelectedCLIOwner, '_binary', lambda s: (sys.executable, str(FIXTURE)))
        provider = FakeProvider()
        requests = []
        async def negotiate(request):
            assert request.headers['Authorization'].startswith('Bearer FAKE.')
            assert request.headers['ChatGPT-Account-ID'] == 'FAKE-account'
            payload = await request.json()
            assert payload['session']['model'] == 'gpt-live-1-codex'
            assert payload['session']['audio']['output']['voice'] == 'cove'
            assert 'Fixture Soul only.' in payload['session']['instructions']
            assert 'PRIVATE' not in payload['session']['instructions']
            assert payload['session']['delegation'] == {'type':'client'}
            requests.append(payload)  # no secret header recording
            from aiortc import RTCSessionDescription
            await provider.pc.setRemoteDescription(RTCSessionDescription(payload['sdp'], 'offer'))
            await provider.pc.setLocalDescription(await provider.pc.createAnswer())
            return web.Response(status=201, text=provider.pc.localDescription.sdp,
                                headers={'Location':'/realtime/calls/rtc_FAKE-native'})
        app = web.Application()
        app.router.add_post('/FAKE-local-negotiation', negotiate)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, '127.0.0.1', 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        real_post = broker_api.post_json
        async def explicitly_fake_http_boundary(url, headers, payload):
            assert url == 'https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas'
            return await real_post(f'http://127.0.0.1:{port}/FAKE-local-negotiation', headers, payload)
        monkeypatch.setattr(broker_api, 'post_json', explicitly_fake_http_boundary)
        capture = owner_api.capture_selection(home, environ=env, cwd=tmp_path)
        handle = native_api.install(native, adapter, home, ReceiverFixture, selected_capture=capture)
        await handle.ready
        replies = []
        async def defer(**kwargs):
            assert kwargs == {'ephemeral':True}
        async def send(message, **kwargs):
            replies.append(message)
        interaction.response = NS(defer=defer)
        interaction.followup = NS(send=send)
        try:
            command = native.tree.get_command('livevoice')
            assert command is handle.command
            assert handle.manager.lane_factory.admitted is True
            assert handle.manager.lane_factory.account_ready is False
            if ending == 'voice-mismatch':
                handle.manager.voice = 'juniper'  # not the CLI-selected cove; must not select it
            guest_interaction = NS(**vars(interaction))
            guest_interaction.user = guest
            await command.callback(interaction, 'start')
            if ending == 'voice-mismatch':
                assert 'preflight refused' in replies[-1]
                assert channel.joins == 0 and not requests and not handle.manager.sessions
                return
            assert replies[-1] == 'Voice started.'
            session = handle.manager.sessions[11]
            lane = session.lane
            assert lane.ready and lane.version == 'v3' and lane.failed is False
            # Native mixed PCM travels through actual local Opus RTP.
            from discord_voice.receiver import SpeakerPCM
            import time
            import math
            for i in range(20):
                values = [int(7000*math.sin(2*math.pi*440*(i*960+n)/48000)) for n in range(960)]
                pcm = struct.pack('<1920h', *(v for v in values for _ in range(2)))
                session.incoming.put_nowait((session.generation, SpeakerPCM(speaker=123,
                    pcm=pcm, received_at=time.monotonic()+i*.02, rtp_timestamp=i*960)))
                if speaker_count == 2:
                    session.incoming.put_nowait((session.generation, SpeakerPCM(speaker=789,
                        pcm=pcm, received_at=time.monotonic()+i*.02, rtp_timestamp=100000+i*960)))
            await eventually(lambda: len(provider.input_frames) >= 20 and session.source.buffered_bytes > 0)
            assert any(max(abs(v) for v in struct.unpack('<'+str(f.samples*2)+'h', bytes(f.planes[0])[:f.samples*4])) > 1000
                       for f in provider.input_frames)
            output = session.source.read()
            assert len(output) == 3840 and any(output)
            await provider.send({'type':'delegation.created', 'item': {'type':'delegation', 'target':'client',
                'id':'FAKE-native-action', 'content':[{'type':'input_text','text':'Run a task'}]}})
            await eventually(lambda: bool(provider.control))
            assert 'not executed' in provider.control[0]['content'][0]['text']
            if ending == 'config-change':
                await eventually(lambda: session.source.buffered_bytes > 0)
                (cli/'config.toml').write_text('# FAKE source generation changed\n')
                assert session.source.read() == bytes(3840)  # fence BEFORE async watcher/cleanup
                await eventually(lambda: not handle.manager.sessions)
                await eventually(lambda: lane.pc.connectionState == 'closed')
                assert handle.manager.lane_factory.account_ready is False
            elif speaker_count == 2:
                await command.callback(guest_interaction, 'revoke')
                assert replies[-1] == 'Consent revoked; all call audio paused and discarded.'
                assert session.source.read() == b''
            else:
                await command.callback(interaction, 'pause')
                assert replies[-1] == 'Voice paused; audio discarded.'
            assert lane.pc.connectionState == 'closed' and lane.audio.empty()
            await command.callback(interaction, 'stop')
            assert not handle.manager.sessions and channel.guild.voice_client is None
            assert 'FAKE.' not in ''.join(replies)
        finally:
            await handle.close()
            assert handle.manager.lane_factory._owner._proc.returncode is not None
            await native.close()
            await provider.close()
            await runner.cleanup()
    asyncio.run(run())
