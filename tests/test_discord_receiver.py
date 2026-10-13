"""Fixture native receiver boundary (not real encryption/network)."""
import asyncio
import struct
import threading
from types import SimpleNamespace as NS
from test_discord_pcm import module

class ReceiverFixture:
    def __init__(self, vc, allowed_user_ids=None):
        self._vc = vc
        self._lock = threading.Lock()
        self._buffers = {}
        self._ssrc_to_user = {}
        self._running = False
    def start(self):
        conn = self._vc._connection
        old = conn.hook
        async def hook(ws, msg):
            if old:
                await old(ws, msg)
        conn.hook = conn.ws._hook = hook
        conn.add_socket_listener(self._on_packet)
        self._running = True
    def stop(self):
        self._running = False
        self._vc._connection.remove_socket_listener(self._on_packet)
        self._buffers.clear()
        self._ssrc_to_user.clear()
    def map_ssrc(self, ssrc, uid):
        with self._lock:
            self._ssrc_to_user[ssrc] = uid
    def _on_packet(self, packet):
        ssrc = struct.unpack_from('>I', packet, 8)[0]
        with self._lock:
            self._buffers.setdefault(ssrc, bytearray()).extend(b'\x01' * 3840)


def connection(fail=False):
    listeners = []
    async def old(ws, msg):
        pass
    def add(fn):
        listeners.append(fn)
        if fail:
            raise RuntimeError('fixture partial start')
    return NS(hook=old, ws=NS(_hook=old), add_socket_listener=add,
              remove_socket_listener=lambda fn: listeners.remove(fn) if fn in listeners else None, listeners=listeners)


def packet(ssrc):
    return struct.pack('>BBHII', 0x80, 0x78, 1, 1, ssrc) + bytes(8)


def test_receiver_delivers_trusted_speaker_receive_time_and_rtp_timestamp():
    import time
    received = []
    rx = module('receiver').new_receiver(ReceiverFixture, NS(_connection=connection()),
        {123, 789}, lambda: True, received.append)
    rx.start()
    rx.map_ssrc(4, 789)
    before = time.monotonic()
    rx._on_packet(packet(4))
    assert received[0].speaker == 789
    assert before <= received[0].received_at <= time.monotonic()
    assert received[0].rtp_timestamp == 1
    assert received[0].pcm == b'\x01' * 3840
    rx.stop()


def test_receiver_exact_mapping_only_bounded_and_restores_own_hooks():
    m = module('receiver')
    conn = connection()
    old = conn.hook
    received = []
    valid = [True]
    # A multi-participant lane keeps exact-mapping semantics: only an SSRC that a
    # SPEAKING event mapped is admitted (a sole participant IS inferred, which is
    # what the two tests below cover).
    rx = m.new_receiver(ReceiverFixture, NS(_connection=conn), {123, 789}, lambda: valid[0], received.append)
    rx.start()
    rx._on_packet(packet(4))  # unknown never decoded
    rx.map_ssrc(4, '123')  # raw malformed mapping never an integer identity
    rx._on_packet(packet(4))
    assert not received
    rx.map_ssrc(4, 123)
    rx._on_packet(packet(4))
    assert [item.pcm for item in received] == [b'\x01' * 3840]
    assert not rx._buffers
    rx.map_ssrc(4, 999)  # changing an SSRC identity must not restore admission
    rx.map_ssrc(4, 123)
    rx._on_packet(packet(4))
    assert len(received) == 1
    rx.stop()
    assert conn.hook is old and conn.ws._hook is old and not conn.listeners


def test_installed_native_receiver_hook_is_exercised_offline_when_available():
    import ast
    from collections import defaultdict
    import discord
    import logging
    from pathlib import Path
    import time
    import pytest
    host = Path('/opt/hermes/plugins/platforms/discord/adapter.py')
    if not host.exists():
        pytest.skip('optional installed-host source compatibility check')
    tree = ast.parse(host.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'VoiceReceiver')
    ns = dict(threading=threading, struct=struct, time=time, defaultdict=defaultdict,
              discord=discord, logger=logging.getLogger('fixture-native-receiver'))
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(host), 'exec'), ns)
    conn = connection()
    conn.secret_key = bytes(32)  # synthetic fixture material, not a credential
    conn.dave_session = None
    conn.ssrc = 456
    old = conn.hook
    received = []
    rx = module('receiver').new_receiver(ns['VoiceReceiver'], NS(_connection=conn), frozenset({123,789}), lambda: True, received.append)
    rx.start()
    asyncio.run(conn.hook(conn.ws, {'op': 5, 'd': {'ssrc': 4, 'user_id': '789'}}))
    assert rx._ssrc_to_user == {4: 789}
    rx._on_packet(packet(999))
    assert not rx._decoders and not rx._buffers and not received
    rx.stop()
    assert conn.hook is old and conn.ws._hook is old and not conn.listeners


def test_receiver_reconnect_restores_live_websocket_without_clobbering_replacement():
    m = module('receiver')
    conn = connection()
    original = conn.hook
    rx = m.new_receiver(ReceiverFixture, NS(_connection=conn), 123, lambda: True, lambda pcm: None)
    rx.start()
    conn.ws = NS(_hook=conn.hook)
    rx.stop()
    assert conn.hook is original and conn.ws._hook is original
    rx = m.new_receiver(ReceiverFixture, NS(_connection=conn), 123, lambda: True, lambda pcm: None)
    rx.start()
    replacement = object()
    conn.hook = conn.ws._hook = replacement
    rx.stop()
    assert conn.hook is replacement and conn.ws._hook is replacement


def test_receiver_identity_state_is_bounded_under_ssrc_churn():
    m = module('receiver')
    conn = connection()
    rx = m.new_receiver(ReceiverFixture, NS(_connection=conn), 123, lambda: True, lambda pcm: None)
    rx.start()
    for ssrc in range(1, 1000):
        rx.map_ssrc(ssrc, 123)
        rx.map_ssrc(ssrc, 999)
    assert len(rx._rejected) <= 32
    assert len(rx._ssrc_to_user) <= 32
    rx.stop()


def test_receiver_partial_start_rolls_back():
    m = module('receiver')
    conn = connection(True)
    old = conn.hook
    rx = m.new_receiver(ReceiverFixture, NS(_connection=conn), 123, lambda: True, lambda pcm: None)
    try:
        rx.start()
    except RuntimeError:
        pass
    assert conn.hook is old and conn.ws._hook is old and not conn.listeners

def test_sole_participant_is_inferred_so_the_lane_is_not_deaf():
    """One participant, no SPEAKING event (they were already talking at join)."""
    received = []
    rx = module('receiver').new_receiver(ReceiverFixture, NS(_connection=connection()),
        123, lambda: True, received.append)
    rx.start()
    rx._on_packet(packet(4))  # was dropped forever before the fix
    assert [item.speaker for item in received] == [123]
    assert received[0].pcm == b'\x01' * 3840
    rx.stop()


def test_unmapped_ssrc_with_multiple_participants_stays_dropped():
    """Multi-user lanes must not attribute an unknown SSRC to whoever is first."""
    received = []
    rx = module('receiver').new_receiver(ReceiverFixture, NS(_connection=connection()),
        {123, 789}, lambda: True, received.append)
    rx.start()
    rx._on_packet(packet(4))
    assert not received
    rx.stop()


def test_speaking_events_map_each_participant_for_multi_user_lanes():
    """Every participant's SPEAKING event maps their own SSRC, not just the owner."""
    import asyncio as _asyncio
    received = []
    conn = connection()
    rx = module('receiver').new_receiver(ReceiverFixture, NS(_connection=conn),
        {123, 789}, lambda: True, received.append)
    rx.start()
    hook = conn.hook
    for ssrc, uid in ((4, 123), (9, 789)):
        _asyncio.run(hook(None, {'op': 5, 'd': {'ssrc': ssrc, 'user_id': uid}}))
    rx._on_packet(packet(4))
    rx._on_packet(packet(9))
    assert sorted(item.speaker for item in received) == [123, 789]
    rx.stop()
    assert not conn.listeners


def test_speaking_hook_is_removed_with_the_receiver():
    """The mapping hook must not outlive the lane's receiver."""
    import asyncio as _asyncio
    conn = connection()
    original = conn.hook
    rx = module('receiver').new_receiver(ReceiverFixture, NS(_connection=conn), 123, lambda: True, lambda item: None)
    rx.start()
    assert conn.hook is not original
    _asyncio.run(conn.hook(None, {'op': 5, 'd': {'ssrc': 4, 'user_id': 123}}))
    assert rx._ssrc_to_user.get(4) == 123
    rx.stop()
    assert conn.hook is original
