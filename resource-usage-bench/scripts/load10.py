"""Fake traffic for the resource-usage bench: real HTTP calls to the platform's real endpoints.

  python load10.py cms   --products 100 --locales en,ar,hi,ta,zh,si   # publish a catalogue (re-index cost)
  python load10.py run   --minutes 15 --web 5 --wa 4 --staff 1 --out results/r1_load.json

Users in `run` (10 by default), each with 5-15 s pauses like a person:
* website visitors: start a chat session (own IP via X-Forwarded-For), 4-8 messages, then a new session;
* WhatsApp buyers: messages arrive as Meta-signed webhooks (the AI reply runs in the Celery worker);
* staff: signs in with password + authenticator (TOTP), reads deals, creates and approves manual quotes
  (approval -> PDF -> send), the CRM side of the flow.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import random
import statistics
import struct
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

API = os.environ.get("BENCH_API_URL", "http://localhost:28000")
FAKES = os.environ.get("BENCH_FAKES_URL", "http://localhost:29000")
WA_SECRET = b"bench-wa-secret"
CMS_SECRET = b"bench-cms-secret"
STAFF_EMAIL = os.environ.get("BENCH_STAFF_EMAIL", "bench.mgr2@bench.test")
STATE = Path(__file__).resolve().parents[1] / "results" / "staff_state.json"
LIST_PRODUCT_ID = "00000000-0000-7000-8000-0000000000b2"

TEXTS = {
    "en": ["Hello, I need aluminium containers for my restaurant", "Do you have 750 ml containers with lids?",
           "We need about 20,000 pieces per month", "Can you deliver to Muscat?", "Can you print our logo?",
           "What is the lead time?", "Please send me the datasheet", "Thank you, please share an offer"],
    "ar": ["مرحبا، أحتاج حاويات ألمنيوم لمطعمي", "هل لديكم حاويات 750 مل مع أغطية؟", "نحتاج حوالي عشرين ألف قطعة شهريا",
           "هل يمكنكم التوصيل إلى مسقط؟", "هل يمكن طباعة الشعار؟", "شكرا، أرسلوا العرض من فضلكم"],
    "hi": ["नमस्ते, मुझे अपने रेस्टोरेंट के लिए एल्यूमिनियम कंटेनर चाहिए", "क्या ढक्कन वाले 750 ml कंटेनर हैं?",
           "हमें हर महीने लगभग बीस हज़ार पीस चाहिए", "धन्यवाद, कृपया ऑफ़र भेजें"],
    "ta": ["வணக்கம், எனக்கு அலுமினிய கொள்கலன்கள் வேண்டும்", "மூடியுடன் 750 ml கொள்கலன்கள் உள்ளதா?",
           "நன்றி, தயவுசெய்து விலைப்புள்ளி அனுப்பவும்"],
    "zh": ["你好，我需要餐厅用的铝箔餐盒", "有带盖的750毫升餐盒吗？", "我们每月大约需要两万个", "谢谢，请发报价"],
}


class Metrics:
    def __init__(self) -> None:
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.codes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.bytes_up = 0
        self.bytes_down = 0
        self.errors: dict[str, list[str]] = defaultdict(list)

    async def call(self, client: httpx.AsyncClient, name: str, method: str, url: str, **kw: Any) -> httpx.Response | None:
        t = time.perf_counter()
        try:
            r = await client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            self.codes[name][type(exc).__name__] += 1
            return None
        self.lat[name].append(time.perf_counter() - t)
        self.codes[name][str(r.status_code)] += 1
        if r.status_code >= 400 and len(self.errors[f"{name} {r.status_code}"]) < 2:  # why it failed, for the report
            self.errors[f"{name} {r.status_code}"].append(r.text[:400])
        self.bytes_up += len(r.request.content or b"") + 300
        self.bytes_down += len(r.content) + 200
        return r

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, xs in sorted(self.lat.items()):
            s = sorted(xs)
            out[name] = {
                "count": len(xs),
                "codes": dict(self.codes[name]),
                "p50_s": round(statistics.median(s), 3),
                "p95_s": round(s[min(len(s) - 1, max(0, math.ceil(len(s) * 0.95) - 1))], 3),
                "max_s": round(s[-1], 3),
            }
        for name, codes in self.codes.items():
            out.setdefault(name, {"count": 0, "codes": dict(codes)})
        out["_client_bytes"] = {"up": self.bytes_up, "down": self.bytes_down}
        out["_error_samples"] = dict(self.errors)
        return out


def totp(secret_b32: str, at: float | None = None) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((at or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


async def wait_next_window() -> None:
    await asyncio.sleep(31 - (time.time() % 30))


# ------------------------------------------------------------------ staff sign-in
async def staff_login(client: httpx.AsyncClient, m: Metrics, attempt: int = 0) -> str:
    """Password + second factor. First run: change the one-time password, enrol TOTP. Returns access token.
    Gives up after 3 TOTP retries (a server error must end the run, not loop forever)."""
    if attempt > 3:
        raise RuntimeError("staff TOTP login keeps failing")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if not state:
        line = (STATE.parent / "r1_staff_user.txt").read_text().split("temporary password:")[1].split()[0]
        state = {"password": line}
    r = await m.call(client, "auth.login", "POST", "/v1/auth/login", json={"email": STAFF_EMAIL, "password": state["password"]})
    assert r is not None and r.status_code == 200, r and r.text
    challenge = r.json()["challenge_token"]
    if state.get("totp_secret"):
        r = await m.call(client, "auth.totp", "POST", "/v1/auth/mfa/totp",
                         json={"challenge_token": challenge, "code": totp(state["totp_secret"])})
        if r is not None and r.status_code != 200:  # same 30 s code already used: wait for the next one
            await wait_next_window()
            return await staff_login(client, m, attempt + 1)
        assert r is not None
        return str(r.json()["access_token"])
    await m.call(client, "auth.email_send", "POST", "/v1/auth/mfa/email/send", json={"challenge_token": challenge})
    code = (await client.get(f"{FAKES}/_codes/{STAFF_EMAIL}")).json()["code"]
    r = await m.call(client, "auth.email_verify", "POST", "/v1/auth/mfa/email/verify",
                     json={"challenge_token": challenge, "code": code})
    assert r is not None and r.status_code == 200, r and r.text
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    new_pw = "Bench-" + uuid.uuid4().hex
    r = await m.call(client, "auth.password", "POST", "/v1/auth/password", headers=h,
                     json={"current_password": state["password"], "new_password": new_pw})
    assert r is not None and r.status_code < 300, r and r.text
    state["password"] = new_pw
    STATE.write_text(json.dumps(state))
    return await staff_login_enrol(client, m, state)


async def staff_login_enrol(client: httpx.AsyncClient, m: Metrics, state: dict[str, str]) -> str:
    r = await m.call(client, "auth.login", "POST", "/v1/auth/login", json={"email": STAFF_EMAIL, "password": state["password"]})
    assert r is not None and r.status_code == 200, r and r.text
    challenge = r.json()["challenge_token"]
    await m.call(client, "auth.email_send", "POST", "/v1/auth/mfa/email/send", json={"challenge_token": challenge})
    code = (await client.get(f"{FAKES}/_codes/{STAFF_EMAIL}")).json()["code"]
    r = await m.call(client, "auth.email_verify", "POST", "/v1/auth/mfa/email/verify",
                     json={"challenge_token": challenge, "code": code})
    assert r is not None and r.status_code == 200, r and r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await m.call(client, "auth.totp_enrol", "POST", "/v1/auth/totp/enrol", headers=h)
    assert r is not None and r.status_code == 200, r and r.text
    secret = r.json()["secret"]
    r = await m.call(client, "auth.totp_confirm", "POST", "/v1/auth/totp/confirm", headers=h, json={"code": totp(secret)})
    assert r is not None and r.status_code < 300, r and r.text
    state["totp_secret"] = secret
    STATE.write_text(json.dumps(state))
    await wait_next_window()
    return await staff_login(client, m)


# ------------------------------------------------------------------ users
async def website_user(i: int, end: float, m: Metrics) -> None:
    ip = f"10.20.{i // 250}.{i % 250 + 1}"
    async with httpx.AsyncClient(base_url=API, timeout=90, headers={"X-Forwarded-For": ip}) as c:
        while time.time() < end:
            lang = random.choice(list(TEXTS))
            r = await m.call(c, "web.session", "POST", "/v1/public/sessions",
                             json={"language": lang, "cookie_consent": True, "voice_consent": False})
            if r is None or r.status_code != 201:
                await asyncio.sleep(10)
                continue
            s = r.json()
            h = {"Authorization": f"Bearer {s['session_token']}"}
            for _ in range(random.randint(4, 8)):
                if time.time() >= end:
                    break
                await m.call(c, "web.message", "POST", f"/v1/public/sessions/{s['session_id']}/messages",
                             headers=h, json={"text": random.choice(TEXTS[lang]), "language": lang})
                await asyncio.sleep(random.uniform(5, 15))


def wa_payload(phone: str, name: str, text: str) -> bytes:
    body = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "971400000001", "phone_number_id": "100000000000001"},
            "contacts": [{"wa_id": phone, "profile": {"name": name}}],
            "messages": [{"from": phone, "id": f"wamid.{uuid.uuid4().hex}", "timestamp": str(int(time.time())),
                          "type": "text", "text": {"body": text}}],
        }}]}],
    }
    return json.dumps(body, ensure_ascii=False).encode()


async def whatsapp_buyer(i: int, end: float, m: Metrics) -> None:
    phone = f"971{random.choice([50, 52, 54, 55, 56, 58])}{random.randint(1_000_000, 9_999_999)}"  # real UAE mobile prefixes
    lang = random.choice(list(TEXTS))
    async with httpx.AsyncClient(base_url=API, timeout=30) as c:
        while time.time() < end:
            raw = wa_payload(phone, f"Buyer {i}", random.choice(TEXTS[lang]))
            sig = "sha256=" + hmac.new(WA_SECRET, raw, hashlib.sha256).hexdigest()
            await m.call(c, "wa.webhook", "POST", "/webhooks/whatsapp", content=raw,
                         headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
            await asyncio.sleep(random.uniform(8, 20))


async def staff_user(end: float, m: Metrics) -> None:
    async with httpx.AsyncClient(base_url=API, timeout=90) as c:
        token = await staff_login(c, m)
        token_at = time.time()
        last_quote = 0.0
        while time.time() < end:
            if time.time() - token_at > 780:
                token, token_at = await staff_login(c, m), time.time()
            h = {"Authorization": f"Bearer {token}"}
            r = await m.call(c, "crm.deals", "GET", "/v1/deals", headers=h, params={"limit": 25})
            deals = r.json().get("items", []) if r is not None and r.status_code == 200 else []
            if deals:
                d = random.choice(deals)
                await m.call(c, "crm.deal", "GET", f"/v1/deals/{d['id']}", headers=h)
                await m.call(c, "crm.timeline", "GET", f"/v1/deals/{d['id']}/timeline", headers=h)
                await m.call(c, "crm.quotes", "GET", f"/v1/deals/{d['id']}/quotes", headers=h)
                if time.time() - last_quote > 60:
                    last_quote = time.time()
                    if d["stage"] == "NEW":  # the salesperson qualifies the lead before quoting (§5)
                        await m.call(c, "crm.stage_qualify", "POST", f"/v1/deals/{d['id']}/stage", headers=h,
                                     json={"to": "QUALIFIED", "expected_version": d["version"], "reason": "bench"})
                    line = {"description": "Aluminium container 750 ml + lid", "quantity": 20000,
                            "unit_price": "0.27", "unit": "pcs", "product_id": LIST_PRODUCT_ID}
                    q = await m.call(c, "crm.quote_manual", "POST", f"/v1/deals/{d['id']}/quotes/manual", headers=h,
                                     json={"lines": [line], "delivery_time_text": "10-14 working days"})
                    if q is not None and q.status_code < 300:
                        a = await m.call(c, "crm.approvals", "GET", "/v1/approvals", headers=h, params={"status": "open"})
                        for ap in (a.json().get("items", []) if a is not None and a.status_code == 200 else [])[:3]:
                            await m.call(c, "crm.approve", "POST", f"/v1/approvals/{ap['id']}/decision", headers=h,
                                         json={"decision": "approve", "note": "bench"})
            await m.call(c, "crm.inbox", "GET", "/v1/inbox", headers=h)
            await asyncio.sleep(random.uniform(5, 15))


async def run(minutes: float, web: int, wa: int, staff: int, out: Path) -> None:
    m = Metrics()
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(f"{FAKES}/_reset")
    start = time.time()
    end = start + minutes * 60
    tasks = [website_user(i, end, m) for i in range(web)]
    tasks += [whatsapp_buyer(i, end, m) for i in range(wa)]
    tasks += [staff_user(end, m) for _ in range(staff)]
    errors: list[str] = []
    try:  # hard stop: the window plus 3 minutes for in-flight replies, whatever a user task does
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), minutes * 60 + 180)
        errors = [repr(r) for r in results if isinstance(r, BaseException)]
    except TimeoutError:
        errors = ["hard deadline hit"]
    async with httpx.AsyncClient(timeout=10) as c:
        fakes = (await c.get(f"{FAKES}/_stats")).json()
    report = {"start": start, "end": time.time(), "users": {"web": web, "wa": wa, "staff": staff},
              "actions": m.report(), "fakes": fakes, "task_errors": errors}
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps(report["actions"], indent=1, ensure_ascii=False))


async def cms(products: int, locales: list[str]) -> None:
    m = Metrics()
    async with httpx.AsyncClient(base_url=API, timeout=30) as c:
        for n in range(products):
            body = json.dumps({
                "event_id": str(uuid.uuid4()), "operation": "publish", "collection": "products",
                "doc_id": f"p{n:04d}", "locales": locales, "url": f"/products/p{n:04d}",
                "product": {"code": f"BENCH-{n:04d}", "name": f"Bench container {n}", "category_code": "ALUMINIUM",
                            "spec": {"material": "8011", "capacity_ml": 750, "lid": "paper"}, "custom_printing": True},
            }).encode()
            ts = str(int(time.time()))
            sig = "sha256=" + hmac.new(CMS_SECRET, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
            await m.call(c, "cms.publish", "POST", "/webhooks/cms", content=body,
                         headers={"X-Kx-Timestamp": ts, "X-Kx-Signature": sig, "Content-Type": "application/json"})
    print(json.dumps(m.report(), indent=1))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--minutes", type=float, default=15)
    r.add_argument("--web", type=int, default=5)
    r.add_argument("--wa", type=int, default=4)
    r.add_argument("--staff", type=int, default=1)
    r.add_argument("--out", type=Path, required=True)
    k = sub.add_parser("cms")
    k.add_argument("--products", type=int, default=100)
    k.add_argument("--locales", default="en,ar,hi,ta,zh,si")
    a = p.parse_args()
    if a.cmd == "run":
        asyncio.run(run(a.minutes, a.web, a.wa, a.staff, a.out))
    else:
        asyncio.run(cms(a.products, a.locales.split(",")))


if __name__ == "__main__":
    main()
