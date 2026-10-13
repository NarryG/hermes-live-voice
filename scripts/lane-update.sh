#!/usr/bin/env bash
# Publish a pinned Live Voice revision through the SUPPORTED plugin path.
#   docker exec -i <container> bash -s < scripts/lane-update.sh <40-hex-sha> [repo-url]
#
# Why not copy files: the installer's publish transaction ALSO runs the dependency
# sync (plugins_transaction.publish_plugin -> pm.client.sync_venv), which creates the
# dependency generation and the source copy the process imports. Hand-copying into the
# plugin directory skips that, and leaving the previous directory behind as
# "<id>.superseded-*" gives the loader several directories that all declare the same
# plugin id — it can then import an OLD one and a correct deploy looks like a no-op.
#
# The script therefore: guards against duplicate same-id directories, backs up the
# live directory OUTSIDE the plugins directory, installs in place, and verifies.
set -u
SHA=${1:-}
REPO=${2:-https://github.com/NarryG/hermes-live-voice}
PLUGIN=${PLUGIN_DIR:-/opt/data/plugins/talk-desktop}
PLUGIN_DIR_ROOT=$(dirname "$PLUGIN")
ID=$(basename "$PLUGIN")
LIVE_HOME=${HERMES_HOME:-/opt/data}
HERMES=${HERMES_BIN:-/opt/hermes/bin/hermes}
BACKUPS=${BACKUPS_DIR:-/opt/data/backups/live-voice}
TS=$(date -u +%Y%m%dT%H%M%SZ)
FAILED=0

say() { printf '%s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAILED=1; }

[ -n "$SHA" ] || { echo "usage: lane-update.sh <40-hex-sha> [repo-url]"; exit 2; }
case "$SHA" in *[!0-9a-f]*) echo "not a lowercase 40-hex sha: $SHA"; exit 2 ;; esac
[ "${#SHA}" -eq 40 ] || { echo "not a 40-hex sha (${#SHA} chars): $SHA"; exit 2; }

say "0. no duplicate plugin directories (the loader must have exactly one candidate)"
DUPLICATES=$(ls -1d "$PLUGIN_DIR_ROOT/$ID"* 2>/dev/null | grep -v "^$PLUGIN$" || true)
if [ -n "$DUPLICATES" ]; then
  mkdir -p "$BACKUPS"
  say "$DUPLICATES" | while read -r stale; do
    [ -n "$stale" ] || continue
    mv "$stale" "$BACKUPS/plugin-$(basename "$stale")-$(date -u +%Y%m%dT%H%M%SZ)"
    say "  moved duplicate out of the plugins dir: $stale"
  done
fi

say "1. back up the live directory outside the plugins dir"
mkdir -p "$BACKUPS"
[ -d "$PLUGIN" ] && cp -a "$PLUGIN" "$BACKUPS/plugin-$TS-pre-${SHA:0:7}" && say "  $BACKUPS/plugin-$TS-pre-${SHA:0:7}"

say "2. install the pinned revision in place (publish + dependency sync)"
# Deliberately no --no-deps (the sync is the point) and no --no-enable (keep it live).
HOME=$LIVE_HOME HERMES_HOME=$LIVE_HOME "$HERMES" plugins install "$REPO" --ref "$SHA" 2>&1 | tail -6

say "3. verify the published tree"
if [ -f "$PLUGIN/.hermes-catalog.json" ]; then
  say "  marker: $(tr -d '\n' < "$PLUGIN/.hermes-catalog.json" | cut -c1-140)"
fi
if HOME=$LIVE_HOME HERMES_HOME=$LIVE_HOME "$HERMES" plugins list 2>/dev/null | grep -q "$ID"; then
  say "  listed by the CLI"
else
  bad "the CLI no longer lists $ID — check 'hermes plugins list' before restarting"
fi

cat <<EOS

NEXT (run on the HOST, where docker lives):
  docker restart --timeout 45 <container>
  systemctl restart hermes-desktop-backend
Then confirm which copy the boot loaded — it must be $PLUGIN:
  docker exec <container> sh -c 'grep "native lane module loaded from" /opt/data/logs/gateway.log | tail -1'
EOS
[ "$FAILED" = 0 ] || exit 1
exit 0
