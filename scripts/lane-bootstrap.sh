#!/usr/bin/env bash
# Live Voice lane: runtime prerequisites inside a Hermes container.
# Run after EVERY container recreate or Hermes image update.
#   docker exec -i <container> bash -s < scripts/lane-bootstrap.sh
# Idempotent. Exits non-zero when the lane could not be made ready.
#
# Why: the gateway venv and the system libraries live in the image layer, so a
# recreate removes the media stack, the Opus codec and (if outdated) davey and
# edge-tts, while the plugin itself survives on the data volume.
set -u

PLUGIN=${PLUGIN_DIR:-/opt/data/plugins/talk-desktop}
CORE_VENV=${HERMES_VENV:-/opt/hermes/.venv}
UV=$(ls -1 /opt/hermes/tools/uv-*/uv 2>/dev/null | head -1)
FAILED=0

say() { printf '%s\n' "$*"; }
ok()  { printf '  [ ok ] %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAILED=1; }

# The plugin imports from the SELECTED generation's venv, not from the image venv.
FACTS=$(ls -1 /opt/data/installs/*/facts.json 2>/dev/null | head -1)
GEN_VENV=""
if [ -n "$FACTS" ]; then
  GEN_VENV=$(sed -n 's/.*"environment": "\([^"]*\)".*/\1/p' "$FACTS" | head -1)
  [ -d "$GEN_VENV" ] || GEN_VENV=""
fi
PY_BIN="${GEN_VENV:-$CORE_VENV}/bin/python"

say "lane bootstrap"
say "  plugin     : $PLUGIN"
say "  core venv  : $CORE_VENV"
say "  generation : ${GEN_VENV:-(none found; core venv only)}"
[ -n "$UV" ] || bad "uv not found under /opt/hermes/tools/uv-*/uv"
[ -x "$PY_BIN" ] || bad "no interpreter at $PY_BIN"

# --------------------------------------------------------------- python deps
say "1. dependencies (media stack, DAVE/E2EE, TTS)"
# davey >=0.1.7: Discord DAVE (E2EE) voice is mandatory; older davey drops
#                incoming audio with NoDecryptorForUser (silent call).
# edge-tts >=7.2.8: older builds get HTTP 403 from Microsoft's endpoint, which
#                is what breaks the core's normal voice replies.
REQS=( 'aiortc==1.15.0' 'av==17.1.0' 'discord.py[voice]==2.7.1' 'davey>=0.1.7' 'edge-tts>=7.2.8' )
for target in "$GEN_VENV" "$CORE_VENV"; do
  [ -n "$target" ] && [ -x "$target/bin/python" ] || continue
  if [ -n "$UV" ] && "$UV" pip install --python "$target/bin/python" --quiet "${REQS[@]}" >/dev/null 2>&1; then
    ok "installed into $target"
  else
    bad "install failed for $target"
  fi
done

VERIFY=$("$PY_BIN" -c "
import importlib.metadata as m, importlib.util as u
want = {'davey': (0,1,7), 'edge-tts': (7,2,8), 'discord.py': (2,7,1)}
bad = []
for name, minimum in want.items():
    try: got = m.version(name)
    except Exception: bad.append(name + ' missing'); continue
    if tuple(int(p) for p in got.split('+')[0].split('.')[:3]) < minimum: bad.append('%s %s < %s' % (name, got, '.'.join(map(str, minimum))))
for mod in ('aiortc','av','discord','nacl'):
    if not u.find_spec(mod): bad.append(mod + ' missing')
print('BAD ' + '; '.join(bad) if bad else 'OK davey %s | edge-tts %s | discord.py %s' % (m.version('davey'), m.version('edge-tts'), m.version('discord.py')))
" 2>&1 | tail -1)
case "$VERIFY" in
  OK*) ok "deps: ${VERIFY#OK }" ;;
  *)   bad "deps: ${VERIFY#BAD }" ;;
esac

# --------------------------------------------------------------------- codec
say "2. Opus codec (Discord playback) and ffmpeg"
opus_line() { "$1" -c "
import ctypes.util, discord.opus as o
lib = ctypes.util.find_library('opus')
if not lib: print('MISSING'); raise SystemExit
try: o.load_opus(lib)
except Exception as exc: print('LOADFAIL ' + str(exc)); raise SystemExit
print('LOADS ' + lib + (' (encoder ok)' if o.is_loaded() else ''))
" 2>&1 | tail -1; }
STATE=$(opus_line "$PY_BIN")
case "${STATE%% *}" in
  LOADS*) ok "libopus ${STATE#LOADS }" ;;
  *)
    say "  -- not usable ($STATE); installing the packaged codec"
    # The image's apt cache starts empty, so refresh before installing.
    (apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends libopus0) >/dev/null 2>&1
    ldconfig 2>/dev/null
    STATE=$(opus_line "$PY_BIN")
    if [ "${STATE%% *}" = "LOADS" ]; then
      ok "libopus installed (${STATE#LOADS })"
    else
      # av bundles a private libopus whose SONAME is the hashed filename, so a
      # symlink alone never satisfies find_library('opus'): point discord.py at it.
      SITE=$("$PY_BIN" -c "import av, os; print(os.path.dirname(os.path.dirname(av.__file__)))" 2>/dev/null)
      BUNDLED=$(ls -1 "$SITE"/av.libs/libopus-*so* 2>/dev/null | head -1)
      if [ -n "$BUNDLED" ]; then
        printf "import ctypes, glob, discord.opus as _o\n_o._lib = ctypes.cdll.LoadLibrary(glob.glob('%s/av.libs/libopus-*so*')[0])\n" "$SITE" > "$SITE/hermes_lane_opus_preload.pth"
        ok "preload shim written for av's bundled libopus"
      else
        bad "no libopus anywhere: install libopus0 or bake it into the image"
      fi
    fi
    ;;
esac
if command -v ffmpeg >/dev/null 2>&1; then ok "ffmpeg present"; else bad "ffmpeg missing (core voice playback needs it)"; fi

# ------------------------------------------------------------------- wiring
say "3. plugin present"
if [ -d "$PLUGIN" ]; then
  ok "plugin dir present ($(grep -c 'def admitted' "$PLUGIN/discord_voice/session.py" 2>/dev/null || echo 0) start-gate marker)"
else
  bad "plugin not installed at $PLUGIN"
fi

if [ "$FAILED" = 0 ]; then
  say "RESULT: ready — restart the container and hermes-desktop-backend so the codec and deps are picked up"
  exit 0
fi
say "RESULT: INCOMPLETE — see [FAIL] lines"
exit 1
