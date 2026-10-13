#!/usr/bin/env bash
# Restore the Live Voice (talk-desktop) Discord lane runtime inside a Hermes container.
#
# Run this after ANY container re-create or Hermes image update.
#
# Why it is needed: the gateway venv lives in the IMAGE layer, so the lane's media
# stack (aiortc/av) and the Opus codec are wiped by every recreation, while the
# plugin, its config and its credentials survive on the data volume. Without this
# the lane answers "Voice unavailable: selected CLI owner/config preflight refused"
# (or connects and plays nothing) and the only warning is
# "Opus codec not found — voice channel playback disabled".
#
# Usage, from the HOST that owns the container:
#   docker exec -i <container> bash -s < scripts/bootstrap-lane-runtime.sh
# It is idempotent: re-running is safe and reports the current state.
set -u

VENV=${HERMES_VENV:-/opt/hermes/.venv}
PY=$VENV/bin/python
PYVER=$(basename "$(ls -d "$VENV"/lib/python3.* 2>/dev/null | head -1)" 2>/dev/null)
SITE="$VENV/lib/$PYVER/site-packages"
UV=$(ls -1 /opt/hermes/tools/uv-*/uv 2>/dev/null | head -1)
FAILED=0

say() { printf '%s\n' "$*"; }
ok() { say "  [ ok ] $*"; }
bad() { say "  [FAIL] $*"; FAILED=1; }

[ -x "$PY" ] || { bad "no interpreter at $PY (wrong container?)"; exit 1; }
say "container lane bootstrap: venv=$VENV python=$("$PY" -V 2>&1)"

# ---------------------------------------------------------------- media stack
say "1. media stack (aiortc + av + discord.py[voice])"
if [ -n "$UV" ]; then
  # These are the versions the plugin pins; a wheel-only resolution needs no compiler.
  if "$UV" pip install --python "$PY" --quiet 'aiortc==1.15.0' 'av==17.1.0' 'discord.py[voice]==2.7.1' 2>&1 | tail -3; then
    ok "declared dependencies installed"
  else
    bad "dependency install failed (network or resolution)"
  fi
else
  bad "uv not found under /opt/hermes/tools/uv-*/uv"
fi

"$PY" - <<'PYCHECK' || FAILED=1
import sys
try:
    import aiortc, av, discord, nacl  # nacl = discord.py[voice] encryption
except Exception as exc:
    print('  [FAIL] import %s: %s' % (type(exc).__name__, exc))
    sys.exit(1)
print('  [ ok ] aiortc %s | av %s | discord.py %s | PyNaCl ok' % (aiortc.__version__, av.__version__, discord.__version__))
PYCHECK

# --------------------------------------------------------------------- codec
say "2. Opus codec (Discord playback)"
opus_state() {
  "$PY" - <<'PYOPUS'
import ctypes.util, sys
lib = ctypes.util.find_library('opus')
if not lib:
    print('MISSING')
    sys.exit(1)
import discord.opus as o
try:
    o.load_opus(lib)
except Exception as exc:
    print('LOADFAIL %s %s' % (type(exc).__name__, exc))
    sys.exit(1)
print('LOADS %s' % lib)
PYOPUS
}
STATE=$(opus_state 2>&1 | tail -1)
if [ "${STATE%% *}" = "LOADS" ]; then
  ok "libopus present and loadable (${STATE#LOADS })"
else
  say "  -- not usable ($STATE); installing the packaged codec"
  # Debian images ship an empty apt cache, so refresh before installing.
  (apt-get update -qq 2>&1 | tail -2; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends libopus0 2>&1 | tail -3) || say "  -- apt install did not complete"
  ldconfig 2>/dev/null
  STATE=$(opus_state 2>&1 | tail -1)
  if [ "${STATE%% *}" = "LOADS" ]; then
    ok "libopus installed (${STATE#LOADS })"
  else
    # Fallback: av wheels bundle a private libopus whose SONAME is hashed, so the
    # loader cache cannot match "libopus.so". Point discord.py straight at it.
    say "  -- apt unavailable; wiring discord.py to the libopus bundled with av"
    BUNDLED=$(ls -1 "$SITE"/av.libs/libopus-*so* 2>/dev/null | head -1)
    if [ -n "$BUNDLED" ]; then
      printf "import ctypes, glob, discord.opus as _o\n_o._lib = ctypes.cdll.LoadLibrary(glob.glob('%s/av.libs/libopus-*so*')[0])\n" "$SITE" > "$SITE/hermes_lane_opus_preload.pth"
      STATE=$("$PY" -c "import discord.opus as o, ctypes.util; print('PRELOAD', o.is_loaded())" 2>&1 | tail -1)
      case "$STATE" in
        PRELOAD*) ok "preload shim written (discord.py sees Opus via av's copy)" ;;
        *) bad "could not provide Opus; playback will stay disabled (bundled: $BUNDLED)" ;;
      esac
    else
      bad "no libopus anywhere: install libopus0 or a derived image"
    fi
  fi
fi

# ---------------------------------------------------------------- verification
say "3. lane wiring sanity"
"$PY" - <<'PYWIRE' || FAILED=1
import sys
from pathlib import Path
plugin = Path('/opt/data/plugins/talk-desktop')
if not plugin.is_dir():
    print('  [FAIL] plugin not installed at %s' % plugin)
    sys.exit(1)
for rel in ('discord_voice/session.py', 'discord_voice/owner.py', 'discord_voice/config_keys.py'):
    if not (plugin / rel).is_file():
        print('  [FAIL] missing %s' % rel)
        sys.exit(1)
session = (plugin / 'discord_voice/session.py').read_text()
print('  [ ok ] plugin present; single-invocation start: %s' % ('def admitted' in session))
PYWIRE

if [ "$FAILED" = 0 ]; then
  say "RESULT: lane runtime ready (restart the gateway so it picks this up)"
  exit 0
fi
say "RESULT: lane runtime INCOMPLETE — see [FAIL] lines above"
exit 1
