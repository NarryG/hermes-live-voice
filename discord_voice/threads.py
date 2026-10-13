"""Let the call see the text threads around it.

Reported live: "I just started this thread and it told me it can't see it". The call had
no way to know which threads exist, so it could not answer questions about them. This
builds a directory from the guild's own threads (the plugin shares the process with the
Discord client, so it reads them directly -- no token juggling, no REST client) and
searches it on demand.
"""

import logging

logger = logging.getLogger(__name__)

DIRECTORY_LIMIT = 20
NAME_LIMIT = 60


def _threads(guild):
    """Threads Discord currently considers active, newest first where known."""
    found = {}
    for source in ('threads', 'active_threads'):
        getter = getattr(guild, source, None)
        try:
            items = getter() if callable(getter) else getter
        except Exception:
            logger.debug('Live Voice: could not read guild.%s', source, exc_info=True)
            items = None
        for thread in items or ():
            thread_id = getattr(thread, 'id', None)
            if thread_id is not None:
                found.setdefault(str(thread_id), thread)

    def recency(thread):
        return int(getattr(thread, 'last_message_id', 0) or 0)

    return sorted(found.values(), key=recency, reverse=True)


def _line(thread):
    name = ' '.join(str(getattr(thread, 'name', '') or '').split())[:NAME_LIMIT]
    parent = getattr(getattr(thread, 'parent', None), 'name', None)
    where = f' in #{parent}' if parent else ''
    return f'- {name or "(unnamed)"} (id {thread.id}{where})'


def directory(guild, limit=DIRECTORY_LIMIT):
    """A short list of threads the call can be asked about."""
    lines = [_line(t) for t in _threads(guild)[:limit]]
    return '\n'.join(lines)


def find(guild, query, limit=DIRECTORY_LIMIT):
    """Threads whose name contains *query* (case-insensitive); all of them if blank."""
    needle = ' '.join(str(query or '').split()).lower()
    threads = _threads(guild)
    if needle:
        threads = [t for t in threads if needle in str(getattr(t, 'name', '') or '').lower()]
    return threads[:limit]


def report(guild, query=''):
    """What to tell a caller who asked about threads."""
    found = find(guild, query)
    if not found:
        known = directory(guild)
        tail = f'\nThreads I can see right now:\n{known}' if known else '\nI cannot see any threads in this server.'
        return f'No thread matches "{query}".{tail}' if query else 'I cannot see any threads in this server.'
    header = f'Threads matching "{query}":' if query else 'Threads I can see:'
    return header + '\n' + '\n'.join(_line(t) for t in found)
