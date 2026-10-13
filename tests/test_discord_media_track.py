import asyncio

import pytest
from aiortc.mediastreams import MediaStreamError

from discord_voice.webrtc import MediaRefused, PCMTrack


def _gate():
    gate = asyncio.Event()
    gate.set()
    return gate


def test_a_live_track_hands_aiortc_a_frame():
    track = PCMTrack(_gate(), lambda: None)
    frame = asyncio.run(track.recv())
    assert frame.samples == 480


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
