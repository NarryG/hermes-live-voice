import asyncio

import pytest
from aiortc.mediastreams import MediaStreamError

from discord_voice.webrtc import MediaRefused, PCMTrack


def _gate():
    gate = asyncio.Event()
    gate.set()
    return gate


def _pcm(frames=1, value=b'\x11'):
    return value * 960 * frames


def test_a_filled_cushion_sends_audio_immediately():
    track = PCMTrack(_gate(), lambda: None)
    asyncio.run(track.append(_pcm(PCMTrack.CUSHION_FRAMES)))
    frame = asyncio.run(track.recv())
    assert frame.samples == 480
    assert bytes(frame.planes[0])[:2] == b'\x11\x11'


def test_the_cushion_holds_audio_until_it_is_filled():
    """Running exactly in real time, one late frame is audible silence; buffer first."""
    track = PCMTrack(_gate(), lambda: None)
    asyncio.run(track.append(_pcm(1)))
    frame = asyncio.run(track.recv())
    assert bytes(frame.planes[0])[:2] == b'\x00\x00'   # silence, not the only frame
    assert track.frames.qsize() == 1                     # the audio is still held


def test_a_closed_lane_ends_the_sender_cleanly():
    def scope():
        raise MediaRefused('media lane closed')

    track = PCMTrack(_gate(), scope)
    with pytest.raises(MediaStreamError):
        asyncio.run(track.recv())


def test_a_closed_lane_never_leaks_a_media_refusal_into_the_rtp_loop():
    def scope():
        raise MediaRefused('media lane closed')

    track = PCMTrack(_gate(), scope)
    with pytest.raises(MediaStreamError) as caught:
        asyncio.run(track.recv())
    assert not isinstance(caught.value, MediaRefused)
    assert 'media lane closed' in str(caught.value)


def test_appending_bad_pcm_still_refuses():
    track = PCMTrack(_gate(), lambda: None)
    with pytest.raises(MediaRefused):
        asyncio.run(track.append(b'\x01'))


def test_a_quiet_call_is_not_counted_as_a_dropout():
    track = PCMTrack(_gate(), lambda: None)
    asyncio.run(track.recv())
    counters = track.stats()
    assert counters['underruns'] == 1      # nothing buffered
    assert counters['dropouts'] == 0       # but no audio had been flowing
    assert set(counters) == {'frames', 'underruns', 'dropouts', 'late_rebases', 'max_queue',
                             'queued_bytes', 'in_bytes', 'in_maxgap', 'in_droughts'}


def test_silence_right_after_audio_is_counted_as_a_dropout():
    track = PCMTrack(_gate(), lambda: None)
    asyncio.run(track.append(_pcm(PCMTrack.CUSHION_FRAMES)))
    asyncio.run(track.recv())              # sends the first frame
    for _ in range(PCMTrack.CUSHION_FRAMES + 3):
        asyncio.run(track.recv())          # drains, then silence while audio was flowing
    assert track.stats()['dropouts'] >= 1


def test_the_feed_is_measured_on_the_track():
    track = PCMTrack(_gate(), lambda: None)
    asyncio.run(track.append(_pcm(2)))
    counters = track.stats()
    assert counters['in_bytes'] == 960 * 2
    assert counters['in_droughts'] == 0
