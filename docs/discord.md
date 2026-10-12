# Discord Live Voice — host-selected broker preview

The native `/livevoice` command is wired to an owning-host Codex metadata adapter,
a server-side token-opaque subscription negotiation broker, and direct V3 WebRTC
media. This is an **offline-exercised preview**, not a live entitlement or Discord
playback certification. Independent review, a pinned release, and the separate
installation agent's handoff remain required; no deployment/restart was performed.

## Supported selection and effects

Initial support is the **registered hosting/default gateway adapter** only. The
registration captures Hermes home and the existing producer's process environment
and working directory once. It does not read credentials/config or launch Codex.
The native factory validates the actual runner `_owning_profile` and immutable
`AdapterScope(home, profile)` before persona loading or command installation.
Named/satellite profiles are refused: no preconfigured, proven per-profile
`CODEX_HOME` mapping seam is implemented. No late ambient home, Desktop-selected
bot, Hermes-managed OAuth, copied login or account selector is substituted.

After authenticated command admission and the owner's two-step disclosure/opt-in,
the asynchronous owner preflight runs **before capacity reservation, Discord join,
media construction or provider negotiation**. Implementation admission (`admitted`)
is deliberately distinct from successful same-scope `account_ready`; `factory()`
refuses until that preflight succeeds. Other participants consent before media.

The owner has approved **normal native CLI metadata/config/cache loading and
renewal of its existing login on its owning host**. This is not a read-only sandbox:
Codex startup can perform native managed-config discovery, cache writes and existing
login recovery. The client itself sends only:

- `initialize` and `initialized` (home/version metadata; pinned **0.162.1**).
- `account/read` (`refreshToken:false` normally; one proactive native renewal if
  the captured existing token is expired/near expiry).
- `config/read` with `includeLayers:true` and the captured producer CWD.
- `configRequirements/read`, with the pinned unit/omitted-params wire shape.

There are **no** thread/start, realtime/start, turn, tool, login, account-selection,
task or delegation-execution requests. Voice never travels through app-server.
There are no `-c` overrides, feature/policy changes, added keys or API fallback.

## Current account/config proof and refusal cases

The adapter compares the selected file credential's account/user/plan/email before
and after normal native loading/renewal. It requires the native account metadata's
email/plan and workspace routing account to agree with that same owner. `chatgpt`
type alone is not proof. JWT claims are consistency checks, not a signature verifier;
authority is the scoped native file owner and that pinned CLI process.

Two consecutive native account/config/layers/requirements reads, with a stable
credential/file sandwich, must agree. File reads are bounded, no-follow and owner-
permission checked. Native source names, versions, origin references and user/project
paths are checked. The complete metadata and credentials contribute to the pinned
generation. File/home/permissions/native-scope/environment/process checks are cheap
and performed during media; one persistent child polls metadata every two seconds.
Cached truth expires after eight seconds without a successful unchanged poll. Any
change or unknown response revokes the capability; there is no fast retry under a
changed identity/config and no per-audio-frame process launch.

Supported credentials are native **file** mode, including its source-proven default
and legacy missing `auth_mode` shape. **Keyring, auto, ephemeral/session/external
bearers, API keys, PAT, agent identity, Bedrock and FedRAMP are refused** rather than
reading a different store. Managed/cloud/MDM policy layers or requirements, custom
provider definitions/endpoints, unsupported config/source fields, routing/residency
constraints, selected CLI profile-v2 layers, unsafe paths/permissions, mismatched
identity, unknown version or stale metadata are also refused. These restrictions
are not instructions to change the owner's existing configuration.

Voice selection follows the existing Desktop V3/WebRTC consumer and pinned native
semantics, without rewriting effective config. Missing ordinary provider/mode/
transport fields resolve to the source-proven OpenAI conversational WebRTC lane;
conflicting explicit values are refused. The CLI **text model is not the voice
model**. The configured realtime model is retained, otherwise the pinned V3 default
is `gpt-live-1-codex`. Configured V3 voice is retained, otherwise the existing
consumer default is `cove`. A plugin voice label can only match the selected voice;
it cannot select another. V3 labels are juniper/maple/spruce/ember/vale/breeze/arbor/
sol/cove, not the older V2 set.

## Native consent and call controls

- One-to-one and consented groups (at most 16 admitted humans) in **guild/server
  voice channels**, never DM self-bots, DM ringing, another bot/client or recording
  bots. Existing guild voice connections are refused, not taken over.
- First `start` discloses the processing destination and exact audience, with no
  credentials/metadata load, capacity or join. Repeat within 60 seconds for that
  unchanged audience to opt the owner in.
- Each participant's first `consent` discloses processing/audience; a second native,
  authenticated consent for the same call/epoch/audience records opt-in. Group
  media starts only after every admitted participant consents and owner resumes.
- The owner controls `pause`, `resume`, `stop`, `status` in the original text
  channel/profile. Any participant can `revoke`. Membership/access uncertainty
  immediately fences/drains media and requires fresh consent.
- Persona includes only validated owner's `SOUL.md`, not USER/MEMORY/WORKING,
  Desktop history or task state. Language defaults to English; Spanish is explicit.
- Delegations receive an honest **not executed** reply on ordered `oai-events`.
  No executor callback, worker or automatic text-task/private-session attachment exists.

## Media and cleanup contract

The broker alone holds credentials and posts the native subscription call request
to the selected default ChatGPT endpoint. It returns only SDP, call ID and a
nonsecret model/voice/media capability. HTTP redirects, environment proxy/netrc
lookup and provider negotiation retries are not used.

The lane uses real Opus RTP and an ordered `oai-events` channel. Ready requires a
connected peer, open ordered channel, remote audio negotiation and an actual
`session.started`/`session.updated` session identity; channel open or RPC acknowledgement
alone is not readiness. Explicit provider model/voice/version/session mismatch fails.

Input/output are PCM signed16 little-endian **24kHz mono** at the native boundary.
Per-admitted-speaker conversion uses receive-time aligned 20ms mixing with a 40ms
window and sum/clamp, not fabricated provider speaker labels. Bounded queues,
256-packet UDP handoff, 100 mixer bins, independent collection and one in-flight
append retain normal group audio without silent truncation. Output backpressure
retains a complete valid 48000-byte chunk before taking later output. Sustained
overload fails/fences visibly. Synchronous drain is safe from native UDP threads;
resuming creates a fresh peer/conversation.

`stop()` waits for actual local peer cleanup but returns **provider stop unknown**:
a local close is not a remote acknowledgement. Failed Discord disconnect retains
exact session/capacity as `cleanup_pending`, with audio fenced and owner stop retry.
It is never reported as stopped while still owning a connection.

## Command ownership and gateway lifecycle

The plugin preserves native `_get_discord_command_sync_policy()` **safe/bulk/off**,
awaits `_post_connect_task`, never bulk-syncs the shared tree, and uses single-command
HTTP routes with exact application/command ID, version/payload and readback.
`off` makes no remote requests. Existing command collisions and ambiguous writes
need operator reconciliation; no deletion by guessed name occurs.

Failed disconnect/deletion keeps retryable ownership. The host offers no remote
command lease/CAS, so fetch-then-write/readback does not serialize another external
writer. Replacement clients/local commands are preserved. **Same-client hot reload
or re-enable is unsupported** because native factory keys are cached for the bot
lifetime. An owning gateway restart is required after a reviewed install/reload;
no cache mutation, playback monkeypatch or core change is used here.

## Offline verification and installation gate

`pyproject.toml` is the native installer dependency declaration and CI source:
aiortc 1.15.0, PyAV 17.1.0, aiohttp 3.x and Discord.py 2.7.1 with voice extras.
The source plugin still requires the existing Hermes native Discord host APIs and
receiver. An installer must verify those APIs/dependencies on its own host.
See [voice dependency bootstrap](voice-dependencies.md) for the read-only native
codec/ffmpeg preflight, explicit administrator provisioning, Hermes PM Python
dependency admission, and durable container image prerequisites.

```sh
python -m pip install '.[test]'
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

Tests never run real CLI/auth/config/provider loading. Only one fixed, explicitly
labelled local metadata child is admitted by the test subprocess guard. Integrated
native command tests use actual Discord SDK command/bot objects, synthetic host/
voice boundaries, localhost HTTP negotiation and **two real local aiortc peers with
STUN disabled**. They exercise owner preflight, one/two-speaker PCM, ordered events,
delegation refusal, consent/revocation and actual local peer cleanup. A clean source
archive also imports/registers without Git history or an installed Hermes Talk.
The obsolete WebSocket/backing-agent prototype and its fake protocol tests are removed.

These results do **not** attest provider entitlement, live Discord voice, physical
playback, remote command CAS or live OAuth renewal success. Installation belongs to
the separate agent after independent review and a pinned publication; live testing
also needs a selected channel and participant scope. This document is not deployment
or new-login permission.

## Source and attribution

Pinned Codex source: `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`, 0.162.1;
`app-server-protocol` account/config/v1 initialize schemas, native file auth semantics,
`core/src/realtime_conversation.rs` V3 model default, and this plugin's existing
`dashboard/plugin_api.py` Desktop V3 consumer. The reduced schema key list carries
Apache-2.0 attribution. PCM/framing patterns retain the MIT-licensed Hermes Talk
notice. See `NOTICE.md` and `licenses/Codex-Apache-2.0.txt`.
