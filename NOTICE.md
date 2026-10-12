# Notices

## Third-party code

This plugin bundles a fallback copy of parts of **hermes-talk**
(https://github.com/TheSmokeDev/hermes-talk), by TheSmokeDev and contributors,
under the MIT License. The bundled files live in `dashboard/talk_vendor/` and
keep their upstream copyright headers.

The bundled copy exists so the plugin is **self-contained**: a fresh install
does not need the hermes-talk plugin present. When hermes-talk IS installed,
it is preferred at runtime and the bundled copy stays unused.

Upstream hermes-talk is excellent — if you want its full voice experience
(native voice tools like memory search, background-agent delegation with
approvals, run steering, cascade voice modes), install the real plugin and
this one will use it automatically.

## Discord transport adaptations

The PCM conversion and audio framing/lifecycle patterns in `discord_voice/` are adapted from `TheSmokeDev/hermes-talk`, inspected at commit `2dcf20125dae44b04904d0785c8f6d3db55bbcf1`. The original Synero plugin and its existing attribution remain intact. The upstream permission notice is reproduced below.

MIT License

Copyright (c) 2026 SmokeDev

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Codex source/schema contract

`discord_voice/config_keys.py` is a modified, reduced list derived from the
Apache-2.0 Codex ConfigToml schema. Native protocol/auth/default semantics were
inspected at OpenAI Codex 0.162.1, revision
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`.

OpenAI Codex — Copyright 2025 OpenAI.
The complete upstream licence is in `licenses/Codex-Apache-2.0.txt`.
No Codex binary, OAuth credentials, or external model implementation is bundled.
Upstream's Ratatui notice does not apply to this reduced schema list; no UI code
from Ratatui is included.
