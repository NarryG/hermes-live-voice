"""Narrow per-instance adapter around the factory's native VoiceReceiver.

Host decrypt/DAVE/Opus remain unchanged; unknown SSRC is never inferred.
No references to host receiver/mixer registries are mutated.
"""
import logging
import struct
import threading
import time
from typing import NamedTuple


logger = logging.getLogger(__name__)


class SpeakerPCM(NamedTuple):
    speaker: int
    received_at: float
    rtp_timestamp: int
    pcm: bytes


def new_receiver(base, voice_client, owner, guard, deliver):
    owners = frozenset({owner} if type(owner) is int else owner)
    class OwnedReceiver(base):
        def __init__(self):
            super().__init__(voice_client, allowed_user_ids={str(uid) for uid in owners})
            self._lock = threading.RLock()
            self._rejected = set()
            self._seen = set()
            self._drops = 0
            self._delivered = 0
            self._snapshot = None
            self._installed = None

        def map_ssrc(self, ssrc, user_id):
            if type(ssrc) is not int or type(user_id) is not int or ssrc <= 0 or user_id <= 0:
                return
            with self._lock:
                old = self._ssrc_to_user.get(ssrc)
                if user_id not in owners or (old is not None and old != user_id):
                    # Bounded map: remember only previously accepted SSRC conflicts.
                    if old is not None:
                        self._rejected.add(ssrc)
                    self._ssrc_to_user.pop(ssrc, None)
                    if hasattr(self, '_decoders'):
                        self._decoders.pop(ssrc, None)
                    return
                if ssrc not in self._rejected and (ssrc in self._seen or len(self._seen) < 32):
                    self._seen.add(ssrc)
                    super().map_ssrc(ssrc, user_id)

        def start(self):
            conn = self._vc._connection
            ws = getattr(conn, 'ws', None)
            self._snapshot = (conn, conn.hook, ws, getattr(ws, '_hook', None))
            try:
                super().start()
            except BaseException:
                self._installed = conn.hook
                self.stop()
                raise
            self._installed = conn.hook

        def stop(self):
            try:
                super().stop()
            finally:
                if self._snapshot:
                    conn, old, ws, ws_old = self._snapshot
                    if conn.hook is self._installed:
                        conn.hook = old
                    if ws is not None and getattr(ws, '_hook', None) is self._installed:
                        ws._hook = ws_old
                    live_ws = getattr(conn, 'ws', None)
                    if live_ws is not ws and getattr(live_ws, '_hook', None) is self._installed:
                        live_ws._hook = old
                    self._snapshot = None
                self._rejected.clear()
                self._seen.clear()

        def _on_packet(self, packet):
            if len(packet) < 16 or packet[0] >> 6 != 2 or packet[1] & 0x7f != 0x78:
                return
            ssrc = struct.unpack_from('>I', packet, 8)[0]
            with self._lock:
                if not self._running or not guard() or self._ssrc_to_user.get(ssrc) not in owners or ssrc in self._rejected:
                    self._buffers.clear()
                    if self._drops < 10:
                        self._drops += 1
                        logger.info('Live Voice rx: dropping ssrc=%s (running=%s guard=%s mapped=%s rejected=%s)',
                                    ssrc, self._running, bool(guard()),
                                    self._ssrc_to_user.get(ssrc), ssrc in self._rejected)
                    return
                super()._on_packet(packet)
                # Host only buffers one decoded Opus packet before we drain.
                pcm = bytes(self._buffers.pop(ssrc, b''))
                self._buffers.clear()
                if hasattr(self, '_last_packet_time'):
                    self._last_packet_time.clear()
                if pcm and len(pcm) <= 23040 and guard():
                    if self._delivered < 10:
                        self._delivered += 1
                        logger.info('Live Voice rx: delivering %d bytes from user=%s', len(pcm), self._ssrc_to_user[ssrc])
                    deliver(SpeakerPCM(self._ssrc_to_user[ssrc], time.monotonic(),
                                       struct.unpack_from('>I', packet, 4)[0], pcm))

    return OwnedReceiver()
