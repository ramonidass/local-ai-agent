#!/bin/sh
# Puts back what agent-backup.sh saved, then reloads systemd and restarts what the news-picks update
# touched: Ollama (its networks) and Caddy (its config); the proxy only if its config changed. The
# job image goes back to the saved tag (no rebuild). Then news-build remakes news.json with the
# restored code. Data (agent.db, mail, fetched feeds) is left as it is.
# Usage: sh ~/agent-setup/bin/agent-restore.sh [-y] [folder]
# (default folder: the newest whole backup in ~/agent-backup; -y: don't ask)
set -eu
# ~/agent/bin is replaced below: run from a copy if started from there
case "$0" in
  "$HOME/agent/bin/"*) t=$(mktemp); cp "$0" "$t"; exec sh "$t" "$@" ;;
esac
YES=no
if [ "${1:-}" = "-y" ]; then YES=yes; shift; fi
if [ $# -gt 0 ]; then
  B=${1%/}
else
  B=$(for d in "$HOME"/agent-backup/*/; do [ -f "$d/DONE" ] && echo "${d%/}"; done | sort | tail -n 1)
fi
if [ -z "${B:-}" ] || [ ! -f "$B/DONE" ]; then
  echo "no whole backup found (${B:-nothing in ~/agent-backup}): nothing changed" >&2
  exit 1
fi
Q=$HOME/.config/containers/systemd
echo "Restoring from $B:"
sed 's|^|  ~/agent/|' "$B/dirs"
echo "  $Q"
if [ -f "$B/image" ]; then echo "  the job image, from $(cat "$B/image")"; fi
if [ "$YES" != yes ]; then
  printf "Go ahead? [y/N] "
  read -r answer
  case "$answer" in y|Y) ;; *) echo "nothing changed"; exit 1 ;; esac
fi

# no fetch, build or picks run in the middle of it
timer=$(systemctl --user is-active news-fetch.timer 2>/dev/null || true)
systemctl --user stop news-fetch.timer 2>/dev/null || true
systemctl --user stop news-picks.service 2>/dev/null || true

proxy=same
if [ -d "$B/agent/egress-proxy" ] && ! diff -r "$B/agent/egress-proxy" "$HOME/agent/egress-proxy" >/dev/null 2>&1; then
  proxy=changed
fi
while read -r d; do
  rm -rf "${HOME:?}/agent/$d"
  cp -a "$B/agent/$d" "$HOME/agent/$d"
  echo "restored ~/agent/$d"
done < "$B/dirs"
rm -rf "$Q"
cp -a "$B/quadlets" "$Q"
echo "restored $Q"
if [ ! -f "$B/had-picks-data" ] && [ -d "$HOME/agent/data/web/picks" ]; then
  rm -rf "$HOME/agent/data/web/picks"
  echo "removed ~/agent/data/web/picks (not there before)"
fi
# from here on, keep going if one step fails, and say so at the end
set +e
failed=""
step() {   # what, command...: run it, remember it if it fails
  what=$1; shift
  if "$@"; then return 0; fi
  failed="$failed
  $what: $*"
  return 1
}
if [ -f "$B/image" ] && podman tag "$(cat "$B/image")" localhost/agent-jobs:latest; then
  echo "job image: back to $(cat "$B/image")"
else
  echo "no saved job image: rebuilding it from the restored Containerfile (a few minutes)"
  step "rebuild the job image" systemctl --user restart agent-jobs-build
fi

step "reload systemd" systemctl --user daemon-reload
while systemctl --user -q is-active email-triage; do echo "waiting for email-triage to finish..."; sleep 10; done
step "restart Ollama and Caddy" systemctl --user restart ollama caddy && echo "restarted ollama and caddy"
if [ "$proxy" = changed ]; then step "restart the proxy" systemctl --user restart egress-proxy && echo "restarted egress-proxy"; fi
if [ ! -f "$Q/picks.network" ] && podman network exists systemd-picks; then
  step "remove the systemd-picks network" podman network rm systemd-picks >/dev/null && echo "removed the systemd-picks network"
fi
step "rebuild news.json (journalctl --user -u news-build)" systemctl --user start news-build && echo "news.json rebuilt"
if [ "$timer" = active ]; then step "start news-fetch.timer" systemctl --user start news-fetch.timer; fi
if [ -n "$failed" ]; then
  echo "Files restored, but these steps failed; run them by hand:$failed"
  exit 1
fi
echo "Restored. Reload the page."
