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
                result = await asyncio.wait_for(self._run(text), self._timeout)
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


class ChatWork:
    """Runs a delegated request in the chat the call belongs to.

    Dispatches through the adapter's own inbound path, so the work inherits the
    channel's or thread's normal session, tools and permissions, and captures the
    reply from the adapter's outbound ``send`` for that chat only.
    """

    def __init__(self, adapter, source, chat_id, *, quiet=QUIET_S, settle=None):
        self.adapter = adapter
        self.source = source
        self.chat_id = str(chat_id)
        self.quiet = quiet
        self.settle = settle
        self._parts = []

    def _capture(self, chat_id, content, *args, **kwargs):
        if str(chat_id) == self.chat_id and isinstance(content, str) and content.strip():
            self._parts.append(content.strip())

    async def __call__(self, text):
        from gateway.platforms.event import MessageEvent

        event = MessageEvent(text=str(text), source=self.source,
                             user_id=getattr(self.source, 'user_id', None),
                             user_name=getattr(self.source, 'user_name', None))
        send = self.adapter.send
        self.adapter.send = self._capture
        try:
            await self.adapter.handle_message(event)
            while True:
                before = len(self._parts)
                await asyncio.sleep(self.quiet)
                if len(self._parts) == before:
                    break
        finally:
            self.adapter.send = send
        joined = '\n\n'.join(self._parts).strip()
        return joined or FAILED_TEXT
