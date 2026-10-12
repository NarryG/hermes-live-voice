"""Offline direct media tests. All provider/auth values are explicitly FAKE."""
import asyncio
import importlib
import json
import pytest


def api():
    try:
        return importlib.import_module('discord_voice.webrtc')
    except ModuleNotFoundError:
        pytest.fail('Direct media-only V3 WebRTC lane not implemented')


def test_v3_parser_preserves_real_wire_fields_and_refuses_delegation():
    w = api()
    event = w.parse_event(json.dumps({'type': 'delegation.created', 'item': {
        'type': 'delegation', 'target': 'client', 'id': 'FAKE-item', 'content': [
            {'type': 'input_text', 'text': 'Run '}, {'type': 'input_text', 'text': 'a task'}]}}))
    assert event.kind == 'delegation'
    assert event.item_id == 'FAKE-item'
    assert event.text == 'Run a task'
    refusal = w.delegation_refusal(event)
    assert refusal['type'] == 'delegation.context.append'
    assert refusal['delegation_item_id'] == 'FAKE-item'
    assert refusal['content'][0]['type'] == 'input_text'
    assert 'not executed' in refusal['content'][0]['text']
    assert w.parse_event(json.dumps({'type': 'input_transcript.added', 'item': {'text': 'Hello'}})).text == 'Hello'
    assert w.parse_event(json.dumps({'type': 'turn.done', 'turn': {'role': 'assistant', 'transcript': 'Hi'}})).text == 'Hi'
    assert w.parse_event('{"type":"session.updated","session":{"id":"FAKE-session"}}').kind == 'session'
    assert w.parse_event('{"type":"output_audio.delta","audio":"AA=="}').kind == 'pcm-sideband'
    with pytest.raises(w.MediaRefused):
        w.parse_event('not json')


def test_native_factory_preflight_requires_exact_host_registered_adapter_scope():
    from pathlib import Path
    from types import SimpleNamespace
    from discord_voice.broker import SubscriptionBroker
    from test_discord_broker import fake_snapshot
    w = api()
    b = importlib.import_module('discord_voice.broker')
    snapshot = fake_snapshot(b)
    scope = SimpleNamespace(home=Path('/synthetic/FAKE-host'), profile=None)
    host = object()
    broker = SubscriptionBroker(host, lambda: snapshot)
    binding = broker.register(host, adapter_scope=scope)
    factory = w.lane_factory(broker, binding)
    assert factory.supported_version == 'v3'
    assert asyncio.run(factory.preflight(scope)) is True
    assert asyncio.run(factory.preflight(SimpleNamespace(home=scope.home, profile=scope.profile))) is False


def make_lane(w, provider, timeout=3):
    from test_discord_broker import fake_snapshot
    from discord_voice.broker import SubscriptionBroker
    host = object()
    snapshot = fake_snapshot(importlib.import_module('discord_voice.broker'))
    broker = SubscriptionBroker(host, lambda: snapshot, http_post=provider.post)
    binding = broker.register(host)
    factory = w.lane_factory(broker, binding, startup_timeout=timeout)
    assert factory.admitted is True
    return factory(), broker, binding


async def eventually(predicate, seconds=3):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.parametrize('rate,layout', [(48000, 'stereo'), (24000, 'mono'), (16000, 'mono')])
def test_actual_two_local_peers_opus_rtp_and_ordered_events(rate, layout):
    import struct
    from fixtures.fake_webrtc_provider import FakeProvider
    w = api()
    async def run():
        provider = FakeProvider(rate=rate, layout=layout)
        lane, broker, binding = make_lane(w, provider)
        try:
            assert lane.ready is False
            await lane.start('Host-owned persona; no private task context', None)
            assert lane.ready is True and lane.failed is False and lane.version == 'v3'
            # Large native mixed PCM chunk, consumed through real Opus RTP.
            pcm = struct.pack('<24000h', *([7000, -7000] * 12000))
            await lane.append(pcm)
            await eventually(lambda: len(provider.input_frames) >= 20 and not lane.audio.empty())
            incoming = lane.audio.get_nowait()
            assert incoming and len(incoming) % 2 == 0 and len(incoming) <= 48000
            assert max(abs(v) for v in struct.unpack('<'+str(len(incoming)//2)+'h', incoming)) > 1000
            assert provider.input_frames[-1].sample_rate == 48000
            decoded_input = bytes(provider.input_frames[-1].planes[0])[:provider.input_frames[-1].samples * 4]
            assert max(abs(v) for v in struct.unpack('<'+str(len(decoded_input)//2)+'h', decoded_input)) > 1000
            assert lane.media_contract == {'input': 'pcm-s16le-24000-mono-mixed',
                                            'output': 'pcm-s16le-24000-mono',
                                            'speaker_attribution': 'native-controller'}
            await provider.send({'type': 'delegation.created', 'item': {'type': 'delegation',
                                 'target': 'client', 'id': 'FAKE-action', 'content': [
                                     {'type': 'input_text', 'text': 'Execute a shell command'}]}})
            await eventually(lambda: bool(provider.control))
            assert provider.control[0]['type'] == 'delegation.context.append'
            assert 'not executed' in provider.control[0]['content'][0]['text']
            assert await lane.stop() == 'unknown'  # local close != upstream stop ACK
            assert lane.ready is False and lane.audio.empty()
            assert lane.pc.connectionState == 'closed'
        finally:
            await lane.close()
            await provider.close()
    asyncio.run(run())


@pytest.mark.parametrize('event', [
    {'type': 'session.started', 'session': {'id': 'FAKE-session', 'model': 'wrong-model'}},
    {'type': 'session.updated', 'session': {'id': 'FAKE-session', 'audio': {'output': {'voice': 'wrong-voice'}}}},
])
def test_selected_model_voice_mismatch_never_becomes_ready(event):
    from fixtures.fake_webrtc_provider import FakeProvider
    w = api()
    async def run():
        provider = FakeProvider(startup_event=event)
        lane, _, _ = make_lane(w, provider, .5)
        try:
            with pytest.raises(w.MediaRefused):
                await lane.start('Host-owned persona', None)
            assert lane.ready is False and lane.failed is True
        finally:
            await lane.close()
            await provider.close()
    asyncio.run(run())


def test_stop_cancels_blocked_append_and_waits_for_actual_peer_close():
    from fixtures.fake_webrtc_provider import FakeProvider
    w = api()
    async def run():
        provider = FakeProvider()
        lane, _, _ = make_lane(w, provider)
        try:
            await lane.start('Host persona', None)
            # Force real backpressure while RTP track pacing is gated.
            lane._gate.clear()
            append = asyncio.create_task(lane.append(b'\x01\x00' * (480 * 51)))
            await eventually(lambda: lane._track.frames.full())
            first, second = await asyncio.gather(lane.stop(), lane.stop())
            assert first == second == 'unknown'
            assert append.done()
            assert append.cancelled() or append.exception() is not None
            assert lane.pc.connectionState == 'closed'
            assert lane._track.frames.empty() and lane._track.pending == b''
            await asyncio.gather(append, return_exceptions=True)
        finally:
            await lane.close()
            await provider.close()
    asyncio.run(run())


def test_native_drain_synchronously_fences_blocked_speech_before_stop_await():
    from fixtures.fake_webrtc_provider import FakeProvider
    w = api()
    async def run():
        provider = FakeProvider()
        lane, _, _ = make_lane(w, provider)
        try:
            await lane.start('Host persona', None)
            lane._gate.clear()
            append = asyncio.create_task(lane.append(b'\x01\x00' * (480 * 51)))
            await eventually(lambda: lane._track.frames.full())
            lane.drain()  # native consent revocation is synchronous before await
            assert lane.ready is False
            assert lane._track.frames.empty()
            await asyncio.sleep(.01)
            assert append.done()
            assert append.cancelled() or append.exception() is not None
            assert lane._track.frames.empty()
            await asyncio.gather(append, return_exceptions=True)
        finally:
            await lane.close()
            await provider.close()
    asyncio.run(run())


def test_native_udp_thread_drain_is_loop_safe_and_fences_immediately():
    from fixtures.fake_webrtc_provider import FakeProvider
    w = api()
    async def run():
        provider = FakeProvider()
        lane, _, _ = make_lane(w, provider)
        try:
            await lane.start('Host persona', None)
            lane._gate.clear()
            append = asyncio.create_task(lane.append(b'\x01\x00' * (480 * 51)))
            await eventually(lambda: lane._track.frames.full())
            await asyncio.to_thread(lane.drain)
            assert not lane.ready
            await eventually(lambda: append.done())
            assert append.cancelled() or append.exception() is not None
            await asyncio.gather(append, return_exceptions=True)
            assert lane._track.frames.empty()
        finally:
            await lane.close()
            await provider.close()
    asyncio.run(run(), debug=True)


def test_shutdown_waits_for_inflight_cleanup_on_every_caller(monkeypatch):
    w = api()
    async def run():
        lane = w.MediaLane(lambda: None, lambda *args: None)
        entered, finish = asyncio.Event(), asyncio.Event()
        class SlowPeer:
            connectionState = 'connected'
            async def close(self):
                entered.set()
                await finish.wait()
                self.connectionState = 'closed'
        lane.pc = SlowPeer()
        first = asyncio.create_task(lane.close())
        await entered.wait()
        second = asyncio.create_task(lane.stop())
        await asyncio.sleep(.01)
        assert not second.done()
        finish.set()
        await asyncio.gather(first, second)
    asyncio.run(run())


@pytest.mark.parametrize('startup,event', [
    (False, None),
    (True, {'type': 'rpc.ack', 'result': {}}),
    (True, {'type': 'error', 'error': {'message': 'FAKE provider error'}}),
    (True, {'type': 'session.started', 'session': {'id': 'FAKE', 'version': 'v2'}}),
    (True, {'type': 'session.started', 'session': {}}),
])
def test_transport_open_is_not_provider_readiness(startup, event):
    from fixtures.fake_webrtc_provider import FakeProvider
    w = api()
    async def run():
        provider = FakeProvider(startup=startup, startup_event=event)
        lane, _, _ = make_lane(w, provider, .3)
        try:
            with pytest.raises(w.MediaRefused):
                await lane.start('Host persona', None)
            assert lane.failed and not lane.ready
            assert lane.pc.connectionState == 'closed'
            assert not provider.input_frames
        finally:
            await lane.close()
            await provider.close()
    asyncio.run(run())


def test_input_sample_clock_rebases_after_stall_without_burst_or_speech_drop():
    w = api()
    async def run():
        gate = asyncio.Event()
        gate.set()
        track = w.PCMTrack(gate, lambda: None)
        try:
            pcm = b'\x01\x00' * (480 * 8)
            await track.append(pcm)
            frames = [await track.recv()]
            await asyncio.sleep(.1)
            times = []
            for _ in range(7):
                frames.append(await track.recv())
                times.append(asyncio.get_running_loop().time())
            assert all(b-a > .012 for a, b in zip(times, times[1:]))
            assert b''.join(bytes(f.planes[0])[:f.samples*2] for f in frames) == pcm
            assert [f.pts for f in frames] == [480 * i for i in range(8)]
            assert all(f.sample_rate == 24000 and f.layout.name == 'mono' for f in frames)
        finally:
            track.stop()
    asyncio.run(run())


def test_reordered_session_updated_then_started_is_ready_but_identity_change_fences():
    from fixtures.fake_webrtc_provider import FakeProvider
    w = api()
    async def run():
        provider = FakeProvider(startup_event={'type': 'session.updated', 'session': {'id': 'FAKE-session'}})
        lane, _, _ = make_lane(w, provider)
        try:
            await lane.start('Host persona', None)
            await provider.send({'type': 'session.started', 'session': {'id': 'FAKE-session'}})
            await asyncio.sleep(.02)
            assert lane.ready and not lane.failed
            await provider.send({'type': 'session.updated', 'session': {'id': 'FAKE-different'}})
            await eventually(lambda: lane.failed)
            await lane.close()
            assert not lane.ready and lane.audio.empty()
        finally:
            await lane.close()
            await provider.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['control-close', 'output-overflow', 'scope-rotation'])
def test_active_media_failure_fences_all_audio_and_closes_peer(failure):
    import dataclasses
    from fixtures.fake_webrtc_provider import FakeProvider
    w = api()
    async def run():
        provider = FakeProvider()
        lane, broker, binding = make_lane(w, provider)
        try:
            await lane.start('Host persona', None)
            if failure == 'control-close':
                provider.channel.close()
            elif failure == 'output-overflow':
                import queue
                lane.audio = queue.Queue(maxsize=1)
            else:
                current = broker._bindings[binding]
                rotated = dataclasses.replace(current, generation='FAKE-rotated')
                broker._snapshot = lambda: rotated
            await eventually(lambda: lane.failed)
            await lane.close()
            assert not lane.ready and lane.audio.empty()
            assert lane.pc.connectionState == 'closed'
            with pytest.raises((w.MediaRefused, w.ScopeRefused)):
                await lane.append(b'\x00\x00' * 480)
        finally:
            await lane.close()
            await provider.close()
    asyncio.run(run())
