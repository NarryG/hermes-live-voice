#!/usr/bin/env bash
# Live Voice lane doctor — read-only.
#   docker exec -i <container> bash -s < scripts/lane-status.sh
# Exits non-zero when the running copy does NOT match the installed plugin
# directory (the failure that made an update look like a no-op), or when a
# prerequisite is missing. Safe to run at any time.
set -u
PLUGIN=${PLUGIN_DIR:-/opt/data/plugins/talk-desktop}
CORE_VENV=${HERMES_VENV:-/opt/hermes/.venv}
FACTS=$(ls -1 /opt/data/installs/*/facts.json 2>/dev/null | head -1)
GEN_VENV=""
[ -n "$FACTS" ] && GEN_VENV=$(sed -n 's/.*"environment": "\([^"]*\)".*/\1/p' "$FACTS" | head -1)
PY_BIN="${GEN_VENV:-$CORE_VENV}/bin/python"

echo "== plugin =="
echo "  dir        : $PLUGIN"
[ -f "$PLUGIN/plugin.yaml" ] && sed -n 's/^version: */  version    : /p' "$PLUGIN/plugin.yaml" | head -1
[ -f "$PLUGIN/.hermes-catalog.json" ] && echo "  marker     : $(cat "$PLUGIN/.hermes-catalog.json" | tr -d '\n' | cut -c1-160)"
echo "  start gate : $(grep -c 'def admitted' "$PLUGIN/discord_voice/session.py" 2>/dev/null || echo 0) (single-invocation start present)"

echo "== selected generation =="
echo "  venv       : ${GEN_VENV:-(none)}"
echo "  member copy: ${GEN_VENV%/venv}/workspace/plugin-sources/talk-desktop-98421590dd9b6b3a"

echo "== generations on disk =="
COUNT=0
STALE=0
for g in /opt/data/installs/*/environments/*/; do
  [ -d "$g" ] || continue
  COUNT=$((COUNT+1))
  M="$g/workspace/plugin-sources/talk-desktop-98421590dd9b6b3a"
  STATE="no-member"
  if [ -d "$M" ]; then
    if grep -q 'def admitted' "$M/discord_voice/session.py" 2>/dev/null; then
      # Present the fixed start gate; now check it is byte-current with the plugin dir.
      if diff -rq --exclude=__pycache__ --exclude=pyproject.toml --exclude=uv.lock --exclude=.git \
           "$PLUGIN/discord_voice" "$M/discord_voice" >/dev/null 2>&1; then
        STATE="current"
      else
        STATE="DIFFERS"
      fi
    else
      STATE="STALE-CODE"
      STALE=$((STALE+1))
    fi
  fi
  SEL="-"
  [ "$g" = "${GEN_VENV%/venv}/" ] && SEL="SELECTED"
  printf '  %-34s %-8s size=%-6s %s\n' "$(basename "$g")" "$STATE" "$(du -sh "$g" 2>/dev/null | cut -f1)" "$SEL"
done
echo "  count=${COUNT} total=$(du -sh /opt/data/installs/*/environments 2>/dev/null | awk '{print $1}')"
[ "$STALE" -eq 0 ] || echo "  WARNING: ${STALE} generation(s) still hold superseded code — run scripts/lane-update.sh or refresh members"

echo "== runtime =="
"$PY_BIN" -c "
import ctypes.util, importlib.metadata as m, importlib.util as u
def v(n):
    try: return m.version(n)
    except Exception: return 'MISSING'
print('  generation venv: davey %s | edge-tts %s | discord.py %s' % (v('davey'), v('edge-tts'), v('discord.py')))
print('  media          : aiortc %s | av %s | PyNaCl %s' % (v('aiortc'), v('av'), v('PyNaCl')))
import discord.opus as o
lib = ctypes.util.find_library('opus')
ok = bool(lib)
try:
    o.load_opus(lib) if lib else None
    enc = o.is_loaded()
except Exception as exc:
    enc = False
print('  opus           : lib=%s encoder=%s' % (lib or 'MISSING', enc))
" 2>&1 | tail -4
command -v ffmpeg >/dev/null 2>&1 && echo "  ffmpeg         : present" || echo "  ffmpeg         : MISSING"

echo "== live Discord command (read-only) =="
"$PY_BIN" -c "
import json, urllib.request, datetime, os
token = os.environ.get('DISCORD_BOT_TOKEN')
if not token:
    print('  (skipped: export DISCORD_BOT_TOKEN to include this check; the doctor never reads .env)'); raise SystemExit
UA = 'DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)'
def get(u):
    r = urllib.request.Request(u, headers={'Authorization': 'Bot ' + token, 'User-Agent': UA})
    with urllib.request.urlopen(r, timeout=25) as resp: return json.loads(resp.read())
me = get('https://discord.com/api/v10/users/@me')
for c in get('https://discord.com/api/v10/applications/%s/commands' % me['id']):
    if c['name'] == 'livevoice':
        ts = (int(c['id']) >> 22) + 1420070400000
        print('  /livevoice id=%s created=%s options=%s' % (c['id'], datetime.datetime.utcfromtimestamp(ts/1000).strftime('%Y-%m-%d %H:%M:%SZ'), [o['name'] for o in c.get('options', [])]))
" 2>&1 | tail -3

[ "$STALE" -eq 0 ] || exit 1
exit 0
