"""§8.1 "When the buyer says STOP": the rules that run before any AI (pure, no I/O; D18).

Step 1 (match first, rules not AI):
* the word is the whole message ("STOP", "Stop.", "unsubscribe") -> always an opt-out;
* clear phrases ("don't message me", "remove me") anywhere -> an opt-out;
* a stop word inside a sentence ("stop, let me check", "stop, I meant 5,000") -> the meaning decides: the
  AI reads it (step 6) and may ADD a stop, never remove one.
Step 6 channel stops: "use email instead" stops WhatsApp only; "don't call me" stops calls only.
Step 10 voice: "stop" means "wait"; the call ends only on a clear request; "stop calling me" stops calls.

Languages: the SRS names English and "the Arabic and Hindi equivalents (e.g. إيقاف)". Tamil, Chinese and
Sinhala stop words are not listed (OPEN_QUESTIONS #42): those messages go to the AI, which may add a stop.
Confirmation texts (step 3) other than English are Kairox drafts for native-speaker + lawyer review (#41).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

Kind = Literal["opt_out", "channel_stop", "context", "none"]
VoiceKind = Literal["wait", "end_call", "phone_stop", "unclear", "none"]
Scope = Literal["all", "whatsapp", "email", "phone"]

# whole-message stop words -> language (step 1: "STOP, UNSUBSCRIBE, CANCEL, END and QUIT, the Arabic and
# Hindi equivalents (e.g. إيقاف)")
STOP_WORDS: Final[dict[str, str]] = {
    "stop": "en",
    "stop all": "en",
    "stopall": "en",
    "unsubscribe": "en",
    "cancel": "en",
    "end": "en",
    "quit": "en",
    "opt out": "en",
    "optout": "en",
    "إيقاف": "ar",
    "ايقاف": "ar",
    "توقف": "ar",
    "الغاء": "ar",
    "إلغاء": "ar",
    "الغاء الاشتراك": "ar",
    "إلغاء الاشتراك": "ar",
    "रोकें": "hi",
    "रोको": "hi",
    "बंद": "hi",
    "बंद करो": "hi",
    "बंद करें": "hi",
    "अनसब्सक्राइब": "hi",
}
# words that, inside a sentence, hand the message to the AI (step 1 "context decides")
_IN_SENTENCE: Final = ("stop", "unsubscribe", "opt out", "إيقاف", "ايقاف", "توقف", "रोको", "रोकें", "बंद")
# clear opt-out phrases anywhere in the message (step 1)
OPT_OUT_PHRASES: Final[dict[str, str]] = {
    "don't message me": "en",
    "do not message me": "en",
    "stop messaging me": "en",
    "stop sending me messages": "en",
    "stop sending messages": "en",
    "stop texting me": "en",
    "don't contact me": "en",
    "do not contact me": "en",
    "stop contacting me": "en",
    "remove me": "en",
    "remove my number": "en",
    "unsubscribe me": "en",
    "take me off your list": "en",
    "no more messages": "en",
    "لا ترسل لي": "ar",
    "لا تراسلني": "ar",
    "توقف عن مراسلتي": "ar",
    "أوقف الرسائل": "ar",
    "احذف رقمي": "ar",
    "मुझे मैसेज मत करो": "hi",
    "मैसेज मत भेजो": "hi",
    "मुझे संदेश मत भेजो": "hi",
}
# step 6: one channel only
CHANNEL_PHRASES: Final[dict[str, tuple[Scope, str]]] = {
    "use email instead": ("whatsapp", "en"),
    "email me instead": ("whatsapp", "en"),
    "send it by email": ("whatsapp", "en"),
    "contact me by email": ("whatsapp", "en"),
    "email only": ("whatsapp", "en"),
    "don't call me": ("phone", "en"),
    "do not call me": ("phone", "en"),
    "stop calling me": ("phone", "en"),
    "no calls": ("phone", "en"),
    "no phone calls": ("phone", "en"),
    "لا تتصل بي": ("phone", "ar"),
    "मुझे कॉल मत करो": ("phone", "hi"),
}
# step 10 voice: a clear wish to end the call (not a stop on messages)
VOICE_END: Final = ("i don't want this call", "i do not want this call", "goodbye", "bye", "end the call")
VOICE_WAIT: Final = ("stop", "wait", "stop, let me talk", "hold on", "one moment")

_SPACES: Final = re.compile(r"\s+")
_APOSTROPHES: Final = str.maketrans({chr(0x2019): "'", chr(0x2018): "'", "`": "'"})


def _edge(ch: str) -> bool:
    """Punctuation, symbols, separators and controls (not letters or combining vowel signs: Devanagari,
    Tamil and Sinhala vowels are marks, which a regex non-word class would wrongly strip)."""
    return unicodedata.category(ch)[0] in "PSZC"


def normalise(text: str) -> str:
    """Case-fold, unify apostrophes, collapse spaces, trim punctuation at the ends ("Stop." == "stop")."""
    t = _SPACES.sub(" ", unicodedata.normalize("NFKC", text).translate(_APOSTROPHES).casefold())
    start, end = 0, len(t)
    while start < end and _edge(t[start]):
        start += 1
    while end > start and _edge(t[end - 1]):
        end -= 1
    return t[start:end]


def _contains(haystack: str, needle: str) -> bool:
    """Whole-word containment (Latin words get word boundaries; other scripts plain containment)."""
    if needle.isascii():
        return re.search(rf"(?<![\w']){re.escape(needle)}(?![\w'])", haystack) is not None
    return needle in haystack


@dataclass(frozen=True, slots=True)
class StopMatch:
    kind: Kind
    scope: Scope = "all"
    rule: str = ""  # which rule decided (logged permanently, step 9)
    language: str = "en"
    reason: str | None = None  # the buyer's own words besides the stop ("stop, found cheaper"), step 4

    @property
    def stops(self) -> bool:
        return self.kind in ("opt_out", "channel_stop")


def _leftover(text: str, phrase: str) -> str | None:
    rest = normalise(normalise(text).replace(phrase, " ", 1))
    return rest if len(rest) >= 3 else None


def match(text: str) -> StopMatch:
    """Rules for a written message (WhatsApp, email, website chat)."""
    t = normalise(text)
    if not t:
        return StopMatch("none")
    if (lang := STOP_WORDS.get(t)) is not None:
        return StopMatch("opt_out", "all", f"word:{t}", lang)
    for phrase, (scope, lang) in CHANNEL_PHRASES.items():
        if _contains(t, phrase):
            return StopMatch("channel_stop", scope, f"channel:{phrase}", lang, _leftover(t, phrase))
    for phrase, lang in OPT_OUT_PHRASES.items():
        if _contains(t, phrase):
            return StopMatch("opt_out", "all", f"phrase:{phrase}", lang, _leftover(t, phrase))
    for word in _IN_SENTENCE:
        if _contains(t, word):
            return StopMatch("context", "all", f"in_sentence:{word}", STOP_WORDS.get(word, "en"))
    return StopMatch("none")


def match_voice(text: str) -> VoiceKind:
    """§8.1 step 10: on a call "stop" means wait; only a clear request ends the call or stops calls."""
    t = normalise(text)
    if not t:
        return "none"
    for phrase, (scope, _) in CHANNEL_PHRASES.items():
        if scope == "phone" and _contains(t, phrase):
            return "phone_stop"
    if any(_contains(t, p) for p in VOICE_END):
        return "end_call"
    if t in VOICE_WAIT or t.startswith("stop, let me") or t.startswith("wait"):
        return "wait"
    if any(_contains(t, w) for w in _IN_SENTENCE):
        return "unclear"
    return "none"


# step 3: may say the stop is done, what stops, how to restart; never an apology, button, question, offer
CONFIRMATION: Final[dict[str, str]] = {
    "en": "You're unsubscribed from {company} messages. You won't hear from us again. "
    "To restart, message us any time.",
    "ar": "تم إلغاء اشتراكك في رسائل {company}. لن تصلك منا رسائل بعد الآن. "
    "لإعادة التفعيل، راسلنا في أي وقت.",
    "hi": "आपको {company} के संदेशों से अनसब्सक्राइब कर दिया गया है। अब आपको हमसे कोई संदेश नहीं मिलेगा। "
    "फिर से शुरू करने के लिए, कभी भी हमें संदेश भेजें।",
    "ta": "{company} செய்திகளிலிருந்து நீங்கள் விலகிவிட்டீர்கள். இனி எங்களிடமிருந்து செய்திகள் வராது. "
    "மீண்டும் தொடங்க, எப்போது வேண்டுமானாலும் எங்களுக்குச் செய்தி அனுப்புங்கள்.",
    "zh": "您已退订 {company} 的消息。我们不会再联系您。如需重新开始，请随时给我们发消息。",
    "si": "ඔබ {company} පණිවිඩ වලින් ඉවත් කර ඇත. ඔබට අපෙන් තවත් පණිවිඩ නොලැබේ. "
    "නැවත ආරම්භ කිරීමට, ඕනෑම වේලාවක අපට පණිවිඩයක් එවන්න.",
}
FORBIDDEN_IN_CONFIRMATION: Final = ("sorry", "?", "discount", "offer", "pause")


def confirmation_text(language: str | None, company: str) -> str:
    return CONFIRMATION.get((language or "en").split("-")[0], CONFIRMATION["en"]).format(company=company)
