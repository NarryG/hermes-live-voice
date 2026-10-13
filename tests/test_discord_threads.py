"""The call can be asked about text threads; it must not answer that it cannot see them."""

from types import SimpleNamespace

from discord_voice.threads import directory, find, report


class _Thread(SimpleNamespace):
    pass


def _guild(*threads, parent='general'):
    items = [_Thread(id=int(i), name=n, last_message_id=int(lm), parent=SimpleNamespace(name=parent))
             for i, (n, lm) in enumerate(threads, start=1)]
    return SimpleNamespace(threads=items, active_threads=lambda: items)


def test_the_directory_lists_threads_with_ids():
    guild = _guild(('live voice: check the repo', 10), ('Unravel Buffer', 20))
    text = directory(guild)
    assert 'Unravel Buffer' in text and '(id 2' in text
    assert 'live voice: check the repo' in text


def test_newest_threads_come_first():
    guild = _guild(('older', 10), ('newer', 99))
    assert directory(guild).splitlines()[0].startswith('- newer')


def test_a_search_finds_a_thread_by_name():
    guild = _guild(('live voice: add two numbers', 10), ('Unravel Buffer identity', 20))
    found = find(guild, 'unravel')
    assert [t.name for t in found] == ['Unravel Buffer identity']


def test_no_match_says_so_and_lists_what_is_visible():
    guild = _guild(('something else', 10))
    text = report(guild, 'nonexistent')
    assert 'No thread matches' in text
    assert 'something else' in text


def test_a_guild_with_no_threads_says_so_plainly():
    guild = SimpleNamespace(threads=[], active_threads=lambda: [])
    assert 'cannot see any threads' in report(guild, 'anything')


def test_the_call_prompt_carries_the_thread_directory():
    """The call is told what threads exist, so it stops answering that it cannot see them."""
    import asyncio
    from types import SimpleNamespace

    from discord_voice.session import SessionManager

    class Manager(SessionManager):
        def __init__(self, prompt='BASE', guild=None):
            self.prompt = prompt
            self._guild = guild

    guild = _guild(('Unravel Buffer identity', 20))
    manager = Manager(guild=guild)
    session = SimpleNamespace(guild=guild)
    Manager.refresh_prompt(manager, session)
    assert 'Unravel Buffer identity' in manager.prompt
    assert manager.prompt.startswith('BASE')
    # refreshing twice must not stack directories
    Manager.refresh_prompt(manager, session)
    assert manager.prompt.count('Unravel Buffer identity') == 1
