"""Build docs/Kairox_Free_Stack_Guide.docx: the complete, detailed guide to every platform the bench ran.

Every results table is generated from results/*.json (measured). Costs come from results/rank_costs.json and
results/users_per_day_costs.json (scripts/rank_costs.py, scripts/users_per_day_costs.py), the repo's price documents,
AWS's price list, Namecheap's configure pages (as indexed 14 Nov 2026) and coolify.io. Run diagrams.py first:
  F:/kx-bench/venv/docbuild/Scripts/python.exe scripts/guide/diagrams.py
  F:/kx-bench/venv/docbuild/Scripts/python.exe scripts/guide/build_guide.py
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BENCH = Path(__file__).resolve().parents[2]
RES = BENCH / "results"
IMG = BENCH / "docs" / "guide_images"
OUT = BENCH / "docs" / "Kairox_Free_Stack_Guide.docx"


def j(name: str) -> dict:
    return json.loads((RES / name).read_text(encoding="utf-8"))


RANK = j("rank_costs.json")
UPD = j("users_per_day_costs.json")
U = UPD["by_users_per_day"]
NAVY = RGBColor(0x24, 0x32, 0x4A)

doc = Document()
for s in doc.sections:
    s.left_margin = s.right_margin = Cm(2.0)
    s.top_margin = s.bottom_margin = Cm(1.8)
st = doc.styles["Normal"]
st.font.name = "Calibri"
st.font.size = Pt(11)
st.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
for lvl, size in ((1, 20), (2, 15), (3, 12.5)):
    hs = doc.styles[f"Heading {lvl}"]
    hs.font.name = "Calibri"
    hs.font.size = Pt(size)
    hs.font.color.rgb = NAVY


# ================================================================================================ helpers
def shade(cell, hex_fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcpr.append(shd)


def runs(p, text: str, size: float | None = None) -> None:
    """Add text with **bold** and `code` markup to paragraph p."""
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not part:
            continue
        if part.startswith("**"):
            r = p.add_run(part[2:-2])
            r.bold = True
        elif part.startswith("`"):
            r = p.add_run(part[1:-1])
            r.font.name = "Consolas"
            r.font.size = Pt(9.5)
        else:
            r = p.add_run(part)
        if size and not part.startswith("`"):
            r.font.size = Pt(size)


def para(text: str, size: float | None = None, italic: bool = False, align=None, space_after: float = 6) -> None:
    p = doc.add_paragraph()
    runs(p, text, size)
    if italic:
        for r in p.runs:
            r.italic = True
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)


def bullets(items: list[str]) -> None:
    for it in items:
        p = doc.add_paragraph(style="List Bullet")
        runs(p, it)
        p.paragraph_format.space_after = Pt(2)


def steps(items: list[str]) -> None:
    for i, it in enumerate(items, 1):
        p = doc.add_paragraph()
        runs(p, f"**{i}.** {it}")
        p.paragraph_format.left_indent = Cm(0.6)
        p.paragraph_format.space_after = Pt(2)


def h(text: str, level: int) -> None:
    doc.add_heading(text, level=level)


def callout(title: str, body: list[str] | str, fill: str = "EAF4FF", mono: bool = False) -> None:
    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    c = t.rows[0].cells[0]
    shade(c, fill)
    r = c.paragraphs[0].add_run(title)
    r.bold = True
    r.font.color.rgb = NAVY
    for line in [body] if isinstance(body, str) else body:
        q = c.add_paragraph()
        if mono:
            rr = q.add_run(line)
            rr.font.name = "Consolas"
            rr.font.size = Pt(9)
        elif re.match(r"^[\u0600-\u06FF]", line):  # an Arabic sentence on its own line: right-to-left
            q.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            q._p.get_or_add_pPr().append(OxmlElement("w:bidi"))
            rr = q.add_run(line)
            rr.font.size = Pt(11)
            rr._r.get_or_add_rPr().append(OxmlElement("w:rtl"))
        else:
            runs(q, line, 10.5)
        q.paragraph_format.space_after = Pt(1)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def table(headers: list[str], rows: list[list], widths: list[float] | None = None, size: float = 9.5,
          fill: str = "24324A") -> None:
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, hd in enumerate(headers):
        c = t.rows[0].cells[i]
        shade(c, fill)
        r = c.paragraphs[0].add_run(hd)
        r.bold = True
        r.font.size = Pt(size)
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for i, val in enumerate(row):
            runs(cells[i].paragraphs[0], str(val), size)
            if ri % 2:
                shade(cells[i], "F3F6FA")
    for row in t.rows:  # never split one row across two pages
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def image(name: str, width_cm: float = 17.0, caption: str | None = None) -> None:
    doc.add_picture(str(IMG / name), width=Cm(width_cm))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    if caption:
        para(caption, size=9, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER)


def page_break() -> None:
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def money(x: float) -> str:
    return f"${x:,.2f}"


def mib(x: float) -> str:
    return f"{x/1024:.2f} GiB" if x >= 1024 else f"{x:.0f} MiB"


EMOJI = re.compile("([\u2190-\u21ff\u2300-\u27bf\u2b00-\u2bff\U0001f000-\U0001faff\ufe0f\u200d]+)")
DEVA = re.compile("[\u0900-\u097f]")
ARAB = re.compile("[\u0600-\u06ff]")


def script_fonts(document) -> None:
    """Hindi runs get Nirmala UI (Calibri has no Devanagari); Arabic runs get right-to-left and Arial as their script font."""

    def fix(p) -> None:
        arabic_start = bool(ARAB.match(p.text.strip()[:1] or " "))
        for r in p.runs:
            if not (DEVA.search(r.text) or ARAB.search(r.text)):
                continue
            rpr = r._r.get_or_add_rPr()
            fonts = rpr.find(qn("w:rFonts"))
            if fonts is None:
                fonts = OxmlElement("w:rFonts")
                rpr.insert(0, fonts)
            if DEVA.search(r.text):
                for k in ("w:ascii", "w:hAnsi", "w:cs"):
                    fonts.set(qn(k), "Nirmala UI")
            if ARAB.search(r.text):
                fonts.set(qn("w:cs"), "Arial")
                if arabic_start and rpr.find(qn("w:rtl")) is None:
                    rpr.append(OxmlElement("w:rtl"))
        if arabic_start and p._p.pPr is not None and p._p.pPr.find(qn("w:bidi")) is None:
            p._p.pPr.append(OxmlElement("w:bidi"))
        elif arabic_start and p._p.pPr is None:
            p._p.get_or_add_pPr().append(OxmlElement("w:bidi"))

    for p in document.paragraphs:
        fix(p)
    for t in document.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    fix(p)


def emoji_fonts(document) -> None:
    """Give emoji their own runs in Segoe UI Emoji (Word does not reliably fall back from Calibri)."""

    def fix(p) -> None:
        for r in list(p.runs):
            if not EMOJI.search(r.text):
                continue
            anchor = r._r
            for s in [x for x in EMOJI.split(r.text) if x]:
                new = copy.deepcopy(r._r)
                for t in new.findall(qn("w:t")):
                    new.remove(t)
                tt = OxmlElement("w:t")
                tt.text = s
                tt.set(qn("xml:space"), "preserve")
                new.append(tt)
                if EMOJI.fullmatch(s):
                    rpr = new.get_or_add_rPr()
                    fonts = rpr.find(qn("w:rFonts"))
                    if fonts is None:
                        fonts = OxmlElement("w:rFonts")
                        rpr.insert(0, fonts)
                    for k in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
                        fonts.set(qn(k), "Segoe UI Emoji")
                anchor.addnext(new)
                anchor = new
            r._r.getparent().remove(r._r)

    for p in document.paragraphs:
        fix(p)
    for t in document.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    fix(p)


# ================================================================================================ data tables
ACTION = {"web.session": "Website chat: open a session", "web.message": "Website chat: buyer message → AI reply",
          "wa.webhook": "WhatsApp: message arrives (webhook)", "auth.login": "Staff login: password",
          "auth.totp": "Staff login: authenticator code", "crm.deals": "CRM: deals list", "crm.deal": "CRM: open one deal",
          "crm.timeline": "CRM: deal timeline", "crm.quotes": "CRM: quotes list", "crm.inbox": "CRM: inbox",
          "crm.approvals": "CRM: approvals list", "crm.approve": "CRM: approve a quote",
          "crm.quote_manual": "CRM: create a quote by hand", "crm.stage_qualify": "CRM: move deal to Qualified"}
ORDER = list(ACTION)


def codes(c: dict) -> str:
    return ", ".join(f"{k}×{v}" for k, v in sorted(c.items()))


def actions_table(actions: dict, note: str = "") -> None:
    rows = []
    for k in ORDER:
        a = actions.get(k)
        if not a:
            continue
        mx = f"{a['max_s']*1000:,.0f} ms" if a.get("max_s") is not None else "—"
        rows.append([ACTION[k], a["count"], codes(a["codes"]), f"{a['p50_s']*1000:,.0f} ms", f"{a['p95_s']*1000:,.0f} ms", mx])
    table(["Action", "Count", "HTTP answers", "p50", "p95", "max"], rows, [6.0, 1.3, 3.2, 2.0, 2.0, 2.0], size=8.5)
    if note:
        para(note, size=9, italic=True)


def compare_actions(direct: dict, via: dict, label: str) -> None:
    rows = []
    for k in ORDER:
        a, b = direct.get(k), via.get(k)
        if not a or not b:
            continue
        d = (b["p50_s"] - a["p50_s"]) * 1000
        rows.append([ACTION[k], f"{a['count']} · {codes(a['codes'])}", f"{b['count']} · {codes(b['codes'])}",
                     f"{a['p50_s']*1000:,.0f} ms", f"{b['p50_s']*1000:,.0f} ms", f"{d:+,.0f} ms"])
    table(["Action", "Direct: count · answers", f"Via {label}: count · answers", "p50 direct", f"p50 via {label}", "Added"],
          rows, [4.6, 3.3, 3.3, 1.9, 1.9, 1.6], size=8)


def containers_table(cont: dict, cpu_pct: bool = False) -> None:
    rows = []
    for n, c in cont.items():
        if n.startswith("callers-run") or n == "fakes":
            continue
        if cpu_pct:
            ca, cp = c.get("cpu_avg_pct", 0) / 100, c.get("cpu_peak_pct", 0) / 100
        else:
            ca, cp = c.get("cpu_avg_cores", 0), c.get("cpu_peak_cores", 0)
        rx, tx = c.get("net_rx_mb", 0), c.get("net_tx_mb", 0)
        rows.append([n, mib(c["ram_avg_mib"]), mib(c["ram_peak_mib"]), f"{ca:.2f}", f"{cp:.2f}",
                     "— (counter reset)" if min(rx, tx) < 0 else f"{rx:.1f} / {tx:.1f}"])
    table(["Container", "RAM avg", "RAM peak", "CPU avg (cores)", "CPU peak (cores)", "Network in / out (MB)"], rows,
          [3.4, 2.3, 2.3, 2.6, 2.6, 3.8], size=8.5)


R1 = j("r1_summary.json")
R2 = {t: j(f"r2_{t}_summary.json") for t in ("cpu", "gpu", "app2")}


def voice_table(tag: str, agent: str, lk: str) -> None:
    rows = []
    for c in ("c1", "c3", "c10"):
        v = R2[tag][c]
        ss = v.get("speech_server", {})
        ct = v["containers"]
        n = int(c[1:])
        rows.append([f"{n} caller{'s' if n > 1 else ''}", f"{v['answered']} of {v['answered'] + v['missed']}",
                     " / ".join("—" if v.get(k) is None else f"{v[k]:.1f}" for k in ("reply_s_p50", "reply_s_p95", "reply_s_max")) + " s",
                     "—" if ss.get("stt_rtf") is None else f"{ss['stt_rtf']:.2f}",
                     "—" if ss.get("tts_rtf") is None else f"{ss['tts_rtf']:.3f}",
                     f"{mib(ct[agent]['ram_peak_mib'])} · {ct[agent]['cpu_peak_cores']:.1f}",
                     f"{mib(ct[lk]['ram_peak_mib'])} · {ct[lk]['net_rx_mb'] + ct[lk]['net_tx_mb']:.1f} MB"])
    table(["Callers", "Sentences answered", "Reply p50 / p95 / max", "STT RTF", "TTS RTF",
           "Agent RAM peak · CPU peak", "LiveKit RAM peak · traffic"], rows, [1.9, 2.0, 3.3, 1.5, 1.5, 3.4, 3.4], size=8.3)


# ================================================================================================ title page
tp = doc.add_paragraph()
tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = tp.add_run("\nThe Kairox Free-Stack Field Guide")
r.bold = True
r.font.size = Pt(30)
r.font.color.rgb = NAVY
t2 = doc.add_paragraph()
t2.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = t2.add_run("Every platform we ran for Arifa Pack, tested almost like real life:\n"
               "what it is, how it works, how it really performed, what it eats, what it costs,\n"
               "and which one we recommend")
r.font.size = Pt(14)
image("00_big_picture.png", 17.5)
para("Measured 14 November 2026 on a test PC (i5-13400F, 10 cores / 16 threads, 15.8 GB RAM, RTX 5060 8 GB) with fake "
     "users and fake paid vendors. Branch `v1-11nov-work-and-architecture-guide`. No platform source code was changed.",
     size=10, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER)
page_break()

# ================================================================================================ contents
h("What is in this guide", 1)
table(["Chapter", "What you get", "Read it when…"], [
    ["How to read", "the badges that tell you where every number came from", "first, 2 minutes"],
    ["1. The story", "one buyer from first click to quote, and which platform does each step", "you want the big picture"],
    ["2. How we tested", "the test bench, the 8 rounds, the fake users and fake vendors", "you want to trust the numbers"],
    ["3. The platforms (A-G)", "a detailed card for each platform: what it is, how it works, how we tested it, full results "
     "tables from the raw files, real output, resource use, problems, paid version, alternatives, verdict", "you want the detail"],
    ["4. Recommended stack", "the best pick for every field, chosen on measured performance, and how each pick performed", "you are deciding"],
    ["5. Options by field", "every paid / free alternative from the repo's price document side by side", "you compare vendors"],
    ["6. The source-code shelf", "the 25 projects in `repos/`", "you need a licence or version"],
    ["7. Things that broke", "12 problems in the platform found under load", "you plan fixes"],
    ["8. The server", "how big it must be, Namecheap plans, Coolify", "you are buying"],
    ["9. Money", "usage and full cost of the PAID, FREE and RECOMMENDED stacks at 50 / 100 / 150 users a day", "you budget"],
], [3.4, 9.6, 4.0])
h("How to read the numbers (2 minutes)", 1)
table(["Badge", "Meaning", "Example"], [
    ["✅ Measured", "We ran it and read the number off the machine", "Stalwart stored 1,000 emails in 11 s"],
    ["💲 Price", "From an official price page or the repo's own price documents", "Deepgram $0.0092 a minute"],
    ["🔢 Computed", "Arithmetic on measured numbers and prices, by a script you can rerun", "SafeLine on AWS Fargate = $18.07/month"],
    ["⚠️ Assumption", "A planning rule we had to choose; stated, and easy to change", "1 user a day = 10 AI turns"],
    ["❓ Not tested", "We did not run it, so we say so instead of guessing", "Pipecat, ALTCHA"],
], [3.2, 7.5, 6.3])
para("**Words you will meet everywhere** (full glossary at the end):")
bullets(["**p50 / p95 / max**: half of the answers were faster than p50, 95 % faster than p95; max is the slowest one.",
         "**RAM idle → peak**: memory used when nothing happens → the highest memory seen during the test.",
         "**CPU in cores**: 1.0 = one processor core busy all the time; 0.05 = 5 % of one core.",
         "**RTF (real-time factor)**: seconds of work per second of audio. 0.04 = 25× faster than talking; above 1.0 = too slow for a live call."])
callout("📚  The whole guide in one breath", [
    "Your platform is a **sales robot with a CRM**. Buyers reach it by website chat, website voice, WhatsApp and email.",
    "It is built from about **24 platforms**; we ran every one under load. Most are **free software**; three things stay "
    "**paid in every option**: **Claude** (writes the replies), **Meta WhatsApp** and **real phone minutes**.",
    f"**Recommended stack** (best measured performance per field): one **Namecheap 64 GB server** with **Coolify**, running "
    f"PostgreSQL, Redis, your app, **BGE-M3**, **Phoenix**, **SafeLine + CrowdSec**, the **LiveKit server** and **Piper**; paid "
    f"**Claude, WhatsApp, Deepgram, SES + Postmark, Telnyx, Vercel**. Cost: **{money(U['50']['stack_hybrid']['total'])} / "
    f"{money(U['100']['stack_hybrid']['total'])} / {money(U['150']['stack_hybrid']['total'])} a month** at 50 / 100 / 150 users "
    f"a day, against **{money(U['50']['stack_paid']['total_low'])} / {money(U['100']['stack_paid']['total_low'])} / "
    f"{money(U['150']['stack_paid']['total_low'])}** for the all-paid AWS plan.",
    "Catches: Namecheap servers are in **Phoenix, USA** (the plan keeps data in the UAE), have **no GPU**, and one server has "
    "no automatic failover.",
], "E6F7F5")

# ================================================================================================ CH1 STORY
h("1. The story: one buyer, start to finish", 1)
para("Meet **Ahmed**, who buys food containers for a restaurant group in Dubai (SRS scenario 1). Every row is a moment of "
     "his journey, the platform that does the work, and what we measured for that step.")
table(["Step", "What Ahmed sees", "Who does the work", "Measured on the bench ✅"], [
    ["1", "Opens the website, presses **Enter the factory**", "Next.js site (Vercel) + **LiveKit** opens a voice line", "LiveKit: 16-90 MiB with 10 callers"],
    ["2", "Hears \"This conversation is with our AI assistant…\"", "**Piper** speaks the recording notice", "first audio 0.11-0.29 s"],
    ["3", "Asks about 750 ml containers with paper lids", "**Silero VAD** + **turn detector** hear him stop; **faster-whisper** writes it down", "0.24 s per sentence on GPU"],
    ["4", "Gets a correct product answer", "**TEI + pgvector** find the facts; **Claude** writes; price guard checks", "search 0.16 s; reply p50 4.7 s (GPU chain)"],
    ["5", "Receives the Requirement Summary on WhatsApp", "**Celery worker** builds and sends it via Meta", "webhook answered in 14 ms"],
    ["6", "Gets a quote PDF by email", "**Chromium** draws it (52 KB); **Postal** or SES sends it", "Postal: 1,000 mails at 44/s"],
    ["7", "Replies to the email", "**Stalwart** receives it; the app attaches it to the deal", "1,000 stored in 11 s"],
    ["8", "(hidden) Sara is phoned when the quote waits 1 hour", "**Celery beat** + **Asterisk** + SIP trunk", "10 of 10 calls"],
    ["9", "(hidden) every AI call is recorded", "**Phoenix** keeps the trace", "1.9 MB per 1,000 spans"],
    ["10", "(hidden) a hacker tries a trick URL", "**SafeLine** says 403; **CrowdSec** bans repeaters", "3 of 3 attacks blocked"],
], [1.0, 4.5, 6.3, 5.2], size=8.8)
image("01_chat_flow.png", 17.5, "One AI turn, from message to reply.")
page_break()

# ================================================================================================ CH2 BENCH
h("2. How we tested (almost like real life)", 1)
para("A number you cannot trust is worse than no number. Everything here was **run** under load, with the real software "
     "from its official image or source code, and every test counted real results, not just \"it started\".")
h("The bench", 2)
bullets([
    "**Machine:** i5-13400F (10 cores / 16 threads), 15.8 GB RAM, Docker VM 8.2 GB, NVIDIA RTX 5060 8 GB.",
    "**Your real platform:** the production image built from the repo's `Dockerfile` (2.68 GB, with Chromium and fonts), "
    "running api, Celery worker, Celery beat and the platform's own voice code.",
    "**Fake paid vendors** (`scripts/fake_services.py`): a fake Claude that answers like the real API after about 3 s, fake "
    "WhatsApp (Meta Graph), fake SES / Postmark, fake S3, fake CMS. Nothing left the PC, nothing was paid.",
    "**Fake users** (`scripts/load10.py`): 5 website chatters, 4 WhatsApp buyers (real UAE mobile prefixes, signed webhooks) "
    "and 1 staff member who logs in with password + authenticator code and works in the CRM.",
    "**Fake callers** (`scripts/voice_callers.py`): browsers joining LiveKit and speaking recorded sentences in English, "
    "Arabic, Hindi and Chinese, 4 sentences each.",
    "**Watchers:** `docker stats` every ~2 s (RAM, CPU, network, disk), database sizes before and after, and each platform's "
    "own counters (mailbox count, call statistics, stored spans, firewall logs…).",
])
h("The 8 rounds", 2)
table(["Round", "What ran", "Load", "Proof of work counted"], [
    ["R1", "Your platform: API, worker, beat, PostgreSQL, Redis, TEI (Qwen3), Phoenix", "10 users for 25 min; catalogue re-index", "555 + 375 AI turns, 1,237 model calls, per-table growth"],
    ["R2", "Free voice chain and the platform's real voice worker", "1, 3, 10 simultaneous callers, CPU and GPU", "sentences answered, reply time per caller"],
    ["R3", "Speech engines alone: faster-whisper, whisper.cpp, sherpa-onnx, Piper, Chatterbox", "1 and 10 clips at once", "transcripts vs the true text"],
    ["R4", "Embeddings: Qwen3-0.6B, BGE-M3, multilingual-e5-large", "48 chunks, 10 queries at once, 62 questions", "right fact found first"],
    ["R5", "AI tracing: Phoenix, Langfuse, Opik", "1,000 AI spans at 10 per second", "spans counted in each store"],
    ["R6", "Email: Haraka, Stalwart, Postal", "1,000 quote emails, 72 KB each", "messages stored / sent"],
    ["R7", "Phone: Asterisk, FreeSWITCH", "10 simultaneous SIP calls with audio", "SIPp success counters"],
    ["R8", "Security: CrowdSec, BunkerWeb, SafeLine", "100k log lines; 5-min load; 3 attacks", "lines parsed, requests blocked / passed"],
], [1.3, 6.2, 4.5, 5.0], size=8.8)
callout("🧪  Honesty corner: our own mistakes, fixed", [
    "• The RAM / CPU sampler counted every reading twice in rounds 5-8 (`docker stats` prints each frame twice). All those "
    "numbers were recomputed from the raw files; the values in this guide are the corrected ones.",
    "• FreeSWITCH first looked broken: our test command had a missing space. Postal's web screen first looked broken: it "
    "listens on 127.0.0.1 unless told otherwise. Both passed once fixed.",
    "• The first SafeLine load run had no staff user; it was rerun completely.",
], "FFF0F0")
page_break()

# ================================================================================================ CH3 CARDS
h("3. The platforms, one detailed card each", 1)
para("Each card follows the same order: **quick facts → what it is → its job in Arifa Pack → how it works → how we tested "
     "it → results (from the raw files) → real output → resource use → problems we hit → paid version → alternatives → verdict.**")


def card(c: dict) -> None:
    h(f"{c['emoji']}  {c['name']}", 2)
    table(["Quick facts", ""], [[k, v] for k, v in c["facts"]], [4.5, 12.5], size=9)
    h("What it is", 3)
    for ptxt in c["what"]:
        para(ptxt)
    callout("🎭  Think of it as…", c["analogy"], "FFF6DD")
    h("Its job in Arifa Pack", 3)
    bullets(c["job"])
    if c.get("how"):
        h("How it works, step by step", 3)
        steps(c["how"])
    if c.get("img"):
        image(c["img"], 17.0)
    if c.get("tested"):
        h("How we tested it", 3)
        bullets(c["tested"])
    if c.get("results"):
        h("Results (measured)", 3)
        c["results"]()
    if c.get("seen"):
        callout("🔎  Real output from the test", c["seen"], "EEF9EE")
    if c.get("appetite"):
        h("Resource use at a glance", 3)
        table(["What", "Value"], c["appetite"], [5.5, 11.5], size=9)
    if c.get("problems"):
        h("Problems and surprises we hit", 3)
        bullets(c["problems"])
    if c.get("money"):
        h("Paid version and price", 3)
        bullets(c["money"])
    if c.get("alts"):
        h("Alternatives", 3)
        table(["Option", "Type", "Price / footprint", "How it did / note"], c["alts"], [4.3, 2.3, 5.2, 5.2], size=8.8)
    if c.get("fun"):
        callout("🎉  Fun fact", c["fun"], "F4ECFF")
    callout(f"🏁  Verdict: {c['rank']}", c["verdict"], "E6F7F5")


# ------------------------------------------------------------------------------------------------- 3A core
h("3A. The core: your platform's own engine", 1)
ph = R1["phases"]
ph_min = {k: (v[1] - v[0]) / 60 for k, v in ph.items()}
C10, C3 = R1["containers"]["C_load_worker10"], R1["containers"]["D_load_worker3"]


def r1_tables() -> None:
    para(f"Round 1 ran in five phases (minutes): idle {ph_min['A_idle']:.0f}, catalogue re-index {ph_min['B_cms_index']:.0f}, "
         f"10 users with 10 worker children {ph_min['C_load_worker10']:.0f}, idle with 3 children {ph_min['D0_idle_worker3']:.1f}, "
         f"10 users with 3 children {ph_min['D_load_worker3']:.0f}. Every container, every phase:")
    for key, title in (("A_idle", "Phase A: idle (nobody using it)"),
                       ("C_load_worker10", "Phase C: 10 users, worker with 10 children (the default)"),
                       ("D_load_worker3", "Phase D: 10 users, worker with 3 children (recommended)"),
                       ("B_cms_index", "Phase B: one CMS publish re-indexing the catalogue (100 products × 6 languages)")):
        para(f"**{title}**", size=10)
        containers_table(R1["containers"][key], cpu_pct=True)


card({
    "emoji": "🗄️", "name": "PostgreSQL 18 + pgvector (the database)",
    "facts": [["Version tested", "PostgreSQL 18 with pgvector (`pgvector/pgvector:pg18`)"], ["Licence", "PostgreSQL Licence (open source)"],
              ["Image size", "631 MB"], ["Databases inside", "crm, automation, agents, cms, phoenix (5)"],
              ["Where in the code", "every change goes through `kx.crm.services.journal.record()` (audit + timeline + outbox in one transaction)"]],
    "what": ["PostgreSQL is the most trusted open-source database in the world: more than 35 years old and used by banks, "
             "governments and most software companies. It keeps data in tables and guarantees that every change is either "
             "fully saved or not saved at all (a \"transaction\"), even if the power goes off halfway.",
             "**pgvector** is an add-on that lets PostgreSQL store and search **vectors**, the lists of numbers that capture "
             "meaning. That is how the AI's knowledge search lives inside the same database, with no separate vector database "
             "to run, pay for or back up."],
    "analogy": "The **filing cabinet of the whole company**. Every drawer is one database. pgvector is a special drawer where "
               "facts are filed by meaning instead of by name, so \"aluminium tray\" and \"foil container\" end up side by side.",
    "job": ["Keeps the CRM: contacts, deals, quote versions, approvals, the deal timeline and the audit trail (SRS §7A, §11).",
            "Keeps the **outbox** and the **job schedule** (follow-ups at day 2/5/10, the 30/60/120-minute approval ladder) so "
            "nothing is lost if a server restarts.",
            "Stores the knowledge vectors (pgvector) and Phoenix's AI traces."],
    "how": ["The API or worker takes a connection from its pool.",
            "It writes the change together with its audit, timeline and outbox rows in one transaction.",
            "The outbox relay (every 5 s) picks up new outbox rows and sends the messages.",
            "Knowledge search asks pgvector for the vectors closest to the question's vector."],
    "tested": ["Ran inside Round 1 with the full app and 10 users for about 25 minutes.",
               "Snapshots of every table's size and row count before and after each phase gave the bytes per AI turn.",
               "Open connections counted after each load phase."],
    "results": lambda: (
        table(["Measurement", "Worker with 10 children (default)", "Worker with 3 children"], [
            ["RAM avg / peak", f"{mib(C10['postgres']['ram_avg_mib'])} / {mib(C10['postgres']['ram_peak_mib'])}", f"{mib(C3['postgres']['ram_avg_mib'])} / {mib(C3['postgres']['ram_peak_mib'])}"],
            ["CPU avg / peak (cores)", f"{C10['postgres']['cpu_avg_pct']/100:.2f} / {C10['postgres']['cpu_peak_pct']/100:.2f}", f"{C3['postgres']['cpu_avg_pct']/100:.2f} / {C3['postgres']['cpu_peak_pct']/100:.2f}"],
            ["Disk written in the phase", f"{C10['postgres']['disk_write_mb']:.0f} MB", f"{C3['postgres']['disk_write_mb']:.0f} MB"],
            ["Open connections after load", "**106** (agents 59, automation 17, crm 16, phoenix 5, other 9)", "57"],
            ["Default connection limit (100)", "**exceeded**", "fine"],
        ], [5.0, 6.0, 6.0]),
        table(["Growth per unit (measured)", "Size"], [
            ["App databases (crm + automation + agents), per buyer turn", "6.5-8.0 KB"],
            ["Phoenix traces, per AI model call", "4.3 KB (≈ 9.3 KB per buyer turn)"],
            ["Biggest grower", "`automation.scheduled_jobs`: ≈ 1.7 rows and ≈ 1.9 KB per buyer turn"],
            ["Other big growers", "`crm.audit_events`, `crm.timeline_events`, outbox tables in 3 databases, `agents.agent_runs`"],
            ["Database traffic inside the server", "≈ 80 KB per buyer turn (internal, not billed)"],
            ["Per month at 50 / 100 / 150 users a day 🔢", f"{U['50']['disk_growth_gb_per_month']} / {U['100']['disk_growth_gb_per_month']} / {U['150']['disk_growth_gb_per_month']} GB"],
        ], [8.5, 8.5])),
    "appetite": [["RAM idle → busy", "194 MiB → 397 MiB avg / 430 MiB peak ✅"], ["CPU", "0.03 core avg ✅"],
                 ["Disk", "631 MB image + ≈ 17 KB per AI turn including traces ✅"]],
    "problems": ["**Connections:** 106 open at default settings, above PostgreSQL's default limit of 100. Adding the voice worker "
                 "or the CMS would cause \"too many clients\" errors (problem 8).",
                 "**Tables never cleaned:** outbox, inbox, webhook receipts, idempotency keys and finished scheduled jobs grow "
                 "forever (problem 11)."],
    "money": ["**Paid (plan):** AWS RDS PostgreSQL Multi-AZ, $139/month at Light, $312 at Normal, $343 at Heavy 💲. Includes a "
              "standby copy in a second zone (takes over in about 1 minute) and point-in-time restore.",
              "**Free:** the same PostgreSQL in a container on your server: $0, about 0.4 GB RAM. You set up backups yourself."],
    "alts": [["Aurora Serverless v2", "paid", "$231 / $450 / $900 💲", "dearest"], ["RDS Multi-AZ (plan)", "paid", "$139 / $312 / $343 💲", "managed failover"],
             ["RDS single zone", "paid", "$69 / $69 / $83 💲", "staging only"], ["PostgreSQL on your server", "free", "0.4 GB RAM ✅", "fast and tiny at your volume"]],
    "fun": "The busiest table is not deals or messages: it is the job schedule (`scheduled_jobs`), with 1.7 new rows for every buyer message.",
    "rank": "keep it (free, on your server)",
    "verdict": "PostgreSQL is light at your volume (under half a gigabyte). Raise `max_connections` or lower the pools, add a clean-up job, and set up off-site backups.",
})

card({
    "emoji": "📌", "name": "Redis 8.2 (job queue, limits and kill switches)",
    "facts": [["Version tested", "redis:8.2, `noeviction`, no disk persistence"], ["Image size", "204 MB"],
              ["Alternative you already run", "Valkey 8.1 (the open fork; same commands)"],
              ["Where in the code", "Celery broker (`arifaa_pack/celery_app.py`), rate limits, spend caps, kill switches"]],
    "what": ["Redis keeps its data in memory, so reading or writing takes microseconds. It is the most common choice for job "
             "queues and counters.",
             "With `noeviction`, Redis refuses new data instead of throwing old data away when memory is full, which is exactly "
             "what a job queue needs: a queued job must never silently disappear."],
    "analogy": "The **sticky-note board by the door**: \"job #123 waiting\", \"this buyer sent 5 messages this minute\". Quick to write, quick to read, quick to wipe.",
    "job": ["Carries jobs from the API to the Celery worker (AI reply, PDF, follow-up, delivery).",
            "Holds rate-limit counters, daily AI spend caps and the admin's kill switches (SRS §19)."],
    "how": ["The API puts a job on a Redis list.", "A Celery worker child takes it, runs it and acknowledges it only at the end "
            "(`acks_late`, so a crash means the job runs again, never that it is lost).", "Counters use atomic increments that expire."],
    "tested": ["Inside Round 1 through every phase."],
    "results": lambda: table(["Measurement", "Idle", "10 users (10 children)", "10 users (3 children)"], [
        ["RAM avg / peak", f"{mib(R1['containers']['A_idle']['redis']['ram_avg_mib'])} / {mib(R1['containers']['A_idle']['redis']['ram_peak_mib'])}",
         f"{mib(C10['redis']['ram_avg_mib'])} / {mib(C10['redis']['ram_peak_mib'])}", f"{mib(C3['redis']['ram_avg_mib'])} / {mib(C3['redis']['ram_peak_mib'])}"],
        ["CPU avg (cores)", f"{R1['containers']['A_idle']['redis']['cpu_avg_pct']/100:.3f}", f"{C10['redis']['cpu_avg_pct']/100:.3f}", f"{C3['redis']['cpu_avg_pct']/100:.3f}"],
        ["Network in / out", f"{R1['containers']['A_idle']['redis']['net_rx_mb']:.1f} / {R1['containers']['A_idle']['redis']['net_tx_mb']:.1f} MB",
         f"{C10['redis']['net_rx_mb']:.1f} / {C10['redis']['net_tx_mb']:.1f} MB", f"{C3['redis']['net_rx_mb']:.1f} / {C3['redis']['net_tx_mb']:.1f} MB"],
        ["Data stored", "", "2.0 MB used, 2.9 MB peak", ""]], [4.0, 4.0, 4.5, 4.5], size=9),
    "appetite": [["RAM", "≈ 10-12 MiB ✅"], ["CPU", "< 0.01 core ✅"], ["Disk", "204 MB image ✅"]],
    "money": ["**Paid (plan):** AWS ElastiCache, 2 nodes in 2 zones: $29 / $58 / $116 a month 💲 (Valkey version 20 % cheaper).",
              "**Free:** one container, about 12 MB of RAM."],
    "alts": [["ElastiCache Redis (plan)", "paid", "$29 / $58 / $116 💲", "managed, 2 zones"], ["ElastiCache Valkey", "paid", "$23 / $46 / $93 💲", "same commands"],
             ["Redis / Valkey on your server", "free", "12 MB ✅", "the smallest thing on the server"]],
    "rank": "keep it (free)",
    "verdict": "Free Redis or Valkey is far more than enough. Keep `noeviction`.",
})

card({
    "emoji": "🛎️", "name": "Your app: API (FastAPI) + Celery worker + Celery beat",
    "facts": [["What", "your own code (Python 3.14), built from the repo's `Dockerfile`"], ["Image size", "2.68 GB (includes Chromium + Noto fonts for Arabic PDFs)"],
              ["Processes", "api (1 uvicorn), worker (Celery, N children), beat (exactly one clock)"],
              ["Where in the code", "`apps/arifaa-pack` (wiring, API, CLI) and the packages `kx-core-*`, `kx-agent-framework`"]],
    "what": ["The **API** (FastAPI on uvicorn) answers every web request: the CRM screens, website chat, WhatsApp and email "
             "webhooks, and staff login (password + authenticator code).",
             "The **Celery worker** does the slow jobs in the background: AI replies for WhatsApp and email, quote PDFs, "
             "deliveries, follow-ups and reports. **Celery beat** is the one clock that starts timed jobs every 5 s, 30 s or "
             "5 minutes (outbox relays, schedule sweeps, heartbeats)."],
    "analogy": "The **receptionist** (API) answers instantly and writes the request on a sticky note; the **back-office clerks** "
               "(worker) do the slow work; the **office clock** (beat) rings for timed tasks. There must be exactly one clock.",
    "job": ["Website chat runs inside the API; WhatsApp and email replies run in the worker (SRS §19).",
            "The worker runs the approval ladder: WhatsApp reminder at 30 min, alert call at 1 h, manager at 2 h (SRS §11.3).",
            "The backend builds every price; the AI never calculates one (SRS §11.1)."],
    "tested": ["Round 1: 10 users for 15 minutes with the default 10 worker children, then 10 minutes with 3 children.",
               "Every request was timed and its HTTP answer recorded; every container sampled every ~2 s."],
    "results": lambda: (
        para("**Every action, with the default 10 worker children (15.2 minutes):**", size=10),
        actions_table(R1["load_worker10"]["actions"], "The website reply time includes the fake Claude's ~3 s thinking time. "
                      "The 500 answers are problem 1 (Sonnet) and problem 5 (unparseable WhatsApp number). The 422s on "
                      "\"approve\" are correct: those website buyers left no phone or email."),
        para("**The same with 3 worker children (10.2 minutes):**", size=10),
        actions_table(R1["load_worker3"]["actions"]),
        r1_tables()),
    "appetite": [["API", "350 MiB idle → 583 MiB avg / 817 MiB peak; 0.14 core avg, 4.9 cores in short peaks ✅"],
                 ["Worker, 10 children (default)", "0.72 GiB fresh → 2.36 GiB after first jobs → 2.60 GiB peak ✅"],
                 ["Worker, 3 children", "0.86 → 0.90 GiB peak, **same reply times** ✅"], ["Beat", "38 MiB → 85 MiB peak; 0.012 core ✅"],
                 ["Traffic handled", "555 buyer turns and 1,237 model calls (2.2 per turn) in 15 min ✅"]],
    "problems": ["**Sonnet calls fail** before leaving the server (`temperature=0.2` rejected): money turns break (problem 1).",
                 "**Heartbeat** task fails every 30 s (problem 6). **Unparseable WhatsApp number** → HTTP 500 (problem 5).",
                 "**Worker RAM** at default (2.6 GiB) is above the AWS plan's 2 GB (problem 9)."],
    "money": ["Your own code: no licence cost. On AWS it runs on Fargate inside the $150/month server line 💲."],
    "rank": "keep it, run the worker with 3 children",
    "verdict": "With `KX_APP_WORKER_CONCURRENCY=3` the same traffic gets the same reply times with 1.7 GB less RAM and half the database connections.",
})

card({
    "emoji": "🧠", "name": "Claude (paid; the brain that writes replies)",
    "facts": [["Models", "Claude Sonnet 5.5 on money turns, Haiku 5.5 on the rest"], ["Called through", "the Claude API directly, from LangGraph agents"],
              ["Price", "≈ $0.0035 per AI turn at the plan's 40 % money-turn mix 💲"]],
    "what": ["Claude is Anthropic's large language model. It reads the buyer's message, the conversation so far, the rules and "
             "the facts found by knowledge search, and writes the reply or decides which tool to call.",
             "It runs on Anthropic's servers; your server only sends the request and waits, which is why the AI uses almost no CPU on your side."],
    "analogy": "A brilliant **consultant on the phone**. Your server dials, reads out ~20 KB of notes and writes down the answer. "
               "You pay per call; the consultant never sees your filing cabinet.",
    "job": ["Writes every AI reply on every channel (chat, WhatsApp, email, voice).",
            "Never calculates a price: prices come from the backend and every reply passes the price guard (SRS §11.1)."],
    "tested": ["Replaced by a fake Claude that answers like the real API after about 3 s, so the platform's real code path ran but nothing was paid."],
    "results": lambda: table(["Measurement", "Value"], [
        ["Model calls per buyer turn", "2.2 (1,237 calls for 555 turns)"], ["Sent per turn", "19.4 KB up, 1.1 KB down (≈ 8.7 KB per call)"],
        ["Sonnet calls that left the server", "**0** (all failed before sending: problem 1)"],
        ["Monthly traffic at 50 / 100 / 150 users a day 🔢", "0.29 / 0.58 / 0.87 GB"]], [8, 9]),
    "problems": ["`kx/agents/models.py:35` sends `temperature=0.2`, which the installed `langchain_anthropic` rejects for Sonnet 5.5: "
                 "every pricing / negotiation turn fails (HTTP 500 to the buyer). Tests miss it because they use a fake model."],
    "money": ["$49 / $228 / $735 a month at Light / Normal / Heavy 💲; $52.50 / $105 / $157.50 at 50 / 100 / 150 users a day 🔢.",
              "**No free replacement was tested**, so it stays paid in every stack."],
    "alts": [["Sonnet on every turn", "paid", "$113 / $526 / $1,701 💲", "strongest, dearest"], ["Gemini 3.8 Flash", "paid", "$98 / $373 / $1,156 💲", "backup only if it passes the tests"],
             ["Haiku on every turn", "paid", "$6 / $28 / $91 💲", "cheap; price turns not proven"], ["Sonnet + Haiku mix (plan)", "paid", "$49 / $228 / $735 💲", "recommended"]],
    "rank": "stays paid",
    "verdict": "Keep Claude, but fix problem 1 first or every price conversation fails.",
})


def r4_tables() -> None:
    m = {"qwen3_06b": j("r4_qwen3_06b.json"), "bge_m3": j("r4_bge_m3.json"), "e5_large": j("r4_e5_large.json")}
    hs = j("r4_hard_summary.json")
    name = {"qwen3_06b": "Qwen3-0.6B", "bge_m3": "BGE-M3", "e5_large": "e5-large"}
    rows = []
    for k, d in m.items():
        hh = hs[k]
        rows.append([name[k], hh["tei_backend"], f"{hh['ram_idle_cached_mib']/1024:.2f} GiB", f"{d['container']['ram_peak_mib']/1024:.2f} GiB",
                     f"{d['index_seconds_per_chunk']:.2f} s", f"{d['query_single_p50_s']:.2f} s",
                     f"{d['query_10_at_once_p50_s']:.2f} / {d['query_10_at_once_max_s']:.2f} s", f"{d['quality']['top1_all']*24:.0f}/24",
                     f"{round(hh['top1_all']*38)}/38", f"{hh['mrr']:.3f}", f"{hh['cold_start_cached_s']:.0f} s"])
    table(["Model", "Engine", "RAM idle", "RAM peak", "Index / chunk", "1 query", "10 at once p50 / max", "Easy test", "Hard test", "MRR", "Start"],
          rows, [2.1, 1.7, 1.5, 1.5, 1.5, 1.2, 2.1, 1.3, 1.3, 1.1, 1.2], size=7.8)
    para("**Questions a model got wrong or ranked lower (hard test):**", size=10)
    rows = []
    for k in ("qwen3_06b", "bge_m3", "e5_large"):
        for miss in j(f"r4_hard_{k}.json")["misses"]:
            rows.append([name[k], miss["lang"], miss["q"], miss["want"], miss["got"], miss["rank"]])
    table(["Model", "Lang", "Question", "Right fact", "Found first", "Right fact at rank"], rows, [2.2, 1.0, 6.0, 2.6, 2.6, 2.6], size=8.5)
    para("All three give 1,024-number vectors, so they fit the existing `vector(1024)` column. Inside the full app under load "
         "(Round 1), Qwen3 took 5.5 s per chunk with 10 cores busy and each indexing request ≈ 26 s, which is why the default "
         "2-second timeout breaks re-indexing (problem 7).", size=9.5)


card({
    "emoji": "📚", "name": "Knowledge search: TEI + an embedding model (Qwen3 / BGE-M3 / e5)",
    "facts": [["Server", "Hugging Face Text Embeddings Inference 1.8.3 (CPU image, amd64 only)"],
              ["Models tested", "Qwen/Qwen3-Embedding-0.6B (Apache-2.0), BAAI/bge-m3 (MIT), intfloat/multilingual-e5-large (MIT)"],
              ["Image size", "938 MB + model files 1.2-2.3 GB"], ["Where in the code", "`HttpEmbedder` in `arifaa_pack/adapters.py`; vectors in pgvector"]],
    "what": ["An **embedding model** turns a piece of text into 1,024 numbers that describe its meaning. Texts with similar meaning "
             "get similar numbers, even across languages. **TEI** is the small server that runs the model and answers \"give me "
             "the numbers for this text\" over HTTP.",
             "Qwen3 runs on TEI's Candle engine; BGE-M3 and e5 run on ONNX Runtime, which turned out to be much faster on a CPU."],
    "analogy": "A **librarian who reads every language**. When you publish the catalogue she files each paragraph by meaning; a "
               "buyer asks in Arabic and she walks straight to the right English shelf.",
    "job": ["Every AI answer first searches the product knowledge (falling back to keyword search if this takes more than 2 s).",
            "Every CMS publish re-indexes the knowledge (SRS §7: \"The AI's knowledge is re-indexed automatically\")."],
    "img": "06_embed_flow.png",
    "tested": ["Each model alone on the same TEI server the app uses, 16 CPU threads.",
               "Indexed 48 product chunks; timed single queries and 10 queries at the same moment (the app gives up at 2 s).",
               "Easy test: 24 questions about 8 facts in en / ar / hi / zh. Hard test: 38 questions about 16 facts built as "
               "look-alike pairs (\"750 ml with lid\" vs \"1-litre without lid\").",
               "Queries written exactly as each model expects (Qwen3 with the app's instruction, e5 with `query:` / `passage:`)."],
    "results": r4_tables,
    "seen": ["Chinese \"可以放进烤箱吗\" (can it go in the oven?) → all three found the paper-lid fact first and the oven fact second "
             "(score gap 0.016): the two Chinese facts are close in meaning.",
             "Arabic \"is the lid included with the litre box?\" → BGE-M3 ranked the right fact 7th: a true look-alike trap."],
    "appetite": [["Qwen3 (default)", "2.82 GiB idle, 3.10 GiB peak; 4.7 s per chunk; 6-9 cores while indexing ✅"],
                 ["BGE-M3", "4.74 GiB idle, 4.83 GiB peak; 0.75 s per chunk; short bursts on all threads ✅"],
                 ["e5-large", "4.69 / 4.76 GiB; 0.88 s per chunk; reads only the first 512 tokens ✅"],
                 ["2,500-chunk re-index", "Qwen3 ≈ 3 h 15 min, BGE-M3 ≈ 31 min, e5 ≈ 37 min ✅"]],
    "problems": ["The repo pins TEI `cpu-1.7.2`, which crashes with Qwen3 on CPU; `cpu-1.8` works with `--auto-truncate` and `--max-batch-tokens 1024` (problem 10).",
                 "Switching to BGE-M3 needs a small code change (no query instruction) and a full re-index; e5 needs two changes."],
    "money": ["**Plan:** Qwen3 self-hosted on AWS Fargate (1 vCPU + 4 GB): $44/month 💲. BGE-M3 needs 8 GB: $57.82 🔢.",
              "**Paid API alternative:** Gemini Embedding 2, $0.20 per million tokens: $0.28 / $1.30 / $4.20 a month 💲, but buyer questions leave the UAE.",
              "**On your own server:** $0, only RAM."],
    "alts": [["BAAI/bge-m3", "free", "4.7 GiB ✅", "36/38, fastest"], ["Qwen3-Embedding-0.6B", "free", "2.8 GiB ✅", "35/38, slow indexing"],
             ["multilingual-e5-large", "free", "4.7 GiB ✅", "35/38, 512-token limit"], ["all-MiniLM-L6-v2", "free", "small", "English only: out"],
             ["Gemini Embedding 2", "paid API", "$0.28-4.20 💲", "data leaves the UAE"]],
    "fun": "BGE-M3 re-indexes the whole catalogue in the time it takes to drink a coffee (31 min); Qwen3 needs a long lunch (3 h 15 min).",
    "rank": "BGE-M3 (rank 1), Qwen3 (rank 2)",
    "verdict": "On a 64 GB server pick **BGE-M3**: same answer quality, 6× faster indexing, 4× faster with 10 questions at once and far from the 2-second limit.",
})


def r5_table() -> None:
    rows = []
    for k, label in (("phoenix", "Phoenix"), ("langfuse", "Langfuse"), ("opik", "Opik")):
        d = j(f"r5_{k}.json")
        rows.append([label, len(d["containers"]), f"{d['ram_idle_total_mib']/1024:.2f} GB",
                     f"{d['load']['ram_total_avg_mib']/1024:.2f} / {d['load']['ram_total_peak_mib']/1024:.2f} GB",
                     f"{d['load']['cpu_total_avg_cores']:.2f} / {d['load']['cpu_total_peak_cores']:.2f}", "1,000 of 1,000",
                     f"{d['disk_growth_per_1000_spans_mb']:.1f} MB", f"{d['control_growth_bytes_same_duration_no_spans']/1e6:.0f} MB"])
    table(["Store", "Containers", "RAM idle", "RAM avg / peak", "CPU avg / peak", "Spans stored", "Disk / 1,000 spans", "Writes, no traffic"],
          rows, [1.8, 1.8, 1.7, 2.6, 2.1, 2.0, 2.4, 2.6], size=8.3)
    para("Disk figures are net of a control period of the same length with no spans. ClickHouse (Langfuse, Opik) merges small "
         "parts later, so a short test overstates its long-run size per span; Phoenix's figure is exact.", size=9)
    para("**Idle RAM of every container:**", size=10)
    rows = []
    for k in ("phoenix", "langfuse", "opik"):
        d = j(f"r5_{k}.json")
        rows.append([k.capitalize(), ", ".join(f"{n.split('-', 1)[1].rsplit('-1', 1)[0]} {v:.0f}" for n, v in d["ram_idle_mib"].items()) + " (MiB)"])
    table(["Store", "Containers and their idle RAM"], rows, [2.5, 14.5], size=8.5)


card({
    "emoji": "✈️", "name": "Arize Phoenix 21.1 (AI tracing) vs Langfuse v4 vs Opik",
    "facts": [["Versions tested", "arize-phoenix v21.1.0; Langfuse 4 and Opik from their official compose files"],
              ["Licence", "Phoenix: Elastic License 2.0 · Langfuse: MIT outside `ee/` · Opik: Apache-2.0"],
              ["Image size", "Phoenix 1.44 GB; Langfuse ≈ 4.3 GB (7 images); Opik ≈ 5.7 GB (8 images)"],
              ["Where in the code", "`kx/agents/tracing.py` (OpenInference spans; prompt and reply cut at 8,000 characters)"]],
    "what": ["Every time the AI calls Claude, the platform records a **span**: the prompt, the reply, the tools used, the time "
             "taken and the tokens spent. A tracing store keeps these spans and shows them on a screen, so staff can open any "
             "conversation and see exactly why the AI said what it said.",
             "Phoenix keeps spans in the platform's own PostgreSQL. Langfuse and Opik each need a whole extra set of databases "
             "(ClickHouse, object storage, Redis and PostgreSQL or MySQL)."],
    "analogy": "The **black-box flight recorder**. Nobody reads it on a good day; on a bad day it explains everything.",
    "job": ["Audit trail of AI answers; prices and chats stay inside your own database (Options by field §14)."],
    "img": "07_trace_flow.png",
    "tested": ["Each store alone from its official setup; 1,000 spans shaped like the platform's real ones (up to 8,000 characters) "
               "sent at 10 a second, then counted in each store's own tables or API.",
               "A control period with no spans measured background writes, so disk growth is net."],
    "results": r5_table,
    "seen": ["Phoenix: `spans` table 1,485 → **2,485** rows (+1,000); database 18.1 MB → 20.0 MB.",
             "Langfuse: ClickHouse `events_core` 0 → **1,000** and `events_full` 0 → **1,000** rows.",
             "Opik: its API answered **404** before the test (no project yet) and **1,000 spans** after.",
             "Round 1 with real platform traffic: Phoenix kept **4.3 KB per model call**."],
    "appetite": [["Phoenix", "0.48 GB idle, 0.51 GB peak, 0.05 core, 1.9 MB per 1,000 spans ✅"],
                 ["Langfuse", "2.49 GB idle, 2.19 GB peak, 0.22 core, 29.8 MB per 1,000 spans ✅"],
                 ["Opik", "2.61 GB idle, 3.32 GB peak, 1.43 cores, 98.3 MB per 1,000 spans ✅"]],
    "problems": ["Phoenix 21 turns login **on** by default; spans were rejected (401) until `PHOENIX_ENABLE_AUTH=false` (production already sets it).",
                 "Langfuse's compose file publishes ports that clash with an existing PostgreSQL / Redis on the same machine."],
    "money": ["**Paid:** Langfuse Cloud Core $29 / $47 / $105 a month; Pro $199 / $217 / $275 💲.",
              "**Plan:** Phoenix self-hosted, $19/month on AWS 💲; on your own server ≈ 0.5 GB RAM, $0."],
    "alts": [["Phoenix (plan)", "free", "0.48 GB, 1 container ✅", "lightest by far"], ["Langfuse self-hosted", "free", "2.5 GB, 7 containers ✅", "15× more disk"],
             ["Opik self-hosted", "free", "2.6 GB, 8 containers ✅", "50× more disk"], ["Langfuse Cloud Core / Pro", "paid", "$29+ / $199+ 💲", "data leaves your server"]],
    "fun": "With no traffic at all, Phoenix wrote 0 bytes; Langfuse and Opik wrote 19-30 MB in 2.5 minutes just keeping themselves busy.",
    "rank": "Phoenix (rank 1), Langfuse (rank 2)",
    "verdict": "Keep **Phoenix**: a fifth of the RAM, 1/15 to 1/50 of the disk, one container, inside your own PostgreSQL.",
})

card({
    "emoji": "🖨️", "name": "Headless Chromium (quote PDFs)",
    "facts": [["What", "Chromium browser without a screen, inside the app image, with Noto fonts"], ["Where in the code", "`pdf.py` (a new browser process per PDF)"]],
    "what": ["To make a quote PDF, the worker builds an HTML page and asks Chromium to \"print\" it. A real browser engine is the "
             "most reliable way to get Arabic letters joined and ordered correctly, right-to-left, next to English and numbers."],
    "analogy": "The **office printer that reads Arabic properly**.",
    "job": ["Draws every quote PDF and Requirement Summary (SRS §11.2)."],
    "results": lambda: table(["Measurement", "Value"], [["Size of one quote PDF", "**52 KB** (the draft guessed 200 KB)"],
                                                       ["RAM / CPU", "inside the worker's numbers (not isolated)"], ["Disk", "part of the 2.68 GB app image"]], [8, 9]),
    "money": ["Free; on AWS it adds about $7/month of worker memory 💲."],
    "rank": "keep it", "verdict": "Free and already inside the app image.",
})

# ------------------------------------------------------------------------------------------------- 3B voice
h("3B. The voice team", 1)
image("02_voice_flow.png", 17.5, "A website voice call, step by step.")


def r2_results() -> None:
    para("**A. Bench voice agent, speech-to-text on the CPU** (faster-whisper int8, 8 threads):", size=10)
    voice_table("cpu", "agent", "livekit")
    para("**B. Bench voice agent, speech-to-text on the GPU** (faster-whisper float16, RTX 5060):", size=10)
    voice_table("gpu", "agent", "livekit")
    para("**C. The platform's REAL voice worker** (your `kx.agents.voice` code with its Kairox graphs, price guard and recording "
         "notice; two blocking bugs worked around; LiveKit server built from source; GPU speech-to-text):", size=10)
    voice_table("app2", "voice", "livekit-src")
    para("Reply time = from the end of the caller's sentence to the first sound of the answer, including the fake Claude's 1-2 s. "
         "Every caller says 4 sentences. RTF below 1 = faster than real time.", size=9)
    c10 = j("r2_callers_app2_c10.json")["per_caller"]
    para("**Per caller, platform worker, 10 callers at once** (seconds; \"—\" = no reply within 60 s):", size=10)
    rows = []
    for c in sorted(c10, key=lambda x: x["caller"]):
        rows.append([c["caller"], f"{c['join_s']:.1f}", f"{c['notice']['first_audio_s']:.1f}",
                     " · ".join("—" if x is None else f"{x:.1f}" for x in c["reply_after_speech_s"])])
    table(["Caller", "Joined after", "Recording notice started after", "Reply after each of 4 sentences"], rows, [1.6, 2.4, 4.8, 8.2], size=8.5)


card({
    "emoji": "📞", "name": "LiveKit server + LiveKit Agents (the voice exchange)",
    "facts": [["Versions tested", "LiveKit server v1.13.11 (built from `repos/livekit-server`), LiveKit Agents 1.8.10 (as the platform pins)"],
              ["Licence", "Apache-2.0; turn-detector model: LiveKit Model License"], ["Image size", "133 MB (server)"],
              ["Inside the agent", "Silero VAD (voice activity detection), LiveKit multilingual turn detector"],
              ["Where in the code", "`kx/agents/voice.py` (`make_livekit_entrypoint`), `arifaa_pack/cli.py:_voice`"]],
    "what": ["**LiveKit server** is a real-time media server (WebRTC): it carries live audio between the visitor's browser and the "
             "AI agent with very little delay and copes with bad networks.",
             "**LiveKit Agents** is the framework the platform's voice worker is built on. It joins each call, uses **Silero VAD** "
             "to hear when someone is speaking and a small **turn-detector** model to guess when they finished a sentence, then "
             "hands the text to your Kairox graphs and speaks the answer back."],
    "analogy": "The **telephone exchange**: it connects each caller to a free agent and carries the sound both ways.",
    "job": ["Every website voice call (SRS §6 step 1, §19 \"Website voice and chat\").",
            "Plays the recording notice first, lets the buyer interrupt, ends the call politely on \"stop calling me\" (SRS §8.1)."],
    "how": ["The browser joins a LiveKit room.", "LiveKit gives the call to a warm agent process (it keeps min(cores, 4) ready).",
            "The agent plays the recording notice.", "Silero VAD + the turn detector notice the buyer has finished.",
            "Speech-to-text → Kairox graph (knowledge search + Claude) → price guard → text-to-speech.", "LiveKit streams the answer back."],
    "tested": ["1, 3 and 10 simultaneous fake callers, each saying 4 sentences in en / ar / hi / zh; first with a small bench agent, "
               "then with the **platform's own voice worker**.", "CPU and GPU speech-to-text compared."],
    "results": r2_results,
    "seen": ["10 callers, platform worker: **10 of 10 calls** connected, **29 of 40** sentences answered, reply p50 **10.0 s**.",
             "Calls 6, 7 and 8 (notice after 2.7-5.2 s) had every sentence answered; the others lost their first one or two "
             "sentences while each call's process was still building the whole app (problem 4).",
             "The unmodified `kx voice` logged `Can't pickle local object <function _voice.<locals>.entry>` 3,380 times in 90 s and never answered a call (problem 2)."],
    "appetite": [["LiveKit server", "16-90 MiB, ≤ 1 core with 10 callers ✅"],
                 ["Platform voice worker", "1.41 GiB idle (4 warm processes) → 1.75 / 2.33 / 4.21 GiB with 1 / 3 / 10 calls; 10 cores peak ✅"],
                 ["Bandwidth at the server", "0.3-1.7 MB per call-minute (both legs); the caller's leg 0.15-0.85 MB ✅"]],
    "problems": ["**Problem 2:** `kx voice` cannot start a job (local function cannot be pickled).",
                 "**Problem 3:** the session has no LLM, so LiveKit skips every reply: the caller hears the notice, then silence.",
                 "**Problem 4:** every call builds the whole app at its start: first sentences are lost under 10 calls.",
                 "The platform's worker is about **2× slower and 2× heavier** than the small bench agent for the same calls."],
    "money": ["**Paid (plan):** LiveKit Cloud Ship, $50/month including 5,000 agent minutes, then $0.01/min, 20 calls at once 💲.",
              "**Free:** self-host the server (90 MiB). Needs open UDP ports on the server."],
    "alts": [["LiveKit Cloud Ship (plan)", "paid", "$50 / $75 / $250 💲", "easiest"], ["Pipecat Cloud", "paid", "$0.01 / session-minute 💲", "agent rewrite"],
             ["Self-hosted LiveKit on AWS", "paid infra", "$134 / $267 / $534 💲 (repo estimate)", "dearer than Cloud"],
             ["Self-hosted LiveKit server", "free", "16-90 MiB ✅", "**tiny**"], ["Pipecat (self-hosted)", "free", "❓ not tested", "agent rewrite"]],
    "rank": "self-hosted LiveKit server (rank 1)",
    "verdict": "The server is tiny: self-host it and save $50/month. But fix the three voice bugs first, or no call is ever answered.",
})


def r3_stt_table() -> None:
    g, cpu, sh = j("r3_fw_gpu.json"), j("r3_fw_cpu.json"), j("r3_sherpa_cpu.json")
    w5, w16 = j("r3_wcpp_q5_0_cpu.json"), j("r3_wcpp_f16_cpu.json")
    rows = [
        ["faster-whisper float16", "GPU", "1.62 GB", f"{g['float16']['load']['wall_s']:.1f} s", f"{g['float16']['single_latency_s_avg']:.2f} s ({g['float16']['single_rtf']:.3f})",
         f"{g['float16']['x10_parallel']['wall_s']:.1f} s", f"{g['float16']['x10_parallel']['cores_busy']:.1f}", f"VRAM {g['float16']['gpu_mem_model_mb']/1024:.1f} GB"],
        ["faster-whisper int8_float16", "GPU", "1.62 GB", f"{g['int8_float16']['load']['wall_s']:.1f} s", f"{g['int8_float16']['single_latency_s_avg']:.2f} s ({g['int8_float16']['single_rtf']:.3f})",
         f"{g['int8_float16']['x10_parallel']['wall_s']:.1f} s", f"{g['int8_float16']['x10_parallel']['cores_busy']:.1f}", "VRAM ≈ 2.9 GB"],
        ["faster-whisper int8", "CPU", "1.62 GB", f"{cpu['int8']['load']['wall_s']:.1f} s", f"{cpu['int8']['single_latency_s_avg']:.2f} s ({cpu['int8']['single_rtf']:.2f})",
         f"{cpu['int8']['x10_parallel']['wall_s']:.1f} s", f"{cpu['int8']['x10_parallel']['cores_busy']:.1f}", f"RAM {cpu['int8']['x10_parallel']['peak_rss_mb']/1024:.1f} GB"],
        ["sherpa-onnx int8", "CPU", "1.04 GB", f"{sh['load_one_recognizer']['wall_s']:.1f} s", f"RTF {sh['single_rtf']:.2f}",
         f"{sh['x10_parallel']['wall_s']:.1f} s", f"{sh['x10_parallel']['cores_busy']:.1f}", f"RAM {sh['x10_parallel']['peak_rss_mb']/1024:.1f} GB"],
        ["whisper.cpp q5_0", "CPU", "0.57 GB", "—", f"{w5['single_latency_s_avg']:.1f} s ({w5['single_rtf']:.2f})", "118 s (one at a time)", "8 threads", f"RAM {w5['ram_peak_mib']/1024:.2f} GB"],
        ["whisper.cpp full", "CPU", "1.62 GB", "—", f"{w16['single_latency_s_avg']:.1f} s ({w16['single_rtf']:.2f})", "121 s (one at a time)", "8 threads", f"RAM {w16['ram_peak_mib']/1024:.2f} GB"],
    ]
    table(["Engine", "On", "Model", "Load time", "One clip: time (RTF)", "10 clips (59 s of audio)", "Cores busy", "Memory peak"],
          rows, [3.6, 1.0, 1.5, 1.5, 2.9, 2.6, 1.6, 2.3], size=8.3)
    para("All engines run the same Whisper large-v3-turbo model, so they produce almost identical text; they differ in speed and "
         "memory. The automatic word-error figures in the raw files are not usable (they count \"750\" vs \"seven hundred and "
         "fifty\" as errors), so the transcripts were read against the true text by hand.", size=9)


card({
    "emoji": "👂", "name": "faster-whisper (speech-to-text) and its rivals",
    "facts": [["Version tested", "faster-whisper 1.2.1 (CTranslate2 4.9), model large-v3-turbo"], ["Licence", "MIT (code and Whisper model)"],
              ["Rivals tested", "whisper.cpp (MIT), sherpa-onnx (Apache-2.0)"], ["Model on disk", "1.62 GB"],
              ["Where it would plug in", "the platform's `VoicePlugins` speech-to-text adapter (OpenAI-compatible endpoint)"]],
    "what": ["Whisper is OpenAI's open speech-recognition model, trained on 680,000 hours of audio in about 100 languages; "
             "large-v3-turbo is its fast, high-quality version.",
             "**faster-whisper** runs the same model several times faster with the CTranslate2 engine, and on an NVIDIA GPU it is "
             "extremely fast. **whisper.cpp** is a C++ version built for CPUs; **sherpa-onnx** runs it with ONNX Runtime."],
    "analogy": "The **ears** of the voice agent: on a GPU sharp and instant; on a CPU just as sharp but painfully slow.",
    "job": ["Turns the caller's speech into text in the visitor's language (SRS §6 step 3: en, ar, hi, ta, zh, si)."],
    "tested": ["Round 3: each engine alone, 10 clips with known text (3 en, 3 ar, 2 hi, 2 zh), one at a time and all 10 at once.",
               "Round 2: inside the live voice chain with 1 / 3 / 10 callers, on CPU and on GPU."],
    "results": r3_stt_table,
    "seen": ["SAID: \"Our aluminium container holds seven hundred and fifty millilitres and comes with a paper lid.\"",
             "HEARD: \"Our aluminium container holds 750 milliliters and comes with a paper lid.\" ✅ perfect",
             "SAID (Arabic, same sentence):", "حاوية الألمنيوم لدينا تتسع لسبعمائة وخمسين مليلترا وتأتي مع غطاء ورقي.",
             "HEARD (Arabic): the number word \"seven hundred\" came out split in two; every other word matched; meaning kept:",
             "حاوية الألمنيوم لدينا تتسع لسبع ماء وخمسين مليلترا وتأتي مع غطاء ورقي.",
             "SAID (zh): \"我们的铝箔餐盒容量为七百五十毫升，配有纸盖。\"",
             "HEARD (zh): \"我们的铝博餐和容量为750毫升配有纸杆\" ❌ \"foil\", \"box\" and \"lid\" mis-heard",
             "Hindi: meaning intact, small spelling slips (कागस for कागज़, वाटसाप for WhatsApp). Tamil and Sinhala: not tested."],
    "appetite": [["GPU (float16)", "0.24 s per clip; 59 s of audio in 2.7 s; ≈ 3.5 GB VRAM; 6-7 CPU cores for audio preparation ✅"],
                 ["CPU (int8)", "5.5 s per clip; 10 clips in 39.5 s; 4.3 GB RAM ✅"],
                 ["Live chain on CPU", "1 caller: 12.8 s reply; 10 callers: **0 of 40** answered ✅"], ["Live chain on GPU", "10 callers: 36 of 40 answered, p50 4.7 s ✅"]],
    "problems": ["On a CPU, Whisper processes a fixed 30-second window for every short sentence, so even one caller waits ~13 s.",
                 "faster-whisper 1.2.1 passes an option that PyAV 19 rejects; the bench decoded the audio itself.",
                 "Chinese product words were mis-heard (partly because the test voice was synthetic): test with real Chinese speech."],
    "money": ["**Paid (plan):** Deepgram Nova-3 Multilingual, $0.0092/min: $14 / $69 / $230 a month 💲 (ar, hi, ta, zh; no Sinhala).",
              "**Free needs a GPU.** Namecheap sells none; the cheapest UAE GPU server with an official price is AWS g6.xlarge (NVIDIA L4 24 GB), $721.42/month 💲."],
    "alts": [["Deepgram Nova-3 (plan)", "paid", "$0.0092/min 💲", "no GPU needed"], ["ElevenLabs Scribe v2", "paid", "$0.39/hour 💲", "cheaper"],
             ["Soniox", "paid", "$0.12/hour 💲", "backup"], ["faster-whisper GPU", "free", "3.5 GB VRAM ✅", "**fastest free, GPU only**"],
             ["sherpa-onnx CPU", "free", "1.5-7.2 GB RAM ✅", "RTF 0.86, not live"], ["whisper.cpp CPU", "free", "0.6-1.8 GB RAM ✅", "RTF 2.0, not live"]],
    "fun": "On the GPU it transcribed 59 seconds of speech in 2.7 seconds: **22× faster than people talk**.",
    "rank": "faster-whisper GPU (rank 1 free); Deepgram on a server without GPU",
    "verdict": "The best free ears, but only with a GPU. On a Namecheap server (no GPU) keep **Deepgram**.",
})


def r3_tts_table() -> None:
    p, c = j("r3_piper_cpu.json"), j("r3_chatterbox_gpu.json")
    rows = []
    for lang in ("en", "ar", "hi", "zh"):
        rows.append([lang, f"{p['single'][lang]['ttfa_s_avg']:.2f} s", f"{p['single'][lang]['rtf']:.3f}", f"{p['single'][lang]['audio_s']:.1f} s",
                     f"{c['single'][lang]['latency_s']:.1f} s", f"{c['single'][lang]['rtf']:.2f}", f"{c['single'][lang]['audio_s']:.1f} s"])
    table(["Language", "Piper: first audio", "Piper RTF", "Piper audio made", "Chatterbox: time to audio", "Chatterbox RTF", "Chatterbox audio"],
          rows, [1.8, 2.5, 1.8, 2.3, 3.2, 2.2, 2.4], size=8.5)
    table(["Measurement", "Piper (CPU)", "Chatterbox (GPU)"], [
        ["Load", f"{p['load_4_voices']['wall_s']:.1f} s for 4 voices; {p['load_4_voices']['peak_rss_mb']:.0f} MB", f"{c['load']['wall_s']:.1f} s; {c['load']['peak_rss_mb']/1024:.1f} GB RAM while loading"],
        ["10 jobs at once", f"{p['x10_parallel']['audio_s']:.0f} s of speech in {p['x10_parallel']['wall_s']:.1f} s; first audio p95 {p['x10_parallel']['ttfa_s_p95']:.1f} s; {p['x10_parallel']['peak_rss_mb']/1024:.2f} GB RAM", "not tested (one at a time was already about real time)"],
        ["CPU", f"{p['x10_parallel']['cpu_s']:.0f} CPU-s for {p['x10_parallel']['audio_s']:.0f} s of speech = **0.43 core per voice speaking**", "GPU"],
        ["Memory", "0.40 GB loaded", f"{c['gpu_mem_model_mb']/1024:.1f} GB VRAM (torch peak {c['torch_max_allocated_mb']/1024:.1f} GB)"],
        ["Model on disk", "63 MB per voice", "13.9 GB repository downloaded"]], [3.0, 7.0, 7.0], size=8.5)


card({
    "emoji": "🗣️", "name": "Piper (text-to-speech) vs Chatterbox",
    "facts": [["Versions tested", "piper-tts 1.8.0 (voices en, ar, hi, zh); Chatterbox 0.1.7 multilingual"], ["Licence", "Piper: GPL-3.0 · Chatterbox: MIT"],
              ["Model size", "Piper 63 MB per voice; Chatterbox 13.9 GB"], ["Where it would plug in", "the platform's `VoicePlugins` text-to-speech adapter"]],
    "what": ["**Piper** is a small, fast neural voice engine from the Home Assistant / Open Home Foundation community. Each voice "
             "is one small file, and it runs comfortably on a normal CPU.",
             "**Chatterbox** (Resemble AI) is a bigger, more natural-sounding model that needs a GPU and generates the whole "
             "sentence before you hear anything."],
    "analogy": "The **voice** of the agent. Piper is a quick, clear radio announcer who never needs a coffee break; Chatterbox is a "
               "stage actor, lovely to hear but taking a long breath before every line.",
    "job": ["Speaks the recording notice and every AI answer on a voice call (SRS §6)."],
    "tested": ["Round 3: each engine alone, one sentence per language, and 10 jobs at once (Piper).",
               "Round 2: Piper inside the live voice chain with up to 10 callers."],
    "results": r3_tts_table,
    "seen": ["Piper was **never** the slow part of any voice run (RTF 0.03-0.10 even with 10 callers).",
             "Chatterbox needed 11.3 s to produce 5.7 s of Chinese speech (RTF 1.98): slower than real time."],
    "appetite": [["Piper", "0.40 GB → 2.07 GB with 10 jobs; 0.43 core per voice speaking ✅"], ["Chatterbox", "≈ 4 GB VRAM, 5.2 GB RAM while loading ✅"]],
    "problems": ["Piper has **no Tamil and no Sinhala voice**; SRS §6 step 3 asks for both.",
                 "Chatterbox first failed with \"paging file too small\" while other tests ran: it needs a lot of memory to load."],
    "money": ["**Paid (plan):** ElevenLabs Flash, $0.04 per 1,000 characters: $27 / $135 / $450 a month 💲.",
              "**Free:** Piper runs on your server's CPU at $0 extra; on AWS Fargate $23.43 (1 voice at once) to $374.93 (10 at once) 🔢."],
    "alts": [["ElevenLabs Flash (plan)", "paid", "$0.04 / 1k characters 💲", "best quality, more languages"], ["Google Chirp 3 HD", "paid", "1M free, then $30/1M chars 💲", "backup"],
             ["Google Standard", "paid", "4M free, then $4/1M chars 💲", "robotic"], ["Piper", "free", "0.4-2.1 GB RAM, CPU ✅", "**rank 1 free**"],
             ["Chatterbox", "free", "4 GB VRAM, 5-11 s first audio ✅", "too slow live"]],
    "fun": "Piper made 157 seconds of speech in 4.3 seconds: a 2.5-minute speech in the time it takes to say \"hello\".",
    "rank": "Piper (rank 1), Chatterbox (rank 2, not live)",
    "verdict": "**Piper** for English, Arabic, Hindi and Chinese on a plain CPU. Keep ElevenLabs only for Tamil and Sinhala calls.",
})

# ------------------------------------------------------------------------------------------------- 3C email
h("3C. The email team", 1)
image("03_email_flow.png", 17.0)


def email_table(only: tuple[str, ...] = ("haraka", "stalwart", "postal")) -> None:
    rows = []
    for k, label in (("haraka", "Haraka (receive + discard)"), ("stalwart", "Stalwart (receive + store)"), ("postal", "Postal (auth, sign, store, deliver)")):
        if k not in only:
            continue
        d = j(f"r6_{k}.json")
        s = d["send"]
        idle = sum(d["ram_idle_mib"].values()) if isinstance(d["ram_idle_mib"], dict) else d["ram_idle_mib"]
        rows.append([label, f"{s['sent']:,} / {s['worker_errors']} errors", f"{s['messages_per_s']} / s", f"{s['per_message_s_p50']*1000:.0f} ms",
                     f"{s['per_message_s_max']*1000:.0f} ms", f"{idle:.0f} → {d['load']['ram_total_peak_mib']:.0f} MiB",
                     f"{d['load']['cpu_total_avg_cores']:.2f} / {d['load']['cpu_total_peak_cores']:.2f}"])
    table(["Server", "Accepted / errors", "Speed", "Per message p50", "max", "RAM idle → peak", "CPU avg / peak"], rows,
          [4.0, 2.6, 1.6, 2.0, 1.5, 3.0, 2.3], size=8.5)
    st_, po = j("r6_stalwart.json"), j("r6_postal.json")
    proof = [["Each test email", "72 KB: HTML body + a 52 KB quote PDF"]]
    if "stalwart" in only:
        proof += [["Stalwart mailbox count (JMAP) before → after", f"{st_['stored_before']} → **{st_['stored_after']}** (+{st_['delivered']})"],
                  ["Stalwart store growth", f"{st_['store_growth_bytes']/1e6:.0f} MB for 1,000 emails = {st_['store_per_1000_mails_mb']:.0f} KB per 72 KB email"]]
    if "postal" in only:
        proof += [["Postal message status", ", ".join(f"{k} {v:,}" for k, v in po["status_counts"].items())
                   + f"; all handed to the relay {po['accepted_to_all_sent_s']} s after the first"],
                  ["Postal database growth", f"{po['db_growth_bytes']/1e6:.0f} MB for 1,000 emails ({po['db_per_1000_mails_mb']:.0f} MB per 1,000)"]]
    table(["Proof and storage", "Value"], proof, [7.0, 10.0], size=9)


card({
    "emoji": "📮", "name": "Postal 3.3.4 (sending platform)",
    "facts": [["Version tested", "ghcr.io/postalserver/postal:3.3.4 + MariaDB 11.8 (4 containers: web, SMTP, worker, database)"],
              ["Licence", "MIT"], ["Image size", "1.9 GB + MariaDB 470 MB"], ["Written in", "Ruby on Rails"]],
    "what": ["Postal is a complete mail-sending platform, like running your own Mailgun or SendGrid. Applications log in over "
             "SMTP or an HTTP API; Postal signs each message with **DKIM**, stores it, queues it, delivers it and records the "
             "result (sent, bounced, held), with a web dashboard for every mail server."],
    "analogy": "Your own **post office**: it stamps every letter (DKIM), keeps a copy, delivers it and writes \"delivered\" in the ledger.",
    "job": ["Sends quote emails with PDFs from one company address (SRS §11.4, §19)."],
    "how": ["The app connects with an SMTP credential.", "Postal accepts and stores the raw message.",
            "The worker signs it (DKIM) and delivers it (in the bench: to a local Haraka sink).", "The status becomes \"Sent\" with the remote server's answer."],
    "tested": ["Set up exactly like an operator would: admin user, organisation, mail server, verified domain, SMTP credential.",
               "1,000 authenticated quote emails over 10 connections; every message counted as \"Sent\" in Postal's own database.",
               "The same 1,000 emails also went to Haraka and Stalwart, so this table compares all three email servers."],
    "results": email_table,
    "seen": ["SMTP answer: `250 2.0.0 Message queued` for every message.",
             "Delivery record: `Sent | Message for buyer@bench.test accepted by 172.23.0.3:2525 (haraka)`.",
             "Final count in `postal-server-1.messages`: `Sent 1000`."],
    "appetite": [["RAM", "455 MB idle (4 containers) → 652 MB peak ✅"], ["CPU", "0.71 core avg, 1.32 peak ✅"], ["Disk", "≈ 2.4 GB images + 97 MB per 1,000 emails ✅"]],
    "problems": ["Its web screen only listens on 127.0.0.1 by default (its official install uses host networking); in a normal "
                 "Docker network it stays silent until `web_server.default_bind_address` is set to 0.0.0.0.",
                 "**Not measurable locally:** whether mail from your own server's IP lands in buyers' inboxes (sender reputation)."],
    "money": ["**Paid (plan):** Amazon SES Essentials $0.16 per 1,000 + Postmark Pro $16.50/month for replies: $17 / $19 / $22 a month 💲.",
              "**Free:** $0 software, ≈ 0.65 GB RAM on your server."],
    "alts": [["Postmark Pro", "paid", "$16.50 / 10k, then $1.30 / 1k 💲", "simple"], ["SES + Postmark (plan)", "paid", "$17 / $19 / $22 💲", "**trusted sending IPs, cheapest**"],
             ["SES + Mail Manager", "paid", "$50+ 💲", "dearer"], ["Gmail API (salesperson's mailbox)", "paid seats", "Workspace seat 💲", "v2 route"],
             ["Postal", "free", "0.65 GB ✅", "rank 1 free sender"]],
    "fun": "Postal signed and delivered 1,000 quotes with PDFs in 39 seconds: faster than a person can print one.",
    "rank": "SES + Postmark (recommended); Postal (rank 1 free)",
    "verdict": "Postal works very well, but SES + Postmark cost only about $17/month and come with trusted sending IPs: keep them unless you must be 100 % in-house.",
})

card({
    "emoji": "📬", "name": "Stalwart v1.1 (mail server and mailbox)",
    "facts": [["Version tested", "stalwartlabs/stalwart v1.1.0"], ["Licence", "AGPL-3.0 (+ its own enterprise licence for paid features)"],
              ["Image size", "469 MB"], ["Written in", "Rust; storage RocksDB; IMAP, JMAP, SMTP, spam filter, DKIM"]],
    "what": ["Stalwart is a modern all-in-one mail server: it receives mail (SMTP), stores it, and lets programs and people read it "
             "(IMAP and the newer JSON-based JMAP), with spam filtering built in."],
    "analogy": "The **company mailbox room**: letters come in, get filed by recipient, and the app can open the drawer and read them.",
    "job": ["Receives buyers' email replies so the app can attach them to the deal (SRS §19)."],
    "tested": ["Finished its 5-step web set-up wizard (hostname, domain, storage, directory, DNS), created a buyer mailbox through "
               "its JMAP management API, then sent 1,000 quote emails to it.", "Delivery proven by counting the messages in the mailbox over JMAP."],
    "results": lambda: email_table(("stalwart",)),
    "seen": ["`250 2.1.5 OK` then `250 2.0.0 Message queued with id 4b9bc718aa00000.`",
             "With its default limits, after 25 messages: `452 4.4.5 Rate limit exceeded, try again later.`",
             "After 10 messages in one session: `452 4.4.5 Maximum number of messages per session exceeded.`"],
    "appetite": [["RAM", "17-23 MB before set-up; 85 MB idle → 228 MB peak ✅"], ["CPU", "0.22 core avg, 0.83 peak ✅"],
                 ["Disk", "469 MB image + ≈ 142 KB per 72 KB email (before compaction) ✅"]],
    "problems": ["Starts in **bootstrap mode** with no email service at all until the web wizard is finished.",
                 "Default limits: **5 connections a second per IP**, **25 messages an hour per sender-domain + recipient**, 10 messages "
                 "per session. Fine for real inbound mail; must be raised if it ever receives your own bulk mail."],
    "money": ["Replaces the inbound part of Postmark (inside the $17-22 email line) 💲. Free: ≈ 0.23 GB RAM."],
    "alts": [["Postmark inbound (plan)", "paid", "inside $17-22 💲", "zero maintenance"], ["Stalwart", "free", "0.23 GB ✅", "rank 1 free mailbox"],
             ["Haraka", "free", "0.13 GB ✅", "rank 2: receives only"]],
    "rank": "rank 1 free mailbox",
    "verdict": "If you self-host email, Stalwart is the one that really stores and serves mail. Know its default limits.",
})

card({
    "emoji": "🚪", "name": "Haraka 3 (fast SMTP receiver)",
    "facts": [["Version tested", "Haraka 3 from npm on node:22-slim"], ["Licence", "MIT"], ["Image size", "329 MB (Node base) + Haraka"]],
    "what": ["Haraka is a lightweight, very fast SMTP server written in Node.js. Almost everything it does (checking, storing, "
             "forwarding) is a plugin, so it can be shaped into a receiver that hands mail straight to your app."],
    "analogy": "The **mail slot in the door**: very fast at taking letters in; what happens next depends on the plugins you add.",
    "job": ["Can receive mail for the app; in the bench it was also the delivery sink for Postal."],
    "tested": ["1,000 quote emails over 10 connections, configured to accept and discard (it measures receiving only)."],
    "results": lambda: email_table(("haraka",)),
    "appetite": [["RAM", "91 MB idle → 127 MB peak ✅"], ["CPU", "0.18 core avg, 0.32 peak ✅"], ["Speed", "1,000 in 8.4 s (119 a second), 0 errors ✅"]],
    "rank": "rank 2 (receiver only)",
    "verdict": "The fastest and lightest, but storing mail or passing it to the app needs extra plugins (not tested).",
})

# ------------------------------------------------------------------------------------------------- 3D phone
h("3D. The phone team", 1)
image("04_call_flow.png", 17.0)


def calls_table(only: tuple[str, ...] = ("asterisk", "freeswitch")) -> None:
    rows = []
    for k, label in (("asterisk", "Asterisk 23.5.0"), ("freeswitch", "FreeSWITCH 1.11.3")):
        if k not in only:
            continue
        d = j(f"r7_{k}.json")
        s = d["sipp_stats"]
        rows.append([label, s["SuccessfulCall(C)"], s["FailedCall(C)"], s["Retransmissions(C)"], f"{float(s['CallRate(C)']):.2f}/s",
                     f"{sum(d['ram_idle_mib'].values()):.0f} → {d['load']['ram_total_peak_mib']:.0f} MiB",
                     f"{d['load']['cpu_total_avg_cores']:.2f} / {d['load']['cpu_total_peak_cores']:.2f}"])
    table(["Server", "Successful calls", "Failed", "Retransmissions", "Call rate", "RAM idle → 10 calls", "CPU avg / peak"], rows,
          [3.4, 2.2, 1.5, 2.3, 1.7, 3.3, 2.6], size=8.8)
    para("SIPp's built-in scenario plays an 8-second G.711 a-law recording and hangs up, so each call carried 8-10 s of two-way "
         "audio (G.711 is about 80 kbit/s each way per call).", size=9)


card({
    "emoji": "☎️", "name": "Asterisk 23.5 (phone switch)",
    "facts": [["Version tested", "Asterisk 23.5.0 (andrius/asterisk image)"], ["Licence", "GPL-2.0"], ["Image size", "485 MB"],
              ["Test tool", "SIPp (`uac_pcap` scenario with G.711 audio)"]],
    "what": ["Asterisk is the classic open-source phone system (a PBX), around since 1999. It places, answers, routes and records "
             "calls over SIP (internet telephony), plays messages and runs call scripts called a \"dialplan\"."],
    "analogy": "The **switchboard operator**: plugs calls through, plays the message, and writes down who called whom.",
    "job": ["Scripted alert calls to staff (approvals, change requests, below-minimum offers) and click-to-call from the CRM (SRS §11.3, §14, §16).",
            "Never cold-calls buyers (SRS §19: UAE rules)."],
    "tested": ["SIPp placed 10 simultaneous calls with two-way G.711 audio (answer + echo), all inside Docker, no phone lines."],
    "results": calls_table,
    "seen": ["SIPp statistics: `SuccessfulCall(C) 10 · FailedCall(C) 0 · Retransmissions(C) 0`."],
    "appetite": [["RAM", "49 MB idle → 52 MB with 10 calls (≈ 0.3 MB per call) ✅"], ["CPU", "0.05 core avg, 0.17 peak ✅"]],
    "problems": ["Debian's SIPp package ships without the audio files its own scenario needs; they were taken from SIPp's GitHub."],
    "money": ["**Paid (plan):** Telnyx, $0.25/min to UAE mobiles + $0.002/min Voice API: $10 / $46 / $152 a month 💲.",
              "**Free:** Asterisk only replaces the $0.002/min Voice API part; real phones still need a paid SIP trunk (\"phone-line minutes paid\")."],
    "alts": [["Twilio", "paid", "$0.2995/min 💲", "backup, already built"], ["Telnyx (plan)", "paid", "$0.25/min + $0.002 💲", "cheapest; test du / Etisalat"],
             ["AWS End User Messaging", "paid", "$0.269/min 💲", "scripted only"], ["Asterisk + SIP trunk", "free + minutes", "≈ 50 MB ✅", "rank 1 free"],
             ["FreeSWITCH + SIP trunk", "free + minutes", "≈ 50 MB ✅", "rank 2"]],
    "rank": "Telnyx (recommended); Asterisk (rank 1 free)",
    "verdict": "Asterisk is perfect and tiny, but it saves almost nothing: the phone minutes are the real cost (≈ $0.07-1.20/month saved).",
})

card({
    "emoji": "🔀", "name": "FreeSWITCH 1.11.3 (phone engine)",
    "facts": [["Version tested", "FreeSWITCH 1.11.3 (safarov/freeswitch image)"], ["Licence", "MPL-1.1"], ["Image size", "352 MB"]],
    "what": ["FreeSWITCH is a telephony engine built for large call platforms and conferencing. It is very flexible but needs more set-up than Asterisk."],
    "analogy": "A **switchboard with more buttons**: powerful, but you must set it up carefully.",
    "job": ["Same job as Asterisk."],
    "tested": ["Same 10-call SIPp test, to number 5000 on its public profile (answer + echo)."],
    "results": lambda: calls_table(("freeswitch",)),
    "seen": ["10 of 10 calls; audio verified both ways on a probe call: 76 kB in, 70 kB echoed back."],
    "appetite": [["RAM", "41 MB idle → 50 MB with 10 calls ✅"], ["CPU", "0.03 core avg, 0.05 peak ✅"]],
    "problems": ["By default it discovers your **public IP** with STUN and puts it in every call set-up, so audio leaves the network. "
                 "`external_rtp_ip` / `external_sip_ip` had to be set to the server's own address."],
    "rank": "rank 2 free", "verdict": "Equally light; needed more configuration.",
})

# ------------------------------------------------------------------------------------------------- 3E security
h("3E. The security team", 1)
image("05_waf_flow.png", 17.0)
SL, BW = j("r8_safeline_proxy.json"), j("r8_bunkerweb.json")
PB, PS = j("r8_waf_probe_bunkerweb.json"), j("r8_waf_probe_safeline.json")
CS = j("r8_crowdsec.json")


def probes_table(only_bunkerweb: bool = False) -> None:
    if only_bunkerweb:
        rows = [[k.replace("attack: ", "⚔️ ").replace("normal: ", "✔️ "), PB[k]["direct"], PB[k]["via_waf"]] for k in PB]
        table(["Request", "Straight to the API", "Through BunkerWeb"], rows, [9.0, 4.0, 4.0], size=9)
        return
    rows = [[k.replace("attack: ", "⚔️ ").replace("normal: ", "✔️ "), PB[k]["direct"], PB[k]["via_waf"], PS[k]["via_waf"]] for k in PB]
    table(["Request", "Straight to the API", "Through BunkerWeb", "Through SafeLine"], rows, [7.5, 3.0, 3.2, 3.3], size=9)


def safeline_results() -> None:
    probes_table()
    para(f"**10-user load for 5 minutes, straight vs through SafeLine** (RAM {SL['ram_idle_total_mib']/1024:.2f} GB idle; "
         f"{SL['via_safeline']['safeline_load']['ram_total_peak_mib']/1024:.2f} GB peak while protecting):", size=10)
    compare_actions(SL["direct"]["actions"], SL["via_safeline"]["actions"], "SafeLine")
    para("The website 500s are problem 1 (Sonnet) and appear in both runs; the 422s are correct (no contact details).", size=9)


def bunkerweb_results() -> None:
    probes_table(only_bunkerweb=True)
    para("**10-user load for 5 minutes, straight vs through BunkerWeb:**", size=10)
    compare_actions(BW["direct"]["actions"], BW["via_bunkerweb"]["actions"], "BunkerWeb")


card({
    "emoji": "🛡️", "name": "SafeLine 9.2.7 (web application firewall)",
    "facts": [["Version tested", "SafeLine 9.2.7 from its official compose (7 containers: tengine proxy, detector, management, …)"],
              ["Licence", "GPL-3.0"], ["Images", "≈ 2.3 GB"], ["Made by", "Chaitin Tech; detection by analysing the request's meaning, not only fixed rules"]],
    "what": ["SafeLine is a web application firewall: a reverse proxy that sits in front of your API, reads every request and blocks "
             "attacks such as SQL injection, script injection (XSS) and path traversal before they reach your code.",
             "Instead of only matching fixed patterns, its detector parses the request like a language, which is why it blocked the "
             "attacks without blocking your normal traffic."],
    "analogy": "The **security guard at the door** who reads every visitor's note and turns away anyone carrying a crowbar, but lets the postman in.",
    "job": ["Protects the CRM, website chat and webhooks (SRS §22)."],
    "tested": ["Protected the platform API through an \"Application\" set up in its own web console (default protection).",
               "Same 10-user load for 5 minutes straight and through SafeLine; then 3 textbook attacks and 3 normal requests "
               "(including correctly signed WhatsApp webhooks) straight and through it."],
    "results": safeline_results,
    "seen": ["`?id=1' OR '1'='1` → **403** · `?q=<script>alert(1)</script>` → **403** · `?file=../../../../etc/passwd` → **403**",
             "Signed WhatsApp webhooks (English and Arabic) → **200** · all 86 webhooks in the load → 200"],
    "appetite": [["RAM", f"{SL['ram_idle_total_mib']:,.0f} MiB idle → {SL['via_safeline']['safeline_load']['ram_total_peak_mib']:,.0f} MiB peak while protecting (≈ 1.6 GiB) ✅"], ["CPU", "0.03 core avg, 0.12 peak ✅"], ["Delay added", "none measurable (±1 ms) ✅"]],
    "problems": ["Its proxy uses host networking: on Docker Desktop the protected port was reachable only inside Docker's VM, so the "
                 "load ran from a host-network container (normal on a Linux server). Not tested on AWS Fargate.",
                 "Its console uses a self-signed certificate and shows a licence agreement on first use."],
    "money": ["**Paid (plan):** AWS WAF basic + Cloudflare Turnstile: $10 / $11 / $14 a month 💲 (Cloudflare Business $250).", "**Free:** $0 software, ≈ 1.6 GiB RAM."],
    "alts": [["Cloudflare Business", "paid", "$250 💲", "top"], ["AWS WAF + Bot Control + Turnstile", "paid", "$20-24 💲", "medium"],
             ["AWS WAF basic + Turnstile (plan)", "paid", "$10-14 💲", "cheapest managed"], ["SafeLine", "free", "1.6 GiB ✅", "**3/3 blocked, 0 false blocks**"],
             ["BunkerWeb + OWASP CRS", "free", "0.3-0.5 GB ✅", "3/3 blocked, blocks WhatsApp"]],
    "rank": "rank 1 free",
    "verdict": "The free firewall to use: it blocked every test attack, let all real traffic through with default settings and added no delay.",
})

card({
    "emoji": "🧱", "name": "BunkerWeb 1.6.6 with the OWASP Core Rule Set",
    "facts": [["Version tested", "bunkerity/bunkerweb-all-in-one 1.6.6; ModSecurity + OWASP CRS 4.20.0 (833 rules)"], ["Licence", "AGPL-3.0 (CRS: Apache-2.0)"], ["Image", "1.68 GB"]],
    "what": ["BunkerWeb is nginx with security built in. Its firewall is ModSecurity running the OWASP Core Rule Set: 833 hand-written "
             "rules where each suspicious match adds points and a request scoring 5 or more is blocked."],
    "analogy": "A **very strict guard** who also stops the postman, because the word \"profile\" on the envelope looks suspicious.",
    "job": ["Same job as SafeLine."],
    "tested": ["Same 10-user load straight and through it, and the same attack / normal probes after its rules had loaded."],
    "results": bunkerweb_results,
    "seen": ["Its own log for a signed WhatsApp webhook: `[id \"930120\"] [msg \"OS File Access Attempt\"]` matched `.profile` inside "
             "the field `contacts[0].profile.name`, then `[id \"949110\"]` blocked it (anomaly score 5).",
             "Every WhatsApp webhook from Meta contains that field, so with stock rules **WhatsApp silently stops working**."],
    "appetite": [["RAM", "290 MB idle → 425 MB avg / 472 MB peak while proxying ✅"], ["CPU", "0.05 core avg, 0.43 peak ✅"], ["Delay added", "+4-6 ms per CRM request ✅"]],
    "problems": ["It answers health checks about a minute before its 833 rules are loaded: our first probe passed every attack because the rules were not loaded yet."],
    "rank": "rank 2 free",
    "verdict": "Usable only after excluding rule 930120 for `/webhooks/whatsapp`. Lighter than SafeLine (−1.2 GB), slower (+4-6 ms).",
})

card({
    "emoji": "📹", "name": "CrowdSec 1.7.3 (log watcher)",
    "facts": [["Version tested", "crowdsecurity/crowdsec v1.7.3"], ["Licence", "MIT"], ["Image", "476 MB"]],
    "what": ["CrowdSec reads your server's logs, recognises attack patterns (brute force, scanning, bad bots) and bans the attacker's "
             "IP. It can also use a community blocklist shared by thousands of servers."],
    "analogy": "The **CCTV reviewer** who watches the tapes and pins troublemakers' photos on the \"do not admit\" board.",
    "job": ["A second layer next to the firewall: bans IPs that keep attacking any service on the server."],
    "tested": ["Read a 100,000-line web log from start to end (`mode: cat`)."],
    "results": lambda: table(["Measurement", "Value"], [
        ["Lines read / parsed", f"{CS['acquisition_row'][1]} / {CS['acquisition_row'][2]}"], ["Sent to detection buckets", CS["acquisition_row"][4]],
        ["Whitelisted (private test IPs)", CS["acquisition_row"][5]], ["Time / speed", f"{CS['parse_seconds_from_log']} s = {CS['lines_per_second']:,} lines a second"],
        ["RAM avg / peak / after", f"{CS['load']['ram_total_avg_mib']:.0f} / {CS['load']['ram_total_peak_mib']:.0f} / 158 MiB"],
        ["CPU avg / peak during the burst", f"{CS['load']['cpu_total_avg_cores']} / {CS['load']['cpu_total_peak_cores']} cores"]], [7, 10]),
    "problems": ["The test log used private 10.x addresses, which CrowdSec trusts by default, so detection work was small.",
                 "It refuses to start without a data volume, and by default only reads new log lines."],
    "rank": "recommended extra layer", "verdict": "Light and fast; add it next to SafeLine.",
})

# ------------------------------------------------------------------------------------------------- 3F devops
h("3F. The DevOps helper", 1)
image("08_coolify_flow.png", 17.0)
card({
    "emoji": "🏗️", "name": "Coolify (self-hosted deployment platform)",
    "facts": [["Type", "open-source, self-hosted PaaS (like Heroku / Vercel on your own server)"],
              ["Minimum (official docs)", "2 CPU cores, 2 GB RAM, 10 GB disk; Linux; amd64 or arm64"],
              ["Price", "self-hosted free; Cloud $5/month incl. 2 servers, +$3 per extra server 💲"]],
    "what": ["Coolify installs Docker on your server and gives you a web dashboard to deploy apps from Git, run databases and "
             "services, add domains and free HTTPS certificates, read logs and set up backups, without typing server commands."],
    "analogy": "The **building manager**: hand over the plans (Git) and it builds, opens the doors (HTTPS), keeps the lights on and keeps the logbook.",
    "job": ["Deploys your api, worker, beat and voice worker from the repo's `Dockerfile`, and runs every free platform in this guide on one server."],
    "how": ["Install with its script (installs Docker, creates `/data/coolify`).", "Connect your GitHub repo.",
            "Add each service (app containers, PostgreSQL, Redis, TEI, Phoenix, SafeLine, CrowdSec …).", "Add domains; Coolify gets HTTPS certificates.",
            "Push to GitHub → Coolify builds and redeploys."],
    "appetite": [["Minimum", "2 cores, 2 GB RAM, 10 GB disk (docs) 💲"], ["Our test", "❓ not load-tested on the bench"]],
    "money": ["Self-hosted: free forever, all features. Cloud: $5/month for 2 servers 💲."],
    "rank": "recommended (self-hosted)",
    "verdict": "Use self-hosted Coolify on the same server (≈ 2 GB of the 64 GB), or Cloud for $5 if you prefer its dashboard off the server.",
})

# ------------------------------------------------------------------------------------------------- 3G test tools
h("3G. The test tools (not part of production)", 1)
table(["Tool", "What it did", "Why it matters"], [
    ["`load10.py` (fake users)", "5 web chatters, 4 WhatsApp buyers, 1 staff member for 5-15 minutes", "real request mix, real timings"],
    ["`fake_services.py` (fake vendors)", "fake Claude (~3 s), WhatsApp, SES, Postmark, S3, CMS", "the real code path, no cost, no data sent"],
    ["`voice_callers.py`", "browsers joining LiveKit and speaking recorded sentences", "reply time measured at the caller's ear"],
    ["SIPp", "10 simultaneous SIP calls with audio", "the standard telephony load tester"],
    ["`smtp_send.py`", "1,000 quote emails with PDFs over 10 connections", "realistic email size"],
    ["`waf_probe.py`", "3 attacks + 3 real requests straight and through a firewall", "blocks vs false blocks"],
    ["`sample.py` + `docker stats`", "RAM, CPU, network, disk every ~2 s", "every resource number"],
], [4.5, 7.0, 5.5], size=9)
page_break()

# ================================================================================================ CH4 RECOMMENDED
h("4. The recommended stack (chosen on measured performance)", 1)
para("For every field we pick the platform that performed best in the tests **for your situation** (one Namecheap server, no GPU, "
     "50-150 users a day). Where a paid service won, because the free one needs a GPU, saves almost nothing, or puts email "
     "delivery at risk, we say so.")
image("15_recommended_map.png", 17.5)
table(["Field", "✅ Recommended", "Runner-up", "Why (measured)"], [
    ["Database", "**PostgreSQL + pgvector** (free)", "RDS Multi-AZ (paid)", "0.43 GB peak; 0.03 core with 10 users"],
    ["Job queue / limits", "**Redis / Valkey** (free)", "ElastiCache", "12 MB; nothing to gain by paying"],
    ["App processes", "**api + worker (3 children) + beat**", "worker with 10", "same reply times, −1.7 GB RAM, 57 vs 106 DB connections"],
    ["Knowledge search", "**BGE-M3 on TEI** (free)", "Qwen3-0.6B", "36/38 vs 35/38; 6× faster indexing; 0.42 s vs 1.93 s worst with 10 queries"],
    ["AI tracing", "**Phoenix** (free)", "Langfuse", "0.5 GB vs 2.5 GB; 1.9 MB vs 29.8 MB per 1,000 spans"],
    ["AI brain", "**Claude** (paid)", "—", "no free option tested"],
    ["Voice transport", "**LiveKit server, self-hosted** (free)", "LiveKit Cloud", "16-90 MiB with 10 callers; saves $50/month"],
    ["Speech-to-text", "**Deepgram** (paid)", "faster-whisper GPU", "free needs a GPU ($721+/month); free on CPU = 0/40 replies with 10 callers"],
    ["Text-to-speech", "**Piper** (free) + ElevenLabs only for ta / si", "ElevenLabs", "RTF 0.03-0.05, first audio 0.1-0.3 s on CPU; no ta / si voices"],
    ["Email out", "**SES + Postmark** (paid)", "Postal", "≈ $17/month with trusted IPs; Postal works but inbox delivery is untestable"],
    ["Email in", "**Postmark inbound** (paid, in the line above)", "Stalwart", "same reason; Stalwart is the free fallback"],
    ["Staff calls", "**Telnyx** (paid)", "Asterisk + SIP trunk", "minutes cost the same either way; Asterisk saves ≈ $1/month"],
    ["Web firewall", "**SafeLine** (free)", "BunkerWeb", "3/3 attacks blocked, 0 false blocks, no delay"],
    ["Log-based bans", "**CrowdSec** (free)", "—", "100k lines in 39 s, 159 MB"],
    ["WhatsApp", "**Meta Cloud API** (paid)", "Telnyx / Twilio", "cheapest per the repo's prices"],
    ["Websites", "**Vercel Pro** (paid)", "self-hosted Next.js ❓", "not measured on the bench"],
    ["Deploy / manage", "**Coolify self-hosted** (free)", "Coolify Cloud $5", "one dashboard for everything"],
    ["Server", "**Namecheap Xeon E-2236, 64 GB, 2 × 960 GB SSD**", "32 GB version", "needs ≈ 19.5 GiB RAM and ≈ 11.4 GB of images"],
], [3.0, 4.6, 3.0, 6.4], size=8.6)

h("How the recommended platforms performed (the detail behind each pick)", 2)
rec = [
    ("🗄️ PostgreSQL + pgvector", ["With 10 users: 397 MiB average, 430 MiB peak, 0.03 core. Every CRM screen answered in 5-11 ms (p50).",
                                 "Each buyer turn added 6.5-8 KB to the app databases, ≈ 17 KB with traces: ≈ 0.77 GB a month at 150 users a day.",
                                 "Watch: 106 connections at default worker settings (limit 100); with 3 worker children, 57."]),
    ("🛎️ App with 3 worker children", ["10 users, 375 buyer turns in 10 minutes: website reply p50 5.3 s (3 s of it is the fake Claude); WhatsApp webhooks acknowledged in 13 ms.",
                                       "Worker 0.86 → 0.90 GiB (vs 2.60 GiB with 10 children) with identical reply times."]),
    ("📚 BGE-M3", ["Right fact first for 36 of 38 hard questions (95 %), including 10 of 11 non-English ones; MRR 0.964.",
                  "0.75 s per chunk to index: a 2,500-chunk catalogue in ≈ 31 minutes instead of ≈ 3 h 15 min.",
                  "10 buyers asking at the same moment: 0.30 s typical, 0.42 s worst, far below the app's 2-second limit."]),
    ("✈️ Phoenix", ["Stored 1,000 of 1,000 spans; 1.9 MB per 1,000 spans; 0 bytes written when idle; ready 9 s after start.",
                   "On real platform traffic: 4.3 KB per AI call. 0.48 → 0.51 GB RAM, 0.05 core."]),
    ("📞 LiveKit server (self-hosted)", ["Carried every call in every voice test; 16-90 MiB and at most 1 core with 10 callers.",
                                        "Traffic 0.3-1.7 MB per call-minute at the server: even 4,800 voice minutes a month is under 8.2 GB."]),
    ("🗣️ Piper", ["First sound after 0.11 s (English) to 0.29 s (Arabic); 20-37× faster than real time.",
                 "10 voices at once: 157 s of speech in 4.3 s, 2.07 GB RAM, 0.43 core per voice speaking.",
                 "Never the bottleneck in any of the 9 voice runs."]),
    ("🛡️ SafeLine + 📹 CrowdSec", ["SafeLine: 3 of 3 attacks → 403; 86 of 86 WhatsApp webhooks and every CRM and chat request passed; p50 changed by at most 1 ms.",
                                  "CrowdSec: 100,000 log lines in 39 s on 0.91 core and 159 MB."]),
    ("💲 The paid picks", ["**Deepgram**, because the free ears need a GPU: on CPU one caller waited 12.8 s and 10 callers got no answer at all.",
                          "**SES + Postmark**, because for ≈ $17/month you get sending IPs that inboxes already trust; Postal performed well (44/s) but its inbox delivery cannot be proven without real sending.",
                          "**Telnyx**, because the phone minutes are the cost either way; Asterisk (10/10 calls, 52 MB) would save about $1 a month."]),
]
for title, lines in rec:
    callout(title, lines, "F1FAF4")
page_break()

# ================================================================================================ CH5 OPTIONS
h("5. Every field and its alternatives (from \"Options by field\")", 1)
para("The repo's `Options by field.md` lists High / Medium / Low choices with official prices. Here they are next to the free "
     "platform we measured. US$ a month at Light / Normal / Heavy 💲. **Bold** = the 11 Nov plan's pick.")
table(["Field", "High", "Medium", "Low / budget", "Free platform measured ✅"], [
    ["AI brain", "Sonnet every turn 113/526/1,701", "Gemini 3.8 Flash 98/373/1,156", "Haiku 6/28/91; **mix 49/228/735**", "none (stays paid)"],
    ["Speech-to-text", "**Deepgram 14/69/230**", "ElevenLabs Scribe 10/49/162", "Soniox 3/15/50", "faster-whisper (GPU only)"],
    ["Text-to-speech", "**ElevenLabs Flash 27/135/450**", "Google Chirp 3 HD 0/71/308", "Google Standard 0/0/29", "Piper (CPU)"],
    ["Real-time voice", "**LiveKit Cloud 50/75/250**", "Pipecat Cloud 15/75/250", "self-hosted LiveKit on AWS 134/267/534", "LiveKit server (90 MiB)"],
    ["Alert calls", "Twilio 12/55/181", "**Telnyx 10/46/152**", "AWS End User Msg 10/48/161", "Asterisk / FreeSWITCH + SIP trunk"],
    ["WhatsApp", "Twilio 56/359/1,243", "Telnyx 50/330/1,146", "**Meta direct 26/213/756**", "none (Meta only)"],
    ["Email", "Postmark Pro 16/20/66", "**SES + Postmark 17/19/22**", "SES + Mail Manager 50/51/55", "Postal + Stalwart + Haraka"],
    ["Servers", "**Fargate ARM 150/150/188**", "", "EC2 t4g 177/177/239", "Namecheap + Coolify (ch. 8)"],
    ["Database", "Aurora 231/450/900", "**RDS step 139/312/343**", "RDS single zone 69/69/83", "PostgreSQL (0.4 GB)"],
    ["Cache / queue", "**ElastiCache Redis 29/58/116**", "Valkey 23/46/93", "Redis on Fargate 19/19/37", "Redis (12 MB)"],
    ["Website + CRM", "**Vercel Pro 20/40/40**", "Netlify 20", "Cloudflare Workers 5", "❓ self-hosted Next.js not tested"],
    ["Knowledge search", "**Qwen3 container 44**", "BGE-M3 44", "e5-large 44; Gemini API 0.28-4.20", "BGE-M3 / Qwen3 / e5 (all tested)"],
    ["AI tracing", "Langfuse Pro 199/217/275", "Langfuse Core 29/47/105", "**Phoenix 19**", "Phoenix / Langfuse / Opik (all tested)"],
    ["Monitoring", "Datadog 93+", "**Better Stack 64/64/154**", "Grafana Cloud free 0", "❓ not tested"],
    ["Staff login", "Auth0 35", "Cognito 0", "**in-house 0**", "in-house (your code)"],
    ["Firewall + bots", "Cloudflare Business 250", "AWS WAF + Bot Control 20-24", "**AWS WAF + Turnstile 10-14**", "SafeLine / BunkerWeb / CrowdSec"],
    ["Background jobs", "**Celery + Beat 0**", "Procrastinate 0", "", "Celery (your code)"],
], [2.6, 3.4, 3.4, 3.8, 3.8], size=8.3)
page_break()

# ================================================================================================ CH6 REPOS
h("6. The source-code shelf (resource-usage-bench/repos)", 1)
para("The source code of every platform we tested, at the version we ran where that version has a tag. You never need to open "
     "these folders to use the platforms; they are there so anyone can check exactly what was tested, read its licence or "
     "rebuild it. Ignored by git (not part of your project). Full list with commits: `repos/SOURCES.md`.")
WHAT = {"livekit-server": "LiveKit media server; built from this source for the voice test",
        "livekit-agents": "LiveKit Agents framework used by the platform's voice worker", "asterisk": "Asterisk phone switch",
        "freeswitch": "FreeSWITCH phone engine", "postal": "Postal mail-sending platform", "haraka": "Haraka SMTP receiver",
        "stalwart": "Stalwart mail server", "text-embeddings-inference": "TEI, the embedding server the app uses",
        "FlagEmbedding": "BAAI's code behind BGE-M3", "phoenix": "Arize Phoenix AI tracing", "langfuse": "Langfuse AI tracing",
        "opik": "Opik AI tracing", "bunkerweb": "BunkerWeb firewall", "crowdsec": "CrowdSec log watcher", "SafeLine": "SafeLine firewall",
        "coreruleset": "OWASP Core Rule Set inside BunkerWeb", "altcha": "ALTCHA bot challenge (not tested)",
        "faster-whisper": "faster-whisper speech-to-text", "whisper.cpp": "whisper.cpp speech-to-text", "whisper": "OpenAI's original Whisper",
        "sherpa-onnx": "sherpa-onnx speech toolkit", "piper1-gpl": "Piper text-to-speech", "chatterbox": "Chatterbox text-to-speech",
        "pipecat": "Pipecat voice framework (not tested)", "sipp": "SIPp call generator used for the phone test"}
LIC = {"asterisk": "GPL-2.0", "freeswitch": "MPL-1.1", "postal": "MIT", "haraka": "MIT",
       "stalwart": "AGPL-3.0 + Stalwart Enterprise Licence", "text-embeddings-inference": "Apache-2.0",
       "FlagEmbedding": "MIT", "phoenix": "Elastic License 2.0", "langfuse": "MIT (ee/ folders: own licence)",
       "opik": "Apache-2.0", "bunkerweb": "AGPL-3.0", "crowdsec": "MIT", "SafeLine": "GPL-3.0", "coreruleset": "Apache-2.0",
       "altcha": "MIT", "faster-whisper": "MIT", "whisper.cpp": "MIT", "whisper": "MIT", "sherpa-onnx": "Apache-2.0",
       "piper1-gpl": "GPL-3.0", "chatterbox": "MIT", "pipecat": "BSD-2-Clause", "sipp": "GPL-2.0 or later"}
rows = [["livekit-server", "v1.13.11", "Apache-2.0", "", WHAT["livekit-server"]], ["livekit-agents", "livekit-agents@1.8.10", "Apache-2.0", "", WHAT["livekit-agents"]]]
for line in (BENCH / "repos" / "SOURCES.md").read_text(encoding="utf-8").splitlines():
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) == 7 and cells[0] not in ("Repo", "---"):
        name, _url, _asked, ref, _commit, lic, size = cells
        rows.append([name, ref, LIC.get(name, lic.split(":", 1)[-1].strip()[:45]), size, WHAT.get(name, "")])
table(["Folder", "Version cloned", "Licence", "Size", "What it is"], rows, [3.0, 3.2, 4.0, 1.4, 5.4], size=8.3)
page_break()

# ================================================================================================ CH7 PROBLEMS
h("7. Things that broke (and what to do)", 1)
para("Running the real code under load found these problems in the platform. Nothing was changed in your code.")
table(["#", "Problem", "Effect", "Fix"], [
    ["1", "Every Claude **Sonnet** call fails (`temperature=0.2` rejected)", "Pricing / negotiation turns fail (HTTP 500)", "`kx/agents/models.py:35`: no temperature for Sonnet"],
    ["2", "`kx voice` cannot start a job (local function cannot be pickled)", "No voice call is ever answered", "Move the entry function to module level"],
    ["3", "Voice session has no LLM, so LiveKit skips every reply", "Caller hears the notice, then silence", "Pass any LLM object to `AgentSession`"],
    ["4", "Each voice call builds the whole app at its start", "First sentences lost under 10 calls", "Build it in `prewarm_fnc`"],
    ["5", "Unparseable WhatsApp number → HTTP 500", "Meta keeps retrying", "Log and acknowledge"],
    ["6", "Heartbeat task fails every 30 s", "Queue metrics never sent", "`kx/core/metrics.py:46`"],
    ["7", "Embedding timeout 2 s; re-index requests take ≈ 26 s on CPU", "Catalogue re-index fails", "Longer timeout for indexing"],
    ["8", "106 PostgreSQL connections at defaults (limit 100)", "\"Too many clients\"", "Worker concurrency 3 / smaller pools"],
    ["9", "Worker needs 2.6 GiB at default; AWS plan gives 2 GB", "Out-of-memory restarts on AWS", "Concurrency 3 (0.9 GiB)"],
    ["10", "Pinned TEI `cpu-1.7.2` crashes with Qwen3", "Knowledge search does not start", "`cpu-1.8` with `--auto-truncate`"],
    ["11", "Some tables are never cleaned up", "`scheduled_jobs` grows forever", "Add a retention job"],
    ["12", "BunkerWeb default rules block every WhatsApp webhook", "WhatsApp silently stops", "Exclude CRS rule 930120, or use SafeLine"],
], [0.8, 6.0, 4.6, 5.6], size=8.6)
page_break()

# ================================================================================================ CH8 SERVER
h("8. The server: Namecheap + Coolify", 1)
h("How big must it be?", 2)
HR, FR = UPD["stack_ram_gib"]["hybrid"], UPD["stack_ram_gib"]["free"]
para(f"Adding up the **measured peak RAM** of everything in the recommended stack gives **≈ {HR['total']} GiB**; the maximum-free "
     f"stack (adds Postal, Stalwart, Asterisk) needs **≈ {FR['total']} GiB**.")
table(["Part (recommended stack)", "Peak RAM (GiB)"], [[k, f"{v:g}"] for k, v in HR["items"].items()] + [["**Total**", f"**{HR['total']}**"]],
      [12.0, 5.0], size=9)
callout("💡  What this means", [
    "**32 GB** works; **64 GB** gives room for growth and for deploying a new version next to the old one, for about $4 a month more.",
    "**CPU:** bursts matter. 10 simultaneous voice calls pushed the voice worker to 10 cores, and re-indexing takes every core it can "
    "get. At 50-150 users a day the average number of calls at once is 0.09-0.27 (1,600-4,800 voice minutes a month over 300 "
    "working hours, i.e. 10 hours × 30 days ⚠️ 🔢), so a 6-core / 12-thread Xeon E-2236 is enough.",
    f"**Disk:** images and models ≈ {UPD['stack_disk_gb']['hybrid']['total']} GB (recommended) / {UPD['stack_disk_gb']['free']['total']} GB "
    f"(maximum free) + database growth {U['50']['disk_growth_gb_per_month']} / {U['100']['disk_growth_gb_per_month']} / "
    f"{U['150']['disk_growth_gb_per_month']} GB a month. 2 × 960 GB SSD is plenty; keep backups **off** the server.",
    "**Bandwidth:** Namecheap's listing shows 100 TB a month; you need well under 30 GB (voice is the biggest: 0.15-0.85 MB per call-minute ✅).",
], "E6F7F5")
h("Namecheap dedicated servers (as indexed on 14 Nov 2026)", 2)
para("Namecheap blocks automated reading of its pages; these list prices come from its own configure pages as indexed by search "
     "engines on 14 Nov 2026. The same plan showed different prices on different pages (promotions, billing terms), so **check the "
     "live configurator before buying**.")
table(["Plan", "List price, monthly term", "Fits?"],
      [[k, money(v), ("**recommended**" if k == UPD["namecheap_pick"] else ("yes, less headroom" if "32 GB" in k else "more than needed"))]
       for k, v in UPD["namecheap_plans"].items()], [8.5, 3.5, 5.0], size=9)
callout("⚠️  Read before you buy", [
    "**Location:** all Namecheap dedicated servers are in phoenixNAP's data centre in **Phoenix, Arizona, USA**. The 11 Nov plan keeps "
    "prices and chats **in the UAE** and needs Arifa's approval for every processor.",
    "**No GPU:** so the free speech-to-text cannot run live there; keep Deepgram (or rent a GPU elsewhere: AWS UAE g6.xlarge $721.42/month 💲).",
    "**One server = one point of failure:** no automatic database failover, no managed backups, no monitoring unless you add them.",
], "FFF0F0")
image("13_footprint_chart.png", 16.0)
page_break()

# ================================================================================================ CH9 MONEY
h("9. Money: PAID vs FREE vs RECOMMENDED, at 50 / 100 / 150 users a day", 1)
callout("⚠️  The one assumption in this chapter", [
    "**1 user a day = 1 buyer conversation of 10 AI turns.** Voice minutes, WhatsApp messages, emails and staff-call minutes grow with "
    "AI turns at the same ratios as the repo's \"Light\" level (per AI turn: 0.107 voice minutes, 0.279 WhatsApp messages, 0.143 "
    "emails out, 0.0026 call minutes). 50 users a day ≈ the plan's Light level (15,000 vs 14,000 turns).",
    "Change `TURNS_PER_USER` in `scripts/users_per_day_costs.py` and rerun to see other numbers.",
], "FFF6DD")
uu = ("50", "100", "150")
h("Usage per month", 2)
table(["", "50 users / day", "100 users / day", "150 users / day"], [
    ["AI turns", *[f"{U[u]['usage_per_month']['turns']:,.0f}" for u in uu]],
    ["Voice minutes", *[f"{U[u]['usage_per_month']['voice_min']:,.0f}" for u in uu]],
    ["WhatsApp messages", *[f"{U[u]['usage_per_month']['whatsapp']:,.0f}" for u in uu]],
    ["Emails out / in", *[f"{U[u]['usage_per_month']['email_out']:,.0f} / {U[u]['usage_per_month']['email_in']:,.0f}" for u in uu]],
    ["Staff call minutes", *[f"{U[u]['usage_per_month']['call_min']:,.0f}" for u in uu]],
    ["Database growth 🔢", *[f"{U[u]['disk_growth_gb_per_month']} GB" for u in uu]],
    ["Traffic to Claude 🔢", *[f"{U[u]['usage_per_month']['turns'] * 19.4 / 1e6:.2f} GB" for u in uu]],
    ["Voice traffic at the server 🔢 (0.3-1.7 MB / min)", *[f"{U[u]['usage_per_month']['voice_min'] * 0.3 / 1000:.1f}-{U[u]['usage_per_month']['voice_min'] * 1.7 / 1000:.1f} GB" for u in uu]],
], [6.2, 3.6, 3.6, 3.6], size=9)

h("Table A: the PAID stack (the 11 Nov AWS plan, every service paid)", 2)
para("Everything managed on AWS in the UAE: two zones, RDS Multi-AZ, ElastiCache, Fargate, monitoring, AWS support, plus every paid "
     "service. AWS hosting lines are the repo's Light sizes (low) and Normal sizes (high).")
vl = list(U["50"]["vendor_lines"])
UNIT = {"AI replies (Claude Sonnet + Haiku)": "≈ $0.0035 / turn", "WhatsApp (Meta)": "Meta fees", "speech-to-text (Deepgram)": "$0.0092 / min",
        "text-to-speech (ElevenLabs)": "$0.04 / 1k chars", "voice transport (LiveKit Cloud Ship)": "$50 incl. 5,000 min",
        "staff alert calls (Telnyx)": "$0.25 / min + $0.002", "email (SES + Postmark Pro)": "$0.16 / 1k + $16.50"}
table(["Line", "Unit price 💲", "50 users / day", "100 users / day", "150 users / day"],
      [[k, UNIT[k], *[money(U[u]["vendor_lines"][k]) for u in uu]] for k in vl]
      + [["AWS hosting (servers, RDS, cache, network, monitoring, support, Vercel, Phoenix, embeddings, WAF)", "repo lines",
          *[f"{money(U[u]['A_aws_plan_all_paid']['hosting_light'])} - {money(U[u]['A_aws_plan_all_paid']['hosting_normal'])}" for u in uu]]]
      + [["**Total PAID**", "", *[f"**{money(U[u]['stack_paid']['total_low'])} - {money(U[u]['stack_paid']['total_high'])}**" for u in uu]]],
      [5.3, 3.4, 2.8, 2.8, 2.8], size=8.3)

h("Table B: the FREE stack (as free as possible)", 2)
para("Every service with a free platform runs free: one Namecheap server + Coolify for the app, database, cache, BGE-M3, Phoenix, "
     "SafeLine, CrowdSec, LiveKit, Piper, Postal, Stalwart and Asterisk, plus a **GPU server** for the free speech-to-text (Namecheap "
     "has no GPU). Still paid: Claude, WhatsApp, phone minutes, Vercel.")
fi = list(U["50"]["stack_free"]["items"])
table(["Line", "50 users / day", "100 users / day", "150 users / day"],
      [[k, *[money(U[u]["stack_free"]["items"][k]) for u in uu]] for k in fi]
      + [["**Total FREE**", *[f"**{money(U[u]['stack_free']['total'])}**" for u in uu]]], [8.0, 3.0, 3.0, 3.0], size=8.5)
para(f"Server needs: ≈ {FR['total']} GiB RAM and ≈ {UPD['stack_disk_gb']['free']['total']} GB of images on the Namecheap server, plus a "
     "GPU server with ≈ 3.5 GB of VRAM. The GPU server alone costs more than all the paid voice services at these volumes.", size=9.5)

h("Table C: the RECOMMENDED stack (paid + free mix)", 2)
para("The best measured performance per field (chapter 4): free where it performed as well or better and saves money; paid where the "
     "free one needs a GPU, puts email delivery at risk or saves almost nothing.")
hi_ = list(U["50"]["stack_hybrid"]["items"])
table(["Line", "50 users / day", "100 users / day", "150 users / day"],
      [[k, *[money(U[u]["stack_hybrid"]["items"][k]) for u in uu]] for k in hi_]
      + [["**Total RECOMMENDED**", *[f"**{money(U[u]['stack_hybrid']['total'])}**" for u in uu]]], [8.0, 3.0, 3.0, 3.0], size=8.5)
para(f"Not included: ElevenLabs for Tamil / Sinhala calls only, at ≈ ${U['50']['stack_hybrid']['elevenlabs_per_min_for_ta_si_calls']} per "
     f"minute of those calls (their share is not known). Server needs: ≈ {HR['total']} GiB RAM, ≈ "
     f"{UPD['stack_disk_gb']['hybrid']['total']} GB of images, no GPU.", size=9.5)

h("Table D: the three stacks compared in every way", 2)
table(["", "PAID (AWS plan)", "FREE (max)", "✅ RECOMMENDED"], [
    ["Cost at 50 users / day", f"{money(U['50']['stack_paid']['total_low'])} - {money(U['50']['stack_paid']['total_high'])}", money(U["50"]["stack_free"]["total"]), f"**{money(U['50']['stack_hybrid']['total'])}**"],
    ["Cost at 100 users / day", f"{money(U['100']['stack_paid']['total_low'])} - {money(U['100']['stack_paid']['total_high'])}", money(U["100"]["stack_free"]["total"]), f"**{money(U['100']['stack_hybrid']['total'])}**"],
    ["Cost at 150 users / day", f"{money(U['150']['stack_paid']['total_low'])} - {money(U['150']['stack_paid']['total_high'])}", money(U["150"]["stack_free"]["total"]), f"**{money(U['150']['stack_hybrid']['total'])}**"],
    ["Servers", "AWS Fargate + RDS + ElastiCache", "Namecheap 64 GB + GPU server", "Namecheap 64 GB"],
    ["RAM in use", "≈ 26 GB reserved on AWS (repo doc)", f"≈ {FR['total']} GiB + 3.5 GB VRAM", f"≈ {HR['total']} GiB"],
    ["Disk for images / models", "managed by AWS", f"≈ {UPD['stack_disk_gb']['free']['total']} GB", f"≈ {UPD['stack_disk_gb']['hybrid']['total']} GB"],
    ["Voice reply time (bench)", "vendors not tested live (fakes)", "p50 4.7 s (GPU chain, 10 callers)", "Piper first audio 0.1-0.3 s; Deepgram not tested live"],
    ["Languages", "all 6 (ElevenLabs; Deepgram has ta)", "no Tamil / Sinhala voice", "Piper 4 + ElevenLabs for ta / si"],
    ["Data location", "UAE", "USA (Namecheap) + UAE GPU", "USA (Namecheap)"],
    ["Failover / backups", "two zones, managed", "none unless added", "none unless added"],
    ["Who looks after it", "AWS (managed)", "you (most work)", "you, with Coolify"],
    ["Firewall", "AWS WAF + Turnstile", "SafeLine + CrowdSec", "SafeLine + CrowdSec"],
], [3.6, 4.4, 4.4, 4.6], size=8.5)
image("14_stacks_chart.png", 16.5)

h("Table E: service by service, paid vs free", 2)
sv = RANK["saving_per_month_light_normal_heavy"]
table(["Service", "Paid (plan) 💲 L / N / H", "Free platform", "Free cost", "Saving L / N / H 🔢", "Verdict"], [
    ["Voice (STT + TTS + transport)", "91 / 279 / 930", "faster-whisper + Piper + LiveKit", "GPU server $721", " / ".join(f"{x:+.0f}" for x in sv["voice chain (STT+TTS+transport) on g6.xlarge"]), "paid wins unless Heavy"],
    ["Text-to-speech only", "27 / 135 / 450", "Piper on CPU", "$0 extra", "+27 / +135 / +450", "**free wins** (no ta / si)"],
    ["Voice transport only", "50 / 75 / 250", "LiveKit server", "$0 extra", "+50 / +75 / +250", "**free wins**"],
    ["Alert calls", "10 / 46 / 152", "Asterisk", "$0 + minutes", "≈ +0.07 / +0.36 / +1.20", "no real saving"],
    ["Email", "17 / 19 / 22", "Postal + Stalwart", "$0 extra", "+17 / +19 / +22", "works; delivery risk"],
    ["Firewall", "10 / 11 / 14", "SafeLine", "$0 extra", "+10 / +11 / +14", "**free wins**"],
    ["AI tracing", "Langfuse Cloud 29 / 47 / 105", "Phoenix", "$0 extra", "+29 / +47 / +105", "**free wins**"],
    ["Knowledge search", "plan container 44", "BGE-M3", "$0 extra", "+44", "**free wins**"],
    ["Database + cache", "RDS + ElastiCache 168 / 370 / 459", "PostgreSQL + Redis", "$0 extra", "+168 / +370 / +459", "free wins; you do backups"],
    ["AI brain, WhatsApp", "49-735, 26-756", "none", "—", "0", "stay paid"],
], [3.0, 3.0, 3.0, 2.2, 3.2, 2.6], size=8.3)
para(f"\"$0 extra\" = no extra cost on a server you already pay for; the server ({money(UPD['namecheap_plans'][UPD['namecheap_pick']])}/month) "
     "is counted once, in Tables B and C.", size=9, italic=True)
image("12_voice_cost_chart.png", 14.0, "Voice is the one place where \"free\" needs expensive hardware.")
image("10_ram_chart.png", 15.0)
callout("🏆  The bottom line", [
    f"The **recommended** mix costs **{money(U['50']['stack_hybrid']['total'])} / {money(U['100']['stack_hybrid']['total'])} / "
    f"{money(U['150']['stack_hybrid']['total'])} a month** at 50 / 100 / 150 users a day: about a quarter to a third of the all-paid AWS plan.",
    "Going **fully free** is *more* expensive at these volumes, because free speech-to-text needs a GPU server.",
    "The saving comes from hosting and from self-hosting the light services (database, cache, knowledge search, tracing, firewall, "
    "voice transport, text-to-speech). Claude, WhatsApp and phone minutes stay paid in every stack.",
    "What you give up: UAE data location, two-zone failover, managed backups and monitoring. Arifa must approve the US location first.",
], "E6F7F5")
page_break()

# ================================================================================================ GLOSSARY
h("Glossary (plain words)", 1)
table(["Word", "Meaning"], [
    ["AI turn", "One buyer message and the AI's answer to it"], ["RAM / GiB / MiB", "Short-term memory; 1 GiB ≈ 1,074 MB, 1 MiB ≈ 1.05 MB"],
    ["Core / vCPU", "One worker inside the processor; \"0.5 core\" = half of one, busy all the time"],
    ["p50 / p95 / max", "Half / 95 % of answers were faster than this; max = the slowest"],
    ["RTF (real-time factor)", "Seconds of work per second of audio: 0.04 = 25× faster than talking; above 1 = too slow live"],
    ["GPU / VRAM", "Graphics card and its memory; speech AI runs 20× faster on it"],
    ["Container / image", "A packaged program (image, on disk) and a running copy of it (container)"],
    ["Webhook", "A message another service (like Meta) sends to your server when something happens"],
    ["SIP / SIP trunk / G.711", "Internet phone calls / the paid line to real phones / the standard phone audio format"],
    ["WAF", "Web Application Firewall: the guard that checks every web request"], ["Embedding / vector", "1,024 numbers that capture a sentence's meaning"],
    ["MRR", "How high the right answer was ranked on average (1.0 = always first)"], ["Span / trace", "One recorded AI call / a group of calls for one answer"],
    ["DKIM", "A digital stamp proving an email came from your domain"], ["JMAP / IMAP", "Ways for programs to read a mailbox"],
    ["Coolify", "A free dashboard that deploys and watches your apps on your own server"],
], [4.0, 13.0], size=9)
h("Where everything is", 2)
bullets(["This guide: `resource-usage-bench/docs/Kairox_Free_Stack_Guide.docx` (rebuild: `scripts/guide/diagrams.py`, then `build_guide.py`).",
         "Detailed text: `docs/RESOURCE_USAGE.md`, `docs/RANK.md`, round files `docs/R1_RESULTS.md` … `R6_R8_RESULTS.md`.",
         "Raw data: `results/`. Cost scripts: `scripts/rank_costs.py`, `scripts/users_per_day_costs.py`. Sources: `repos/SOURCES.md`.",
         "Prices: the repo's price documents, AWS's price list files, namecheap.com configure pages (indexed), coolify.io."])

emoji_fonts(doc)
script_fonts(doc)
for target in [OUT] + [OUT.with_name(f"{OUT.stem}_v{i}.docx") for i in range(2, 10)]:
    try:  # a copy that is open in Word is locked: fall back to the next free name
        doc.save(target)
        print("saved", target)
        break
    except PermissionError:
        print("locked (open in Word):", target.name)
