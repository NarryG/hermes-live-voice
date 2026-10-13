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

CONCURRENCY = 2   # turns at once: the voice process shares its loop with them
BACKLOG = 4       # further requests wait here instead of being refused
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


class TaskBoard:
    """Dispatches delegated work in parallel and reports each task as it finishes.

    A call can hand over several things at once; each runs in its own thread, so each has
    its own session and they cannot interrupt one another (the gateway leases a session
    to one turn at a time -- sharing a session is what aborted a delegated turn and left
    the call waiting for an answer that could never arrive).

    ``run(text, item_id)`` executes one request, ``report(item_id, text)`` hands its
    answer back to the call. Capacity is the number of tasks in flight at once; beyond it
    a request is refused rather than silently dropped.
    """

    def __init__(self, run, report, *, capacity=CONCURRENCY, backlog=BACKLOG, timeout=TIMEOUT_S):
        self._run = run
        self._report = report
        self._capacity = max(1, int(capacity))
        self._backlog = max(0, int(backlog))
        self._timeout = timeout
        self._inflight = {}
        self._waiting = []
        self._closed = False

    @property
    def inflight(self):
        return dict(self._inflight)

    @property
    def closed(self):
        return self._closed

    def start(self):
        return self

    def submit(self, item_id, text):
        """Queue one request. A busy voice process runs few turns at once on purpose.

        Agent turns hold the process's event loop; several at once starved the audio path
        badly enough to produce ten-second gaps in both directions. Extra work waits in a
        bounded backlog instead of being refused while there is room for it.
        """
        if self._closed:
            return False
        item_id = str(item_id)
        if len(self._inflight) >= self._capacity:
            if len(self._waiting) >= self._backlog:
                return False
            self._waiting.append((item_id, str(text or '').strip()))
            logger.warning('Live Voice: task queued item=%s waiting=%d inflight=%d',
                           item_id, len(self._waiting), len(self._inflight))
            return True
        self._dispatch(item_id, str(text or '').strip())
        return True

    def _dispatch(self, item_id, text):
        task = asyncio.create_task(self._serve(item_id, text))
        self._inflight[item_id] = task
        task.add_done_callback(lambda _t, i=item_id: self._pump(i))
        logger.warning('Live Voice: task dispatched item=%s inflight=%d waiting=%d',
                       item_id, len(self._inflight), len(self._waiting))

    def _pump(self, finished):
        self._inflight.pop(finished, None)
        while self._waiting and len(self._inflight) < self._capacity and not self._closed:
            item_id, text = self._waiting.pop(0)
            self._dispatch(item_id, text)

    async def stop(self):
        self._closed = True
        self._waiting.clear()
        tasks = list(self._inflight.values())
        self._inflight.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _serve(self, item_id, text):
        answer = FAILED_TEXT
        try:
            outcome = await asyncio.wait_for(self._run(text, item_id), self._timeout)
            answer = outcome if isinstance(outcome, str) and outcome.strip() else FAILED_TEXT
        except asyncio.TimeoutError:
            answer = TIMEOUT_TEXT
            logger.warning('Live Voice: task timed out item=%s', item_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Live Voice: delegated task failed item=%s', item_id)
        try:
            await self._report(item_id, answer)
        except Exception:
            logger.exception('Live Voice: could not append delegation result item=%s', item_id)


def core_message_event(text, source):
    """The core's inbound event type; imported here so the module stays importable alone."""
    from gateway.platforms.event import MessageEvent

    return MessageEvent(text=text, source=source,
                        user_id=getattr(source, 'user_id', None),
                        user_name=getattr(source, 'user_name', None))


_ROUTER_ATTR = '_live_voice_send_router'


class SendRouter:
    """One observer on the adapter, shared by every task watching for an answer.

    Wrapping ``adapter.send`` per task looked simple and was not: with several tasks
    running at once, each read the adapter's *current* send (often another task's wrapper)
    and installed its own over it, so the wrappers could chain -- or point at each other,
    which raised "maximum recursion depth exceeded" and made the call report that a task
    could not be run. There is exactly one wrapper per adapter, it always calls the real
    send, and it hands the result to whichever tasks are currently watching.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self._original_send = adapter.send
        self._original_edit = getattr(adapter, 'edit_message', None)
        self._watchers = set()
        adapter.send = self.send
        if self._original_edit is not None:
            adapter.edit_message = self.edit

    async def send(self, chat_id, content, *args, **kwargs):
        result = await self._original_send(chat_id, content, *args, **kwargs)
        self._notify(chat_id, content)
        return result

    async def edit(self, chat_id, message_id, content=None, *args, **kwargs):
        result = None
        if self._original_edit is not None:
            result = await self._original_edit(chat_id, message_id, content, *args, **kwargs)
        self._notify(chat_id, content)
        return result

    def _notify(self, chat_id, content):
        for watcher in tuple(self._watchers):
            try:
                watcher.observe(chat_id, content)
            except Exception:
                logger.exception('Live Voice: a watcher failed to observe a send')

    @property
    def original(self):
        return self._original_send

    def add(self, watcher):
        self._watchers.add(watcher)

    def discard(self, watcher):
        self._watchers.discard(watcher)


def router_for(adapter):
    """The adapter's single send observer, installed on first use."""
    router = getattr(adapter, _ROUTER_ATTR, None)
    if not isinstance(router, SendRouter):
        router = SendRouter(adapter)
        setattr(adapter, _ROUTER_ATTR, router)
    return router


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

    def observe(self, chat_id, content):
        """Called by the adapter's send observer; never sends anything itself."""
        if str(chat_id) == self._chat and isinstance(content, str) and content.strip():
            self._parts.append(content.strip())

    async def _seed(self, chat_id, text):
        """Show the caller what was asked, so the thread reads on its own."""
        try:
            await self.adapter.send(chat_id, '\N{STUDIO MICROPHONE} ' + ' '.join(str(text).split()))
        except Exception:
            logger.warning('Live Voice: could not seed task thread %s', chat_id, exc_info=True)

    def _thread_parent(self):
        """Where task threads live: the call's channel, or the channel its thread sits in.

        A Discord thread cannot host a child thread, so a call started inside one still
        needs the parent channel for per-task threads -- otherwise every task shares the
        call's own session and they abort each other's turns.
        """
        if getattr(self.source, 'chat_type', None) != 'thread':
            return self.chat_id
        parent = getattr(self.source, 'parent_chat_id', None)
        return str(parent) if parent else self.chat_id

    async def _thread_for(self, text, item_id):
        create = getattr(self.adapter, 'create_handoff_thread', None)
        if not callable(create):
            return self.chat_id
        parent = self._thread_parent()
        name = (self.name_prefix + ' '.join(str(text).split()))[:80].strip() or self.name_prefix.strip()
        try:
            thread_id = await create(parent, name)
        except Exception:
            logger.warning('Live Voice: task thread creation failed; using the call chat', exc_info=True)
            return self.chat_id
        if not thread_id:
            return self.chat_id
        thread_id = str(thread_id)
        self.threads[str(item_id)] = thread_id
        logger.warning('Live Voice: task %s runs in thread %s', item_id, thread_id)
        await self._seed(thread_id, text)
        # Say WHERE, in the chat the caller is actually watching: task threads are opened
        # in the parent channel, which is a different place from the call's own thread.
        try:
            await self.adapter.send(self.chat_id, '\N{STUDIO MICROPHONE} on it in <#%s>' % thread_id)
        except Exception:
            logger.warning('Live Voice: could not point at task thread %s', thread_id, exc_info=True)
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
                             thread_id=chat_id, parent_chat_id=self._thread_parent())
        event = self.event_factory(str(text), source)
        session_key = self._session_key(source)
        router = router_for(self.adapter)
        router.add(self)
        self._chat, self._parts, self.outcome = chat_id, [], None
        completion, release = self._watch_turn(session_key)
        try:
            await self.adapter.handle_message(event)
            await self._turn_finished(completion, item_id)
            if self.outcome == 'failure':
                logger.warning('Live Voice: the turn for item=%s reported failure', item_id)
        finally:
            release()
            router.discard(self)
        joined = '\n\n'.join(self._parts).strip()
        return joined or FAILED_TEXT
