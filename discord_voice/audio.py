"""Plugin-owned Discord player; no host mixer or STT changes."""
import threading
import discord


class PCMSource(discord.AudioSource):
    FRAME = 3840  # 20 ms * 48 kHz * stereo * s16

    def __init__(self, guard, max_frames=25):
        self.guard = guard
        self.limit = self.FRAME * max_frames
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self._stopped = False

    @property
    def buffered_bytes(self):
        with self._lock:
            return len(self._buffer)

    def feed(self, data):
        """Return accepted byte count; the caller retains the remainder."""
        with self._lock:
            if self._stopped or not self.guard():
                return 0
            accepted = min(len(data), max(0, self.limit - len(self._buffer)))
            self._buffer.extend(data[:accepted])
            return accepted

    def drain(self):
        with self._lock:
            self._buffer.clear()

    def read(self):
        with self._lock:
            if self._stopped:
                return b''
            if not self.guard():
                self._buffer.clear()
                return bytes(self.FRAME)
            if len(self._buffer) < self.FRAME:
                return bytes(self.FRAME)
            frame = bytes(self._buffer[:self.FRAME])
            del self._buffer[:self.FRAME]
            return frame

    def cleanup(self):
        with self._lock:
            self._stopped = True
            self._buffer.clear()
