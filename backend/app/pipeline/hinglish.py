"""Hinglish (Hindi-English code-mixed) transcript repair.

Deepgram nova-3 with `language=multi` is the only hosted ASR here that keeps
English words in English inside Hindi speech. Two things still go wrong:

  * it writes some Hindi words in Devanagari, which the caption renderer then
    has to transliterate mechanically (`unidecode` turns "ज़बरदस्त" into
    "zabaradast", and worse, turns a misheard word into nonsense);
  * it mishears common Hinglish phrases that are obvious from context ("bahut
    hi" -> "daayad high").

Keyterm boosting fixes a surprising amount of the first problem by asking the
model for the Latin spelling of the words Indian creators actually use. The
second needs language context, so a small LLM pass rewrites each utterance into
readable Roman Hinglish and we re-time the corrected words across the original
utterance. Word count may change; the utterance's start/end never does, so clip
boundaries and cue timing stay anchored.
"""

from __future__ import annotations

import difflib
import json

# Nova-3 accepts up to 100 keyterms. These are the words that show up in almost
# every Hinglish video and are cheap to bias; the title and the user's request
# are appended at runtime.
HINGLISH_TERMS = [
    # Ordered by how much they improve decoding. The first ~20 are the words
    # nova-3 most often turns into an unreadable Devanagari spelling; putting
    # them first also keeps the request inside Deepgram's token budget.
    "bahut", "hi", "hai", "hain", "kya", "yaar", "nahi", "aaj", "hum", "baat",
    "karenge", "dekhte", "kaam", "karta", "chalo", "kaise", "zabardast",
    "feature", "update", "honestly", "bhai", "toh", "bhi", "abhi", "acha",
    "achha", "aur", "agar", "aisa", "aisi", "bad", "bohot", "bolo",
    "chahiye", "dekho", "dimaag", "ek", "fir", "firse", "haan", "hoga",
    "hogi", "inka", "iska", "jab", "jaisa", "karna", "karte", "kyun", "log",
    "matlab", "mast", "nahin", "paisa", "problem", "sach", "sahi", "samajh",
    "scene", "seedha", "thoda", "yeh", "zyada", "bawaal", "bakwaas", "faltu",
    "jugaad", "maza", "mazaa", "pagal", "pura", "poora", "sasta", "time",
    "video", "vibe", "wala", "wali", "bas", "ko", "me", "mein", "na", "par",
    "se", "tha", "thi", "the", "unka", "uska", "wahi", "kyunki", "lekin",
    "magar",
]


def keyterms(title: str = "", intent: str = "", extra=None, limit: int = 100):
    """Latin Hinglish terms to bias nova-3 toward, title/intent included."""
    out, seen = [], set()

    def add(term):
        term = (term or "").strip().strip(".,!?;:\"'()[]")
        if not term or len(term) > 40:
            return
        low = term.lower()
        if low in seen:
            return
        seen.add(low)
        out.append(term)

    # The first core terms improve decoding most; the title and request follow
    # so names still get through. Deepgram rejects requests whose keyterms
    # exceed its token budget, so stop at 60 terms / 420 characters rather than
    # letting the request fall back to no boosting at all.
    for term in HINGLISH_TERMS[:20]:
        add(term)
    for source in (title, intent):
        for token in (source or "").replace("/", " ").split():
            if len(token) >= 3:
                add(token)
    for term in HINGLISH_TERMS[20:]:
        add(term)
    for term in extra or []:
        add(term)

    capped, chars = [], 0
    for term in out:
        if len(capped) >= min(limit, 60):
            break
        if chars + len(term) > 420:
            break
        capped.append(term)
        chars += len(term)
    return capped


def has_devanagari(utterances) -> bool:
    for utterance in utterances or []:
        if any(0x0900 <= ord(c) <= 0x097F
               for c in (utterance.get("transcript") or "")):
            return True
        if any(0x0900 <= ord(c) <= 0x097F
               for c in (utterance.get("words") and "".join(
                   (w.get("punctuated_word") or w.get("word") or "")
                   for w in utterance["words"]) or "")):
            return True
    return False


# Explicit examples matter more than instructions here. Without the "bahut hi"
# example the model normalises the script but leaves the misheard word alone.
_PROMPT = """You are fixing an automatic Hinglish (Hindi-English code-mixed) transcript
for burned-in captions. The speaker is an Indian creator. The ASR mixes
Devanagari and Latin and sometimes confuses similar-sounding words.

For every line:
- Rewrite it as natural, readable Hinglish in ROMAN script. Keep English words
  in English.
- Fix obvious ASR mistakes using context. Example: "feature bahut high,
  zabardast high" is almost certainly "feature bahut hi zabardast hai".
- Keep the speaker's meaning and content. Do not add claims, names, numbers or
  details. Do not summarise. Do not translate into pure Hindi or pure English.
- Prefer the spelling a young Indian creator actually types: "hai" not "haiṁ",
  "kya" not "kyaa", "nahi" not "nahii".
- If a line is already good English, leave it as it is.

Return JSON only: {{"lines":[{{"i":<input index>,"text":"<corrected line>"}}]}}

INPUT:
{lines}
"""


def _norm(token: str) -> str:
    return (token or "").lower().strip(".,!?;:\"'()[]")


def _distribute(start, end, tokens):
    """Character-weighted timing for tokens with no counterpart."""
    total = sum(max(1, len(t)) for t in tokens) or 1
    cursor = start
    out = []
    for token in tokens:
        span = (end - start) * max(1, len(token)) / total
        out.append((round(cursor, 3), round(cursor + span, 3)))
        cursor += span
    return out


def retime(original_words, text: str, start: float, end: float):
    """Give corrected tokens timings inside the original utterance.

    Exact matches keep their original timings, so a word the ASR got right does
    not drift. Runs that the rewrite introduced are interpolated between the
    surrounding anchors.
    """
    tokens = (text or "").split()
    if not tokens:
        return []
    if not original_words:
        return [{"word": t, "punctuated_word": t, "start": s, "end": e,
                 "confidence": 1.0}
                for t, (s, e) in zip(tokens, _distribute(start, end, tokens))]

    old = [_norm(w.get("punctuated_word") or w.get("word") or "")
           for w in original_words]
    new = [_norm(t) for t in tokens]
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    times = [None] * len(tokens)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            src = original_words[block.a + k]
            times[block.b + k] = (float(src.get("start", start)),
                                  float(src.get("end", end)))

    # Leading and trailing runs.
    first = next((i for i, t in enumerate(times) if t), None)
    if first is None:
        return [{"word": t, "punctuated_word": t, "start": s, "end": e,
                 "confidence": 1.0}
                for t, (s, e) in zip(tokens, _distribute(start, end, tokens))]
    if first > 0:
        for k, (s, e) in enumerate(
                _distribute(start, times[first][0], tokens[:first])):
            times[k] = (s, e)
    last = max(i for i, t in enumerate(times) if t)
    if last < len(tokens) - 1:
        for k, (s, e) in enumerate(
                _distribute(times[last][1], end, tokens[last + 1:]),
                start=last + 1):
            times[k] = (s, e)

    # Interior gaps between two anchors.
    i = 0
    while i < len(tokens):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and times[j] is None:
            j += 1
        gap_start = times[i - 1][1]
        gap_end = times[j][0] if j < len(tokens) else end
        for k, (s, e) in enumerate(_distribute(gap_start, gap_end, tokens[i:j])):
            times[i + k] = (s, e)
        i = j

    # Clamp and keep monotonic; ASR word boundaries are allowed to touch.
    out = []
    previous = start
    for token, (s, e) in zip(tokens, times):
        s = max(start, min(float(s), end))
        e = max(s, min(float(e), end))
        s = max(s, previous)
        e = max(e, s)
        previous = s
        out.append({"word": token, "punctuated_word": token,
                    "start": round(s, 3), "end": round(e, 3),
                    "confidence": 1.0})
    return out


def clean(utterances, chat=None, model=None, chunk=30):
    """Rewrite Hinglish utterances in place. Returns the same list.

    `chat` is injectable so this is testable without an API call. Any failure
    leaves the original transcript alone: captions that are merely transliterated
    are better than captions that disappeared.
    """
    if not utterances:
        return utterances
    if chat is None:
        from app.pipeline import analyzer
        chat = analyzer._openai_chat

    for offset in range(0, len(utterances), chunk):
        block = utterances[offset:offset + chunk]
        payload = [{"i": offset + i,
                    "text": u.get("transcript") or ""}
                   for i, u in enumerate(block)]
        try:
            data = chat(_PROMPT.format(lines=json.dumps(
                payload, ensure_ascii=False, indent=1)),
                temperature=0.1, model=model)
        except Exception as exc:
            print(f"[clipper] Hinglish cleanup skipped ({exc.__class__.__name__})")
            return utterances

        corrected = {}
        for entry in data.get("lines", []):
            try:
                idx = int(entry["i"])
                text = str(entry.get("text") or "").strip()
            except (KeyError, TypeError, ValueError):
                continue
            if text:
                corrected[idx] = text

        for i, utterance in enumerate(block):
            text = corrected.get(offset + i)
            if not text or text == (utterance.get("transcript") or ""):
                continue
            words = utterance.get("words") or []
            utterance["asr_transcript"] = utterance.get("transcript")
            utterance["transcript"] = text
            if words:
                corrected_words = retime(
                    words, text,
                    float(utterance.get("start", 0.0)),
                    float(utterance.get("end", 0.0)))
                # Diarization labels live on the words, not only the utterance.
                # Re-timing builds new dicts, so put the label back or active
                # speaker cuts lose their mapping and fall back to mouth motion.
                speaker = utterance.get("speaker")
                if speaker is not None:
                    for word in corrected_words:
                        word["speaker"] = speaker
                utterance["words"] = corrected_words
    return utterances


def is_hinglish(utterances) -> bool:
    return has_devanagari(utterances)


# ---------------------------------------------------------------------------
# Offline romanisation
# ---------------------------------------------------------------------------
# The LLM pass above costs tokens on every Hinglish job, and Devanagari is the
# most expensive script there is to send a model -- several tokens per word.
# Most of what it fixed is mechanical: Deepgram wrote a Hindi word in Devanagari
# and the caption needs it in the Roman spelling creators actually type. That is
# transliteration, and it can be done exactly, per word, with the ASR's own
# timings left untouched -- which the LLM rewrite could not promise.
#
# Two layers. A lexicon for the words that make up most of any Hinglish
# transcript (and English loanwords the Hindi model spells phonetically), where
# rule output would be readable but not how anyone writes it ("nahin" vs
# "nahi", "pheechar" vs "feature"). Rules for everything else: inherent-vowel
# deletion, long vowels only where creators double them, nukta, conjuncts.

LEXICON = {
    # function words and the everyday core
    "है": "hai", "हैं": "hain", "था": "tha", "थी": "thi", "थे": "the",
    "हूँ": "hoon", "हूं": "hoon", "हो": "ho", "होगा": "hoga", "होगी": "hogi",
    "होंगे": "honge", "होता": "hota", "होती": "hoti", "होते": "hote",
    "क्या": "kya", "नहीं": "nahi", "नही": "nahi", "हाँ": "haan", "हां": "haan",
    "बहुत": "bahut", "बहोत": "bahot", "भी": "bhi", "ही": "hi", "तो": "toh",
    "यह": "yeh", "ये": "ye", "वह": "woh", "वो": "woh", "मैं": "main",
    "में": "mein", "मुझे": "mujhe", "मुझ": "mujh", "तुम": "tum", "तू": "tu",
    "आप": "aap", "हम": "hum", "कि": "ki", "की": "ki", "का": "ka", "के": "ke",
    "को": "ko", "से": "se", "पर": "par", "और": "aur", "लेकिन": "lekin",
    "मगर": "magar", "क्यों": "kyun", "क्यूँ": "kyun", "क्यूं": "kyun",
    "कैसे": "kaise", "कैसा": "kaisa", "कैसी": "kaisi", "अब": "ab",
    "अभी": "abhi", "फिर": "phir", "यार": "yaar", "भाई": "bhai", "भाइयों": "bhaiyon",
    "अच्छा": "achha", "अच्छी": "achhi", "अच्छे": "achhe", "ठीक": "theek",
    "सही": "sahi", "बात": "baat", "काम": "kaam", "पैसा": "paisa", "पैसे": "paise",
    "लोग": "log", "लोगों": "logon", "कुछ": "kuch", "सब": "sab", "सबसे": "sabse",
    "एक": "ek", "दो": "do", "तीन": "teen", "चार": "chaar", "पांच": "paanch",
    "पाँच": "paanch", "कोई": "koi", "कभी": "kabhi", "सच": "sach",
    "मतलब": "matlab", "बिल्कुल": "bilkul", "ज़्यादा": "zyada", "ज्यादा": "zyada",
    "थोड़ा": "thoda", "थोड़ी": "thodi", "वाला": "wala", "वाली": "wali",
    "वाले": "wale", "रहा": "raha", "रही": "rahi", "रहे": "rahe", "गया": "gaya",
    "गई": "gayi", "गयी": "gayi", "गए": "gaye", "गये": "gaye", "करना": "karna",
    "कर": "kar", "करते": "karte", "करता": "karta", "करती": "karti",
    "किया": "kiya", "दिया": "diya", "लिया": "liya", "देखो": "dekho",
    "देख": "dekh", "चलो": "chalo", "जब": "jab", "तब": "tab", "जो": "jo",
    "उस": "us", "इस": "is", "उन": "un", "इन": "in", "अपना": "apna",
    "अपने": "apne", "अपनी": "apni", "मेरा": "mera", "मेरी": "meri",
    "मेरे": "mere", "तेरा": "tera", "तेरी": "teri", "तेरे": "tere",
    "हमारा": "humara", "हमारी": "humari", "हमारे": "humare", "उनका": "unka",
    "उसका": "uska", "इसका": "iska", "उसकी": "uski", "इसकी": "iski",
    "क्योंकि": "kyunki", "इसलिए": "isliye", "बस": "bas", "ना": "na",
    "ज़बरदस्त": "zabardast", "जबरदस्त": "zabardast", "मज़ा": "maza",
    "मजा": "maza", "पागल": "pagal", "दिमाग": "dimaag", "दिमाग़": "dimaag",
    "समझ": "samajh", "समझो": "samjho", "पता": "pata", "चाहिए": "chahiye",
    "सकता": "sakta", "सकती": "sakti", "सकते": "sakte", "वैसे": "waise",
    "ऐसा": "aisa", "ऐसे": "aise", "ऐसी": "aisi", "वहाँ": "wahan",
    "वहां": "wahan", "यहाँ": "yahan", "यहां": "yahan", "कहाँ": "kahan",
    "कहां": "kahan", "आज": "aaj", "कल": "kal", "साल": "saal", "दिन": "din",
    "रात": "raat", "घर": "ghar", "ज़िंदगी": "zindagi", "जिंदगी": "zindagi",
    "दुनिया": "duniya", "बड़ा": "bada", "बड़ी": "badi", "बड़े": "bade",
    "छोटा": "chhota", "छोटी": "chhoti", "नया": "naya", "नई": "nayi",
    "पहले": "pehle", "बाद": "baad", "शायद": "shayad", "वाकई": "waqai",
    "हमेशा": "hamesha", "ज़रूर": "zaroor", "जरूर": "zaroor", "बोलो": "bolo",
    "बोला": "bola", "बोल": "bol", "सुनो": "suno", "आओ": "aao", "जाओ": "jao",
    "आया": "aaya", "आई": "aayi", "आए": "aaye", "जाना": "jaana", "जा": "ja",
    "कहना": "kehna", "कहा": "kaha", "रहना": "rehna", "बहन": "behen",
    "पहला": "pehla", "पहली": "pehli", "लगता": "lagta", "लगा": "laga",
    "लगी": "lagi", "मिला": "mila", "मिली": "mili", "मिलेगा": "milega",
    "देना": "dena", "लेना": "lena", "सिर्फ": "sirf", "सिर्फ़": "sirf",
    "ज़रा": "zara", "जरा": "zara", "बिलकुल": "bilkul", "एकदम": "ekdum",
    "सच्ची": "sachchi", "भाईसाहब": "bhaisaab", "अरे": "arre", "ओए": "oye",
    "चल": "chal", "जल्दी": "jaldi", "धीरे": "dheere", "बाहर": "bahar",
    "अंदर": "andar", "ऊपर": "upar", "नीचे": "neeche", "आगे": "aage",
    "पीछे": "peeche", "साथ": "saath", "बिना": "bina", "जैसे": "jaise",
    "जैसा": "jaisa", "तरह": "tarah", "वजह": "wajah", "जगह": "jagah",
    "सवाल": "sawaal", "जवाब": "jawab", "दोस्त": "dost", "दोस्तों": "doston",
    "प्यार": "pyaar", "ख़ुद": "khud", "खुद": "khud", "गलत": "galat",
    "ग़लत": "galat", "मुश्किल": "mushkil", "आसान": "aasaan",
    # English loanwords as the Hindi model spells them
    "वीडियो": "video", "विडियो": "video", "फोन": "phone", "फ़ोन": "phone",
    "गेम": "game", "गेम्स": "games", "गेमप्ले": "gameplay", "लेवल": "level",
    "टाइम": "time", "प्रॉब्लम": "problem", "प्रोब्लम": "problem",
    "एक्चुअली": "actually", "बेसिकली": "basically", "लिटरली": "literally",
    "चैनल": "channel", "सब्सक्राइब": "subscribe", "लाइक": "like",
    "कमेंट": "comment", "कमेंट्स": "comments", "मनी": "money",
    "बिजनेस": "business", "बिज़नेस": "business", "कंपनी": "company",
    "मार्केट": "market", "प्रोडक्ट": "product", "रिव्यू": "review",
    "लॉन्च": "launch", "प्राइस": "price", "ऑफर": "offer", "बेस्ट": "best",
    "रियली": "really", "मिशन": "mission", "मैप": "map", "कार": "car",
    "पुलिस": "police", "गन": "gun", "फीचर": "feature", "फ़ीचर": "feature",
    "फीचर्स": "features", "अपडेट": "update", "ऑनेस्टली": "honestly",
    "सीन": "scene", "वाइब": "vibe", "पॉडकास्ट": "podcast",
    "इंटरव्यू": "interview", "स्टोरी": "story", "आइडिया": "idea",
    "प्लान": "plan", "स्टार्टअप": "startup", "फाउंडर": "founder",
    "इन्वेस्ट": "invest", "इन्वेस्टमेंट": "investment", "सेल्स": "sales",
    "ग्रोथ": "growth", "मार्केटिंग": "marketing", "ब्रांड": "brand",
    "कस्टमर": "customer", "टीम": "team", "ऑफिस": "office", "स्कूल": "school",
    "कॉलेज": "college", "जॉब": "job", "सैलरी": "salary", "लाइफ": "life",
    "फ्रेंड": "friend", "फ्रेंड्स": "friends", "फैमिली": "family",
    "मोबाइल": "mobile", "ऐप": "app", "इंटरनेट": "internet", "सोशल": "social",
    "मीडिया": "media", "यूट्यूब": "YouTube", "इंस्टाग्राम": "Instagram",
    "कंटेंट": "content", "क्रिएटर": "creator", "ओके": "okay", "सॉरी": "sorry",
    "थैंक": "thank", "थैंक्स": "thanks", "प्लीज": "please", "प्लीज़": "please",
    "हेलो": "hello", "बाय": "bye", "गाइज": "guys", "गाइज़": "guys",
    "वेलकम": "welcome", "बैक": "back", "नेक्स्ट": "next", "फर्स्ट": "first",
    "सेकंड": "second", "लास्ट": "last", "टॉप": "top", "गुड": "good",
    "बैड": "bad", "कूल": "cool", "ऑसम": "awesome", "क्रेज़ी": "crazy",
    "क्रेजी": "crazy", "सीरियसली": "seriously", "एक्सपीरियंस": "experience",
    "पॉइंट": "point", "बॉस": "boss", "किल": "kill", "विन": "win",
    "गेमर": "gamer", "सर्वर": "server", "प्लेयर": "player",
    "ग्राफिक्स": "graphics", "मैच": "match", "राउंड": "round", "हेड": "head",
    "शॉट": "shot", "हेडशॉट": "headshot", "ग्रुप": "group", "पार्ट": "part",
    "स्टार्ट": "start", "एंड": "end", "ओवर": "over", "डेटा": "data",
    "एआई": "AI", "सिस्टम": "system", "मॉडल": "model", "कोड": "code",
}

_VOWELS = {
    "अ": "a", "आ": "A", "इ": "i", "ई": "I", "उ": "u", "ऊ": "U", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ऍ": "e", "ऑ": "o", "ओ": "o", "औ": "au",
}
_MATRAS = {
    "ा": "A", "ि": "i", "ी": "I", "ु": "u", "ू": "U", "ृ": "ri",
    "ॅ": "e", "े": "e", "ै": "ai", "ॉ": "o", "ो": "o", "ौ": "au",
}
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh",
    "ज": "j", "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh",
    "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "ऩ": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r",
    "ऱ": "r", "ल": "l", "ळ": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s",
    "ह": "h",
    # precomposed nukta forms
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "dh",
    "फ़": "f", "य़": "y",
}
# A combining nukta changes the consonant before it.
_NUKTA = {"k": "q", "kh": "kh", "g": "g", "j": "z", "d": "d", "dh": "dh",
          "ph": "f"}
_DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}
_VIRAMA, _NUKTA_MARK = "्", "़"
_ANUSVARA, _CHANDRABINDU, _VISARGA = "ं", "ँ", "ः"
_LABIALS = ("p", "b", "m")
_PUNCT = {"।": ".", "॥": ".", "ऽ": ""}

# Long vowels are carried as A/I/U through the rules and only spelled at the
# end, because whether "ā" is written "a" or "aa" depends on the word.
_LONG = {"A": ("aa", "a"), "I": ("ee", "i"), "U": ("oo", "u")}


def _syllables(word: str):
    """[[consonants, vowel, nasal]] with vowel None for an inherent schwa."""
    units = []
    for ch in word:
        if ch in _CONSONANTS:
            units.append([_CONSONANTS[ch], None, ""])
        elif ch == _NUKTA_MARK and units and units[-1][1] is None:
            units[-1][0] = _NUKTA.get(units[-1][0], units[-1][0])
        elif ch == _VIRAMA and units:
            units[-1][1] = ""                         # no vowel: a conjunct
        elif ch in _MATRAS and units and units[-1][1] is None:
            units[-1][1] = _MATRAS[ch]
        elif ch in _VOWELS:
            units.append(["", _VOWELS[ch], ""])
        elif ch in (_ANUSVARA, _CHANDRABINDU) and units:
            units[-1][2] = "n"
        elif ch == _VISARGA and units:
            units[-1][2] = "h"
        else:
            units.append([_DIGITS.get(ch, _PUNCT.get(ch, ch)), "", ""])
    return units


def _delete_schwas(units):
    """Mark which inherent vowels are silent, as Hindi speech drops them.

    Final: "कर" is "kar", not "kara". Medial, right to left: a schwa between a voiced
    syllable and a consonant carrying a vowel goes ("करना" -> "karna").
    """
    silent = [False] * len(units)
    n = len(units)

    # A final cluster keeps its vowel only when it ends in r/y/v -- "mitra",
    # "vakya" -- and drops it like any other word otherwise: "dost", "sambandh".
    after_cluster = n > 1 and units[-2][1] == ""
    if (n > 1 and units[-1][1] is None and units[-1][0] and not units[-1][2]
            and not (after_cluster and units[-1][0] in ("r", "y", "v"))):
        silent[-1] = True
    for i in range(n - 2, 0, -1):
        if units[i][1] is not None or not units[i][0] or units[i][2]:
            continue
        left, right = units[i - 1], units[i + 1]
        if (left[1] != "" and not silent[i - 1]
                and right[0] and right[1] != "" and not silent[i + 1]):
            silent[i] = True
    return silent


def _rule_romanize(word: str) -> str:
    units = _syllables(word)
    if not units:
        return word
    silent = _delete_schwas(units)
    short_word = sum(1 for u in units if u[1] != "") <= 2
    out = []
    for i, (cons, vowel, nasal) in enumerate(units):
        out.append(cons)
        if vowel is None:
            vowel = "" if silent[i] else "a"
        if vowel in _LONG:
            # Closed syllable in a short word, or a word-initial long vowel,
            # is where creators double it: kaam, baat, theek, phool, aaj.
            nxt = i + 1
            closed = nxt < len(units) and (
                nxt == len(units) - 1 and silent[nxt]
                or units[nxt][1] == "")
            initial = i == 0 and not cons
            vowel = _LONG[vowel][0] if (short_word and closed) or initial \
                else _LONG[vowel][1]
        out.append(vowel)
        if nasal == "n":
            nxt = units[i + 1][0] if i + 1 < len(units) else ""
            out.append("m" if nxt.startswith(_LABIALS) else "n")
        else:
            out.append(nasal)
    text = "".join(out)
    # Doubled aspirates ("च्छ" -> chchh) are written once in Roman.
    for doubled, single in (("chchh", "chh"), ("kkh", "kkh"), ("jn", "gy")):
        text = text.replace(doubled, single)
    return text


def _has_devanagari(text: str) -> bool:
    return any(0x0900 <= ord(c) <= 0x097F for c in text or "")


def romanize_word(token: str) -> str:
    """One token in the Roman spelling Hinglish creators type.

    Punctuation around the word is kept; Latin tokens are returned unchanged.
    """
    if not _has_devanagari(token):
        return token
    # Split off leading/trailing non-Devanagari punctuation (and the danda).
    start, end = 0, len(token)
    while start < end and not (0x0900 <= ord(token[start]) <= 0x0963
                               or 0x0966 <= ord(token[start]) <= 0x097F):
        start += 1
    while end > start and not (0x0900 <= ord(token[end - 1]) <= 0x0963
                               or 0x0966 <= ord(token[end - 1]) <= 0x097F):
        end -= 1
    head, core, tail = token[:start], token[start:end], token[end:]
    tail = "".join(_PUNCT.get(c, c) for c in tail)
    roman = LEXICON.get(core) or _rule_romanize(core)
    return head + roman + tail


def romanize_text(text: str) -> str:
    return " ".join(romanize_word(t) for t in (text or "").split())


def romanize(utterances):
    """Roman Hinglish in place, word by word. Returns the same list.

    Free and deterministic, so it runs on every job that has Devanagari. Each
    word keeps the ASR's own timing -- one token in, one token out -- which is
    why this does not need the re-timing the LLM rewrite does.
    """
    for utterance in utterances or []:
        text = utterance.get("transcript") or ""
        words = utterance.get("words") or []
        if not (_has_devanagari(text) or any(
                _has_devanagari(w.get("punctuated_word") or w.get("word") or "")
                for w in words)):
            continue
        utterance.setdefault("asr_transcript", text)
        for word in words:
            for key in ("word", "punctuated_word"):
                if word.get(key):
                    word[key] = romanize_word(word[key])
        utterance["transcript"] = (
            " ".join(w.get("punctuated_word") or w.get("word") or "" for w in words)
            if words else romanize_text(text))
    return utterances
