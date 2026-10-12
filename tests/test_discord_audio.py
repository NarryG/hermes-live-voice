from test_discord_pcm import module
import discord


def test_feed_reports_exact_accepted_bytes_for_bounded_backpressure():
    source = module('audio').PCMSource(lambda: True, max_frames=2)
    data = b'\x01' * 192000
    accepted = source.feed(data)
    assert accepted == 7680
    assert source.feed(data[accepted:]) == 0
    assert source.read() == data[:3840]
    assert source.feed(data[accepted:]) == 3840


def test_real_source_is_bounded_and_checks_guard_each_player_read():
    m = module('audio')
    allowed = [True]
    source = m.PCMSource(lambda: allowed[0], max_frames=2)
    assert isinstance(source, discord.AudioSource)
    assert source.read() == bytes(3840)  # ordinary underrun is not EOF
    source.feed(b'\x01' * (3840 * 100))
    assert source.buffered_bytes <= 3840 * 2
    assert source.read() == b'\x01' * 3840
    allowed[0] = False
    assert source.read() == bytes(3840)
    assert source.buffered_bytes == 0
    allowed[0] = True
    assert source.read() == bytes(3840)
    source.cleanup()
    assert source.read() == b''
