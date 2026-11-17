# offline-home-agent

A small assistant that runs on a mini PC at home and has no internet access. It sorts my Proton
Mail, pushes the emails that matter to my phone, and writes a morning brief from my calendar,
Obsidian to-do list and time tracking, with a short look at the markets and the news. Only two
things are allowed out of the house: the mail bridge, only to Proton's servers, and a fetch job,
only to a fixed list of market-data and news hosts.

I built it because I wanted the useful parts of an "AI inbox" without handing my email to a
cloud service. Everything here, the language model included, runs locally.

## What it does

- **Email triage, every 10 minutes.** New mail is sorted into urgent, work, personal, purchase,
  jobs, info, ads or unsure. My own rules (`rules.yaml`) decide first; a local model (qwen3.5 9B
  via Ollama) handles the rest. Each email gets a Proton label, and the important ones become a
  push notification on my phone.
- **Digest at 08:00 and 18:00** with everything that didn't need a push, including a list of what
  the model filed away quietly, so a wrong guess gets caught the same day.
- **Morning brief at 07:55.** Today's events and the next week, tasks by priority (Obsidian Tasks
  format plus calendar tasks), a month preview, this week's tracked time against last week, and the
  email waiting for me. The model writes a short summary at the top from facts the code has already
  checked, so it can't invent numbers.
- **Markets and News, fetched twice a day** (07:35 and 18:00) from Tiingo, Finnhub, FRED, Hacker
  News and a list of RSS feeds (Europe first, then the US and the world). Rules in code, not the
  model, decide what counts as an alert: a move far outside a stock's usual range, a 52-week high or
  low, a sector beating or trailing the S&P 500, earnings above or below estimates. The same story
  from several outlets is grouped into one, with each outlet's headline a tap away. The local model
  then picks 5 to 7 stories from the day's Top list, with a line on why each matters; code checks
  its answer and falls back to the top of the list. No buy or sell suggestions, ever.
- **A small web page** for reading all of this on the phone, because a long notification is hard
  to read. Tapping a push opens it. Five tabs: Brief (today only: tasks, wins and what's slipping,
  a small markets table, top stories), Emails, Stats (this week's goals and time, then the long
  view), Markets and News. Static files plus a few JSON files, served on the LAN.
- **Health alerts.** If triage fails three times in a row (bridge down, password changed), I get a
  push saying so, and another when it recovers. Silence never means "no mail" by accident.
- **Grafana stats** (optional): counts only, never subjects or addresses.
- **Night sleep** (optional): the box suspends at 00:30 and wakes itself at 07:30.

## How it's put together

Everything is a rootless Podman container managed by systemd (Quadlet files), on a Fedora mini PC
(Ryzen 7 8845HS, 32 GB). The model runs on the CPU.

```
                    internet
                       │
          (Proton; market and news hosts)
                       │
              ┌────────┴────────┐
              │  egress-proxy   │  Squid, two allowlists
              │  egress network │
              └───┬────────┬────┘
                  │        └──── fetch network (internal): news-fetch only,
                  │              allowed only the market and news hosts
   ────────────────────┼──────────── agent network (internal, no route out)
        │              │              │             │            │
   hydroxide      email-triage    morning-brief   ollama       ntfy
  (IMAP bridge)   email-digest    (one-shot)      (model)    (pushes)
                  (one-shot jobs)                    │          │
                                     news-picks ─────┘        caddy ── ports 8090 / 8088
                         (picks network, internal:        (socket-activated, no internet)
                          only news-picks and ollama)           │
                                                     phone, over WireGuard only
```

A few decisions that shaped it:

- **Four networks.** `agent` is internal: containers on it can talk to each other and nothing else.
  Only the proxy also sits on `egress`, and from `agent` Squid refuses anything that isn't Proton.
  The fetch job sits alone on a third internal network, `fetch`, which Squid lets reach only the
  market and news hosts; it can't see the mail bridge, the model or the page. The jobs that turn
  its raw data into the Markets and News tabs run with no network at all. The one that asks the
  model for the day's news picks, `news-picks`, shares a fourth internal network, `picks`, with
  Ollama alone, and sees only the list it picks from.
- **Fetched text is untrusted, like email.** Headlines never go into the brief's text (a chat
  model with tools reads it); the page shows them as plain text and only links `https://` addresses.
  The model's lines about the news picks are untrusted too (it read the headlines): checked by
  code, shown as plain text, never in the brief's text.
  API keys are Podman secrets, sent in headers where the API allows, and scrubbed from saved files.
- **No published ports.** Caddy is started by a systemd socket (`caddy.socket`), so the ports are
  opened on the host by systemd and handed in. Caddy itself has no internet either.
- **The jobs are one-shot containers** with a read-only filesystem, no capabilities, and the
  user's own UID. They start on a timer, do their work and exit.
- **Email text is untrusted.** The model only classifies and summarises it; it never gets tools.
  The page shows email text with `textContent`, never as HTML, and its Content-Security-Policy only
  allows its own files.
- **Everything is pinned.** Image tags and Python packages are fixed versions, so nothing changes
  until I update it on purpose.
- **Remote access is WireGuard only**, on my router. SSH accepts keys only, and only from WireGuard
  addresses; `security-check.sh` verifies the firewall and SSH settings.

## Repository layout

| Path | What it is |
|---|---|
| `agent-jobs/` | The Python jobs: `triage.py` (sorting, pushes, digest), `brief.py` (morning brief), `fetch.py` (market data and feeds), `markets.py` (closes, earnings, alert rules), `news.py` (stories grouped across outlets), `news_picks.py` (the model's picks from the Top list), `agentdb.py` (SQLite record, page data, stats) and their container |
| `quadlets/` | Podman Quadlet units: networks, containers, the job image build |
| `timers/` | systemd user timers for triage, digest, brief and fetch, and the network self-check |
| `bin/agent-net-check.sh` | Restarts all containers if the proxy can't reach Proton but the host can (a stale rootless network after a reboot) |
| `bin/check-picks.sh` | PASS/FAIL checks that news-picks ran, on its own network, reaching Ollama and nothing else |
| `bin/agent-backup.sh`, `bin/agent-restore.sh` | Save what an update changes (files, units, the job image) and put it back |
| `caddy/` | `Caddyfile` and `caddy.socket` |
| `egress-proxy/` | Squid config with the Proton and fetch allowlists |
| `hydroxide/`, `bridge/` | Two ways to get IMAP from Proton: [hydroxide](https://codeberg.org/emersion/hydroxide) (free) or the official Bridge (needs a paid plan) |
| `ntfy/` | ntfy server config |
| `site/` | The web page (plain HTML, CSS and JavaScript, no build step, no dependencies) |
| `state-email/rules.yaml` | Example sorting rules and category hints for the model |
| `state-feeds/` | The watchlist, alert thresholds and central-bank dates (`markets.yaml`), and the news feeds (`news.yaml`) |
| `grafana/` | Dashboard for the counts-only stats |
| `power/` | Night suspend and morning wake timers |
| `tests/` | Plain Python test scripts |
| `docs/` | Setup guide and remote-access notes |

## Running it

The full walkthrough is in [docs/setup.md](docs/setup.md). It takes an afternoon, plus a week or two
of adding rules until the sorting matches how you think about your mail. You need:

- Linux with Podman 5.2+ and systemd (I use Fedora); 16 GB RAM is the practical minimum for the 9B model
- A Proton Mail account
- Optional, for Markets: free API keys from Tiingo, Finnhub and FRED
- ntfy on your phone, and WireGuard (or similar) if you want pushes away from home
- Optional: Fossify Calendar and SimpleTimeTracker backups synced with Syncthing, and an Obsidian vault

## Tests

No framework; each file is a script that exits non-zero on failure. From the repo root:

```bash
pip install -r agent-jobs/requirements.txt
for f in tests/test_*.py; do PYTHONPATH=agent-jobs python3 "$f" > /dev/null && echo "ok  $f" || echo "FAIL $f"; done
```

They use fake IMAP, ntfy and Ollama servers, so they need no accounts or network. The page test
(`test_news_picks_page.py`) also needs Node; without it, it says so and skips.

## Limits and trade-offs

- A 9B model on a CPU is slow (a classification takes a few seconds) and sometimes wrong. That's
  why rules come first and the digest lists what the model filed quietly.
- hydroxide is unofficial. It has been reliable for me, but the official Bridge is the fallback.
- The page uses plain HTTP. It's only reachable over WireGuard, which already encrypts the traffic.
- Market data is end-of-day (Bitcoin is as of the last fetch), on free API tiers. It's a
  calendar and a few rules, not a trading tool.
- It's built for one person and one mailbox. There's no multi-user support and no plan for it.

## How it was built

I built this with Claude Code over a couple of weeks, one piece at a time: first triage, then the brief,
then the isolation and the page, then hardening remote access. I made the design calls and tested
every step on the real machine; most of the code was written in conversation with the assistant.

## License

MIT. See [LICENSE](LICENSE).
