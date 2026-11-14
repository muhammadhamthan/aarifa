"""Rounds 6-8: free email, call and firewall servers from `free version.docx`, each ALONE from its official image.

  python r678_driver.py [haraka stalwart postal asterisk freeswitch crowdsec bunkerweb safeline]
  -> results/r6_*.json, r7_*.json, r8_*.json, r678_summary.json

Every step records idle RAM, RAM / CPU while working (docker stats every ~2 s) and proof the work happened:
emails accepted (smtp_send), calls completed (SIPp's own statistics), log lines read (cscli metrics),
requests answered through the proxy (load10 codes).
"""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from r5_driver import ram_now, window  # noqa: E402

BENCH = Path(__file__).resolve().parents[1]
RES = BENCH / "results"
PY = sys.executable
C = BENCH / "compose"


def dc(project: str, files: list[str], *args: str, env: dict[str, str] | None = None, out: bool = False,
       env_file: str | None = None) -> str:
    cmd = ["docker", "compose", "-p", project]
    if env_file:
        cmd += ["--env-file", env_file]
    for f in files:
        cmd += ["-f", f]
    r = subprocess.run([*cmd, *args], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       check=False)
    return (r.stdout + r.stderr) if out else ""


def names_of(project: str) -> list[str]:
    return subprocess.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}", "--format",
                           "{{.Names}}"], capture_output=True, text=True, check=False).stdout.split()


def wait_tcp(port: int, limit: float = 600) -> float:
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
                if port in (2525, 2526, 2527):  # SMTP: wait for the banner, not just the open port
                    s.settimeout(10)
                    if s.recv(64).startswith(b"220"):
                        return round(time.time() - t0, 1)
                else:
                    return round(time.time() - t0, 1)
        except OSError:
            pass
        time.sleep(2)
    raise TimeoutError(f"port {port}")


def wait_http(url: str, limit: float = 900) -> float:
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            if httpx.get(url, timeout=10, verify=False).status_code < 500:
                return round(time.time() - t0, 1)
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise TimeoutError(url)


class Sampler:
    def __init__(self, project: str, tag: str) -> None:
        self.path = RES / f"{tag}_stats.csv"
        self.p = subprocess.Popen([PY, str(BENCH / "scripts" / "sample.py"), "stream", "--project", project,
                                   "--out", str(self.path), "--seconds", "3600"])

    def stop(self, names: list[str]) -> dict[str, Any]:
        time.sleep(3)
        # kill the whole tree: terminate() alone left sample.py's `docker stats` child running on Windows
        # (28 orphans had piled up by 14 Nov and kept the driver's output pipe open)
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.p.pid)], capture_output=True, check=False)
        self.p.wait(timeout=30)
        return window(self.path, names)


def save(tag: str, res: dict[str, Any]) -> dict[str, Any]:
    (RES / f"{tag}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return res


# ------------------------------------------------------------------ Round 6: email
def smtp_load(port: int, tag: str, to: str = "buyer@bench.test", count: int = 1000, conns: int = 10,
              extra: list[str] | None = None) -> dict[str, Any]:
    out = RES / f"{tag}_send.json"
    subprocess.run([PY, str(BENCH / "scripts" / "smtp_send.py"), "--host", "localhost", "--port", str(port),
                    "--count", str(count), "--to", to, "--conns", str(conns), "--out", str(out), *(extra or [])],
                   capture_output=True, text=True, check=False)
    return json.loads(out.read_text()) if out.exists() else {"error": "smtp_send wrote nothing"}


def haraka() -> dict[str, Any]:
    files = [str(C / "r6-email.yml")]
    dc("kxmail", files, "--profile", "haraka", "up", "-d")
    ready = wait_tcp(2525)
    time.sleep(30)
    names = names_of("kxmail")
    idle = ram_now(names)
    s = Sampler("kxmail", "r6_haraka")
    send = smtp_load(2525, "r6_haraka")
    load = s.stop(names)
    dc("kxmail", files, "--profile", "haraka", "down", "-v")
    return save("r6_haraka", {"ready_s": ready, "ram_idle_mib": idle, "load": load, "send": send,
                              "note": "accept + discard (queue/discard): measures SMTP receive cost, no storage"})


BUYER = ("buyer@bench.test", "bench-pass-123-Xy")  # bench-only mailbox, created at setup (see R6_R8_RESULTS.md)


def stalwart_mailbox_total() -> int:
    """Messages actually stored in the buyer's mailbox (JMAP), the proof that delivery happened."""
    r = httpx.post("http://localhost:28090/jmap/", auth=BUYER, timeout=60, json={
        "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
        "methodCalls": [["Email/query", {"accountId": "c", "calculateTotal": True}, "q"]]})
    return int(r.json()["methodResponses"][0][1]["total"])


def stalwart() -> dict[str, Any]:
    """Stalwart v1.1 must first be set up once in its web wizard (bootstrap mode has no SMTP). The bench did that
    with a browser (hostname mail.bench.test, domain bench.test, ACME off, RocksDB) and created the buyer mailbox
    with the JMAP management call x:Account/set; the volumes keep that setup. This step measures the set-up server."""
    files = [str(C / "r6-email.yml")]
    dc("kxmail", files, "--profile", "stalwart", "up", "-d")
    ready = wait_tcp(2526, 120)
    time.sleep(30)
    names = names_of("kxmail")
    idle = ram_now(names)
    stored0 = stalwart_mailbox_total()
    vol0 = volume_total("kxmail")
    s = Sampler("kxmail", "r6_stalwart")
    # 10 connections, as for Haraka. Stalwart's two default inbound throttles (5 connections/s per IP; 25 messages/h
    # per sender-domain + recipient) were switched off for this capacity test with x:MtaInboundThrottle/set: with
    # them on, the first run stored 25 messages and then answered "452 4.4.5 Rate limit exceeded".
    # Its default of 10 messages per SMTP session is kept: the sender reconnects every 10 messages, as real MTAs do.
    send = smtp_load(2526, "r6_stalwart", conns=10, extra=["--per-session", "10"])
    time.sleep(15)  # let the queue finish delivering into the mailbox
    load = s.stop(names)
    stored1 = stalwart_mailbox_total()
    vol1 = volume_total("kxmail")
    ram_after = ram_now(names)
    return save("r6_stalwart", {"ready_s": ready, "ram_idle_mib": idle, "load": load, "ram_after_mib": ram_after,
                                "send": send, "stored_before": stored0, "stored_after": stored1,
                                "delivered": stored1 - stored0, "store_growth_bytes": vol1 - vol0,
                                "store_per_1000_mails_mb": round((vol1 - vol0) / 1e6 * 1000 / max(1, stored1 - stored0), 2),
                                "note": "receive + spam/DKIM/SPF checks + store in RocksDB (full-text index on)"})


def volume_total(project: str) -> int:
    vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                          capture_output=True, text=True, check=False).stdout.split()
    total = 0
    for v in vols:
        out = subprocess.run(["docker", "run", "--rm", "-v", f"{v}:/v:ro", "alpine:3.21", "du", "-sb", "/v"],
                             capture_output=True, text=True, check=False).stdout.split()
        total += int(out[0]) if out else 0
    return total


def postal_counts() -> dict[str, int]:
    """Messages per status in mail server 1's message database (Sent = handed to the relay and accepted)."""
    out = subprocess.run(["docker", "exec", "kxmail-mariadb-1", "mariadb", "-uroot", "-ppostal", "-N", "-e",
                          "select status, count(*) from `postal-server-1`.messages group by status"],
                         capture_output=True, text=True, check=False).stdout
    return {ln.split("\t")[0]: int(ln.split("\t")[1]) for ln in out.splitlines() if "\t" in ln}


def postal() -> dict[str, Any]:
    """Postal 3.3.4 + MariaDB, set up as an operator would (postal.yml: web bound to 0.0.0.0 because the bench uses
    a bridge network, outgoing mail relayed to the Haraka sink). bench_setup.rb creates admin user, organisation,
    mail server, verified domain and SMTP credential (the web UI's records). 1,000 quote emails are then sent
    with SMTP auth; the worker signs them and delivers each to the relay. Haraka is excluded from Postal's numbers."""
    files = [str(C / "r6-email.yml")]
    dc("kxmail", files, "--profile", "postal", "down", "-v")
    init = dc("kxmail", files, "--profile", "postal", "run", "--rm", "postal-web", "postal", "initialize", out=True)
    dc("kxmail", files, "--profile", "postal", "up", "-d")
    ready = wait_http("http://localhost:25000/login", 600)
    setup = dc("kxmail", files, "--profile", "postal", "run", "--rm", "--no-deps", "postal-web", "sh", "-c",
               "cd /opt/postal/app && bin/rails runner /config/bench_setup.rb", out=True)
    key = re.search(r"SMTP_KEY=(\S+)", setup)
    if not key:
        return save("r6_postal", {"error": "provisioning failed", "setup_tail": setup[-800:]})
    time.sleep(60)
    names = [n for n in names_of("kxmail") if "haraka" not in n]
    idle = ram_now(names)
    db0 = volume_total("kxmail")  # postaldb is the only volume: main + message databases
    s = Sampler("kxmail", "r6_postal")
    t0 = time.time()
    send = smtp_load(2527, "r6_postal", conns=10,
                     extra=["--sender", "sales@bench.test", "--user", "bench/quotes", "--password", key.group(1)])
    counts: dict[str, int] = {}
    while time.time() - t0 < 900:  # until the worker has handed every message to the relay
        counts = postal_counts()
        if counts.get("Sent", 0) >= send.get("sent", 0) and sum(counts.values()) >= send.get("sent", 0):
            break
        time.sleep(3)
    all_sent_s = round(time.time() - t0, 1)
    time.sleep(5)
    load = s.stop(names)
    ram_after = ram_now(names)
    db1 = volume_total("kxmail")
    dc("kxmail", files, "--profile", "postal", "down", "-v")
    return save("r6_postal", {"ready_s": ready, "initialize_tail": init[-400:], "ram_idle_mib": idle,
                              "ram_idle_total_mib": round(sum(idle.values()), 1), "load": load,
                              "ram_after_mib": ram_after, "send": send, "status_counts": counts,
                              "accepted_to_all_sent_s": all_sent_s, "db_growth_bytes": db1 - db0,
                              "db_per_1000_mails_mb": round((db1 - db0) / 1e6 * 1000 / max(1, counts.get("Sent", 0)), 1),
                              "note": "SMTP auth + DKIM sign + store raw message in MariaDB + deliver to relay sink"})


# ------------------------------------------------------------------ Round 7: calls
def call_server(name: str, target: str) -> dict[str, Any]:
    files = [str(C / "r7-calls.yml")]
    dc("kxcalls", files, "--profile", name, "up", "-d")
    time.sleep(25)
    names = names_of("kxcalls")
    idle = ram_now(names)
    s = Sampler("kxcalls", f"r7_{name}")
    t0 = time.time()
    env = dict(os.environ, TARGET=target, CALLS="10", TOTAL="10", HOLD_MS="60000", TAG=name,
               DIAL="5000" if name == "freeswitch" else "1000")
    out = dc("kxcalls", files, "--profile", "run", "run", "--rm", "sipp", env=env, out=True)
    wall = round(time.time() - t0, 1)
    load = s.stop(names)
    dc("kxcalls", files, "--profile", name, "down", "-v")
    stat: dict[str, str] = {}
    f = RES / f"sipp_{name}.csv"
    if f.exists():
        rows = list(csv.reader(f.open(), delimiter=";"))
        if len(rows) >= 2:
            stat = {k: v for k, v in zip(rows[0], rows[-1]) if k.startswith(("SuccessfulCall", "FailedCall",
                                                                              "CallRate", "Retransmissions"))}
    return save(f"r7_{name}", {"ram_idle_mib": idle, "load": load, "sipp_wall_s": wall, "sipp_stats": stat,
                               "sipp_tail": out[-800:],
                               "note": "10 simultaneous SIP calls, 60 s each, G.711 audio both ways (answer + echo)"})


# ------------------------------------------------------------------ Round 8: firewall
def crowdsec() -> dict[str, Any]:
    files = [str(C / "r8-firewall.yml")]
    dc("kxfw", files, "--profile", "crowdsec", "down", "-v")
    s = Sampler("kxfw", "r8_crowdsec")
    t0 = time.time()
    dc("kxfw", files, "--profile", "crowdsec", "up", "-d")
    table = ""
    while time.time() - t0 < 600:  # in "cat" mode CrowdSec reads the whole file, then logs "Acquisition is finished"
        time.sleep(2)
        m = subprocess.run(["docker", "exec", "kxfw-crowdsec-1", "cscli", "metrics", "show", "acquisition"],
                           capture_output=True, text=True, check=False).stdout
        table = next((ln for ln in m.splitlines() if "access.log" in ln), table)
        logs = subprocess.run(["docker", "logs", "kxfw-crowdsec-1"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)
        if "Acquisition is finished" in logs.stdout + logs.stderr:
            break
    names = ["kxfw-crowdsec-1"]
    ram_after = ram_now(names)
    load = s.stop(names)
    text = logs.stdout + logs.stderr
    stamps = {k: re.search(rf'time="([^"]+)"[^\n]*{k}', text) for k in ("loading acquisition file", "Acquisition is finished")}
    secs = None
    if all(stamps.values()):
        from datetime import datetime

        a, b = (datetime.fromisoformat(stamps[k].group(1).replace("Z", "+00:00")) for k in stamps)  # type: ignore[union-attr]
        secs = round((b - a).total_seconds(), 1)
    cols = [c.strip() for c in table.split("|") if c.strip()]
    dc("kxfw", files, "--profile", "crowdsec", "down", "-v")
    return save("r8_crowdsec", {
        "acquisition_row": cols,  # source | lines read | parsed | unparsed | poured to bucket | whitelisted
        "parse_seconds_from_log": secs,
        "lines_per_second": round(100_000 / secs) if secs else None,
        "load": load, "ram_after_mib": ram_after,
        "note": "test log uses private 10.x addresses, which CrowdSec whitelists by default, so scenario work was small"})


def bunkerweb() -> dict[str, Any]:
    r1 = ["docker", "compose", "-p", "kxbench", "-f", str(C / "r1-app.yml")]
    env3 = dict(os.environ, WORKER_CONCURRENCY="3")
    subprocess.run([*r1, "up", "-d", "--no-deps", "postgres", "redis", "fakes", "phoenix"], env=env3,
                   capture_output=True, check=False)
    time.sleep(10)
    subprocess.run([*r1, "up", "-d", "--no-deps", "api", "worker", "beat"], env=env3, capture_output=True, check=False)
    wait_http("http://localhost:28000/healthz")
    files = [str(C / "r8-firewall.yml")]
    dc("kxfw", files, "--profile", "bunkerweb", "up", "-d")
    ready = wait_http("http://localhost:28088/healthz", 600)
    time.sleep(30)
    names = names_of("kxfw")
    idle = ram_now(names)
    res: dict[str, Any] = {"ready_s": ready, "ram_idle_mib": idle}
    for tag, url in (("direct", "http://localhost:28000"), ("via_bunkerweb", "http://localhost:28088")):
        s = Sampler("kxfw", f"r8_bw_{tag}")
        out = RES / f"r8_load_{tag}.json"
        subprocess.run([PY, str(BENCH / "scripts" / "load10.py"), "run", "--minutes", "5", "--out", str(out)],
                       env=dict(os.environ, BENCH_API_URL=url), capture_output=True, check=False)
        res[tag] = {"bunkerweb_load": s.stop(names)}
        if out.exists():
            rep = json.loads(out.read_text(encoding="utf-8"))
            res[tag]["actions"] = {k: {kk: v.get(kk) for kk in ("count", "codes", "p50_s", "p95_s")}
                                   for k, v in rep["actions"].items() if not k.startswith("_")}
            res[tag]["errors"] = rep["actions"].get("_error_samples")
    dc("kxfw", files, "--profile", "bunkerweb", "down", "-v")
    subprocess.run([*r1, "stop"], capture_output=True, check=False)
    return save("r8_bunkerweb", res)


def safeline() -> dict[str, Any]:
    files = [r"F:\kx-bench\repos\SafeLine\compose.yaml"]
    env_file = r"F:\kx-bench\safeline\.env"
    up = dc("kxsafeline", files, "up", "-d", out=True, env_file=env_file)
    try:
        ready = wait_http("https://localhost:29443/", 600)
    except TimeoutError as exc:
        ready = repr(exc)
    time.sleep(90)
    names = names_of("kxsafeline")
    idle = ram_now(names)
    dc("kxsafeline", files, "down", env_file=env_file)
    return save("r8_safeline", {"ready_s": ready, "up_tail": up[-500:], "ram_idle_mib": idle,
                                "ram_idle_total_mib": round(sum(idle.values()), 1),
                                "note": "idle only (its own compose): protecting a site needs setup in its web UI"})


def safeline_proxy() -> dict[str, Any]:
    """SafeLine 9.2.7 protecting the platform API. Needs (done once, recorded in R6_R8_RESULTS.md): the r1 app stack
    and SafeLine's own compose up, admin password from `docker exec safeline-mgt resetadmin`, and in its web UI an
    Application: HTTP port 28089, upstream http://<api container IP>:8000, default protection.
    SafeLine's proxy (tengine) uses network_mode: host, which on Docker Desktop lives inside Docker's VM, so the
    10-user load runs from a host-network container for BOTH runs (direct :28000 and via SafeLine :28089)."""
    names = names_of("kxsafeline")
    idle = ram_now(names)
    res: dict[str, Any] = {"ram_idle_mib": idle, "ram_idle_total_mib": round(sum(idle.values()), 1)}
    for tag, url in (("direct", "http://127.0.0.1:28000"), ("via_safeline", "http://127.0.0.1:28089")):
        s = Sampler("safeline", f"r8_sl_{tag}")  # container names are safeline-*, not kxsafeline-*
        subprocess.run(["docker", "run", "--rm", "--network", "host", "-e", f"BENCH_API_URL={url}",
                        "-e", "BENCH_FAKES_URL=http://127.0.0.1:29000",
                        # results at /bench/results: load10 keeps the staff login (TOTP) state there
                        "-v", f"{BENCH / 'scripts'}:/bench/scripts:ro", "-v", f"{RES}:/bench/results",
                        "--entrypoint", "/app/.venv/bin/python", "kxbench/app:local",
                        "/bench/scripts/load10.py", "run", "--minutes", "5", "--out",
                        f"/bench/results/r8_sl_load_{tag}.json"],
                       capture_output=True, check=False)
        res[tag] = {"safeline_load": s.stop(names)}
        out = RES / f"r8_sl_load_{tag}.json"
        if out.exists():
            rep = json.loads(out.read_text(encoding="utf-8"))
            res[tag]["actions"] = {k: {kk: v.get(kk) for kk in ("count", "codes", "p50_s", "p95_s")}
                                   for k, v in rep["actions"].items() if not k.startswith("_")}
            res[tag]["errors"] = rep["actions"].get("_error_samples")
    res["ram_after_mib"] = ram_now(names)
    return save("r8_safeline_proxy", res)


STEPS = {"haraka": haraka, "safeline_proxy": safeline_proxy, "stalwart": stalwart, "postal": postal,
         "asterisk": lambda: call_server("asterisk", "asterisk:5060"),
         "freeswitch": lambda: call_server("freeswitch", "freeswitch:5080"),
         "crowdsec": crowdsec, "bunkerweb": bunkerweb, "safeline": safeline}


def main() -> None:
    summary_path = RES / "r678_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    for name in sys.argv[1:] or list(STEPS):
        try:
            res = STEPS[name]()
            summary[name] = {k: v for k, v in res.items() if k not in ("sipp_tail", "initialize_tail", "up_tail")}
        except Exception as exc:  # noqa: BLE001 - recorded, the next step still runs
            summary[name] = {"error": repr(exc)}
        summary_path.write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
        print(name, json.dumps(summary[name], ensure_ascii=False)[:600], flush=True)


if __name__ == "__main__":
    main()
