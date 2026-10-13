"""Delegated work: queued one at a time, run in the call's chat, reported back."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import pytest

from discord_voice.delegation import (FAILED_TEXT, TIMEOUT_TEXT, TaskBoard, speakable)


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
        queue = TaskBoard(run, report).start()
        assert queue.submit('item-1', 'do the thing') is True
        for _ in range(100):
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
        queue = TaskBoard(run, report).start()
        queue.submit('item-2', 'x')
        for _ in range(100):
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
        queue = TaskBoard(run, report, timeout=.05).start()
        queue.submit('item-3', 'slow')
        for _ in range(120):
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

    async def main():
        board = TaskBoard(run, report, capacity=2).start()
        assert board.submit('a', 'x') is True
        assert board.submit('b', 'x') is True
        assert board.submit('c', 'x') is False   # at capacity: refused, never dropped quietly
        await board.stop()

    _run(main())


def test_a_stopped_queue_accepts_nothing():
    async def run(text, item_id=None):
        return 'x'

    async def report(item_id, text):
        pass

    async def main():
        queue = TaskBoard(run, report).start()
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


class _CoreAdapter:
    """Enough of the real adapter: dispatch, outbound send, session completion."""

    def __init__(self, *, thread_id='999', fail_thread=False, answer='the answer',
                 answer_after=0.0, completes=True, complete_after=0.0, session='s1'):
        self.thread_id, self.fail_thread = thread_id, fail_thread
        self.answer, self.answer_after = answer, answer_after
        self.completes, self.complete_after = completes, complete_after
        self.session = session
        self.sends, self.dispatched, self.thread_requests = [], [], []
        self.completed, self.chained, self.restored = [], [], []
        self.on_processing_complete = self._default_complete
        self._original_complete = self.on_processing_complete

    async def _default_complete(self, event, outcome):
        self.chained.append(outcome)

    def _source_session_key(self, source):
        return self.session

    async def create_handoff_thread(self, parent_chat_id, name):
        self.thread_requests.append((parent_chat_id, name))
        if self.fail_thread:
            raise RuntimeError('no permission')
        return self.thread_id

    async def send(self, chat_id, content, *args, **kwargs):
        self.sends.append((str(chat_id), content))

    async def edit_message(self, chat_id, message_id, content=None, *args, **kwargs):
        self.sends.append((str(chat_id), content))

    async def handle_message(self, event):
        chat = str(event.source.chat_id)
        self.dispatched.append(chat)
        if self.answer_after:
            await asyncio.sleep(self.answer_after)
        await self.send(chat, self.answer)
        if self.complete_after:
            await asyncio.sleep(self.complete_after)
        hook = getattr(self, 'on_processing_complete', None)
        if self.completes and callable(hook):
            self.completed.append(chat)
            await hook(event, 'success')


def _factory(text, source):
    return SimpleNamespace(text=text, source=source)


def _work(adapter, source, chat_id, **kwargs):
    from discord_voice.delegation import ChatWork
    return ChatWork(adapter, source, chat_id, event_factory=_factory, **kwargs)


def test_each_task_gets_its_own_thread():
    adapter = _CoreAdapter()
    work = _work(adapter, _Source(), '555')
    answer = _run(work('check the rook repo', 'item-7'))
    assert adapter.thread_requests and adapter.thread_requests[0][0] == '555'
    assert 'rook repo' in adapter.thread_requests[0][1]
    assert adapter.dispatched == ['999']
    assert answer == 'the answer'
    assert work.threads == {'item-7': '999'}


def test_the_seed_never_leaks_into_the_answer():
    adapter = _CoreAdapter()
    work = _work(adapter, _Source(), '555')
    answer = _run(work('do a thing', 'item-8'))
    assert answer == 'the answer'
    assert any('do a thing' in content for chat, content in adapter.sends if chat == '999')


def test_a_call_in_a_thread_with_nowhere_to_open_one_runs_in_place():
    """Discord refuses children of a thread; with no parent channel we use the call chat."""
    class Refusing(_CoreAdapter):
        async def create_handoff_thread(self, parent_chat_id, name):
            self.thread_requests.append((parent_chat_id, name))
            return None

    adapter = Refusing()
    work = _work(adapter, _Source(chat_type='thread', chat_id='777'), '777')
    answer = _run(work('do a thing', 'item-9'))
    assert adapter.thread_requests and adapter.thread_requests[0][0] == '777'
    assert adapter.dispatched == ['777']
    assert answer == 'the answer'


def test_a_channel_that_refuses_threads_still_runs_the_work():
    adapter = _CoreAdapter(fail_thread=True)
    work = _work(adapter, _Source(), '555')
    answer = _run(work('do a thing', 'item-10'))
    assert adapter.dispatched == ['555']
    assert answer == 'the answer'
    assert work.threads == {}


def test_a_missing_thread_helper_falls_back_to_the_call_chat():
    adapter = _CoreAdapter()
    adapter.create_handoff_thread = None
    work = _work(adapter, _Source(), '555')
    answer = _run(work('do a thing', 'item-11'))
    assert adapter.dispatched == ['555']
    assert answer == 'the answer'


def test_the_wait_ends_on_the_turn_completing_not_on_a_stopwatch():
    """The answer arrives after the turn, which the queue records as the completion."""
    order = []
    adapter = _CoreAdapter(answer_after=.05)

    original = adapter.on_processing_complete

    async def watched(event, outcome):
        order.append('complete')
        await original(event, outcome)

    adapter.on_processing_complete = watched
    adapter._original_complete = watched
    work = _work(adapter, _Source(), '555')
    answer = _run(work('do a thing', 'item-15'))
    assert order == ['complete']
    assert answer == 'the answer'
    assert adapter.chained  # the adapter's own hook still ran
    assert adapter.on_processing_complete is watched  # and was restored


def test_a_quiet_turn_without_completion_still_reports():
    adapter = _CoreAdapter(completes=False, answer=None)
    work = _work(adapter, _Source(), '555', deadline=.2)
    assert _run(work('do a thing', 'item-16')) == FAILED_TEXT


def test_another_sessions_completion_does_not_release_our_wait():
    """Only our conversation's turn may end our wait -- and a chat that answered is not a failure."""
    import time

    adapter = _CoreAdapter(completes=False)
    work = _work(adapter, _Source(), '555', deadline=.4)
    other = SimpleNamespace(source=_Source(chat_id='other'))

    async def scenario():
        task = asyncio.ensure_future(work('do a thing', 'item-17'))
        await asyncio.sleep(.05)
        adapter.session = 'other-session'
        await adapter.on_processing_complete(other, 'success')  # fires while ours is pending
        await task
        return task.result()

    started = time.monotonic()
    answer = _run(scenario())
    elapsed = time.monotonic() - started
    # The foreign completion did not release the wait: it ran to its deadline.
    assert elapsed >= .35, elapsed
    # Nothing was reported for our session, and the captured answer is still returned.
    assert work.outcome is None
    assert answer == 'the answer'


def test_an_edited_answer_counts():
    class Editing(_CoreAdapter):
        async def handle_message(self, event):
            chat = str(event.source.chat_id)
            self.dispatched.append(chat)
            await self.edit_message(chat, 'm1', 'the edited answer')
            await self.on_processing_complete(event, 'success')

    adapter = Editing()
    work = _work(adapter, _Source(), '555')
    assert _run(work('do a thing', 'item-18')) == 'the edited answer'


def test_a_turn_without_the_hook_reports_what_it_captured():
    adapter = _CoreAdapter()
    del adapter.on_processing_complete
    work = _work(adapter, _Source(), '555')
    assert _run(work('do a thing', 'item-19')) == 'the answer'


def test_several_tasks_run_at_once_not_one_behind_another():
    """A call must be able to hand over more than one thing and get on with it."""
    import time

    async def run(text, item_id=None):
        await asyncio.sleep(.25)
        return 'done ' + item_id

    async def report(item_id, text):
        pass

    async def main():
        board = TaskBoard(run, report, capacity=4).start()
        started = time.monotonic()
        assert board.submit('a', 'one') is True
        assert board.submit('b', 'two') is True
        assert board.submit('c', 'three') is True
        assert len(board.inflight) == 3
        while board.inflight:
            await asyncio.sleep(.02)
        elapsed = time.monotonic() - started
        await board.stop()
        return elapsed

    elapsed = _run(main())
    assert elapsed < .6, elapsed  # serial would be ~.75


def test_each_task_is_reported_when_it_finishes():
    reported = []

    async def run(text, item_id=None):
        await asyncio.sleep(.3 if item_id == 'slow' else .02)
        return 'answer for ' + item_id

    async def report(item_id, text):
        reported.append((item_id, text))

    async def main():
        board = TaskBoard(run, report, capacity=4).start()
        board.submit('slow', 'x')
        board.submit('quick', 'y')
        for _ in range(200):
            if len(reported) == 2:
                break
            await asyncio.sleep(.01)
        await board.stop()

    _run(main())
    assert [item for item, _ in reported] == ['quick', 'slow']  # as they finish, not in order
    assert dict(reported)['slow'] == 'answer for slow'


def test_capacity_counts_tasks_in_flight():
    async def run(text, item_id=None):
        await asyncio.sleep(.2)
        return 'x'

    async def report(item_id, text):
        pass

    async def main():
        board = TaskBoard(run, report, capacity=2).start()
        assert board.submit('a', 'x') is True
        assert board.submit('b', 'x') is True
        assert board.submit('c', 'x') is False      # at capacity
        while board.inflight:
            await asyncio.sleep(.02)
        assert board.submit('d', 'x') is True       # room again
        await board.stop()

    _run(main())


def test_stopping_cancels_every_task_in_flight():
    async def run(text, item_id=None):
        await asyncio.sleep(30)

    async def report(item_id, text):
        pass

    async def main():
        board = TaskBoard(run, report).start()
        board.submit('a', 'x')
        board.submit('b', 'x')
        await board.stop()
        assert board.inflight == {}
        assert board.submit('c', 'x') is False

    _run(main())


def test_a_call_inside_a_thread_opens_task_threads_in_the_parent_channel():
    """A thread cannot host a thread, and sharing one session aborts turns."""
    adapter = _CoreAdapter()
    source = _Source(chat_type='thread', chat_id='777', parent_chat_id='555')
    work = _work(adapter, source, '777')
    answer = _run(work('do a thing', 'item-20'))
    assert adapter.thread_requests and adapter.thread_requests[0][0] == '555'
    assert adapter.dispatched == ['999']
    assert answer == 'the answer'
