"""Send the same requests through a WAF and straight to the API: 3 textbook attacks (must be blocked) and 3
normal platform requests (must pass), including a correctly signed WhatsApp webhook built exactly like load10.py.

  python waf_probe.py --direct http://127.0.0.1:28000 --waf http://127.0.0.1:28088 --name bunkerweb
  -> results/r8_waf_probe_<name>.json
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load10 import TEXTS, WA_SECRET, wa_payload  # noqa: E402

RES = Path(__file__).resolve().parents[1] / "results"
ATTACKS = {"sql injection": "/healthz?id=1%27%20OR%20%271%27=%271",
           "script injection (XSS)": "/healthz?q=%3Cscript%3Ealert(1)%3C/script%3E",
           "path traversal": "/healthz?file=../../../../etc/passwd"}


def normal_requests() -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {"health check": {"method": "GET", "url": "/healthz"}}
    for lang in ("en", "ar"):
        raw = wa_payload("971501234567", "Probe buyer", TEXTS[lang][0])
        sig = "sha256=" + hmac.new(WA_SECRET, raw, hashlib.sha256).hexdigest()
        out[f"signed WhatsApp webhook ({lang})"] = {"method": "POST", "url": "/webhooks/whatsapp", "content": raw,
                                                     "headers": {"X-Hub-Signature-256": sig,
                                                                 "Content-Type": "application/json"}}
    return out


def send(base: str, req: dict[str, object]) -> int:
    with httpx.Client(base_url=base, timeout=30) as c:
        r = c.request(str(req["method"]), str(req["url"]), content=req.get("content"),  # type: ignore[arg-type]
                      headers=req.get("headers"))  # type: ignore[arg-type]
        return r.status_code


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--direct", required=True)
    p.add_argument("--waf", required=True)
    p.add_argument("--name", required=True)
    a = p.parse_args()
    res: dict[str, dict[str, int]] = {}
    for label, url in ATTACKS.items():
        req = {"method": "GET", "url": url}
        res[f"attack: {label}"] = {"direct": send(a.direct, req), "via_waf": send(a.waf, req)}
    for label, req in normal_requests().items():
        res[f"normal: {label}"] = {"direct": send(a.direct, req), "via_waf": send(a.waf, req)}
    (RES / f"r8_waf_probe_{a.name}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    for k, v in res.items():
        print(f"{k:45s} direct {v['direct']}  via {a.name} {v['via_waf']}")


if __name__ == "__main__":
    main()
