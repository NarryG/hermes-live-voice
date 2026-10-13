import pytest

from discord_voice.native import delegation_mode, lane_prompt


def test_a_server_lane_tells_the_call_it_can_delegate():
    prompt = lane_prompt('Buffer', 'persona', 'English', 'server')
    assert 'delegate it' in prompt
    assert 'No task continuity' not in prompt
    assert 'Never delegate' not in prompt


def test_the_default_lane_still_refuses_task_state():
    prompt = lane_prompt('Buffer', 'persona', 'English', 'client')
    assert 'No task continuity or tools are available in this call.' in prompt
    assert 'Never delegate' in prompt


@pytest.mark.parametrize('settings,expected', [
    (None, 'client'),
    ({}, 'client'),
    ({'delegation': 'client'}, 'client'),
    ({'delegation': 'server'}, 'server'),
    ({'delegation': ' SERVER '}, 'server'),
    ({'delegation': 'yes please'}, 'client'),
])
def test_the_delegation_setting_is_read_leniently(settings, expected):
    assert delegation_mode(settings) == expected
