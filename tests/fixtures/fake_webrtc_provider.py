"""Explicitly FAKE provider: two local aiortc peers, no STUN/auth/provider calls."""
import asyncio
import json
import math
import struct
from fractions import Fraction

import av
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription


class FakeStereoTone(AudioStreamTrack):
    def __init__(self, rate=48000, layout='stereo'):
        super().__init__()
        self.rate, self.layout = rate, layout
        self.channels = 2 if layout == 'stereo' else 1
        self.samples = rate // 50
        self.pts = 0
        self.origin = None

    async def recv(self):
        loop = asyncio.get_running_loop()
        if self.origin is None:
            self.origin = loop.time()
        await asyncio.sleep(max(0, self.origin + self.pts / self.rate - loop.time()))
        frame = av.AudioFrame(format='s16', layout=self.layout, samples=self.samples)
        values = [int(7000 * math.sin(2 * math.pi * 440 * (self.pts+i) / self.rate))
                  for i in range(self.samples)]
        interleaved = [value for value in values for _ in range(self.channels)]
        frame.planes[0].update(struct.pack('<'+str(len(interleaved))+'h', *interleaved))
        frame.sample_rate, frame.time_base, frame.pts = self.rate, Fraction(1, self.rate), self.pts
        self.pts += self.samples
        return frame


class FakeProvider:
    def __init__(self, startup=True, startup_event=None, rate=48000, layout='stereo'):
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pc.addTrack(FakeStereoTone(rate, layout))
        self.channel = None
        self.control = []
        self.input_frames = []
        self.tasks = []
        self.startup = startup
        self.startup_event = startup_event or {'type': 'session.started', 'session': {'id': 'FAKE-session'}}
        self.channel_open = asyncio.Event()
        @self.pc.on('track')
        def track(track):
            async def receive():
                try:
                    while True:
                        self.input_frames.append(await track.recv())
                except Exception:
                    pass
            self.tasks.append(asyncio.create_task(receive()))
        @self.pc.on('datachannel')
        def datachannel(channel):
            self.channel = channel
            assert channel.label == 'oai-events' and channel.ordered is True
            @channel.on('message')
            def message(payload):
                self.control.append(json.loads(payload))
            def opened():
                self.channel_open.set()
                if self.startup:
                    channel.send(json.dumps(self.startup_event))
            if channel.readyState == 'open':
                opened()
            else:
                channel.on('open', opened)

    async def post(self, url, headers, payload):
        from discord_voice.broker import HTTPAnswer
        assert url.startswith('https://chatgpt.com/backend-api/codex/realtime/calls?')
        assert headers['Authorization'].startswith('Bearer FAKE-')
        await self.pc.setRemoteDescription(RTCSessionDescription(payload['sdp'], 'offer'))
        await self.pc.setLocalDescription(await self.pc.createAnswer())
        return HTTPAnswer(201, self.pc.localDescription.sdp, '/realtime/calls/rtc_FAKE-local')

    async def send(self, event):
        await asyncio.wait_for(self.channel_open.wait(), 3)
        self.channel.send(json.dumps(event))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.pc.close()
