# Rounds 6–8 results: free email, call and firewall servers (14 Nov 2026)

Source data: `results/r6_*.json`, `r7_*.json`, `r8_*.json`, `r678_summary.json`, `sipp_*.csv`, `*_stats.csv`.
Script: `scripts/r678_driver.py`; compose files `compose/r6-email.yml`, `r7-calls.yml`, `r8-firewall.yml`.
Each server ran alone from its official image (SafeLine from its own compose file).

> **Corrected 14 Nov.** The "while working" RAM and CPU totals in the first version were double the real values:
> `docker stats` streams every frame twice and the bench's `window()` summed both copies. All rows below are
> recomputed from the same raw CSVs, counting each container once (`scripts/recompute_windows.py`,
> `results/recompute_windows_report.json`). Idle RAM (a single `docker stats` reading) and every work result
> (emails, calls, lines, blocked requests, latency) were not affected.

## Round 6: email

| Server | What was measured | RAM idle | RAM / CPU while working | Result |
|---|---|---|---|---|
| **Haraka 3** (node:22) | 1,000 quote emails (HTML + 52 KB PDF, 72 KB each), 10 connections, accept + discard | 91 MB | 120 MB avg / 127 MB peak; 0.18 core avg, 0.32 peak | **1,000 accepted in 8.4 s (119 per s)**, 0 errors, 83 ms per message p50 |
| **Stalwart v1.1** (mail server: receives and stores) | 1,000 quote emails to one mailbox, 10 connections, after its web setup wizard | 85 MB (17–23 MB before setup) | 202 MB avg / 228 MB peak (225 MB after); 0.22 core avg, 0.83 peak | **1,000 stored in 11 s (90 per s)**, counted in the mailbox over JMAP; 87 ms per message p50; **store grew 142 MB (≈ 142 KB per 72 KB email**, measured right after, before RocksDB compaction; includes the full-text index) |
| **Postal 3.3.4** (+ MariaDB; sending platform) | 1,000 quote emails with SMTP auth, 10 connections; Postal signs (DKIM) and delivers each to a local relay (Haraka sink) | 455 MB, 4 containers (web 118, worker 119, SMTP 114, MariaDB 104) | 613 MB avg / **652 MB peak**; 0.71 core avg, 1.32 peak | **1,000 accepted at 44 per s, all 1,000 "Sent" (delivered to the relay) 39 s after the first**; 210 ms per message p50; **database grew 97 MB per 1,000** (raw message kept) |

Haraka only receives and discards. A server that keeps mail grows: Stalwart ≈ 142 KB per 72 KB quote email, Postal
≈ 97 KB per sent email. At Heavy (40,000 out + 8,000 in a month) that is ≈ 3.9 GB a month in Postal for the sent
mail (40,000 × 97 KB) and ≈ 1.1 GB a month in Stalwart for the received mail (8,000 × 142 KB), unless their
retention is set (both have one).

Defaults that matter (found by the load, not changed in Postal; in Stalwart only for the capacity test):
- **Stalwart limits each sending IP**: 5 connections per second, **25 messages per hour per sender-domain +
  recipient**, and 10 messages per SMTP session. With them on, the first run stored 25 messages and then answered
  `452 4.4.5 Rate limit exceeded`. For real inbound mail (many senders) these are sensible; if Stalwart ever
  receives the platform's own bulk mail (one IP, one recipient), they must be raised. The bench switched the two
  throttles off (`x:MtaInboundThrottle/set`) and kept the 10-per-session limit (the sender reconnects).
- **Postal's web server binds to 127.0.0.1 by default** (`web_server.default_bind_address`); its official compose
  uses `network_mode: host`. In a normal Docker network the UI never answers until it is set to `0.0.0.0`.

## Round 7: calls (SIP)

| Server | Test | RAM idle | RAM / CPU with 10 calls | Result |
|---|---|---|---|---|
| **Asterisk** (andrius/asterisk) | SIPp `uac_pcap`: 10 simultaneous calls, G.711 a-law audio both ways (answer + echo) | 49 MB | 51 MB avg / 52 MB peak (≈ 0.3 MB per call); 0.05 core avg, 0.17 peak | **10 of 10 calls successful**, 0 failed, 0 retransmissions |
| **FreeSWITCH 1.11.3** (safarov/freeswitch) | same (number 5000 on the public profile, answer + echo) | 41 MB | 45 MB avg / 50 MB peak (≈ 0.9 MB per call); 0.03 core avg, 0.05 peak | **10 of 10 calls successful**, 0 failed, 0 retransmissions; audio verified both ways (one probe call: 76 kB in, 70 kB echoed back) |

FreeSWITCH note: the stock image finds its "external" address by STUN and puts the PC's **public IP** in the SDP,
so callers inside the same network send their audio out to the internet. The bench set `external_rtp_ip` and
`external_sip_ip` to the container address (`vars.xml`). Any self-hosted FreeSWITCH behind NAT needs these set.

Note: SIPp's built-in `uac_pcap` scenario plays its 8-second G.711 sample and hangs up, so each call carried about
8–10 s of two-way audio (not the 60 s hold the script asked for). G.711 is ≈ 80 kbit/s each way per call.

## Round 8: firewall / bot protection

| Tool | Test | RAM | CPU | Result |
|---|---|---|---|---|
| **CrowdSec v1.7.3** | parse 100,000 nginx-format web-log lines (`mode: cat`) | 100 MB avg, 159 MB peak, 158 MB after | 0.91 core avg, 1.32 peak | **100,000 lines in 39 s (2,564 lines/s)**; 96.9k whitelisted (the test log uses private 10.x addresses, which CrowdSec trusts by default, so detection work was small) |
| **BunkerWeb 1.6.6** (ModSecurity + OWASP CRS) in front of the platform API, 10-user load for 5 min direct vs through it | 290 MB idle; 425 MB avg / 472 MB peak while proxying | 0.05 core avg, 0.43 peak | **+4–6 ms per CRM request** (deals list p50 14 → 20 ms, deal 5 → 9 ms, inbox 7 → 11 ms); website replies unchanged (the model dominates) |
| ↳ **blocked traffic** | | | | **89 of 89 WhatsApp webhooks blocked (403)** and **2 of 93 website chat messages** (false positives on normal buyer text). **Cause, from BunkerWeb's own log:** OWASP CRS 4.20.0 rule **930120** ("OS File Access Attempt", paranoia level 1) matches `.profile` in the JSON field name `entry[0].changes[0].value.contacts[0].profile.name`, which every WhatsApp webhook from Meta contains; the anomaly score reaches 5 and rule 949110 blocks it. Fix: exclude 930120 for `ARGS_NAMES` on `/webhooks/whatsapp` |
| **SafeLine 9.2.7** (7 containers, its own compose), protecting the platform API: one Application set up in its web UI (HTTP 28089 → API, default protection); same 10-user load for 5 min direct vs through it | 1,618 MiB idle (≈ 1.58 GiB; tengine 1.20 GB, detector 111 MB, mgt 106 MB, fvm 77 MB, pg 59 MB, chaos 46 MB, luigi 36 MB); 1,629 MiB avg / 1,639 MiB peak while protecting | 0.03 core avg, 0.12 peak | **no added delay** (CRM p50 within ±1 ms: deals 9 → 10 ms, deal 6 → 6, inbox 8 → 8, timeline 5 → 5); **0 false blocks**: 86 of 86 WhatsApp webhooks and 95 of 95 answered chat messages passed (the 6 HTTP 500 are problem 1, the same in the direct run) |

Attack test, same requests straight to the API and through each WAF (`scripts/waf_probe.py`,
`results/r8_waf_probe_*.json`):

| Request | Direct | BunkerWeb (833 CRS rules) | SafeLine |
|---|---|---|---|
| SQL injection `?id=1' OR '1'='1` | 200 | **403** | **403** |
| Script injection `?q=<script>alert(1)</script>` | 200 | **403** | **403** |
| Path traversal `?file=../../../../etc/passwd` | 200 | **403** | **403** |
| Health check | 200 | 200 | 200 |
| Signed WhatsApp webhook (en) | 200 | **403 (false block)** | 200 |
| Signed WhatsApp webhook (ar) | 200 | **403 (false block)** | 200 |

Both stop the three textbook attacks; only SafeLine lets the platform's real traffic through unchanged. SafeLine
costs ≈ 1.2 GB more RAM. Note: BunkerWeb answers before its rule set is loaded (≈ 1 min after start it had
"rules loaded 0", then 833); the first probe pass hit that window and passed everything, so it was repeated.
SafeLine's proxy uses `network_mode: host`, which on Docker Desktop sits inside Docker's VM, so its load and probes
ran from a host-network container (both direct and protected runs, same place).

## Bench notes (my mistakes, fixed or stated)

- SIPp first had no audio files: Debian's `sip-tester` ships neither `pcap/g711a.pcap` nor `pcap/dtmf_2833_1.pcap`;
  both were fetched from github.com/SIPp/sipp and mounted. The first two call runs were invalid (no calls).
- CrowdSec first exited at start: the image requires a volume on `/var/lib/crowdsec/data`; added. Its file reader
  tails only new lines by default; the bench sets `mode: cat`.
- Stalwart's driver step looked for an older log line for the admin password; v1.1 prints a different banner and
  does not start SMTP at all before web setup. **Fixed 14 Nov**: the 5-step setup wizard was completed in its web UI
  with a headless browser (hostname mail.bench.test, domain bench.test, ACME off, RocksDB, internal directory) and
  the buyer mailbox was created with the JMAP management call `x:Account/set`. The compose file first mounted the
  volume on `/opt/stalwart`; the image keeps data in `/etc/stalwart` and `/var/lib/stalwart`, so the volumes were
  moved there (otherwise store growth would read 0).
- **FreeSWITCH "not routed" was my compose bug, not FreeSWITCH**: `-s $${DIAL:-1000}-r 2` had no space, so SIPp
  read `2` as a second target and placed no call. Fixed; the Asterisk run predates the `DIAL` change and is valid.
- **Postal "web UI never answered" was the 127.0.0.1 bind** above. Fixed in `postal.yml`; outgoing mail is relayed
  to Haraka (`smtp_relays`). `F:/kx-bench/email/postal/bench_setup.rb` creates admin user, organisation, mail
  server, verified domain and SMTP credential with `rails runner` (the records the web UI creates).
- **Leftover samplers**: stopping `sample.py` killed Python but not its `docker stats` child on Windows; 28 had
  piled up by 14 Nov. They read container stats from Docker (not inside the containers), so container RAM/CPU
  figures are not affected, but the host had extra load. Fixed in `r678_driver.py` (kills the process tree).
- **SafeLine setup**: admin password from `docker exec safeline-mgt resetadmin`; its console has a self-signed
  certificate the headless browser refused, so a bench-only local HTTP forwarder (scratchpad) let the browser use
  the real web UI. The UI first shows Chaitin's software licence agreement; it was accepted to run this local test
  (its text says installing and using SafeLine already means acceptance). The first SafeLine load run had no staff
  user (the load container could not see `results/staff_state.json`); it is kept as
  `r8_safeline_proxy_run1_nostaff.json` and the run above is the second, complete one.
- **Doubled totals** (the correction note at the top): every `*_stats.csv` holds each (time, container) row twice,
  identical, because `docker stats` streams each frame twice. `window()` (Rounds 5–8) summed per timestamp and so
  doubled RAM and CPU. Fixed in `r5_driver.window()`; old values kept as `load_before_dedupe_fix` in each JSON.
  Rounds 1–4 take per-container averages and peaks, which identical duplicates do not change.
