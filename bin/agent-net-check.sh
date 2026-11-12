#!/bin/sh
# Can hydroxide still reach Proton through the proxy? If not, while the server itself can, Podman's
# network is stale (usually after a reboot or resume): stop every container so Podman rebuilds it,
# start them again, and push the result. Run by agent-net-check.timer; safe to run by hand.
set -u
LAST=${XDG_RUNTIME_DIR:-/tmp}/agent-net-check.last   # cleared at every boot
MIN_GAP=7200                                         # at most one repair every 2 hours
SERVICES="egress-proxy hydroxide ollama ntfy caddy.socket caddy"
TIMERS="email-triage.timer email-digest.timer morning-brief.timer"

via_proxy() {   # the same path hydroxide uses: agent network -> Squid -> Proton
  timeout 60 podman run --rm --network systemd-agent localhost/agent-jobs:latest -c \
    "import urllib.request as u; u.build_opener(u.ProxyHandler({'https': 'http://egress-proxy:3128'})).open('https://mail.proton.me', timeout=20)" \
    >/dev/null 2>&1
}

push() {   # title, message; the token goes in on stdin so it doesn't show in `ps`
  tok=$(podman secret inspect --showsecret --format '{{.SecretData}}' ntfy-token 2>/dev/null | tr -d '\n')
  printf 'Authorization: Bearer %s\n' "$tok" | curl -fsS -m 10 -H @- -H "Title: $1" -H "Tags: wrench" \
    -d "$2" http://127.0.0.1:8090/email-agent >/dev/null || echo "push failed"
}

if via_proxy; then echo "proxy reaches Proton: ok"; exit 0; fi
if ! curl -fsS -o /dev/null -m 15 https://mail.proton.me; then
  echo "the server can't reach Proton either (Wi-Fi, router or Proton): nothing to repair here"
  exit 0
fi
if systemctl --user -q is-active email-triage email-digest morning-brief; then
  echo "a job is running: trying again on the next check"
  exit 0
fi
now=$(date +%s)
if [ -f "$LAST" ] && [ $((now - $(cat "$LAST"))) -lt $MIN_GAP ]; then
  echo "proxy can't reach Proton, but a repair already ran less than 2 hours ago: see MAINTENANCE.md"
  exit 1
fi
echo "$now" > "$LAST"

echo "proxy can't reach Proton but the server can: restarting all containers"
systemctl --user stop $TIMERS
# word splitting on purpose: one unit per word
systemctl --user stop $SERVICES
i=0
while [ -n "$(podman ps -q)" ] && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done
[ -n "$(podman ps -q)" ] && echo "still running, so the network may not be rebuilt: $(podman ps --format '{{.Names}}' | tr '\n' ' ')"
systemctl --user start $SERVICES
systemctl --user start $TIMERS
sleep 20

if via_proxy; then
  echo "repaired"
  push "Agent network repaired" "The proxy couldn't reach Proton, so all containers were restarted. Triage works again from the next run."
else
  echo "still failing after the restart"
  push "Agent network: repair didn't help" "The proxy still can't reach Proton after restarting all containers. See MAINTENANCE.md, section 4.1."
  exit 1
fi
