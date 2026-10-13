import asyncio

import pytest

from discord_voice.delegation import (FAILED_TEXT, TIMEOUT_TEXT, DelegationQueue,
                                      speakable)


def _run(coro):
    return asyncio.run(coro)


def test_speakable_trims_on_a_word_boundary():
    text = speakable('word ' * 400, limit=50)
    assert len(text) <= 60
    assert text.endswith('...')
    assert 'word' in text


def test_speakable_leaves_a_short_answer_alone():
    assert speakable('  two\n\nwords ') == 'two words'


def test_a_delegated_request_comes_back_to_the_call():
    seen, reported = [], []

    async def run(text):
        seen.append(text)
        return 'the answer'

    async def report(item_id, text):
        reported.append((item_id, text))

    async def main():
        queue = DelegationQueue(run, report).start()
        assert queue.submit('item-1', 'do the thing') is True
        for _ in range(60):
            if reported:
                break
            await asyncio.sleep(.01)
        await queue.stop()

    _run(main())
    assert seen == ['do the thing']
    assert reported == [('item-1', 'the answer')]


def test_a_failing_task_reports_something_usable():
    reported = []

    async def run(text):
        raise RuntimeError('boom')

    async def report(item_id, text):
        reported.append(text)

    async def main():
        queue = DelegationQueue(run, report).start()
        queue.submit('item-2', 'x')
        for _ in range(60):
            if reported:
                break
            await asyncio.sleep(.01)
        await queue.stop()

    _run(main())
    assert reported == [FAILED_TEXT]


def test_a_task_that_outruns_the_call_reports_and_frees_the_queue():
    reported = []

    async def run(text):
        await asyncio.sleep(30)

    async def report(item_id, text):
        reported.append(text)

    async def main():
        queue = DelegationQueue(run, report, timeout=.05).start()
        queue.submit('item-3', 'slow')
        for _ in range(80):
            if reported:
                break
            await asyncio.sleep(.01)
        await queue.stop()

    _run(main())
    assert reported == [TIMEOUT_TEXT]


def test_a_full_queue_is_refused_not_silently_dropped():
    async def run(text):
        await asyncio.sleep(30)

    async def report(item_id, text):
        pass

    queue = DelegationQueue(run, report, capacity=2)
    assert queue.submit('a', 'x') is True
    assert queue.submit('b', 'x') is True
    assert queue.submit('c', 'x') is False


def test_a_stopped_queue_accepts_nothing():
    async def run(text):
        return 'x'

    async def report(item_id, text):
        pass

    async def main():
        queue = DelegationQueue(run, report).start()
        await queue.stop()
        assert queue.submit('late', 'x') is False

    _run(main())
