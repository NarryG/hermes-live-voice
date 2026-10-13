# Live Voice (talk-desktop) — rebuild, update and diagnose recipes

Everything operational, learned the hard way. Read the mental model first: most
of the confusion in this lane comes from **which copy of the code is running**.

## Mental model (what runs where)

| Thing | Where it lives | Wiped by |
|---|---|---|
| Plugin sources (the thing you edit/update) | `<HERMES_HOME>/plugins/talk-desktop` (`/opt/data/plugins/talk-desktop`) | nothing — it is on the data volume |
| **The copy the process actually imports** | `<HERMES_HOME>/installs/<install>/environments/<generation>/workspace/plugin-sources/talk-desktop-<sha256(path)[:16]>/` | collected when its generation is superseded |
| Plugin Python deps (aiortc, av, discord.py[voice], davey) | the **generation venv** (`…/environments/<generation>/venv`), installed by uv from the plugin's `pyproject.toml` | every new generation materialises them again (from pyproject, so no manual pip is needed) |
| System libraries (libopus, ffmpeg) | the container image layer | every container **recreate** |
| Voice connection + Discord lane | the **dashboard backend** process (`hermes-desktop-backend.service`, a host unit that `docker exec`s in) | container restart |

Three consequences that cost hours here:

1. **Updating `<HERMES_HOME>/plugins/talk-desktop` does not change the running code.** The env member copy is keyed by the *plugin path*, not its content, so an existing generation keeps serving the old bytes. After updating sources you must either force a new generation or refresh the member copies (`lane-update.sh` does the latter).
2. **A container recreate silently removes the media stack and codec**, so `/livevoice` answers `preflight refused`, or the bot joins and plays nothing.
3. **Discord voice has required DAVE (E2EE) since March 2nd.** Without a working `davey` the bot connects, shows green, and is silent in both directions — `DAVE decrypt failed … NoDecryptorForUser` in the log.

## Recipe A — rebuild the lane in a fresh container

Run from the host that owns the container (`docker` must be available; there is no
docker inside the container).

```sh
C=<container>            # e.g. orca-hermes-hermes-1
docker exec -i $C bash -s < scripts/lane-bootstrap.sh     # deps + codec + verification
docker exec -i $C bash -s < scripts/lane-update.sh <40-hex-sha>   # plugin revision
docker restart --timeout 45 $C
systemctl restart hermes-desktop-backend
docker exec -i $C bash -s < scripts/lane-status.sh        # verify
```

Then, from Discord, run `/livevoice` → `operation: status`, then `operation: start`
from inside the private voice channel. A real invocation is the only proof.

Expected: `lane-bootstrap.sh` ends with `RESULT: ready`, `lane-status.sh` reports
`opus=ok`, `davey>=0.1.7`, and a member copy that matches the plugin directory.

## Recipe B — update the plugin to a pinned revision

Never copy files into the live plugin dir by hand and expect them to run (see
consequence 1). Use:

```sh
docker exec -i $C bash -s < scripts/lane-update.sh <sha>
docker restart --timeout 45 $C && systemctl restart hermes-desktop-backend
```

`lane-update.sh` stages the revision with the supported installer, runs that
revision's test suite, publishes it into the plugin directory with a backup, and
**refreshes every generation member copy** so the next start imports the new code.

Do **not** use `hermes plugins update talk-desktop`: it tracks the branch HEAD,
and the fork's `main` carries a known-broken upstream ingest. Pin a SHA.

## Recipe C — update Hermes core (image repin)

The container is Orca-managed (`io.orca.hermes.managed=1`), defined by Orca's
`compose.json`, pinned by image digest. There is no Orca CLI verb and no host
`hermes`; follow the established receipts in that directory: capture state → back
up → repin the digest → recreate → health-check.

- `-p` is **mandatory**: `docker compose -p orca-hermes -f <compose.json> up -d`
  (the file sits in a differently named directory, so a plain `-f` derives the
  wrong project and dies with `Conflict … name is already in use`).
- Verify the new image before repinning, from the image itself:
  `docker run --rm --entrypoint /opt/hermes/bin/hermes -e HERMES_HOME=/tmp/p <image> --version`.
- **After every recreate run Recipe A's bootstrap.** The venv is in the image.
- The plugin and its config live on the volume, so they survive: re-verify
  `config.yaml` / `.env` / `auth.json` hashes are unchanged afterwards.

## Recipe D — diagnose "refuses" or "no audio"

Use `scripts/lane-status.sh` first; it prints all of this. Reading the symptoms:

| Symptom | Cause | Fix |
|---|---|---|
| `Voice unavailable: … preflight refused` | media stack missing after a recreate, or the CLI-config guard refusing | `lane-bootstrap.sh`; the message now names the reason in brackets |
| `Opus codec not found — voice channel playback disabled` in the boot log | libopus absent | `lane-bootstrap.sh` (apt `libopus0`; the image's apt cache starts empty, so `apt-get update` first) |
| Bot joins, green, **silence both ways**, log has `DAVE decrypt failed … NoDecryptorForUser` | `davey` too old (Discord DAVE is mandatory) | `lane-bootstrap.sh` pins `davey>=0.1.7` |
| Core "normal voice" silent, log has `TTS … 403 … speech.platform.bing.com` | edge-tts too old (Microsoft changed the endpoint) | `lane-bootstrap.sh` pins `edge-tts>=7.2.8` |
| `Failed to join voice channel: Already connected` | the lane already holds the channel | expected while a live session is active; stop it with `/livevoice operation: stop` |
| `Can't keep up, shard … behind` / `Disconnected from voice` | event loop starved (usually heavy I/O during restarts/GC), not a lane bug | re-check `docker stats`; it recovers once churn stops |

If the refusal message prints a bracket reason such as
`[non-canonical chatgpt_base_url]`, that is the CLI-config guard by value: a real
override is present. A **default** value (`https://chatgpt.com/backend-api/`) is
not an override and must pass.

## Recipe E — reclaim disk (generation pile-up)

Every boot/update can materialise a new generation: ~1.9 GB each (venv + a full
copy of every plugin). The boot GC only collects generations that are
**unselected, lease-free and older than 24 h**, so a busy day parks ~13 of them
(≈21 GB) until they age out.

```sh
# read-only inventory (also reports generations still holding superseded code)
docker exec -i $C bash -s < scripts/lane-status.sh

# reclaim now — no downtime needed: the collector always keeps the SELECTED
# generation and skips any generation with a live lease.
docker exec --user hermes -i $C bash -s <<'EOF'
HOME=/opt/data /opt/hermes/.venv/bin/python -c "
from pathlib import Path
from hermes_cli.runtime_state import collect_generations
print([p.name for p in collect_generations(Path('/opt/hermes'), min_age_seconds=0)])"
EOF
```

**Pass the CHECKOUT root (`/opt/hermes`), never the data home.** Install state is
keyed by `sha256(project_root)[:16]` (`pm/environments.install_key`), so
`collect_generations(Path('/opt/data'))` resolves to a *different, empty* install
and silently reports `collected 0` — which is exactly how a 21 GB pile looks
"already clean". Verify the key matches the live install first:

```sh
docker exec $C /opt/hermes/.venv/bin/python -c "
import hashlib; from pathlib import Path
print(hashlib.sha256(b'/opt/hermes').hexdigest()[:16])"   # must equal the directory name under /opt/data/installs/
```

What it keeps: the **selected** generation (`facts.json` →
`packages.venv.environment`) and anything with a live lease. It does **not** need
the gateway stopped — the lock is only held during an install, and the lease
mechanism fences in-use generations. Observed here: 13 generations (21 GB) → 1
(2.1 GB). `hermes pm gc` is a different thing: it prunes the package store, not
environments.

## Traps

- **Host vs container.** `docker …` only exists on the host; a script that calls it
  must be piped to the *host* shell (`ssh host 'sh -s' < file`). A script that must
  run *inside* the container is piped through `docker exec -i $C sh -s`.
- **Never inline multi-line heredocs through ssh.** Write the script to a file and
  pipe it (`sh -s`, `python3 -`); quoting inside heredocs broke repeatedly.
- **`pkill -f pytest` kills your own session**: `-f` matches the shell running the
  pattern. Use `pkill -f 'pytes[t]'` or a pid.
- **Discord command identity.** The lane adopts a command that carries the fields it
  specifies, and refuses (or orphans) anything else. Changing the command's
  *options* or its payload changes that shape — so remove behaviour, not option
  names. An unchanged command id across a restart proves adoption worked.
- **`--no-deps` installs skip the lane's runtime deps.** Stage with it, but make the
  generation's own sync install the real ones (Recipe A does).
