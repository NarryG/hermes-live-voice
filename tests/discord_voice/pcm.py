"""Carry-correct signed little-endian PCM without audioop.

Conversion adapted from TheSmokeDev/hermes-talk talk_discord.py (MIT).
Copyright (c) TheSmokeDev and contributors. See LICENSE and NOTICE.md.
"""
import struct
import math


class GroupPCM:
    """Bounded 20ms receive-clock mix; RTP clocks are not shared across users.

    Keep each admitted speaker's converter separate. Wait 40ms for overlapping
    packets, sum/clamp mono samples, and send at most one second per append.
    Overload is explicit, never an implicit truncated conversation.
    """
    FRAME = 960  # 20ms, 24kHz mono s16

    def __init__(self, speakers, max_frames=100):
        self.speakers = frozenset(speakers)
        if not self.speakers or len(self.speakers) > 16:
            raise ValueError('Invalid speaker set.')
        self.converters = {uid: InputPCM() for uid in self.speakers}
        self.next_slot = {}
        self.frames = {}
        self.max_frames = max_frames
        self.flushed = -1

    @property
    def buffered_frames(self):
        return len(self.frames)

    def feed(self, packet):
        if (type(packet.speaker) is not int or packet.speaker not in self.speakers
                or not math.isfinite(packet.received_at) or packet.received_at < 0
                or not packet.pcm or len(packet.pcm) > 23040 or len(packet.pcm) % 4):
            raise ValueError('Invalid speaker audio.')
        start = max(int(packet.received_at / .02), self.next_slot.get(packet.speaker, 0))
        if start <= self.flushed:
            raise BufferError('Late speaker audio.')
        pcm = self.converters[packet.speaker].feed(packet.pcm)
        count = (len(pcm) + self.FRAME - 1) // self.FRAME
        if self.frames and start + count - min(self.frames) > self.max_frames:
            raise BufferError('Speaker audio backlog exceeded.')
        if not self.frames and count > self.max_frames:
            raise BufferError('Speaker audio backlog exceeded.')
        for i in range(count):
            data = pcm[i*self.FRAME:(i+1)*self.FRAME]
            self.frames.setdefault(start+i, {})[packet.speaker] = data
        self.next_slot[packet.speaker] = start + count

    def pop_ready(self, now):
        cutoff = int(now / .02) - 2
        ready = [slot for slot in self.frames if slot <= cutoff]
        if not ready:
            return b''
        start = min(ready)
        end = min(max(ready), start + 49)
        out = bytearray()
        for slot in range(start, end + 1):
            speakers = self.frames.pop(slot, {})
            if not speakers:
                out.extend(bytes(self.FRAME))
                continue
            # Preserve partial valid samples, padding only for other overlapping
            # speakers (normal Discord Opus packets are whole 20ms frames).
            size = max(map(len, speakers.values()))
            samples = [0] * (size // 2)
            for pcm in speakers.values():
                for i, (sample,) in enumerate(struct.iter_unpack('<h', pcm)):
                    samples[i] += sample
            out.extend(b''.join(struct.pack('<h', max(-32768, min(32767, s))) for s in samples))
        self.flushed = end
        return bytes(out)


class InputPCM:
    """48 kHz stereo s16le -> 24 kHz mono s16le, pair averaging."""
    def __init__(self):
        self.carry = b''

    def feed(self, data):
        data = self.carry + data
        usable = len(data) - len(data) % 8
        self.carry = data[usable:]
        return b''.join(struct.pack('<h', sum(s) >> 2)
                        for s in struct.iter_unpack('<hhhh', data[:usable]))


class OutputPCM:
    """24 kHz mono s16le -> 48 kHz stereo; continuous interpolation."""
    def __init__(self):
        self.carry = b''
        self.previous = None

    def feed(self, data):
        data = self.carry + data
        usable = len(data) - len(data) % 2
        self.carry = data[usable:]
        out = bytearray()
        for (sample,) in struct.iter_unpack('<h', data[:usable]):
            previous = sample if self.previous is None else self.previous
            midpoint = int((previous + sample) / 2)
            out.extend(struct.pack('<hhhh', midpoint, midpoint, sample, sample))
            self.previous = sample
        return bytes(out)
