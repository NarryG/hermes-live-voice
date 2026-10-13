#!/usr/bin/env bash
# Publish a pinned Live Voice revision into a Hermes container, so that the code
# that actually RUNS is the revision you tested.
#   docker exec -i <container> bash -s < scripts/lane-update.sh <40-hex-sha> [repo-url]
#
# Why this and not a file copy: the process imports the plugin from
#   <HERMES_HOME>/installs/<install>/environments/<generation>/workspace/plugin-sources/<key>/
# a copy keyed by the plugin PATH (not its content). Replacing the plugin
# directory alone changes nothing at runtime — every existing generation keeps
# serving the old bytes. This script therefore refreshes those copies too, and
# deliberately leaves each generation's pyproject.toml alone (the workspace owns
# its dependency declaration).
#
# The script then prints the host-side restart commands; it cannot run them
# (there is no docker inside the container).
set -u
SHA=${1:-}
REPO=${2:-https://github.com/NarryG/hermes-live-voice}
PLUGIN=${PLUGIN_DIR:-/opt/data/plugins/talk-desktop}
HOME_DIR=${HERMES_HOME:-/opt/data}
HERMES=${HERMES_BIN:-/opt/hermes/bin/hermes}
STAGE_HOME=${STAGE_HOME:-/opt/data/cache/scratch/lane-update-stage}
UV=$(ls -1 /opt/hermes/tools/uv-*/uv 2>/dev/null | head -1)
TS=$(date -u +%Y%m%dT%H%M%SZ)
FAILED=0

say() { printf '%s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAILED=1; }

[ -n "$SHA" ] || { echo "usage: lane-update.sh <40-hex-sha> [repo-url]"; exit 2; }
case "$SHA" in *[!0-9a-f]*) echo "not a lowercase 40-hex sha: $SHA"; exit 2 ;; esac
[ "${#SHA}" -eq 40 ] || { echo "not a 40-hex sha (${#SHA} chars): $SHA"; exit 2; }

say "1. stage $SHA through the installer (isolated home, no enable, no deps)"
rm -rf "$STAGE_HOME"
HOME=$HOME_DIR HERMES_HOME=$STAGE_HOME "$HERMES" plugins install "$REPO" --ref "$SHA" --no-enable --no-deps 2>&1 | tail -3
STAGE=$STAGE_HOME/plugins/talk-desktop
[ -d "$STAGE" ] || { bad "staged tree missing at $STAGE"; exit 1; }
say "  staged: $(ls -1 "$STAGE" | wc -l) entries"

say "2. run the staged revision's own test suite"
if [ -n "$UV" ]; then
  ( cd "$STAGE" && timeout 600 "$UV" run --no-project --with pytest --with 'discord.py==2.7.1' \
      --with aiortc --with av --with aiohttp --with fastapi --with pyyaml \
      python -m pytest -q -p no:warnings --tb=line tests 2>&1 | tail -3 ) || bad "staged suite failed"
else
  bad "uv not found; skipped the suite"
fi

say "3. publish into $PLUGIN (with backup)"
mkdir -p /opt/data/backups/live-voice
# Move stale copies OUT of the plugins directory: every superseded dir still declares
# the same plugin id, and the loader then has four candidates — it can (and did)
# import an old one, so deploys looked like no-ops.
for stale in "$PLUGIN".superseded-*; do
  [ -d "$stale" ] || continue
  mv "$stale" "/opt/data/backups/live-voice/plugin-$(basename "$stale")"
  say "  moved stale $(basename "$stale") out of the plugins dir"
done
cp -a "$PLUGIN" "/opt/data/backups/live-voice/plugin-$TS-pre-${SHA:0:7}"
cp -a "$STAGE" "$PLUGIN.new"
mv "$PLUGIN" "/opt/data/backups/live-voice/plugin-$TS-superseded"
mv "$PLUGIN.new" "$PLUGIN"
chown -R 10000:10000 "$PLUGIN"
find "$PLUGIN" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
say "  backup: /opt/data/backups/live-voice/plugin-$TS-pre-${SHA:0:7}"
say "  published revision: $(cat "$PLUGIN/.hermes-catalog.json" 2>/dev/null | tr -d '\n' | cut -c1-120)"

say "4. refresh every generation's plugin-source copy (this is what actually runs)"
REFRESHED=0
for M in /opt/data/installs/*/environments/*/workspace/plugin-sources/talk-desktop-*/; do
  [ -d "$M" ] || continue
  for item in "$PLUGIN"/*; do
    base=$(basename "$item")
    case "$base" in pyproject.toml|uv.lock) continue ;; esac   # workspace owns these
    cp -a "$item" "$M/"
  done
  # An overlay copies but never deletes, so a module dropped from the revision
  # would linger in the copy that runs. Prune anything the plugin no longer ships.
  python3 -c "
import os, shutil, sys
plugin, member = sys.argv[1], sys.argv[2]
keep = {'pyproject.toml', 'uv.lock', '.git', 'venv', '.venv', '__pycache__'}
for root, dirs, files in os.walk(member):
    rel = os.path.relpath(root, member)
    rel = '' if rel == '.' else rel
    for name in list(dirs) + list(files):
        if name in keep:
            continue
        r = os.path.join(rel, name) if rel else name
        if not os.path.exists(os.path.join(plugin, r)):
            target = os.path.join(member, r)
            print('   prune', r)
            shutil.rmtree(target, ignore_errors=True) if os.path.isdir(target) else os.remove(target)
" "$PLUGIN" "$M"
  REFRESHED=$((REFRESHED+1))
done
chown -R 10000:10000 /opt/data/installs/*/environments/*/workspace/plugin-sources/talk-desktop-*/ 2>/dev/null
find /opt/data/installs -path '*plugin-sources/talk-desktop-*' -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
say "  refreshed ${REFRESHED} generation copy(ies)"

say "5. verify staged == published == running copies"
if diff -r --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache "$STAGE" "$PLUGIN" >/dev/null 2>&1; then
  say "  [ ok ] published tree matches the tested revision"
else
  bad "published tree differs from the staged revision"
fi

cat <<'EOS'

NEXT (run these on the HOST, where docker lives):
  docker restart --timeout 45 <container>
  systemctl restart hermes-desktop-backend
  docker exec -i <container> bash -s < scripts/lane-status.sh     # expect STALE-CODE count 0
Then invoke /livevoice (operation: status, then start) from the private voice
channel — an actual invocation is the only proof the lane answers.
EOS
[ "$FAILED" = 0 ] || exit 1
exit 0
