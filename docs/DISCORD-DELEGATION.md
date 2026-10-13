# Discord voice lane: delegation plan

## Where it stands

The call now runs with `delegation: server` (plugin `settings.json`): the lane's prompt tells the
voice model it has no tools of its own and should delegate work, and the Codex CLI owner reports the
same mode. The transport still declares `delegation: {'type': 'client'}` toward the provider, which is
correct and stays: `client` means *this lane* executes what the call asks for.

What is missing is the executor. Today `webrtc.py` answers every `delegation.created` event with
`delegation_refusal()` -- "Task execution is unavailable in this voice call" -- which is the "I don't
have a lane" line the caller hears.

## The flow we are building

1. The voice model decides a request needs real work and the provider emits `delegation.created`
   (`target: client`, i.e. us). `webrtc.py` already parses this into `Event('delegation', id, text)`.
2. The lane enqueues the request: FIFO, one in flight, bounded queue.
3. A runner executes the request as a real Hermes turn in the call's **work thread**, with tools.
4. The runner's reply is trimmed to something speakable and appended to the voice session as
   `delegation.context.append` (the same message shape the refusal uses today).
5. The voice model reads the result aloud. On failure or timeout the graceful refusal text is
   appended instead, and the reason is logged.

## Phase 1 -- one work thread

- **Work thread** = the text channel the `/livevoice` command was run in; if that is a plain channel,
  open a thread under it (the adapter already ships thread-title support). Everything the call
  delegates lands here, so a listener can watch and continue in text.
- **Serialization**: one delegation in flight per call; queue cap 4; per-request timeout; late results
  are still appended (they are useful context) but never spoken out of order.
- **Speakable replies**: cap the appended text (a few hundred words) so the call reads facts, not an
  essay. Raw output stays in the thread.
- **Gate**: only when `settings.json` says `delegation: server`. The default `client` keeps today's
  refusal, byte for byte, so no existing behaviour changes.
- **Observability**: log each step -- received / queued / started / finished / appended -- with the
  delegation id, so the next failure is visible in the log instead of inferred.
- **Tests**: a delegation reaches the runner with the right text; the result is appended; a timeout
  appends the refusal; client mode never calls a runner; the queue cap holds.

## Phase 2 -- a thread per task ("task to task")

Each delegation opens its own thread under the work thread and runs there, so tasks do not queue in a
single conversation and each one is a normal Hermes thread the caller can open and continue in text.
A small registry maps delegation id -> thread id -> result, which also lets the call report *where* a
task lives ("that one is in the thread called X") and lets results be read back in completion order.

## Constraints and unknowns

- **One call at a time**: `SessionManager` is "one call per supplied profile adapter, one guild owner,
  no automatic joins", with a global capacity semaphore. A single voice channel is therefore a real
  limit, not a bug -- multi-thread work is text-side and does not need a second voice channel.
- **Critical path**: how the lane runs a real turn (the adapter's inbound message path versus the
  gateway's session runner). Prototype this first; every other piece is mechanical once it works.
- **Secrets and state**: delegated work can touch anything the profile can reach. The lane speaks its
  result aloud in a voice channel with a live audience, so the appended text is trimmed and never
  includes tool output verbatim.
