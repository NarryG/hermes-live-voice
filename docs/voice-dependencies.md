# Voice dependency bootstrap and durable image prerequisites

Run from the reviewed plugin checkout with the **actual owning host interpreter**:

```sh
/path/to/host/python -m discord_voice.bootstrap --dry-run
```

Default behavior is also dry-run. Exit 0 means local dependency readiness; exit 1
means not ready, including a dry-run with missing dependencies. The command imports
and checks the declared Discord.py, aiortc, PyAV and aiohttp versions and PyNaCl,
actually encodes and decodes one 20ms 48kHz stereo PCM frame using Discord.py's
native libopus, and executes `ffmpeg -version`. It does not join Discord, negotiate
WebRTC, run Codex, read credentials/config, select a provider/account, or restart.
This is not a participant-consent, provider-entitlement or live playback test.
Use a fresh process after native dependency provisioning.

## Python dependencies: use Hermes PM, not ad hoc pip

The existing `pyproject.toml` is the persistent Python declaration. Current official
Hermes plugin installation/enablement obtains dependency consent and PM prepares
the core/extras/enabled-plugin union before atomic publication. `--yes-deps`
answers that consent up front; `--no-deps` leaves a newly downloaded plugin disabled.
Manual pip installs in a Hermes-generated environment are **not durable** and may
be discarded when PM replaces the generation. Do not use this utility's standalone
pip option to modify a Hermes-managed environment. Resolve conflicts through PM;
never silently change the owner's provider, account or other plugin selection.
The bootstrap does not invoke install/enable or guess the active profile.

Authoritative references inspected for this implementation:

- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
  (Shared dependency admission / Python dependencies).
- https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/plugins.py
  (passive checks never install; active ensure-deps is a separate operation).
- This repository's `pyproject.toml` and `plugin.yaml`. No invented native-package
  manifest fields or registration changes are required.

For a **separate developer/test virtual environment only**, an explicit option can
install the reviewed source project's Python requirements via that interpreter's
pip, then re-probe:

```sh
/path/to/standalone-venv/python -m discord_voice.bootstrap --apply --python-deps
```

A system interpreter is refused. A virtual environment is necessary, not proof
that it is outside PM: the operator must choose a standalone environment. pip must
already exist there. The source checkout is required for this option; the probe
itself works from the packaged module. Missing pip, package failures and failed
post-install readiness return a failure, not fabricated success.

## Native packages: explicit administrator ownership

`libopus` here is the native audio codec, **not an Anthropic Opus model**. Voice
extras/PyNaCl and PyAV wheels do not provide Discord.py's Linux libopus loader.
The bootstrap selects only missing packages:

| Linux family | Native codec package | Package command |
|---|---|---|
| Debian / Ubuntu | `libopus0` | `apt-get install -y libopus0 ffmpeg` |
| Fedora / RHEL | `opus` | `dnf install -y opus ffmpeg` |
| Alpine | `opus` | `apk add opus ffmpeg` |
| Arch | `opus` | `pacman -S --needed --noconfirm opus ffmpeg` |

These use configured repositories only; ffmpeg may require administrator-approved
repositories on some RHEL/Fedora releases. No repository changes, downloads of
handbuilt libraries, linker hacks, sudo, containers or privilege escalation are
attempted. Unsupported distributions fail with instructions rather than guessing.
macOS guidance is `brew install opus ffmpeg`; Windows uses Discord.py's bundled
Opus and requires ffmpeg on PATH. Automatic system installation is Linux-only.

An administrator can explicitly execute the missing native-package plan:

```sh
/path/to/host/python -m discord_voice.bootstrap --apply --system
```

The process must already have root authority if packages are missing. UID10000
gets an actionable administrator command and no package effects. When everything
is ready, repeated `--apply` is a no-op and needs no administrator authority.
Repository metadata refresh is separately administrator-owned (for example,
`apt-get update` before provisioning a fresh Debian image).

## Containers: bake prerequisites into the owning image

Runtime `apt-get` repairs disappear when a container is recreated. Add this layer
to the **actual owning Debian/Ubuntu image build**, adapting its existing user
and build policy, rather than patching an unrelated container:

```dockerfile
# Fragment for the existing image; not a new gateway image or deployment command.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends libopus0 ffmpeg \
 && rm -rf /var/lib/apt/lists/*
# Restore the image's original non-root runtime user (10000 on the verified host).
USER 10000
```

Retain the plugin and its `pyproject.toml` in the deployment's normal persistent
plugin path; let the supported PM admission path prepare Python dependencies in
new generations. Run the read-only preflight with the resulting owning
interpreter at image acceptance/startup, before admitting a voice call. Do not
claim that installing into a transient running container makes a durable image.
This fragment was not built here: no Docker socket/admin/build/deploy permission
was used. Debian package provisioning was done separately by the authorized
administrator; this worker did not repeat it.

## Tests

```sh
python -m pytest tests/test_voice_bootstrap.py -q
python -m pytest -q -p no:cacheprovider
```

Bootstrap effect tests inject a synthetic package runner and verify explicit
scopes, non-root refusal, dry-run/no-op, repeated application and post-install
verification. The local integration probe exercises real codec and ffmpeg
readiness; run these tests in a provisioned voice/test environment. No test
executes a real system package installation or provider voice request.
