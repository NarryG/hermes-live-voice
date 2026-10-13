"""Direct V3 media/control consumer. No backing agent, tools or sideband auth."""
import asyncio
import json
import logging
import queue
from dataclasses import dataclass
from fractions import Fraction

import av
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription, RTCRtpSender
from aiortc.mediastreams import MediaStreamError

from .broker import ScopeRefused, validate_sdp


logger = logging.getLogger(__name__)

class MediaRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Event:
    kind: str
    item_id: str = ''
    text: str = ''
    session: dict | None = None


def parse_event(payload):
    if not isinstance(payload, str) or len(payload) > 65536:
        raise MediaRefused('invalid provider event')
    value = None
    try:
        value = json.loads(payload)
    except (ValueError, TypeError):
        pass
    if not isinstance(value, dict) or not isinstance(value.get('type'), str):
        raise MediaRefused('invalid provider event')
    kind = value['type']
    if kind in ('session.started', 'session.updated'):
        session = value.get('session')
        if not isinstance(session, dict) or not isinstance(session.get('id'), str) or not session['id']:
            raise MediaRefused('missing provider session identity')
        return Event('session', session=session)
    if kind == 'error':
        return Event('error')  # Never forward/log arbitrary provider error text.
    if kind == 'delegation.created':
        item = value.get('item')
        if not isinstance(item, dict):
            raise MediaRefused('invalid delegation')
        if item.get('type') != 'delegation' or item.get('target') != 'client':
            return Event('ignored')
        if (not isinstance(item.get('id'), str) or not item['id'] or len(item['id']) > 256
                or not isinstance(item.get('content'), list)):
            raise MediaRefused('invalid delegation')
        text = ''.join(c['text'] for c in item['content'] if isinstance(c, dict)
                       and c.get('type') == 'input_text' and isinstance(c.get('text'), str))
        return Event('delegation', item['id'], text)
    if kind in ('input_transcript.added', 'output_transcript.added'):
        item = value.get('item', {})
        if isinstance(item, dict) and isinstance(item.get('text'), str):
            return Event(kind, text=item['text'])
    if kind == 'turn.done':
        turn = value.get('turn', {})
        if (isinstance(turn, dict) and turn.get('role') in ('assistant', 'user')
                and isinstance(turn.get('transcript'), str)):
            return Event('turn.' + turn['role'], text=turn['transcript'])
    if kind == 'output_audio.delta':
        # RTP is authoritative for this consumer. Do not double-play sideband PCM.
        return Event('pcm-sideband')
    return Event('ignored')


def delegation_refusal(event):
    return {'type': 'delegation.context.append', 'delegation_item_id': event.item_id,
            'content': [{'type': 'input_text', 'text':
                         'Task execution is unavailable in this voice call. This request was not executed.'}]}


class PCMTrack(AudioStreamTrack):
    """Bounded native mixed PCM input, paced by the local RTP sample clock."""
    CUSHION_FRAMES = 6      # 120ms of slack before sending; bounds added latency

    def __init__(self, gate, scope):
        super().__init__()
        self.gate, self.scope = gate, scope
        self.cushion = self.CUSHION_FRAMES
        self.primed = False
        self.empty_frames = 0
        self.frames = asyncio.Queue(maxsize=50)
        self.pending = b''
        self.pts = 0
        self.origin = None
        self.lock = asyncio.Lock()
        # Counters for what a listener actually hears: silence where audio was expected.
        self.frames_out = 0
        self.underruns = 0
        self.dropouts = 0
        self.in_bytes = 0
        self.in_last = None
        self.in_maxgap = 0.0
        self.in_droughts = 0
        self.late_rebases = 0
        self.max_queue = 0
        self.last_audio = None

    async def append(self, pcm):
        if not isinstance(pcm, bytes) or len(pcm) % 2 or len(pcm) > 480000:
            raise MediaRefused('invalid PCM24 mono input')
        # Counted on the TRACK, because that is where audio lands; the lane reports these.
        # Observation must never be able to break the audio path.
        try:
            now = asyncio.get_running_loop().time()
            if self.in_last is not None:
                gap = now - self.in_last
                self.in_maxgap = max(self.in_maxgap, gap)
                if gap > .08:
                    self.in_droughts += 1
            self.in_last = now
            self.in_bytes += len(pcm)
        except Exception:
            pass
        async with self.lock:
            self.pending += pcm
            self.last_audio = asyncio.get_running_loop().time()
            while len(self.pending) >= 960:
                self.scope()
                await self.frames.put(self.pending[:960])
                self.pending = self.pending[960:]

    async def _require_live(self):
        """End the sender cleanly when the lane is gone.

        aiortc treats MediaStreamError as an orderly end of a track; letting
        MediaRefused escape logs a traceback out of the RTP loop and cuts audio
        mid-sentence, which reaches the call as stuttering and dropouts.
        """
        try:
            self.scope()
        except MediaRefused as exc:
            raise MediaStreamError(str(exc)) from None

    async def recv(self):
        await self.gate.wait()
        await self._require_live()
        loop = asyncio.get_running_loop()
        if self.origin is None:
            self.origin = loop.time()
        target = self.origin + self.pts / 24000
        if loop.time() - target > .06:
            # Keep sample timestamps contiguous but rebase wall time after stalls;
            # never burst overdue frames or discard buffered participant speech.
            self.origin = loop.time() - self.pts / 24000
            target = loop.time()
            self.late_rebases += 1
        await asyncio.sleep(max(0, target - loop.time()))
        await self._require_live()
        # Hold a small cushion. The feed arrives in real time with no slack (measured peak
        # queue depth of one frame), so anything that delays a frame becomes audible
        # silence. Fill a few frames before sending, and refill only after a real gap.
        if self.primed:
            if self.frames.qsize() == 0:
                self.empty_frames += 1
                if self.empty_frames >= 2:
                    self.primed = False
                    self.empty_frames = 0
            else:
                self.empty_frames = 0
        else:
            if self.frames.qsize() < self.cushion:
                self.underruns += 1
                pcm = b'\x00' * 960
                return self._frame(pcm)
            self.primed = True
        try:
            pcm = self.frames.get_nowait()
        except asyncio.QueueEmpty:
            # Nothing buffered. That is a dropout only if audio was flowing a moment ago;
            # a quiet call is not a fault, and counting it as one hid the real cause.
            self.underruns += 1
            now = loop.time()
            if self.last_audio is not None and now - self.last_audio < .25:
                self.dropouts += 1
            pcm = b'\x00' * 960
        self.frames_out += 1
        self.max_queue = max(self.max_queue, self.frames.qsize())
        return self._frame(pcm)

    def _frame(self, pcm):
        frame = av.AudioFrame(format='s16', layout='mono', samples=480)
        frame.planes[0].update(pcm)
        frame.sample_rate, frame.time_base, frame.pts = 24000, Fraction(1, 24000), self.pts
        self.pts += 480
        return frame

    def stats(self):
        return {'frames': self.frames_out, 'underruns': self.underruns,
                'dropouts': self.dropouts, 'late_rebases': self.late_rebases,
                'max_queue': self.max_queue, 'queued_bytes': len(self.pending),
                'in_bytes': self.in_bytes, 'in_maxgap': self.in_maxgap,
                'in_droughts': self.in_droughts}

    def drain(self):
        self.pending = b''
        while not self.frames.empty():
            self.frames.get_nowait()


class MediaLane:
    version = 'v3'
    media_contract = {'input': 'pcm-s16le-24000-mono-mixed',
                      'output': 'pcm-s16le-24000-mono',
                      'speaker_attribution': 'native-controller'}

    def __init__(self, preflight, negotiate, startup_timeout=10):
        # Only token-opaque operations cross into media; no auth object/config.
        self._preflight, self._negotiate = preflight, negotiate
        self._timeout = startup_timeout
        self.ready, self.failed = False, False
        self.audio = queue.Queue(maxsize=100)
        self.pc = None
        self.channel = None
        self._track = None
        self._tasks = set()
        self._appends = set()
        self._loop = asyncio.get_running_loop()
        self._gate = asyncio.Event()
        self._changed = asyncio.Event()
        self._session_id = None
        self._delegations = None
        self._append_lock = None
        self._started = False
        self._remote_audio = False
        self._closed = False
        self._audio_fenced = False
        self._closing = None

    def _scope(self):
        if self._closed or self.failed or self._audio_fenced:
            raise MediaRefused('media lane closed')
        self._preflight()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _fail(self):
        self.failed, self.ready = True, False
        self._changed.set()
        self.drain()
        if self.pc is not None and not self._closed:
            self._spawn(self.close())

    def _message(self, payload):
        try:
            self._scope()
            event = parse_event(payload)
            if event.kind == 'error':
                self._fail()
            elif event.kind == 'session':
                session = event.session
                if (session.get('version', 'v3') != 'v3'
                        or session.get('model', self._capability.model) != self._capability.model
                        or session.get('audio', {}).get('output', {}).get('voice', self._capability.voice) != self._capability.voice
                        or (self._session_id is not None and self._session_id != session['id'])):
                    raise MediaRefused('provider session mismatch')
                self._session_id = session['id']
                self._changed.set()
            elif event.kind == 'delegation':
                if self.channel.readyState != 'open' or self.channel.bufferedAmount > 65536:
                    raise MediaRefused('control backpressure')
                if self._delegations is None or not self._delegations.submit(event.item_id, event.text):
                    # No executor (client-managed call) or a full queue: refuse, never drop
                    # the request silently.
                    self.channel.send(json.dumps(delegation_refusal(event)))
                    logger.warning('Live Voice: delegation refused item=%s (no executor or queue full)',
                                   event.item_id)
            # Transcript deltas remain conversation-only; no executor/callback.
        except Exception:
            self._fail()

    async def _report_audio(self, interval=10.0):
        """Say out loud what the audio path is doing, every few seconds."""
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                if self._closed or self._track is None:
                    continue
                counters = self._track.stats()
                logger.warning('Live Voice audio: frames=%d underruns=%d dropouts=%d late_rebases=%d '
                               'max_queue=%d queued_bytes=%d buffered=%s ready=%s '
                               'in_bytes=%d in_maxgap=%.2f in_droughts=%d',
                               counters['frames'], counters['underruns'], counters['dropouts'],
                               counters['late_rebases'], counters['max_queue'],
                               counters['queued_bytes'],
                               getattr(self.channel, 'bufferedAmount', None), self.ready,
                               counters['in_bytes'], counters['in_maxgap'], counters['in_droughts'])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Live Voice: audio reporting stopped')

    async def start(self, prompt, voice=None):
        if self._started or self._closed:
            raise MediaRefused('fresh lane required')
        self._scope()  # refuse before local peer or provider ingress
        self._capability = self._preflight()
        self._started = True
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._track = PCMTrack(self._gate, self._scope)
        self.pc.addTrack(self._track)
        opus = [c for c in RTCRtpSender.getCapabilities('audio').codecs if c.mimeType.lower() == 'audio/opus']
        self.pc.getTransceivers()[0].setCodecPreferences(opus)
        self.channel = self.pc.createDataChannel('oai-events', ordered=True)
        self.channel.on('message', self._message)
        self.channel.on('open', self._changed.set)
        self.channel.on('close', self._fail)
        @self.pc.on('connectionstatechange')
        def changed():
            self._changed.set()
            if self.pc.connectionState in ('failed', 'closed') and not self._closed:
                self._fail()
        @self.pc.on('track')
        def track(remote):
            if remote.kind != 'audio' or self._remote_audio:
                self._fail()
                return
            self._remote_audio = True
            self._changed.set()
            self._spawn(self._receive(remote))
        try:
            async with asyncio.timeout(self._timeout):
                await self.pc.setLocalDescription(await self.pc.createOffer())
                answer = await self._negotiate(self.pc.localDescription.sdp, prompt, voice)
                if answer.capability.version != 'v3':
                    raise MediaRefused('negotiation version mismatch')
                validate_sdp(answer.sdp)
                await self.pc.setRemoteDescription(RTCSessionDescription(answer.sdp, 'answer'))
                while True:
                    self._scope()
                    if (self.pc.connectionState == 'connected' and self.channel.readyState == 'open'
                            and self._session_id and self._remote_audio):
                        self.ready = True
                        self._gate.set()
                        self._spawn(self._watch_scope())
                        self._spawn(self._report_audio())
                        return
                    self._changed.clear()
                    await self._changed.wait()
        except BaseException as error:
            self.failed = True
            await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            logger.warning('Live Voice: provider media startup failed', exc_info=True)
            raise MediaRefused('provider media startup failed') from None

    async def _watch_scope(self):
        try:
            while not self._closed:
                self._scope()
                await asyncio.sleep(.1)
        except Exception:
            self._fail()

    async def _receive(self, track):
        resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
        try:
            await self._gate.wait()
            while not self._closed:
                frame = await track.recv()
                self._scope()
                for output in resampler.resample(frame):
                    pcm = bytes(output.planes[0])[:output.samples * 2]
                    for offset in range(0, len(pcm), 48000):
                        self.audio.put_nowait(pcm[offset:offset+48000])
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail()  # output overflow is visible failure, never silent truncation

    async def append(self, pcm):
        self._scope()
        if not self.ready:
            raise MediaRefused('provider session not ready')
        task = self._spawn(self._track.append(pcm))
        self._appends.add(task)
        task.add_done_callback(self._appends.discard)
        try:
            async with asyncio.timeout(2):
                await task
            self._scope()
        except BaseException:
            self._fail()
            raise

    def drain(self):
        # Native consent revocation calls this synchronously before stop().
        # Fence the lane, not only its queue: fresh consent uses a fresh peer.
        self.ready = False
        self._audio_fenced = True
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._drain_input()
        elif not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._drain_input)
        while True:
            try:
                self.audio.get_nowait()
            except queue.Empty:
                break

    def _drain_input(self):
        self._gate.clear()
        for task in tuple(self._appends):
            task.cancel()
        if self._track:
            self._track.drain()

    async def stop(self):
        await self.close()
        return 'unknown'  # no upstream V3 stop-ACK protocol has been established

    def attach_delegations(self, queue):
        """Give the lane something that can actually run what the call delegates."""
        self._delegations = queue
        if self._append_lock is None:
            self._append_lock = asyncio.Lock()

    def _delegation_result(self, item_id, text):
        return {'type': 'delegation.context.append', 'delegation_item_id': item_id,
                'content': [{'type': 'input_text', 'text': text}]}

    async def report_delegation(self, item_id, text):
        """Append a delegated result to the call; never raises into the RTP loop."""
        try:
            if self._closed or self.failed or self.channel is None or self.channel.readyState != 'open':
                logger.warning('Live Voice: delegation result not delivered item=%s (lane closed)', item_id)
                return
            if self.channel.bufferedAmount > 65536:
                await asyncio.sleep(.25)
                if self.channel.readyState != 'open' or self.channel.bufferedAmount > 65536:
                    logger.warning('Live Voice: delegation result not delivered item=%s (backpressure)', item_id)
                    return
            # Several tasks can finish at once: append them one at a time.
            async with (self._append_lock or asyncio.Lock()):
                self.channel.send(json.dumps(self._delegation_result(item_id, text)))
        except Exception:
            logger.exception('Live Voice: delegation result send failed item=%s', item_id)

    async def _stop_delegations(self):
        queue = self._delegations
        self._delegations = None
        if queue is not None:
            await queue.stop()

    async def close(self):
        self._closed = True
        await self._stop_delegations()
        if self._closing is None:
            self._closed = True  # fence synchronously before the cleanup await
            self.ready = False
            self._changed.set()
            self.drain()
            self._closing = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._closing)

    async def _cleanup(self):
        tasks = tuple(t for t in self._tasks if t is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        if self._track:
            self._track.stop()
        if self.pc:
            await self.pc.close()
        self.drain()


class LaneFactory:
    supported_version = 'v3'
    processing_disclosure = ('Admitted participant audio goes to the already-selected Codex CLI '
                             'ChatGPT subscription via direct WebRTC. No local task executor is attached.')

    def __init__(self, broker, binding, startup_timeout):
        self._preflight = lambda: broker.preflight(binding)
        self._adapter_preflight = lambda scope: broker.preflight_adapter(binding, scope)
        self._negotiate = lambda offer, prompt, voice: broker.negotiate(binding, offer, prompt, voice)
        self._timeout = startup_timeout

    async def preflight(self, scope):
        try:
            self._adapter_preflight(scope)
            return True
        except ScopeRefused:
            return False

    @property
    def admitted(self):
        try:
            self._preflight()
            return True
        except ScopeRefused:
            return False

    def __call__(self):
        self._preflight()
        return MediaLane(self._preflight, self._negotiate, self._timeout)


def lane_factory(broker, binding, *, startup_timeout=10):
    """Parent passes a host-registered binding, never caller home/account arguments."""
    broker.preflight(binding)
    return LaneFactory(broker, binding, startup_timeout)
