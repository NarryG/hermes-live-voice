"""Phase 1: a delegated request runs as a normal Hermes turn in the call's chat.

The provider emits ``delegation.created`` when the call decides a request needs real
work. Until this module existed the lane answered every one of them with a refusal,
which is what the caller heard as "I don't have a lane".

The queue keeps one request in flight, bounds how many can wait, trims each answer to
something a call can actually read aloud, and reports every step.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

CAPACITY = 4
TIMEOUT_S = 240.0
SPEAKABLE_CHARS = 1200
QUIET_S = 1.5

QUEUE_FULL_TEXT = ('More tasks are already queued in this call than I can track. '
                   'Ask me again once the current one finishes.')
TIMEOUT_TEXT = 'That task is taking longer than this call can wait. Ask me again shortly.'
FAILED_TEXT = 'That task could not be run in this call.'


def speakable(text, limit=SPEAKABLE_CHARS):
    """Trim a reply to what a call should read aloud, on a word boundary."""
    text = ' '.join(str(text or '').split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(' ')
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(' ,.;:') + ' ...'


class DelegationQueue:
    """One delegated request at a time; results are appended to the call in order."""

    def __init__(self, run, report, *, capacity=CAPACITY, timeout=TIMEOUT_S):
        self._run = run
        self._report = report
        self._capacity = capacity
        self._timeout = timeout
        self._queue = asyncio.Queue(maxsize=capacity)
        self._worker = None
        self._closed = False

    @property
    def pending(self):
        return self._queue.qsize()

    @property
    def closed(self):
        return self._closed

    def start(self):
        if self._worker is None:
            self._worker = asyncio.create_task(self._serve())
        return self

    def submit(self, item_id, text):
        """Queue one request; False when the queue is full or the call is closing."""
        if self._closed or self._queue.full():
            return False
        self._queue.put_nowait((str(item_id), str(text or '').strip()))
        logger.warning('Live Voice: delegation queued item=%s depth=%d', item_id, self._queue.qsize())
        return True

    async def stop(self):
        self._closed = True
        if self._worker is not None:
            worker, self._worker = self._worker, None
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass

    async def _serve(self):
        while True:
            item_id, text = await self._queue.get()
            answer = FAILED_TEXT
            try:
                logger.warning('Live Voice: delegation started item=%s', item_id)
                result = await asyncio.wait_for(self._run(text, item_id), self._timeout)
                answer = speakable(result) or FAILED_TEXT
            except asyncio.TimeoutError:
                answer = TIMEOUT_TEXT
                logger.warning('Live Voice: delegation timed out item=%s', item_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Live Voice: delegated task failed item=%s', item_id)
            try:
                await self._report(item_id, answer)
                logger.warning('Live Voice: delegation reported item=%s chars=%d', item_id, len(answer))
            except Exception:
                logger.exception('Live Voice: could not append delegation result item=%s', item_id)


def core_message_event(text, source):
    """The core's inbound event type; imported here so the module stays importable alone."""
    from gateway.platforms.event import MessageEvent

    return MessageEvent(text=text, source=source,
                        user_id=getattr(source, 'user_id', None),
                        user_name=getattr(source, 'user_name', None))


class ChatWork:
    """Runs a delegated request in its own thread under the chat the call came from.

    One thread per task means tasks stop queueing in a single conversation, each one is a
    normal Hermes thread the caller can open and keep typing in, and each gets its own
    session, tools and permissions because the request is dispatched through the
    adapter's own inbound path. A call already running in a thread has nothing to nest
    into, and a channel that refuses thread creation falls back to the call's own chat.

    The wait for the answer is session-aware: it ends when the gateway reports that THIS
    conversation's turn completed, not after a guessed interval. An earlier version gave
    up after 1.5 quiet seconds and told the call a task had failed while the chat was
    still working on it for another twenty.
    """

    def __init__(self, adapter, source, chat_id, *, quiet=QUIET_S, deadline=None,
                 name_prefix='live voice: ', event_factory=None):
        self.adapter = adapter
        self.source = source
        self.chat_id = str(chat_id)
        self.quiet = quiet
        self.deadline = deadline
        self.name_prefix = name_prefix
        self.event_factory = event_factory or core_message_event
        self.threads = {}
        self.outcome = None
        self._chat = self.chat_id
        self._parts = []

    async def _capture(self, chat_id, content, *args, **kwargs):
        # Drop-in for adapter.send, which callers await.
        if str(chat_id) == self._chat and isinstance(content, str) and content.strip():
            self._parts.append(content.strip())

    async def _capture_edit(self, chat_id, message_id, content=None, *args, **kwargs):
        # edit_message(chat_id, message_id, content) -- content is the third argument.
        await self._capture(chat_id, content)

    async def _seed(self, chat_id, text):
        """Show the caller what was asked, so the thread reads on its own."""
        try:
            await self.adapter.send(chat_id, '\N{STUDIO MICROPHONE} ' + ' '.join(str(text).split()))
        except Exception:
            logger.warning('Live Voice: could not seed task thread %s', chat_id, exc_info=True)

    async def _thread_for(self, text, item_id):
        if getattr(self.source, 'chat_type', None) == 'thread':
            return self.chat_id
        create = getattr(self.adapter, 'create_handoff_thread', None)
        if not callable(create):
            return self.chat_id
        name = (self.name_prefix + ' '.join(str(text).split()))[:80].strip() or self.name_prefix.strip()
        try:
            thread_id = await create(self.chat_id, name)
        except Exception:
            logger.warning('Live Voice: task thread creation failed; using the call chat', exc_info=True)
            return self.chat_id
        if not thread_id:
            return self.chat_id
        thread_id = str(thread_id)
        self.threads[str(item_id)] = thread_id
        logger.warning('Live Voice: task %s runs in thread %s', item_id, thread_id)
        await self._seed(thread_id, text)
        return thread_id

    def _session_key(self, source):
        """The core's own key for this conversation, so the right turn is watched."""
        derive = getattr(self.adapter, '_source_session_key', None)
        if not callable(derive):
            return None
        try:
            return derive(source)
        except Exception:
            logger.debug('Live Voice: no session key for delegated work', exc_info=True)
            return None

    def _keys_for(self, source):
        derive = getattr(self.adapter, '_source_session_key', None)
        if not callable(derive):
            return None
        try:
            return derive(source)
        except Exception:
            return None

    def _watch_turn(self, session_key):
        """Watch THIS conversation's completion.

        Installed BEFORE dispatch: a fast turn can finish while handle_message is still
        running, and a hook installed afterwards misses that completion entirely (the
        first version waited out its deadline with the answer already in hand).

        Returns ``(completion, release)``; ``release`` restores the adapter's own hook.
        """
        completion = asyncio.Event()
        original = getattr(self.adapter, 'on_processing_complete', None)
        if not callable(original):
            logger.warning('Live Voice: no completion hook on the adapter; reporting what was captured')
            completion.set()
            return completion, lambda: None

        async def hooked(event, outcome):
            try:
                mine = self._keys_for(getattr(event, 'source', None))
                if session_key is None or mine is None or mine == session_key:
                    self.outcome = getattr(outcome, 'value', outcome)
                    completion.set()
            finally:
                await original(event, outcome)

        self.adapter.on_processing_complete = hooked

        def release():
            self.adapter.on_processing_complete = original

        return completion, release

    async def _turn_finished(self, completion, item_id=None):
        if completion.is_set():
            return True
        try:
            await asyncio.wait_for(completion.wait(), self.deadline)
            return True
        except asyncio.TimeoutError:
            logger.warning('Live Voice: the turn for item=%s has not completed yet', item_id)
            return False

    async def __call__(self, text, item_id=None):
        from dataclasses import replace

        chat_id = await self._thread_for(text, item_id)
        source = self.source
        if chat_id != self.chat_id:
            source = replace(self.source, chat_id=chat_id, chat_type='thread',
                             thread_id=chat_id, parent_chat_id=self.chat_id)
        event = self.event_factory(str(text), source)
        session_key = self._session_key(source)
        send, edit = self.adapter.send, getattr(self.adapter, 'edit_message', None)
        self._chat, self._parts, self.outcome = chat_id, [], None
        self.adapter.send = self._capture
        if edit is not None:
            self.adapter.edit_message = self._capture_edit
        completion, release = self._watch_turn(session_key)
        try:
            await self.adapter.handle_message(event)
            await self._turn_finished(completion, item_id)
            if self.outcome == 'failure':
                logger.warning('Live Voice: the turn for item=%s reported failure', item_id)
        finally:
            release()
            self.adapter.send = send
            if edit is not None:
                self.adapter.edit_message = edit
        joined = '\n\n'.join(self._parts).strip()
        return joined or FAILED_TEXT
