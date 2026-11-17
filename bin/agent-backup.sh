#!/bin/sh
# Saves what an update of the agent changes, so agent-restore.sh can put it back exactly: the job code
# and image recipe, the page, Caddy's config, these scripts, the feeds and Squid config, all Quadlet
# units, and the job image itself (as a tag: restoring it needs no rebuild). Not the data (agent.db,
# mail, fetched feeds, the page's data): a restore never rolls those back.
# Run it before copying in a new version:   sh ~/agent-setup/bin/agent-backup.sh [folder]
# (default folder: ~/agent-backup/<date-time>; it must not exist yet)
set -eu
STAMP=$(date +%Y%m%d-%H%M%S)
B=${1:-$HOME/agent-backup/$STAMP}
Q=$HOME/.config/containers/systemd
IMAGE=localhost/agent-jobs
if [ -e "$B" ]; then echo "$B already exists: give another folder" >&2; exit 1; fi
mkdir -p "$B/agent"
: > "$B/dirs"
for d in agent-jobs site caddy bin state/feeds egress-proxy; do
  [ -d "$HOME/agent/$d" ] || continue
  mkdir -p "$B/agent/$(dirname "$d")"
  cp -a "$HOME/agent/$d" "$B/agent/$d"
  echo "$d" >> "$B/dirs"
done
cp -a "$Q" "$B/quadlets"
# created by the news-picks update; the restore removes it if it wasn't there before
if [ -d "$HOME/agent/data/web/picks" ]; then touch "$B/had-picks-data"; fi
if podman image exists "$IMAGE:latest"; then
  podman tag "$IMAGE:latest" "$IMAGE:backup-$STAMP"
  echo "$IMAGE:backup-$STAMP" > "$B/image"
fi
echo "$STAMP" > "$B/DONE"                 # written last: a folder without it is not a whole backup
echo "Backed up to $B:"
sed 's|^|  ~/agent/|' "$B/dirs"
echo "  $Q"
if [ -f "$B/image" ]; then echo "  the job image, as $(cat "$B/image")"; fi
echo "To go back: sh ~/agent-setup/bin/agent-restore.sh $B"
