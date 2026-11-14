"""Send N quote-style emails (HTML body + a 52 KB PDF, the size of a real quote PDF measured in Round 1) to an
SMTP server, 10 connections at once, all to a local test domain (nothing leaves the PC).

  python smtp_send.py --host localhost --port 2525 --count 1000 --to buyer@bench.test [--user U --password P]
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path

PDF = b"%PDF-1.7\n" + os.urandom(52_000)
BODY = ("<p>Dear buyer,</p><p>Please find attached our quotation for aluminium containers 750 ml with lids. "
        "The quotation is valid for 7 days.</p><p>Arifa Pack sales team</p>") * 3


def build(n: int, sender: str, to: str) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, to, f"Quotation Q-2026-{n:05d}"
    m.set_content("Please see the attached quotation.")
    m.add_alternative(BODY, subtype="html")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename=f"Q-2026-{n:05d}.pdf")
    return m


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=2525)
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--to", default="buyer@bench.test")
    p.add_argument("--sender", default="sales@bench.test")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--out", type=Path)
    # parallel SMTP connections; Stalwart's default allows only 5 at once from one sending IP and closes the rest
    p.add_argument("--conns", type=int, default=10)
    # messages per SMTP session before reconnecting (Stalwart's default allows 10 per session, then answers 452)
    p.add_argument("--per-session", type=int, default=0, help="0 = one session per connection")
    a = p.parse_args()
    size = len(build(0, a.sender, a.to).as_bytes())

    def worker(ids: list[int]) -> list[float]:
        lat = []
        step = a.per_session or len(ids) or 1
        for i in range(0, len(ids), step):
            with smtplib.SMTP(a.host, a.port, timeout=60) as s:
                if a.user:
                    s.login(a.user, a.password or "")
                for n in ids[i : i + step]:
                    t = time.perf_counter()
                    s.send_message(build(n, a.sender, a.to))
                    lat.append(time.perf_counter() - t)
        return lat

    chunks = [list(range(k, a.count, a.conns)) for k in range(a.conns)]
    t0 = time.time()
    errors = 0
    lat: list[float] = []
    with ThreadPoolExecutor(a.conns) as ex:
        for f in [ex.submit(worker, c) for c in chunks]:
            try:
                lat += f.result()
            except Exception as exc:  # noqa: BLE001 - count and report
                errors += 1
                print("worker error:", type(exc).__name__, exc)
    res = {"start": t0, "end": time.time(), "sent": len(lat), "worker_errors": errors, "message_bytes": size,
           "connections": a.conns, "per_session": a.per_session,
           "per_message_s_p50": round(statistics.median(lat), 4) if lat else None,
           "per_message_s_max": round(max(lat), 3) if lat else None,
           "messages_per_s": round(len(lat) / (time.time() - t0), 1)}
    if a.out:
        a.out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res))


if __name__ == "__main__":
    main()
