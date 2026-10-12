"""Offline transport tests; no Discord network or provider calls."""
import importlib.util
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]

def module(name):
    path = ROOT / 'discord_voice' / (name + '.py')
    assert path.exists(), 'Discord voice implementation is missing'
    spec = importlib.util.spec_from_file_location('livevoice_' + name, path)
    m = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(m)
    return m

def test_group_mixer_aligns_overlapping_speakers_without_sharing_carry():
    from types import SimpleNamespace as NS
    m = module('pcm')
    mixer = m.GroupPCM({123, 789})
    packet = lambda uid, t, raw: NS(speaker=uid, received_at=t, rtp_timestamp=1, pcm=raw)
    a = struct.pack('<h', 1000) * 1920
    b = struct.pack('<h', 2000) * 1920
    mixer.feed(packet(123, 1.001, a))
    mixer.feed(packet(789, 1.002, b))
    assert mixer.pop_ready(1.08) == struct.pack('<h', 3000) * 480
    assert mixer.pop_ready(1.08) == b''
    assert len(mixer.converters) == 2
    import pytest
    with pytest.raises(ValueError):
        mixer.feed(packet(999, 1.1, a))
    for i in range(100):
        mixer.feed(packet(123, 2 + i * .02, a))
    with pytest.raises(BufferError):
        mixer.feed(packet(123, 4.02, a))
    assert mixer.buffered_frames <= 100


def test_pcm_chunk_carry_matches_whole_stream():
    m = module('pcm')
    raw = struct.pack('<hhhhhhhh', -100, 100, 300, 500, -800, -400, 200, 600)
    expected = struct.pack('<hh', 200, -100)
    converter = m.InputPCM()
    assert b''.join(converter.feed(raw[i:i+3]) for i in range(0, len(raw), 3)) == expected
    output = m.OutputPCM()
    chunks = [expected[:1], expected[1:3], expected[3:]]
    assert b''.join(output.feed(x) for x in chunks) == struct.pack('<hhhhhhhh', 200, 200, 200, 200, 50, 50, -100, -100)
    assert len(output.carry) < 2
