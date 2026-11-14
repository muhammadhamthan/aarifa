"""Harder retrieval test for the embedding models (Round 4 add-on).

The easy test in embed_bench.py has 8 clearly different facts, so every good model scores 100 %. Here each fact
gets a look-alike that answers a different question on the same topic (750 ml WITH lid vs 1 litre WITHOUT lid,
minimum order vs free samples, carton count vs pallet count, payment terms vs VAT ...). 16 facts, 38 questions
in English / Arabic / Hindi / Chinese.

  python quality_hard.py --url http://localhost:28081 --format qwen3|e5|plain --out results/r4_hard_<name>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from embed_bench import FACTS, LABELLED, as_passage, as_query, cos, embed

DISTRACTORS: dict[str, dict[str, str]] = {
    "lid1000": {
        "en": "The 1000 ml aluminium container is sold without a lid; lids for it are ordered separately.",
        "ar": "تُباع حاوية الألمنيوم سعة 1000 مل بدون غطاء، ويُطلب الغطاء الخاص بها بشكل منفصل.",
    },
    "emboss": {
        "en": "Embossing a logo into the base of the tray is not available; only printing is offered.",
        "ar": "لا يتوفر نقش الشعار بارزاً في قاعدة الصينية، نقدم الطباعة فقط.",
    },
    "samples": {
        "en": "Before ordering, we send up to 20 free samples per size; the buyer pays the courier.",
        "ar": "قبل الطلب نرسل حتى 20 عينة مجانية لكل مقاس، ويدفع المشتري رسوم الشحن.",
    },
    "pickup": {
        "en": "Buyers can collect goods from our Ajman factory, Monday to Saturday, 8 am to 5 pm.",
        "ar": "يمكن للمشترين استلام البضائع من مصنعنا في عجمان من الاثنين إلى السبت من 8 صباحاً حتى 5 مساءً.",
    },
    "paperlid": {
        "en": "Paper-board lids are not oven safe; remove the lid before heating the tray.",
        "zh": "纸板盖不能进烤箱，加热托盘前请先取下盖子。",
    },
    "pallet": {
        "en": "One pallet holds 40 cartons, and a 20 ft container holds 10 pallets.",
        "ar": "تحمل المنصة الواحدة 40 كرتوناً، وتتسع حاوية 20 قدماً لعشر منصات.",
    },
    "msds": {
        "en": "A material safety data sheet for the 8011 alloy is available on request.",
        "ar": "ورقة بيانات سلامة المواد لسبيكة 8011 متوفرة عند الطلب.",
    },
    "vat": {
        "en": "All prices are quoted in AED and exclude 5% VAT.",
        "ar": "جميع الأسعار بالدرهم الإماراتي ولا تشمل ضريبة القيمة المضافة 5%.",
    },
}
HARD_QUESTIONS = [
    ("lid1000", "en", "is the lid included with the one litre container"),
    ("lid1000", "ar", "هل الغطاء مشمول مع علبة اللتر"),
    ("emboss", "en", "can you emboss our logo into the tray bottom"),
    ("samples", "en", "can I get free samples before I order"),
    ("samples", "ar", "هل يمكن الحصول على عينات مجانية"),
    ("pickup", "en", "can we collect the goods from your factory ourselves"),
    ("pickup", "ar", "هل يمكن الاستلام من المصنع في عجمان"),
    ("paperlid", "en", "can the paper lid go in the oven"),
    ("paperlid", "zh", "纸盖可以进烤箱吗"),
    ("pallet", "en", "how many cartons fit on one pallet"),
    ("pallet", "ar", "كم كرتون في المنصة الواحدة"),
    ("msds", "en", "can you send the material safety data sheet"),
    ("vat", "en", "do your prices include VAT"),
    ("vat", "ar", "هل الأسعار تشمل ضريبة القيمة المضافة"),
]


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:28081")
    p.add_argument("--model", default="bench")
    p.add_argument("--format", choices=["qwen3", "e5", "plain"], required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    facts = {**FACTS, **DISTRACTORS}
    passages = [(fid, lang, txt) for fid, langs in facts.items() for lang, txt in langs.items()]
    questions = LABELLED + HARD_QUESTIONS
    async with httpx.AsyncClient(base_url=a.url, timeout=600) as c:
        pv = await embed(c, a.model, [as_passage(a.format, t) for _, _, t in passages])
        qv = await embed(c, a.model, [as_query(a.format, q) for _, _, q in questions])
    rows = []
    for (fid, lang, q), v in zip(questions, qv):
        ranked = sorted(range(len(passages)), key=lambda i: -cos(v, pv[i]))
        top = passages[ranked[0]][0]
        first = next(n for n, i in enumerate(ranked, 1) if passages[i][0] == fid)
        rows.append({"want": fid, "lang": lang, "q": q, "got": top, "rank": first,
                     "margin": round(cos(v, pv[ranked[0]]) - cos(v, pv[ranked[1]]), 4)})
    n = len(rows)
    res = {  # every fact has a look-alike, so every question here is a "confusable pair" question
        "format": a.format, "passages": len(passages), "questions": n,
        "top1_all": round(sum(r["got"] == r["want"] for r in rows) / n, 3),
        "top1_distractor_questions": round(sum(r["got"] == r["want"] for r in rows if r["want"] in DISTRACTORS)
                                           / len(HARD_QUESTIONS), 3),
        "top1_non_english_questions": round(sum(r["got"] == r["want"] for r in rows if r["lang"] != "en")
                                            / sum(1 for r in rows if r["lang"] != "en"), 3),
        "mrr": round(sum(1 / r["rank"] for r in rows) / n, 3),
        "misses": [r for r in rows if r["got"] != r["want"]],
    }
    a.out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "misses"}, ensure_ascii=False), len(res["misses"]), "misses")


if __name__ == "__main__":
    asyncio.run(main())
