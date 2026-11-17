# Setup

Top to bottom, on the machine that will run it. Each step ends with a check; don't move on until
it passes. The examples use `192.168.8.20` for the server's LAN address and `10.0.0.0/24` for the
WireGuard subnet; replace them with yours.

Clone the repo to `~/agent-setup`. The running copy lives in `~/agent`.

## 0. Before you start

- Podman 5.2 or newer: `podman --version`.
- Full-disk encryption on (`lsblk -f` shows `crypto_LUKS`): the database and the mail session live on this disk.
- Your phone can reach home over WireGuard.
- You know your Proton address and the folders Syncthing uses for your calendar and time-tracking backups.

## 1. Put the files in place

```bash
mkdir -p ~/agent/{agent-jobs,bridge,caddy,extra-calendars,state/email,egress-proxy,site}
mkdir -p ~/agent/data/{web,stats} ~/agent/hydroxide/{config,tls} ~/agent/ntfy/{etc,cache,lib}
mkdir -p ~/.config/containers/systemd ~/.config/systemd/user
loginctl enable-linger $USER

cp ~/agent-setup/agent-jobs/*       ~/agent/agent-jobs/
cp ~/agent-setup/egress-proxy/*     ~/agent/egress-proxy/
cp ~/agent-setup/hydroxide/*        ~/agent/hydroxide/
cp ~/agent-setup/bridge/*           ~/agent/bridge/
cp ~/agent-setup/state-email/*      ~/agent/state/email/
cp ~/agent-setup/ntfy/server.yml    ~/agent/ntfy/etc/server.yml
cp ~/agent-setup/caddy/Caddyfile    ~/agent/caddy/Caddyfile
cp ~/agent-setup/caddy/caddy.socket ~/.config/systemd/user/
cp ~/agent-setup/site/*             ~/agent/site/
cp ~/agent-setup/timers/*           ~/.config/systemd/user/
mkdir -p ~/agent/bin && cp ~/agent-setup/bin/* ~/agent/bin/
cp ~/agent-setup/quadlets/*         ~/.config/containers/systemd/
# Starting with hydroxide (free), so drop the official-Bridge units:
rm ~/.config/containers/systemd/protonmail-bridge.{container,build}
```

## 2. Personalise

```bash
PROTON=you@proton.me
SERVER=192.168.8.20                 # this machine's LAN address
ROUTER=192.168.8.1                  # your router: Squid asks it for DNS
CAL=Sync/calendar                   # Fossify Calendar backups (relative to home)
TIME=Sync/timetracker               # SimpleTimeTracker CSV exports
TASKS=Obsidian/Tasks                # folder with To-do.md

Q=~/.config/containers/systemd
sed -i "s|you@proton.me|$PROTON|" $Q/email-triage.container
sed -i "s|%h/Sync/calendar|%h/$CAL|; s|%h/Sync/timetracker|%h/$TIME|; s|%h/Obsidian/Tasks|%h/$TASKS|" $Q/morning-brief.container
sed -i "s|dns_nameservers .*|dns_nameservers $ROUTER|" ~/agent/egress-proxy/squid.conf
sed -i "s|K8-LAN-IP|$SERVER|" ~/agent/ntfy/etc/server.yml $Q/email-triage.container $Q/email-digest.container $Q/morning-brief.container
```

Uncomment the `AGENT_PUSH_LINK` lines in the three job units if you want a tap on a push to open
the page. Then edit `~/agent/state/email/rules.yaml`: people who always matter, your work domain,
shops you buy from.

- [ ] `grep -E "BRIDGE_USER|/calendar|/time|/tasks" $Q/*.container` shows your values.

## 3. Networks and the proxy

```bash
systemctl --user daemon-reload
systemctl --user start egress-proxy
```

- [ ] `podman network ls` shows `systemd-agent` and `systemd-egress`.

## 4. The mail bridge (hydroxide)

Create these labels in Proton first: `AI-Urgent`, `AI-Work`, `AI-Purchase`, `AI-Personal`,
`AI-Info`, `AI-Ads`, `AI-Unsure`. Pick a recent commit from
https://codeberg.org/emersion/hydroxide/commits and build:

```bash
podman build --build-arg COMMIT=<full commit hash> -t localhost/hydroxide:latest ~/agent/hydroxide

openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
  -subj /CN=protonmail-bridge -addext subjectAltName=DNS:protonmail-bridge \
  -keyout ~/agent/hydroxide/tls/key.pem -out ~/agent/hydroxide/tls/cert.pem

podman run --rm -it --network systemd-agent -e HTTPS_PROXY=http://egress-proxy:3128 \
  --userns keep-id:uid=65532,gid=65532 -v ~/agent/hydroxide/config:/home/nonroot/.config:Z \
  localhost/hydroxide:latest auth "$PROTON"
```

It prints a bridge password at the end. Store it as a Podman secret and start the bridge:

```bash
read -rs P && printf '%s' "$P" | podman secret create bridge-pass - && unset P
systemctl --user start hydroxide
podman logs egress-proxy 2>&1 | grep -oE 'CONNECT [^ ]+' | sort | uniq -c
```

- [ ] The proxy log lists only Proton hosts.

If hydroxide doesn't work for you, use the official Bridge instead (`bridge/` and the
`protonmail-bridge.*` units; needs a paid Proton plan).

## 5. The model

```bash
systemctl --user start ollama
podman exec ollama ollama pull qwen3.5:9b
podman exec ollama ollama run qwen3.5:9b "Reply with one word: hello"
```

- [ ] It answers, and `free -h` with your normal apps open still leaves room.

## 6. ntfy and the phone

```bash
systemctl --user enable --now caddy.socket      # opens 8090 (ntfy) and 8088 (page) for Caddy
systemctl --user start ntfy caddy
podman exec -it ntfy ntfy user add --role=admin me      # the phone logs in as this
podman exec -it ntfy ntfy user add agent
podman exec -it ntfy ntfy access agent email-agent write-only
podman exec -it ntfy ntfy access agent brief write-only
podman exec -it ntfy ntfy token add agent               # copy the tk_... token
read -rs P && printf '%s' "$P" | podman secret create ntfy-token - && unset P
```

Firewall: Fedora Workstation's default zone opens every port from 1025 up to the whole LAN. Close
that, then allow the two ports from WireGuard only:

```bash
Z=$(firewall-cmd --get-default-zone)
sudo firewall-cmd --permanent --zone=$Z --remove-port=1025-65535/tcp --remove-port=1025-65535/udp
for port in 8090 8088; do
  sudo firewall-cmd --permanent --zone=$Z --add-rich-rule="rule family=ipv4 source address=10.0.0.0/24 port port=$port protocol=tcp accept"
done
sudo firewall-cmd --reload
```

On the phone: install ntfy, add the server `http://192.168.8.20:8090`, log in as `me`, subscribe
to `email-agent` and `brief`.

- [ ] `curl -u me -d "hello" http://127.0.0.1:8090/email-agent` arrives on the phone over WireGuard.

## 7. Calendar and time tracking (optional)

- Fossify Calendar: automatic backups to a folder, synced to `~/$CAL` with Syncthing (send-only on the phone).
- SimpleTimeTracker: automatic CSV export to a folder, synced to `~/$TIME`.
- Public holidays: save the `.ics` into `~/agent/extra-calendars/`.

## 8. The jobs

```bash
systemctl --user start agent-jobs-build

# Dry run: classifies the last 14 days, changes nothing, sends nothing
podman run --rm --network systemd-agent --userns keep-id --read-only \
  -v ~/agent/state/email:/state:z -v ~/agent/hydroxide/tls/cert.pem:/tls/cert.pem:ro,z \
  -e EMAIL_AGENT_HOME=/state -e BRIDGE_HOST=protonmail-bridge -e BRIDGE_PORT=1144 \
  -e BRIDGE_USER="$PROTON" -e IMAP_SECURITY=ssl -e LABEL_MODE=flag -e BRIDGE_CERT=/tls/cert.pem \
  -e OLLAMA_URL=http://ollama:11434/api/chat \
  --secret bridge-pass,type=env,target=BRIDGE_PASSWORD \
  localhost/agent-jobs:latest /app/triage.py --dry-run
```

Read every line. For each wrong category, add a rule to `rules.yaml` and run it again. When it
looks right:

```bash
systemctl --user start email-triage && journalctl --user -u email-triage -n 30
systemctl --user start morning-brief && cat ~/agent/data/brief.md
systemctl --user enable --now email-triage.timer email-digest.timer morning-brief.timer agent-net-check.timer
```

- [ ] Labels appear in Proton, urgent mail pushes, the brief arrives.
- [ ] `systemctl --user stop hydroxide`, wait three triage runs: a "triage is NOT working" push
      arrives. Start it again: "working again" arrives.

## 8b. Markets and News (optional)

A separate one-shot job, `news-fetch`, at 07:35 and 18:00, on its own internal network
(`fetch.network`): Squid lets it reach only the hosts in `fetch_hosts` in `squid.conf`. Two offline
jobs run after each good fetch: `markets-build` (closes, earnings and alert rules into `agent.db`,
`web/markets.json`) and `news-build` (stories grouped across outlets, in sections by region, each
tagged with the `topics:` of `news.yaml`, plus the day's shortlist of stories with a topic,
`web/news.json`). After `news-build`, `news-picks` asks the local model for 5 to 7 stories from that
shortlist, each with a line on why it matters (`NEWS_ABOUT` in `news-picks.container` says who for).
It runs on `picks.network`, where Ollama is the only other container; it reads only `news.json` and
writes only `web/picks/news-picks.json`. Code checks the model's answer; anything wrong, or Ollama
busy (email triage) or slow past `PICKS_TIMEOUT`, gives the top of the list, and the journal says why.
The page shows the picks in Top on the News tab and in the brief's Top stories card; `brief.md` never
has them.

Free keys from Tiingo (daily closes, Bitcoin), Finnhub (earnings) and FRED (US release dates),
stored as Podman secrets (pasted, not echoed):

```bash
read -rs K && printf '%s' "$K" | podman secret create tiingo-key - && unset K
read -rs K && printf '%s' "$K" | podman secret create finnhub-key - && unset K
read -rs K && printf '%s' "$K" | podman secret create fred-key - && unset K

mkdir -p ~/agent/state/feeds ~/agent/data/feeds
cp state-feeds/*.yaml ~/agent/state/feeds/          # the watchlist and the feeds: edit to taste
cp timers/news-fetch.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart agent-jobs-build egress-proxy ollama   # ollama joins picks.network
systemctl --user start news-fetch                   # about a minute; markets-build, news-build, news-picks follow
journalctl --user -u news-fetch -n 20               # one line per source: ok / FAILED
journalctl --user -u news-picks -n 5                # "6 from the model, checked", or why it fell back
systemctl --user enable --now news-fetch.timer
```

**Tuning the news topics** (`topics:` in `news.yaml`), like triage's eval: export the latest fetch's
stories, fix `expected` where a story got the wrong topics, then change keywords until `--eval`
passes. Offline, in the job image; neither writes `news.json` nor touches `agent.db`:

```bash
mkdir -p ~/agent/eval
E="podman run --rm --network=none --userns keep-id -v $HOME/agent/state/feeds:/config:ro,z
   -v $HOME/agent/data/feeds:/feeds:ro,z -v $HOME/agent/eval:/eval:z -e FEEDS_CONFIG=/config -e FEEDS_DIR=/feeds
   localhost/agent-jobs:latest /app/news.py"
$E --export-eval /eval/news-eval.yaml   # then edit ~/agent/eval/news-eval.yaml (readable by you only)
$E --eval /eval/news-eval.yaml          # after each news.yaml change: what changed, what's wrong; exit 1 if any
```

Prove it's fenced in: from the fetch network, `example.com` and Proton must both get 403, and
`ollama` must not resolve:

```bash
for h in example.com mail.proton.me; do podman run --rm --network systemd-fetch localhost/agent-jobs:latest -c "import urllib.request as u; u.build_opener(u.ProxyHandler({'https': 'http://egress-proxy:3128'})).open('https://$h', timeout=10)" 2>&1 | grep -o 403; done
podman run --rm --network systemd-fetch localhost/agent-jobs:latest -c "import socket; socket.create_connection(('ollama', 11434), 3)" 2>&1 | tail -1
```

**Are the picks any good?** Run them on the labelled sample, as of its fetch, on the picks network
(the model is only reachable there): how many picks are your keeps and drops, beside the same count
for the fallback (the top 7), with the same request, checks and fallback as the job. Writes nothing.
Each run takes as long as a real one; a run that falls back (e.g. behind email triage) says why.

```bash
podman run --rm --network systemd-picks --userns keep-id --read-only \
  -v ~/agent-setup/samples:/samples:ro,z -v ~/agent-setup/state-feeds:/config:ro,z \
  -e FEEDS_DIR=/samples/feeds -e FEEDS_CONFIG=/config \
  -e OLLAMA_URL=http://ollama:11434/api/chat -e MODEL=qwen3.5:9b \
  localhost/agent-jobs:latest /app/news_picks.py --eval-top /samples/labels.yaml --runs 3
```

Check it in one go: `sh ~/agent/bin/check-picks.sh` prints PASS or FAIL for each check (it ran and
why it chose what it did, the page serves it, its network, and what it can reach) and exits 1 if any
fails. By hand: from the picks network, Ollama answers, and the mail bridge, ntfy and the proxy don't
resolve (prints `ollama ok`, then three `gaierror`):

```bash
podman run --rm --network systemd-picks localhost/agent-jobs:latest -c "import socket; socket.create_connection(('ollama', 11434), 3); print('ollama ok')"
for h in hydroxide ntfy egress-proxy; do podman run --rm --network systemd-picks localhost/agent-jobs:latest -c "import socket; socket.create_connection(('$h', 1), 3)" 2>&1 | tail -1 | cut -d: -f1; done
```

Without the keys, leave out the three `Secret=` lines in `news-fetch.container`; the sources that
need them fail and the rest still run.

## 9. The page

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/      # 200
for f in data stats markets news picks/news-picks; do curl -s -o /dev/null -w "$f.json %{http_code}\n" http://127.0.0.1:8088/$f.json; done
```

On the phone, over WireGuard, open `http://192.168.8.20:8088/`. To get an app-like icon on
Android, [Native Alpha](https://github.com/cylonid/NativeAlphaForAndroid) works over plain HTTP;
browsers only install full web apps over HTTPS.

Grafana (optional): sync `~/agent/data/stats/agent-stats.db` to wherever Grafana runs, add it as
an SQLite data source and import `grafana/agent-dashboard.json`.

## 10. Prove the isolation

```bash
# Jobs have no internet: must FAIL
podman run --rm --network systemd-agent localhost/agent-jobs:latest \
  -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)"
# ...but reach their services: prints ok
podman run --rm --network systemd-agent localhost/agent-jobs:latest \
  -c "import socket; [socket.create_connection((h, p), 3).close() for h, p in [('protonmail-bridge', 1144), ('ollama', 11434), ('ntfy', 8080)]]; print('ok')"
# The proxy refuses anything that isn't Proton: prints 403
podman run --rm --network systemd-agent localhost/agent-jobs:latest \
  -c "import urllib.request as u; u.build_opener(u.ProxyHandler({'https': 'http://egress-proxy:3128'})).open('https://example.com', timeout=10)" 2>&1 | grep -o '403'
# Caddy and ntfy are offline: both must FAIL
podman exec caddy wget -q -T 5 -O- https://example.com
podman exec ntfy wget -q -T 5 -O- https://example.com
# No container publishes ports; only systemd listens on 8090 and 8088
podman ps --format '{{.Names}} {{.Ports}}' | grep -- '->' || echo "no published ports"
# Only the proxy is on the internet-capable network
podman ps --filter network=systemd-egress --format '{{.Names}}'
# The Grafana stats contain no email text: must print 0
grep -ac "<a word from a recent subject>" ~/agent/data/stats/agent-stats.db
```

Then the host: `sh ~/agent-setup/security-check.sh` (see [remote-access.md](remote-access.md)).

## Optional: sleep at night

```bash
sudo rtcwake -m mem -s 120            # check the machine wakes itself after 2 minutes
sudo cp ~/agent-setup/power/agent-* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-night-sleep.timer agent-morning-wake.timer
```

It suspends at 00:30 (retrying every 30 minutes until 03:00 if something blocks sleep) and wakes
at 07:30, so triage catches up before the 07:55 brief.

## Keeping it up to date

Before copying in a new version of the repo, save what it changes, so you can go back:

```bash
sh ~/agent-setup/bin/agent-backup.sh        # -> ~/agent-backup/<date-time>, and the job image as a tag
# ... copy in the new files, reload, restart, check ...
sh ~/agent-setup/bin/agent-restore.sh       # back to the newest backup: files, units, image; then reload and restart
```

The backup holds the job code, the page, Caddy's and Squid's config, the feeds config, `~/agent/bin`
and all Quadlet units; never the data (`agent.db`, mail, fetched feeds), which a restore leaves as it
is. Old backups: delete the folder, and `podman rmi localhost/agent-jobs:backup-<date-time>`.

Every image is pinned. Once a month: read the release notes, change the tag in the unit,
`systemctl --user daemon-reload`, restart it, and re-run step 10. Rebuild the job image with
`podman build --pull=always -t localhost/agent-jobs:latest ~/agent/agent-jobs` for Python fixes.
