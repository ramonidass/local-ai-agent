#!/bin/sh
# Did news-picks run, is it on its own network, and can it reach Ollama but not the internet or the
# mail bridge? One line per check: PASS or FAIL (SKIP when something needed isn't running). Exits 1
# if any check fails. Waits first if a run is in progress.   Usage: sh ~/agent/bin/check-picks.sh
set -u
WEB=$HOME/agent/data/web
Q=$HOME/.config/containers/systemd
NET=systemd-picks
IMAGE=localhost/agent-jobs:latest
checks=0
fails=0
pass() { checks=$((checks + 1)); echo "PASS  $1"; }
fail() { checks=$((checks + 1)); fails=$((fails + 1)); echo "FAIL  $1"; }
skip() { echo "SKIP  $1"; }

i=0
while [ "$(systemctl --user show -p ActiveState --value news-picks 2>/dev/null)" = activating ] && [ $i -lt 120 ]; do
  if [ $i -eq 0 ]; then echo "news-picks is running; waiting for it to finish..."; fi
  sleep 5
  i=$((i + 1))
done

# A. it ran, and what it decided
props=$(systemctl --user show news-picks -p LoadState -p Result -p ExecMainStatus -p ExecMainExitTimestamp 2>/dev/null)
prop() { printf '%s\n' "$props" | sed -n "s/^$1=//p"; }
if [ "$(prop LoadState)" != loaded ]; then
  fail "A1 news-picks unit: not found (copied the units? systemctl --user daemon-reload)"
elif [ -z "$(prop ExecMainExitTimestamp)" ]; then
  fail "A1 news-picks has not run yet (systemctl --user start news-build)"
elif [ "$(prop Result)" = success ] && [ "$(prop ExecMainStatus)" = 0 ]; then
  pass "A1 news-picks last run succeeded ($(prop ExecMainExitTimestamp))"
else
  fail "A1 news-picks last run: Result=$(prop Result), exit $(prop ExecMainStatus) (journalctl --user -u news-picks)"
fi
line=$(journalctl --user -u news-picks -n 50 -o cat --no-pager 2>/dev/null | grep '^news picks:' | tail -n 1)
if [ -n "$line" ]; then pass "A2 the journal says why: ${line#news picks: }"; else fail "A2 no 'news picks:' line in its journal"; fi
# (a function: bash misreads a here-document inside $(...) when it has an apostrophe)
picks_file() {
python3 - "$WEB" <<'PY'
import json, sys
web = sys.argv[1]
try:
    p = json.load(open(web + "/picks/news-picks.json"))
    n = json.load(open(web + "/news.json"))
except (OSError, ValueError) as e:
    print(f"can't read it: {e}")
    sys.exit(1)
picks = p.get("picks") if isinstance(p, dict) else None
if p.get("source") not in ("model", "fallback") or not isinstance(picks, list) or not 1 <= len(picks) <= 7:
    print("not a picks file")
    sys.exit(1)
if p.get("list_generated") != n.get("generated"):
    print(f"made for the list of {p.get('list_generated')}, not the current one of {n.get('generated')}")
    sys.exit(1)
what = "by the model" if p["source"] == "model" else f"the fallback ({p.get('reason')})"
print(f"{len(picks)} picks, {what}, for the current list")
PY
}
if msg=$(picks_file); then pass "A3 picks/news-picks.json: $msg"; else fail "A3 picks/news-picks.json: $msg"; fi
if curl -fsS -m 10 http://127.0.0.1:8088/picks/news-picks.json 2>/dev/null | cmp -s - "$WEB/picks/news-picks.json"; then
  pass "A4 the page serves it (http://127.0.0.1:8088/picks/news-picks.json)"
else
  fail "A4 the page doesn't serve it (Caddyfile has /picks/*? systemctl --user restart caddy)"
fi

# B. its network
nets=$(systemctl --user cat news-picks 2>/dev/null | grep -o -- '--network[ =][^ ]*' | sed 's/--network[ =]//' | sort -u | tr '\n' ' ')
if [ "$nets" = "$NET " ]; then pass "B1 news-picks runs on $NET only"; else fail "B1 news-picks networks: '${nets% }', expected $NET only"; fi
if [ "$(podman network inspect $NET --format '{{.Internal}}' 2>/dev/null)" = true ]; then
  pass "B2 $NET is internal (no route out)"
else
  fail "B2 $NET: missing or not internal"
fi
on=$(podman ps --filter network=$NET --format '{{.Names}}' 2>/dev/null | sort | tr '\n' ' ')
others=$(echo "$on" | tr ' ' '\n' | grep -v -x -e ollama -e news-picks -e '' | tr '\n' ' ')
if echo " $on" | grep -q ' ollama ' && [ -z "$others" ]; then
  pass "B3 on $NET now: ${on% }"
else
  fail "B3 on $NET now: '${on% }' (want ollama, and news-picks while it runs)"
fi
units=$(grep -l -x 'Network=picks.network' "$Q"/*.container 2>/dev/null | xargs -n 1 basename 2>/dev/null | sort | tr '\n' ' ')
if [ "$units" = "news-picks.container ollama.container " ]; then
  pass "B4 only news-picks and ollama units use picks.network"
else
  fail "B4 units on picks.network: '${units% }'"
fi

# C. what it can reach: a throwaway container with the job's image, on its network
probe() {   # host port: prints REACHED, or BLOCKED and why; nothing if the test container didn't run
  podman run --rm --network "$NET" "$IMAGE" -c "
import socket
try:
    socket.create_connection(('$1', $2), 5).close()
    print('REACHED')
except OSError as e:
    print('BLOCKED', type(e).__name__, e)" 2>/dev/null | tail -n 1
}
blocked() {  # label host port
  r=$(probe "$2" "$3")
  case "$r" in
    BLOCKED*) pass "$1 (${r#BLOCKED })" ;;
    REACHED) fail "$1: it connected" ;;
    *) fail "$1: the test container didn't run (podman run --network $NET $IMAGE)" ;;
  esac
}
r=$(probe ollama 11434)
if [ -z "$r" ]; then r="the test container did not run"; fi
if [ "$r" = REACHED ]; then pass "C1 reaches Ollama (ollama:11434)"; else fail "C1 can't reach Ollama: $r"; fi
blocked "C2 no internet by name (example.com:443)" example.com 443
blocked "C3 no internet by address (1.1.1.1:443)" 1.1.1.1 443
for hp in protonmail-bridge:1144 egress-proxy:3128 ntfy:8080 caddy:8088; do
  blocked "C4 not ${hp%:*} by name" "${hp%:*}" "${hp#*:}"
done
for hp in protonmail-bridge:1144 egress-proxy:3128; do
  ip=$(podman inspect "${hp%:*}" --format '{{(index .NetworkSettings.Networks "systemd-agent").IPAddress}}' 2>/dev/null)
  if [ -n "$ip" ]; then
    blocked "C5 not ${hp%:*} by address ($ip:${hp#*:})" "$ip" "${hp#*:}"
  else
    skip "C5 ${hp%:*} by address: not running on systemd-agent"
  fi
done

echo
if [ $fails -eq 0 ]; then echo "All $checks checks passed."; exit 0; fi
echo "$fails of $checks checks failed."
exit 1
