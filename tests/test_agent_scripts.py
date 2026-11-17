"""bin/check-picks.sh, bin/agent-backup.sh and bin/agent-restore.sh, with stand-in systemctl,
journalctl, podman and curl (they log every call and answer from a state file), in a fake home,
under every POSIX shell found (dash, bash)."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent
BIN = REPO / "bin"
SHELLS = [s for s in ("dash", "bash") if shutil.which(s)] or ["sh"]

STUB = r'''#!PYTHON
import json, os, re, sys
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
path = os.environ["STUB_STATE"]
state = json.load(open(path))
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(" ".join([name] + args) + "\n")


def done(text="", code=0, save=False):
    if save:
        json.dump(state, open(path, "w"))
    if text:
        print(text)
    sys.exit(code)


if name == "systemctl":
    a = [x for x in args if x != "--user"]
    quiet = "-q" in a
    a = [x for x in a if x != "-q"]
    verb = a[0]
    if verb == "show":
        props = [a[i + 1] for i, x in enumerate(a) if x == "-p"]
        unit = next(x for i, x in enumerate(a[1:], 1) if not x.startswith("-") and a[i - 1] != "-p")
        u = state["units"].get(unit, {"LoadState": "not-found", "Result": "success", "ExecMainStatus": "0",
                                      "ExecMainExitTimestamp": "", "ActiveState": "inactive"})
        done("\n".join(u.get(p, "") if "--value" in a else f"{p}={u.get(p, '')}" for p in props))
    if verb == "cat":
        text = state["unit_text"].get(a[1])
        done(text or "", 0 if text else 1)
    if verb == "is-active":
        on = any(u in state["active"] for u in a[1:])
        done("" if quiet else ("active" if on else "inactive"), 0 if on else 3)
    done("", state["fail"].get(" ".join(a), 0))
if name == "journalctl":
    done(state["journal"])
if name == "curl":
    if state["served"] is None:
        done("", 22)
    sys.stdout.write(open(state["served"]).read())
    sys.exit(0)
if name == "podman":
    if args[:2] == ["image", "exists"]:
        done("", 0 if args[2] in state["images"] else 1)
    if args[0] == "tag":
        if args[1] not in state["images"]:
            done("", 125)
        state["images"].append(args[2])
        done("", 0, save=True)
    if args[:2] == ["network", "inspect"]:
        n = state["networks"].get(args[2])
        done(n or "", 0 if n else 125)
    if args[:2] == ["network", "exists"]:
        done("", 0 if args[2] in state["networks"] else 1)
    if args[:2] == ["network", "rm"]:
        state["networks"].pop(args[2], None)
        done(args[2], 0, save=True)
    if args[0] == "ps":
        net = next(x.split("=", 1)[1] for x in args if x.startswith("network="))
        done("\n".join(state["on_net"].get(net, [])))
    if args[0] == "inspect":
        ip = state["ips"].get(args[1])
        done(ip or "", 0 if ip else 125)
    if args[0] == "run":
        if state["run_fails"]:
            print("Error: localhost/agent-jobs:latest: image not known", file=sys.stderr)
            sys.exit(125)
        host, port = re.search(r"create_connection\(\('([^']+)', (\d+)\)", args[-1]).groups()
        done("REACHED" if f"{host}:{port}" in state["reachable"] else
             "BLOCKED gaierror [Errno -2] Name or service not known")
    done("", 0)
'''


class Box:
    """A fake home with the stand-in commands first on PATH."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = self.root / "home"
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        for name in ("systemctl", "journalctl", "podman", "curl"):
            p = self.stubs / name
            p.write_text(STUB.replace("PYTHON", sys.executable))
            p.chmod(0o755)
        self.state_file, self.log = self.root / "state.json", self.root / "calls.log"

    def state(self, **changes):
        s = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        s.update(changes)
        self.state_file.write_text(json.dumps(s))

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run(self, shell, script, *args, stdin=""):
        self.log.write_text("")
        env = {"HOME": str(self.home), "PATH": f"{self.stubs}:{os.environ['PATH']}", "STUB_STATE": str(self.state_file),
               "STUB_LOG": str(self.log), "LANG": "C.UTF-8"}
        return subprocess.run([shell, str(script), *args], capture_output=True, text=True, env=env, input=stdin,
                              timeout=60)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def tree(root):
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


# ---------- check-picks.sh ----------
UNIT = ("[Service]\nExecStart=/usr/bin/podman run --name news-picks --cidfile=%t/%N.cid --replace --rm "
        "--network systemd-picks --userns keep-id -v /x:/in/news.json:ro localhost/agent-jobs:latest /app/news_picks.py\n")


def healthy(box):
    web = box.home / "agent" / "data" / "web"
    write(web / "news.json", json.dumps({"generated": "2026-11-16T07:36:10+00:00"}))
    write(web / "picks" / "news-picks.json", json.dumps({
        "source": "model", "reason": None, "list_generated": "2026-11-16T07:36:10+00:00",
        "picks": [{"rank": n} for n in (1, 4, 5, 7, 8, 12)]}))
    q = box.home / ".config" / "containers" / "systemd"
    write(q / "news-picks.container", "[Container]\nNetwork=picks.network\n")
    write(q / "ollama.container", "[Container]\nNetwork=egress.network\nNetwork=agent.network\nNetwork=picks.network\n")
    write(q / "news-fetch.container", "[Container]\nNetwork=fetch.network\n")
    write(q / "picks.network", "[Network]\nInternal=true\n")
    box.state(units={"news-picks": {"LoadState": "loaded", "ActiveState": "inactive", "Result": "success",
                                    "ExecMainStatus": "0", "ExecMainExitTimestamp": "Mon 2026-11-16 07:38:12 GMT"}},
              unit_text={"news-picks": UNIT}, active=[], fail={},
              journal="6 picks, checked answer of the model\nnews picks: 6 from the model, checked",
              served=str(web / "picks" / "news-picks.json"), images=["localhost/agent-jobs:latest"],
              networks={"systemd-picks": "true"}, on_net={"systemd-picks": ["ollama"]},
              ips={"protonmail-bridge": "10.89.0.5", "egress-proxy": "10.89.0.2"},
              reachable=["ollama:11434"], run_fails=False)


CHECK = BIN / "check-picks.sh"
for shell in SHELLS:
    box = Box()
    healthy(box)
    r = box.run(shell, CHECK)
    assert r.returncode == 0, (shell, r.stdout, r.stderr)
    marks = [l[:4] for l in r.stdout.splitlines() if l[:4] in ("PASS", "FAIL", "SKIP")]
    assert marks == ["PASS"] * 17, (shell, r.stdout)        # A1-A4, B1-B4, C1-C3, C4 x4, C5 x2
    assert "PASS  A2 the journal says why: 6 from the model, checked\n" in r.stdout
    assert "PASS  A3 picks/news-picks.json: 6 picks, by the model, for the current list\n" in r.stdout
    assert "PASS  B3 on systemd-picks now: ollama\n" in r.stdout
    assert "PASS  C5 not protonmail-bridge by address (10.89.0.5:1144) (gaierror [Errno -2]" in r.stdout
    assert r.stdout.rstrip().endswith("All 17 checks passed."), r.stdout     # 17: C4 and C5 are several
    assert r.stderr == "", r.stderr

    # each way it can go wrong: the check that says so, and exit 1
    cases = [
        ("A1 news-picks unit", dict(units={}), "FAIL  A1 news-picks unit: not found"),
        ("A1 news-picks has not run yet", dict(units={"news-picks": {"LoadState": "loaded", "ExecMainExitTimestamp": ""}}),
         "FAIL  A1 news-picks has not run yet"),
        ("A1 news-picks last run", dict(units={"news-picks": {"LoadState": "loaded", "Result": "exit-code",
                                                              "ExecMainStatus": "1", "ExecMainExitTimestamp": "x"}}),
         "FAIL  A1 news-picks last run: Result=exit-code, exit 1"),
        ("A2 no 'news picks", dict(journal="something else"), "FAIL  A2 no 'news picks:' line"),
        ("A4 the page doesn't serve it", dict(served=None), "FAIL  A4 the page doesn't serve it"),
        ("B1 news-picks networks", dict(unit_text={"news-picks": UNIT.replace("--network systemd-picks",
                                                                              "--network systemd-picks --network systemd-agent")}),
         "FAIL  B1 news-picks networks: 'systemd-agent systemd-picks', expected systemd-picks only"),
        ("B2 systemd-picks", dict(networks={"systemd-picks": "false"}), "FAIL  B2 systemd-picks: missing or not internal"),
        ("B3 on systemd-picks now", dict(on_net={"systemd-picks": ["ntfy", "ollama"]}),
         "FAIL  B3 on systemd-picks now: 'ntfy ollama'"),
        ("B3 on systemd-picks now", dict(on_net={"systemd-picks": []}), "FAIL  B3 on systemd-picks now: ''"),
        ("C1 can't reach Ollama", dict(reachable=[]), "FAIL  C1 can't reach Ollama: BLOCKED"),
        ("C2 no internet by name", dict(reachable=["ollama:11434", "example.com:443"]),
         "FAIL  C2 no internet by name (example.com:443): it connected"),
        ("C3 no internet by address", dict(reachable=["ollama:11434", "1.1.1.1:443"]), "FAIL  C3 no internet by address"),
        ("C4 not protonmail-bridge by name", dict(reachable=["ollama:11434", "protonmail-bridge:1144"]),
         "FAIL  C4 not protonmail-bridge by name: it connected"),
        ("C5 not egress-proxy by address", dict(reachable=["ollama:11434", "10.89.0.2:3128"]),
         "FAIL  C5 not egress-proxy by address (10.89.0.2:3128): it connected"),
    ]
    for check, change, expect in cases:
        healthy(box)
        box.state(**change)
        r = box.run(shell, CHECK)
        assert r.returncode == 1 and expect in r.stdout, (shell, check, r.stdout)
        assert [l for l in r.stdout.splitlines() if l.startswith("FAIL")] == \
               [l for l in r.stdout.splitlines() if l.startswith("FAIL") and check in l], (shell, check, r.stdout)
        assert r.stdout.rstrip().endswith("checks failed."), r.stdout
    # files: none, stale, broken
    healthy(box)
    (box.home / "agent/data/web/picks/news-picks.json").unlink()
    r = box.run(shell, CHECK)
    assert r.returncode == 1 and "FAIL  A3 picks/news-picks.json: can't read it" in r.stdout, r.stdout
    healthy(box)
    write(box.home / "agent/data/web/news.json", json.dumps({"generated": "2026-11-16T18:01:00+00:00"}))
    r = box.run(shell, CHECK)
    assert "FAIL  A3 picks/news-picks.json: made for the list of 2026-11-16T07:36:10+00:00, not the current one" \
           in r.stdout and r.returncode == 1, r.stdout
    healthy(box)
    write(box.home / "agent/data/web/picks/news-picks.json", json.dumps({"source": "model", "picks": []}))
    assert "FAIL  A3 picks/news-picks.json: not a picks file" in box.run(shell, CHECK).stdout
    # a fallback run is a pass, with its reason
    healthy(box)
    write(box.home / "agent/data/web/picks/news-picks.json", json.dumps({
        "source": "fallback", "reason": "no answer within 180 s (Ollama busy, e.g. with email triage, or slow)",
        "list_generated": "2026-11-16T07:36:10+00:00", "picks": [{"rank": n} for n in range(1, 8)]}))
    box.state(journal="news picks: the top of the list (no answer within 180 s (Ollama busy, e.g. with email triage, or slow))")
    r = box.run(shell, CHECK)
    assert r.returncode == 0 and "PASS  A3 picks/news-picks.json: 7 picks, the fallback (no answer within 180 s" in r.stdout
    assert "PASS  A2 the journal says why: the top of the list (no answer within 180 s" in r.stdout
    # another unit on picks.network; the test container can't start: FAIL, never a PASS by mistake
    healthy(box)
    write(box.home / ".config/containers/systemd/ntfy.container", "[Container]\nNetwork=agent.network\nNetwork=picks.network\n")
    r = box.run(shell, CHECK)
    assert "FAIL  B4 units on picks.network: 'news-picks.container ntfy.container ollama.container'" in r.stdout
    healthy(box)
    (box.home / ".config/containers/systemd/ntfy.container").unlink()
    box.state(run_fails=True)
    r = box.run(shell, CHECK)
    c = [l for l in r.stdout.splitlines() if l[6:8] in ("C1", "C2", "C3", "C4", "C5")]
    assert r.returncode == 1 and len(c) == 9 and all(l.startswith("FAIL") for l in c), r.stdout
    assert "FAIL  C2 no internet by name (example.com:443): the test container didn't run" in r.stdout
    # the bridge not running: its address check is skipped, not failed
    healthy(box)
    box.state(ips={"egress-proxy": "10.89.0.2"})
    r = box.run(shell, CHECK)
    assert r.returncode == 0 and "SKIP  C5 protonmail-bridge by address: not running on systemd-agent" in r.stdout
print("CHECK-PICKS OK")


# ---------- agent-backup.sh and agent-restore.sh ----------
def installed(box):
    """The running copy before the update, with some of your own data and settings in it."""
    a, q = box.home / "agent", box.home / ".config" / "containers" / "systemd"
    write(a / "agent-jobs" / "news.py", "old news.py\n")
    write(a / "agent-jobs" / "Containerfile", "COPY news.py /app/\n")
    write(a / "site" / "world.js", "old page\n")
    write(a / "caddy" / "Caddyfile", "@data path /news.json\n# my Grafana at 192.168.20.10\n")
    write(a / "bin" / "agent-net-check.sh", "old check\n")
    write(a / "state" / "feeds" / "news.yaml", "candidates:\n  max: 40\n# my own feeds\n")
    write(a / "egress-proxy" / "squid.conf", "acl fetch_hosts dstdomain a.example\n")
    write(q / "ollama.container", "Network=agent.network\n# my GPU lines\n")
    write(q / "morning-brief.container", "After=ollama.service\nVolume=%h/MyCal:/calendar:ro,z\n")
    write(a / "data" / "agent.db", "mail v1")
    write(a / "data" / "web" / "news.json", "{}")
    box.state(units={}, unit_text={}, active=["news-fetch.timer"], fail={}, journal="", served=None,
              images=["localhost/agent-jobs:latest"], networks={}, on_net={}, ips={}, reachable=[], run_fails=False)


def update(box, proxy=False):
    """What installing the news-picks update does to it (and mail arriving meanwhile)."""
    a, q = box.home / "agent", box.home / ".config" / "containers" / "systemd"
    write(a / "agent-jobs" / "news.py", "new news.py\n")
    write(a / "agent-jobs" / "news_picks.py", "new\n")
    write(a / "agent-jobs" / "Containerfile", "COPY news.py news_picks.py /app/\n")
    write(a / "site" / "world.js", "new page\n")
    write(a / "caddy" / "Caddyfile", "@data path /news.json /picks/*\n# my Grafana at 192.168.20.10\n")
    write(a / "bin" / "check-picks.sh", "new\n")
    write(a / "state" / "feeds" / "news.yaml", "candidates:\n  max: 40\n  sections: {Europe: 0.25}\n# my own feeds\n")
    write(q / "ollama.container", "Network=agent.network\nNetwork=picks.network\n# my GPU lines\n")
    write(q / "morning-brief.container", "After=ollama.service news-picks.service\nVolume=%h/MyCal:/calendar:ro,z\n")
    write(q / "picks.network", "[Network]\nInternal=true\n")
    write(q / "news-picks.container", "Network=picks.network\n")
    write(a / "data" / "web" / "picks" / "news-picks.json", "{}")
    write(a / "data" / "agent.db", "mail v1 + v2")             # data: the restore must keep it
    if proxy:
        write(a / "egress-proxy" / "squid.conf", "acl fetch_hosts dstdomain a.example b.example\n")
    box.state(networks={"systemd-picks": "true"}, images=box_images(box) + ["localhost/agent-jobs:new"])


def box_images(box):
    return json.loads(box.state_file.read_text())["images"]


CONFIG = ["agent-jobs", "site", "caddy", "bin", "state", "egress-proxy"]
BACKUP, RESTORE = BIN / "agent-backup.sh", BIN / "agent-restore.sh"
for shell in SHELLS:
    box = Box()
    installed(box)
    before = {**{k: v for k, v in tree(box.home / "agent").items() if k.split("/")[0] in CONFIG},
              **{"quadlets/" + k: v for k, v in tree(box.home / ".config/containers/systemd").items()}}
    r = box.run(shell, BACKUP)
    assert r.returncode == 0, (shell, r.stdout, r.stderr)
    backups = list((box.home / "agent-backup").iterdir())
    assert len(backups) == 1 and (backups[0] / "DONE").exists(), backups
    B = backups[0]
    stamp = B.name
    assert (B / "dirs").read_text().split() == ["agent-jobs", "site", "caddy", "bin", "state/feeds", "egress-proxy"]
    assert box.calls() == ["podman image exists localhost/agent-jobs:latest",
                           f"podman tag localhost/agent-jobs:latest localhost/agent-jobs:backup-{stamp}"], box.calls()
    assert f"To go back: sh ~/agent-setup/bin/agent-restore.sh {B}" in r.stdout
    assert not (B / "agent" / "data").exists()                  # no data in a backup
    # a second backup into the same folder is refused
    r = box.run(shell, BACKUP, str(B))
    assert r.returncode == 1 and "already exists" in r.stderr

    update(box)
    # asked, answered no: nothing changes
    r = box.run(shell, RESTORE, stdin="n\n")
    assert r.returncode == 1 and "nothing changed" in r.stdout and box.calls() == []
    assert (box.home / "agent" / "agent-jobs" / "news_picks.py").exists()
    # restore, started from ~/agent/bin (a folder it replaces), from the newest whole backup
    write(box.home / "agent-backup" / "29991231-235959" / "dirs", "agent-jobs\n")     # newer, but not whole
    shutil.copy(RESTORE, box.home / "agent" / "bin" / "agent-restore.sh")
    r = box.run(shell, box.home / "agent" / "bin" / "agent-restore.sh", "-y")
    assert r.returncode == 0, (shell, r.stdout, r.stderr)
    assert f"Restoring from {B}:" in r.stdout, r.stdout
    after = {**{k: v for k, v in tree(box.home / "agent").items() if k.split("/")[0] in CONFIG},
             **{"quadlets/" + k: v for k, v in tree(box.home / ".config/containers/systemd").items()}}
    assert after == before, set(after) ^ set(before) or [k for k in after if after[k] != before[k]]
    assert not (box.home / "agent" / "data" / "web" / "picks").exists()               # made by the update: gone
    assert (box.home / "agent" / "data" / "agent.db").read_text() == "mail v1 + v2"   # data kept
    assert (box.home / "agent" / "data" / "web" / "news.json").exists()
    calls = box.calls()
    assert calls == [
        "systemctl --user is-active news-fetch.timer",
        "systemctl --user stop news-fetch.timer",
        "systemctl --user stop news-picks.service",
        f"podman tag localhost/agent-jobs:backup-{stamp} localhost/agent-jobs:latest",
        "systemctl --user daemon-reload",
        "systemctl --user -q is-active email-triage",
        "systemctl --user restart ollama caddy",
        "podman network exists systemd-picks",
        "podman network rm systemd-picks",
        "systemctl --user start news-build",
        "systemctl --user start news-fetch.timer"], (shell, calls)
    assert "removed ~/agent/data/web/picks (not there before)" in r.stdout and "Restored. Reload the page." in r.stdout

    # a proxy config the update changed: restored, and the proxy restarted; a timer that was off stays off
    box = Box()
    installed(box)
    box.run(shell, BACKUP, str(box.root / "b1"))
    update(box, proxy=True)
    box.state(active=[])
    r = box.run(shell, RESTORE, "-y", str(box.root / "b1"))
    assert r.returncode == 0, r.stderr
    assert "systemctl --user restart egress-proxy" in box.calls() and "systemctl --user start news-fetch.timer" not in box.calls()
    assert (box.home / "agent/egress-proxy/squid.conf").read_text() == "acl fetch_hosts dstdomain a.example\n"
    # the saved image tag pruned meanwhile: rebuilt from the restored Containerfile, the rest as usual
    box = Box()
    installed(box)
    box.run(shell, BACKUP, str(box.root / "b2"))
    update(box)
    box.state(images=["localhost/agent-jobs:latest"])
    r = box.run(shell, RESTORE, "-y", str(box.root / "b2"))
    assert r.returncode == 0 and "systemctl --user restart agent-jobs-build" in box.calls(), (r.stdout, box.calls())
    assert box.calls()[-2:] == ["systemctl --user start news-build", "systemctl --user start news-fetch.timer"]
    # a step that fails: the others still run, and it says which failed (exit 1)
    box = Box()
    installed(box)
    box.run(shell, BACKUP, str(box.root / "b3"))
    update(box)
    box.state(fail={"restart ollama caddy": 1})
    r = box.run(shell, RESTORE, "-y", str(box.root / "b3"))
    assert r.returncode == 1, r.stdout
    assert "Files restored, but these steps failed; run them by hand:\n  restart Ollama and Caddy: systemctl --user " \
           "restart ollama caddy" in r.stdout, r.stdout
    assert "podman network rm systemd-picks" in box.calls() and "systemctl --user start news-build" in box.calls()
    assert (box.home / "agent/agent-jobs/news.py").read_text() == "old news.py\n"
    # no backup at all, or one that isn't whole: nothing happens
    box = Box()
    installed(box)
    r = box.run(shell, RESTORE, "-y")
    assert r.returncode == 1 and "no whole backup found" in r.stderr and box.calls() == [], r.stderr
    write(box.root / "half" / "dirs", "agent-jobs\n")
    r = box.run(shell, RESTORE, "-y", str(box.root / "half"))
    assert r.returncode == 1 and "no whole backup found" in r.stderr and box.calls() == []
print("BACKUP AND RESTORE OK")
