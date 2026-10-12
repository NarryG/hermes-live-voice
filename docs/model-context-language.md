# Model context language

This fork defaults to English independently of `navigator.language`. The Desktop gear menu contains an explicit **Voice language** preference, stored under `language.v1`. Only a saved `es` preference selects Spanish. Missing, unsupported or unreadable preferences keep English. Language cannot change while a call is active or being started.

All `/session`, `/codexlive/session` and `/tool` requests carry `language: "en"` or `"es"`, including the currently hidden preview path. The backend selects each request independently; missing or unsupported values use English. Codex thread reuse includes language, so changing the explicit preference starts a thread with matching instructions rather than reusing Spanish context.

The shipped defaults request English or neutral Spanish, with no inherited Chilean dialect or personal owner name. The exact old upstream single-language defaults and bundled `language_directive.txt` are treated as shipped defaults during migration. Missing or blank files use the selected built-in directive. Any different owner-authored file remains a verbatim custom override: the fork does not translate, rewrite or delete it. If a custom directive itself requests Spanish, that explicit content still controls the voice.

Localized model text includes the legacy chat prefix, spoken acknowledgement instruction, delegation results/statuses, omitted-code marker, preview instruction/sample, backend voice/persona wrappers, Codex delegation/skip instructions and tool errors/timeouts. Protocol identifiers such as `skip`, `<realtime_delegation>`, `LIVE_SIN_QUOTA` and `LIVE_HANDOFF_NO_SOPORTADO` are retained for compatibility; their explanations are English by default. Subscription errors do not change the saved engine or billing lane.

Owner requests, dynamic tool output, `SOUL.md`/persona source text and memory are not translated. Existing identity sanitization remains in place; language wrappers must not replace the bot's identity. Backend profile discovery follows Hermes's resolved home rather than a literal `~/.hermes` path.

Tests execute production functions and the real Desktop module with synthetic host/media/provider boundaries. They never sign in, open a microphone, call a provider, or prove spoken-English quality. An older Codex app-server can reject optional prompt fields; the existing dropped-field warning remains, and a prompt omitted by protocol negotiation cannot be claimed to have affected the provider.
