import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

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

    async def run(text, item_id=None):
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

    async def run(text, item_id=None):
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

    async def run(text, item_id=None):
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
    async def run(text, item_id=None):
        await asyncio.sleep(30)

    async def report(item_id, text):
        pass

    queue = DelegationQueue(run, report, capacity=2)
    assert queue.submit('a', 'x') is True
    assert queue.submit('b', 'x') is True
    assert queue.submit('c', 'x') is False


def test_a_stopped_queue_accepts_nothing():
    async def run(text, item_id=None):
        return 'x'

    async def report(item_id, text):
        pass

    async def main():
        queue = DelegationQueue(run, report).start()
        await queue.stop()
        assert queue.submit('late', 'x') is False

    _run(main())


@dataclass
class _Source:
    platform: str = 'discord'
    chat_id: str = '555'
    chat_type: str = 'channel'
    user_id: str = '123'
    user_name: str = 'narry'
    thread_id: Optional[str] = None
    parent_chat_id: Optional[str] = None


class _FakeAdapter:
    """Enough of the core adapter to see which chat a task runs in."""

    def __init__(self, *, thread_id='999', fail_thread=False):
        self.thread_id = thread_id
        self.fail_thread = fail_thread
        self.sends = []
        self.dispatched = []
        self.thread_requests = []

    async def create_handoff_thread(self, parent_chat_id, name):
        self.thread_requests.append((parent_chat_id, name))
        if self.fail_thread:
            raise RuntimeError('no permission')
        return self.thread_id

    async def send(self, chat_id, content, *args, **kwargs):
        self.sends.append((str(chat_id), content))

    async def handle_message(self, event):
        # Goes through send() the way the gateway does, so the capture wrapper sees it.
        chat = str(event.source.chat_id)
        self.dispatched.append(chat)
        await self.send(chat, 'the answer from ' + chat)


def _factory(text, source):
    return SimpleNamespace(text=text, source=source)


def _work(adapter, source, chat_id):
    from discord_voice.delegation import ChatWork
    return ChatWork(adapter, source, chat_id, event_factory=_factory, quiet=.05)


def test_each_task_gets_its_own_thread():
    adapter = _FakeAdapter()
    work = _work(adapter, _Source(), '555')
    answer = _run(work('check the rook repo', 'item-7'))
    assert adapter.thread_requests and adapter.thread_requests[0][0] == '555'
    assert 'rook repo' in adapter.thread_requests[0][1]
    assert adapter.dispatched == ['999']
    assert answer == 'the answer from 999'
    assert work.threads == {'item-7': '999'}


def test_the_seed_never_leaks_into_the_answer():
    adapter = _FakeAdapter()
    work = _work(adapter, _Source(), '555')
    answer = _run(work('do a thing', 'item-8'))
    assert answer.startswith('the answer')
    assert 'MICROPHONE' not in answer
    assert any('do a thing' in content for chat, content in adapter.sends if chat == '999')


def test_a_call_already_in_a_thread_runs_in_place():
    adapter = _FakeAdapter()
    work = _work(adapter, _Source(chat_type='thread', chat_id='777'), '777')
    answer = _run(work('do a thing', 'item-9'))
    assert adapter.thread_requests == []
    assert adapter.dispatched == ['777']
    assert answer == 'the answer from 777'


def test_a_channel_that_refuses_threads_still_runs_the_work():
    adapter = _FakeAdapter(fail_thread=True)
    work = _work(adapter, _Source(), '555')
    answer = _run(work('do a thing', 'item-10'))
    assert adapter.dispatched == ['555']
    assert answer == 'the answer from 555'
    assert work.threads == {}


def test_a_missing_thread_helper_falls_back_to_the_call_chat():
    adapter = _FakeAdapter()
    adapter.create_handoff_thread = None
    work = _work(adapter, _Source(), '555')
    answer = _run(work('do a thing', 'item-11'))
    assert adapter.dispatched == ['555']
    assert answer == 'the answer from 555'
