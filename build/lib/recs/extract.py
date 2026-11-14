"""Turn a stream of chat messages into service-pro recommendations ("mentions").

Platform-neutral: any importer that yields `whatsapp.Message`-shaped objects works.
If a message carries extra["id"] and extra["reply_to"] (e.g. Facebook comments),
explicit threading is used instead of the time-window guess WhatsApp needs.

Pipeline
  1. bundle    consecutive messages from one sender within a few minutes
               (WhatsApp people send "Sam at Acme Electric" / "+1 425..." / card as 3 lines)
  2. thread    a request ("any plumber recs?") opens a thread; later bundles from
               other people inherit its trade unless they name a trade themselves
  3. extract   pro name, person name, phones, websites, contact-card flag
  4. judge     sentiment (positive / negative / neutral) and a one-line quote
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlparse

from .trades import TRADES, trades_in
from .whatsapp import Message, first_name

EXTRACTOR_VERSION = 1
BUNDLE_GAP = timedelta(minutes=10)
THREAD_WINDOW = timedelta(hours=72)


# --------------------------------------------------------------------------- data

@dataclass
class Bundle:
    sender: str
    msgs: list[Message]

    @property
    def ts(self) -> datetime:
        return self.msgs[0].ts

    @property
    def ref(self) -> str:
        m = self.msgs[0]
        return str(m.extra.get("id") or f"L{m.line_no}")

    @property
    def text(self) -> str:
        return "\n".join(m.text for m in self.msgs if not m.is_media).strip()

    @property
    def has_card(self) -> bool:
        return any("contact card" in str(m.extra.get("media", "")) for m in self.msgs)


@dataclass
class Mention:
    posted_at: datetime
    sender: str  # raw sender; the importer hashes it and keeps only the first name
    external_ref: str
    trade: str | None
    trades: list[str]
    sentiment: str
    quote: str
    pro_name: str | None = None
    person_name: str | None = None
    phones: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    is_card: bool = False
    request_ref: str | None = None
    request_text: str | None = None  # the question this answered, as a short quote
    request_by: str | None = None    # raw sender of that question (stored as first name only)
    card_owner: str | None = None  # raw sender who shared the contact card (for "+1" on someone else's card)
    via: str = ""  # which rule fired; handy when debugging


# --------------------------------------------------------------------------- word lists

STOP = set("""avoid beware skip stay don't dont never warning join welcome hurry reserve reposting posting update reminder congratulations congrats omg wow interesting cancelled
quote pricing price prices option options sorry following 
i i'm i've i'd i'll we we're we've we'd we'll you'll they'll he'll she'll it'll that's there's here's let's our ours my me mine you your you're he he's him his she she's her hers they
they're their them it it's its this that these those the a an and or but so if in on at for with from to of by as
is are was were be been being do does did have has had can could would will should may might must not no yes yeah yep yup
ok okay hi hello hey thanks thank thankyou thx ty tia please pls plz sure also just here there try tried check checkout
highly recommend recommended recommending recommendation recommendations reco recos recs contact contacts call text
message msg feel free great good very really super amazing awesome best nice lovely wonderful same following any anyone
someone somebody everyone everybody all folks neighbors neighbours friends frnds guys team hope let last next few one two
three first recently usually since both lot lots quite similarly personally absolutely definitely probably maybe
actually sorry oh wow lol haha fyi psa dm pm ah hmm btw ps met used use using got went worked fixed done called reach
give share sharing sharing today tomorrow yesterday tonight morning evening weekend day week month year years am
phone email website name number cell mobile info details reference referral another other more most some many much
looking need needs needed want wanted know knows anybody does who what when where why how which whom whose if cheers
regards update note please kindly hopefully again still now then than too only even ever never always often
""".split())
MONTHS_DAYS = set("""january february march april may june july august september october november december jan feb mar apr
jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun""".split())
PLACES = set("""sammamish issaquah bellevue redmond seattle kirkland renton kent auburn tacoma bothell everett wa washington eastside pnw king county tukwila mercer usa us america india seattle's""".split())
NOT_PROS = set("""amazon costco target home depot hime lowe's lowes best buy pcc whole foods wholefoods tesla bosch viking
whirlpool kenmore elite eclipse sub zero zeros starlink hoa propvivo vivo prop republic usps ups fedex xfinity comcast
facebook instagram google yelp nextdoor whatsapp chatgpt chat gpt youtube zillow zelle venmo ortho spectracide indian
punjabi telugu telangana bollywood diwali deewali halloween christmas thanksgiving karva chauth ac hvac ev dr mr mrs ms
police city republic""".split())
BUSINESS_WORDS = set("""plumbing plumber plumbers electric electrical electrician handyman handymen roofing roof roofers
irrigation services service inc inc. llc llc. co co. company construction builders builder cleaning cleaners repair repairs
glass window windows fencing fence pest control heating cooling air hvac landscaping landscape lawn tree trees detailing
detail appliance appliances solutions home homes remodeling remodel contractors contracting painting painters gutters gutter
lighting lights sprinkler sprinklers backflow backflows maintenance aid care auto""".split())

def _load_local_words() -> None:
    """Neighbourhood-specific vocabulary lives outside the code, in $RECS_HOME/local_words.json:
    {"places": ["subdivision names to ignore"], "business_words": ["words that mark a business name"],
     "not_pros": ["local organisations that are never service pros"]}"""
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    f = _Path(_os.environ.get("RECS_HOME", _Path.home() / ".recs")) / "local_words.json"
    try:
        d = _json.loads(f.read_text())
    except (OSError, ValueError):
        return
    PLACES.update(w.lower() for w in d.get("places", []))
    BUSINESS_WORDS.update(w.lower() for w in d.get("business_words", []))
    NOT_PROS.update(w.lower() for w in d.get("not_pros", []))


NON_HOME_WORDS = set("""contest submission event events festival sale fundraiser party camp class classes workshop webinar insurance insurer school academy clinic clinics pediatrics pediatric dental dentist law lawyer attorney
salon spa restaurant bakery cafe temple church library college university hospital pharmacy realty realtor bank store
stores outlet mall market""".split())
_load_local_words()
SELF_PHONE_RE = re.compile(r"(?:my (?:number|cell|phone)|call me|text me|reach me|contact me|zelle(?: to)?|venmo|my husband|my wife|whatsapp me|speak with me|reach out to me|orders?)(?:[ \t]+\w+){0,2}\W{0,8}$", re.I)
LOWER_STOP = STOP | set("""for and to in on at with from the a is was were are has have had they he she it we this that last
few every since but so as because who which when also too very really before after about again years yrs now already couple
of them him her his their its our us me service work job""".split()) - {"aid"}

# "used X", "recommend X", "try X" ... the X is a name even when typed in lowercase
LEAD_VERBS = r"(?:avoid|stay away from|beware of|(?:don'?t|do not|never|wouldn'?t|would not) (?:use|hire|recommend|call)|(?:done|remodeled|remodelled|fixed|installed|repaired|cleaned|built|serviced) (?:by|from)|used|use|using|recommend|recommended|reco|try|tried|hired|hire|went with|called|go with|going with|happy with|experience with|work(?:ed)? with|there is|there's|check out|check|got (?:it|ours) (?:done|fixed) (?:by|from|with))"
LEAD_RE = re.compile(rf"\b{LEAD_VERBS}\s+(?P<name>[A-Za-z][\w&'’.-]*(?:\s+(?:[A-Z][\w&'’.-]*|&|and|of|[a-z]+))*)", re.I)
# "Sam at Acme Electric", "Joe with Rapid Rooter", "Lee from cozy heating"
PERSON_AT_RE = re.compile(r"(?:^|\b(?:recommend|try|used|call|contact|text)\s+|[\n.!] *)(?P<person>[A-Z][a-z]+)\s+(?:at|with|from)\s+(?P<biz>[A-Za-z][\w&'’.-]*(?:\s+[A-Za-z][\w&'’.-]*){0,3}?)(?=\s+(?:is|has|have|was|are|he|she|they|for|and|in|who)\b|\s*[.,!?:;()\n]|\s*[^\w\s]|$)")
WE_HAVE_RE = re.compile(r"\b(?:[Ww]e|I) (?:have|had|got) (?P<name>[A-Z][\w&'.-]*(?:[ \t]+[A-Z][\w&'.-]*){0,3})")
KNOW_RE = re.compile(r"\bknow an? (?:[a-z]+ ){0,2}(?P<name>[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+)?)")
NAME_IS_RE = re.compile(r"\b(?i:guy|lady|gentleman|person|named|called|contact is|contact name|(?:his|her|their) name(?: is)?|name is|ask for)\s*:?\s+(?P<name>[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+)?)")
IS_NAME_RE = re.compile(r"\b(?P<name>[A-Z][a-z]+) is (?:his|her|their) name\b")
PLUS_ONE_RE = re.compile(r"^\s*(?:\+1|same|same here|ditto|agree[ds]?|second(?:ed)? (?:this|that|it)|seconded)\s*(?:for\s+)?(?P<rest>.*)$", re.I)
SUBJECT_VERBS = r"(?:is|was|has|have|had|did|does|do|helped|helps|came|comes|lives|cooks|cleans|worked|works|fixed|installed|replaced|charges|takes|seems|offers)"

PHONE_RE = re.compile(r"(?<![\w/])(?:\+?1[\s.\-]?)?\(?\s*(\d{3})\s*\)?[\s.\-]*(\d{3})[\s.\-]*(\d{4})(?![\w/])")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|co|us|io|biz|info)(?:/[^\s<>()\"']*)?\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
JUNK_HOSTS = ("chat.whatsapp.com", "docs.google.com", "forms.gle", "forms.office.com", "gofund.me", "gofundme.com", "a.co",
              "amazon.com", "zillow.com", "youtube.com", "youtu.be", "lnkd.in", "tinyurl.com", "luma.com", "reddit.com",
              "mealtrain.com", "drive.google.com", "medium.com", "c.org", "zeffy.com")

REQUEST_RE = re.compile(
    r"\b(?:any|anyone|anybody|someone|somebody)\b.{0,40}\b(?:recommend\w*|recos?|recs|suggest\w*|referrals?|contacts?|know|have|use[ds]?|tried)\b"
    r"|\brecommendations?\s+(?:for|on)\b|\bcan (?:someone|anyone|you) (?:recommend|suggest|share)\b"
    r"|\blooking for\b|\bneed (?:someone|a |an |recommend)|\bseeking\b|\bdoes anyone\b|\bdo you (?:know|have)\b|\bpls share\b",
    re.I,
)
ENDORSE_RE = re.compile(r"\b(?:our|my) (?:\w+ ){0,3}(?:cleaner|nanny|cook|gardener|handyman|plumber|electrician|landscaper|contractor|care provider|caregiver|helper|sitter|painter|roofer)\b|\bi know an? (?:\w+ ){0,2}\w+\b|\bcan (?:personally )?vouch\b|\b(?:i|we)\s+(?:would\s+|highly\s+|really\s+|strongly\s+|definitely\s+)*recommend\b|\bhighly recommend\b|\bwould recommend\b|\b(?:i|we)(?: \w+)? (?:have |had |'ve )?(?:used|use|hired|went with|tried)\b|\b(?:another|good|great) option\b|\brefer them\b|\b(?:i|we) got (?:it|ours|them) (?:done|fixed|installed)\b", re.I)
SELF_PROMO_RE = re.compile(
    r"\b(?:my|our) (?:high ?school |little |teen(?:age)? |\d+ ?(?:yr|year)s? old )?(?:son|daughter|kid|kiddo|kids|high ?schooler)(?:'s)? (?:is|are|has|will|would|can|wrote|made)\b"
    r"|\b(?:my|our) (?:new |own |small )?(?:business|catering|venture|studio|band|company|shop|store|boutique)\b"
    r"|\bi(?:'m| am) (?:offering|starting|running|hosting|selling)\b|\b(?:we(?:'re| are)|i(?:'m| am)) (?:thrilled|excited|delighted) to\b|\bour new\b",
    re.I,
)

POS_RE = re.compile(
    r"\b(?:great|good|excellent|amazing|awesome|fantastic|wonderful|best|recommend\w*|happy|satisfied|pleased|love[ds]?|loved|reasonabl[ey]|"
    r"prompt|thorough|reliable|professional|knowledgeable|honest|fair|affordable|quick|fast|responsive|smooth|nice|helpful|"
    r"vouch|trust(?:ed|worthy)?|worked (?:well|great|really well)|did (?:a )?(?:good|great|nice|thorough|amazing) job|fixed|helped|"
    r"blessing|a\+|5 stars?|super|lovely|got rid of|lot of (?:folks|people)|folks hire|many of us|quite a few of us|experienced|sweet|efficient|punctual|on[- ]time|clean work|warranty)\b|👍|👌|🙌|💯",
    re.I,
)
NEG_RE = re.compile(
    r"\b(?:(?:do not|don't|dont|wouldn't|would not|won't|never|can't|cannot) (?:recommend|use (?:them|him|her) again|go back)|avoid|stay away|"
    r"terrible|horrible|awful|worst|bad (?:experience|service|job|work)|poor (?:service|job|work|quality)|rip[- ]?off|scam\w*|overcharg\w*|"
    r"unprofessional|rude|unreliable|no[- ]show|never (?:showed|came|called|responded|returned)|didn't (?:show|fix|work|respond)|"
    r"not (?:happy|satisfied|impressed|good)|disappoint\w*|waste of|too expensive|is expensive|very expensive|over ?priced)\b|👎",
    re.I,
)
NEUTRAL_RE = re.compile(
    r"\b(?:haven'?t (?:personally )?(?:used|tried)|have not (?:personally )?(?:used|tried)|not tried|never used|"
    r"can'?t say how good|don'?t have (?:first ?hand|personal) experience|no (?:first ?hand|personal) experience|"
    r"heard (?:good things|about)|not sure (?:how good|if they)|met (?:this|him|her|them) (?:person )?(?:yesterday|at)|"
    r"recommended in another group|i have not tried)\b",
    re.I,
)


# --------------------------------------------------------------------------- helpers

def norm_phone(a: str, b: str, c: str) -> str:
    return f"{a}{b}{c}"


def fmt_phone(p: str) -> str:
    return f"({p[:3]}) {p[3:6]}-{p[6:]}" if len(p) == 10 else p


def norm_url(u: str) -> str | None:
    u = u.rstrip(".,;:!?)'\"")
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u
    try:
        host = (urlparse(u).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or any(host == j or host.endswith("." + j) for j in JUNK_HOSTS):
        return None
    if EMAIL_RE.fullmatch(u.split("://", 1)[1]):
        return None
    return u


def site_domain(url: str) -> str:
    if url.startswith("mailto:"):
        return url[7:]
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


SHARE_HOSTS = ("facebook.com", "instagram.com", "share.google", "yelp.to", "yelp.com", "g.co", "maps.app.goo.gl", "goo.gl",
               "square.site", "linktr.ee", "tiktok.com", "nextdoor.com", "google.com")


def is_share_link(url: str) -> bool:
    if url.startswith("mailto:"):
        return True  # exact-match only
    d = site_domain(url)
    return any(d == h or d.endswith("." + h) for h in SHARE_HOSTS)


def _clean_name(s: str, allow_lower: bool = False) -> str | None:
    s = re.sub(r"['\u2019]s$", "", s.strip())  # possessive at the very end: "Jane's" -> "Jane"
    s = re.sub(r"\s+", " ", s).strip(" -:,.!&")
    words = s.split()
    # trim stop words from both ends
    while words and words[0].lower().strip(".,") in STOP | {"and", "&", "of"}:
        words.pop(0)
    while words and words[-1].lower().strip(".,") in (STOP - {"inc", "llc", "co"}) | {"and", "&", "of"}:
        words.pop()
    if not words or len(words) > 6:
        return None
    lw = [w.lower().strip(".,") for w in words]
    if all(w in STOP | MONTHS_DAYS | PLACES | NOT_PROS | {"and", "&", "of"} or w in BUSINESS_WORDS and len(lw) == 1 for w in lw):
        return None
    if len(words) == 1 and (len(words[0]) < 2 or lw[0] in PLACES | NOT_PROS):
        return None
    if words[0].lower() in {"dr", "dr."} or re.search(r"\b(?:box|suite|ave|st|blvd|road|rd)\b|\d{3,}", " ".join(lw)):
        return None
    if re.fullmatch(r"\d+", lw[0]) or re.search(r"[^\w\s&'.,-]", " ".join(words)):
        return None
    if any(w in NON_HOME_WORDS for w in lw):
        return None
    core = [w for w in lw if w not in BUSINESS_WORDS | {"and", "&", "of"}]
    if core and all(w in NOT_PROS for w in core):
        return None
    lower_words = [w for w, l in zip(words, lw) if w.islower() and l not in {"and", "&", "of"}]
    if lower_words and not allow_lower:
        # lowercase words are fine inside a business name ("Eco shield pest control"), not on their own ("pricing")
        if not any(l in BUSINESS_WORDS for l in lw) or len(lower_words) == len(words):
            return None
    if allow_lower and len(words) == 1 and words[0].islower() and lw[0] not in BUSINESS_WORDS and len(lw[0]) > 4:
        return None  # "use screwdriver" -> not a name; "used abc" -> ABC
    out = " ".join(words).rstrip(".,!")
    if out.islower():
        out = " ".join(w.upper() if len(w) <= 3 and w.isalpha() and w not in {"aid", "and", "of"} else w.capitalize() for w in words)
    return out


def looks_business(name: str) -> bool:
    lw = [w.lower().strip(".,") for w in name.split()]
    raw = name.split()
    return any(w in BUSINESS_WORDS for w in lw) or len(lw) >= 3 or any(len(w) >= 2 and w.isupper() and w.isalpha() for w in raw)


def sanitize_quote(text: str, limit: int = 220) -> str:
    no_urls = URL_RE.sub(lambda m: m.group(0) if EMAIL_RE.search(text[max(0, m.start() - 40): m.end()]) else "", text)
    if len(re.sub(r"\W", "", no_urls)) >= 20:
        text = re.sub(r"[ \t]*\n[ \t]*\n+", "\n", no_urls).strip(" \n/")
    t = re.sub(r"@~?\s*([A-Z][\w]*)(?:\s+[A-Z][\w]*){0,3}", r"@\1", text)  # "@~Jane Q Doe Smith" -> "@Jane"
    t = re.sub(r"https?://\S{60,}", lambda m: m.group(0)[:57] + "…", t)
    t = re.sub(r"\s*\n\s*", " / ", t.strip())
    t = re.sub(r"\s{2,}", " ", t)
    return t if len(t) <= limit else t[: limit - 1].rsplit(" ", 1)[0] + "…"


def sentiment(text: str) -> str:
    if NEUTRAL_RE.search(text):
        return "neutral"
    if NEG_RE.search(text):
        return "negative"
    return "positive"  # sharing a pro in answer to a request is itself an endorsement


# --------------------------------------------------------------------------- candidate extraction

@dataclass
class Cand:
    name: str
    pos: int
    kind: str  # 'business' | 'person'
    rule: str
    partner: str | None = None  # person for a business, or business for a person


def _extend_lower(text: str, start: int, first: str) -> str:
    """After a lead verb, keep following words while they look like part of a name."""
    words = [first]
    rest = text[start:]
    for m in re.finditer(r"([ \t]+)([A-Za-z][\w&'.-]*)|(.)", rest, re.S):
        if m.group(3) is not None or len(words) >= 4:
            break  # punctuation / newline ends the name
        w = m.group(2)
        lw = w.lower().strip(".,")
        if lw in LOWER_STOP:
            break
        if not (w[0].isupper() or lw in BUSINESS_WORDS or (first.islower() and len(words) < 3)):
            break
        words.append(w)
        if w.endswith((".", ",")):
            break
    return " ".join(words)


STRONG_RULES = {"person-at", "name-is", "lead-verb", "biz-span", "before-phone", "after-phone", "phone-line"}
GREETING_RE = re.compile(r"(?:thanks|thank you|thx|ty|hi|hey|hello|dear|congrats|@~?)[\s,]*$", re.I)


def candidates(text: str, reply: bool = False) -> list[Cand]:
    """Name candidates in a bundle's text. `reply` unlocks the "the whole reply is a name" rule."""
    out: list[Cand] = []

    def add(name, pos, rule, kind=None, partner=None):
        if GREETING_RE.search(text[max(0, pos - 12): pos]):
            return
        n = _clean_name(name, allow_lower=rule in ("lead-verb", "short-reply")) if name else None
        if n:
            out.append(Cand(n, pos, kind or ("business" if looks_business(n) else "person"), rule, partner))

    for m in PERSON_AT_RE.finditer(text):
        if m.group(0).rstrip().split()[-1:] and re.search(r"\bof\b", m.group(0)[len(m["person"]) + m.start("person") - m.start():][:5]):
            continue
        biz, person = _clean_name(m["biz"], allow_lower=True), _clean_name(m["person"])
        if biz and person and biz.lower().split()[0] not in PLACES | NOT_PROS and not GREETING_RE.search(text[max(0, m.start("person") - 12): m.start("person")]):
            out.append(Cand(biz, m.start("biz"), "business", "person-at", person))
            out.append(Cand(person, m.start("person"), "person", "person-at", biz))
    for m in NAME_IS_RE.finditer(text):
        add(m["name"], m.start("name"), "name-is", "person")
    for m in WE_HAVE_RE.finditer(text):
        if POS_RE.search(text) and not m["name"].isupper():
            add(m["name"], m.start("name"), "lead-verb")
    for m in KNOW_RE.finditer(text):
        add(m["name"], m.start("name"), "name-is", "person")
    for m in IS_NAME_RE.finditer(text):
        add(m["name"], m.start("name"), "name-is", "person")
    for m in LEAD_RE.finditer(text):
        first = m["name"].split()[0]
        if first.lower().strip(".,") in LOWER_STOP | {"someone", "anyone", "somebody", "it", "one", "another", "other"}:
            continue
        if first.islower() and len(first) <= 2:
            continue
        if re.search(r"\b(?:it'?s|is|was|are|be|been|get|got|always)\s+(?:called|check)\s*$", text[: m.start("name")], re.I):
            continue
        add(_extend_lower(text, m.start("name") + len(first), first), m.start("name"), "lead-verb")

    # sentence-initial name + verb, only when the same sentence praises them: "Joe lives nearby and is a great plumber"
    for m in re.finditer(rf"(?:^|[\n.!?]\s*)(?P<name>[A-Z][\w&'.-]*(?:[ \t]+(?:[A-Z][\w&'.-]*|&|and|[a-z]+)){{0,4}}?)[ \t]+(?P<verb>{SUBJECT_VERBS})\b(?P<rest>[^.!?\n]*)", text):
        near = m["verb"] + " " + " ".join(m["rest"].split()[:7])
        if POS_RE.search(near) or re.search(r"\b(?:done a lot|lot of work|another option|good option)\b", near, re.I):
            add(m["name"], m.start("name"), "subject")
    lines = text.split("\n")
    offs, o = [], 0
    for ln in lines:
        offs.append(o)
        o += len(ln) + 1
    # a line next to a phone-only line: "Pat Kim / Summit Roof Cleaning & Repairs / (206) 555-0123"
    for i, ln in enumerate(lines):
        if PHONE_RE.search(ln) and not PHONE_RE.sub("", ln).strip(" -:()+"):
            for j in (i - 1, i - 2, i + 1):
                if 0 <= j < len(lines) and 0 < len(lines[j].split()) <= 5 and not PHONE_RE.search(lines[j]) and not re.search(r"\d", lines[j]):
                    add(lines[j], offs[j], "phone-line")
    # in a reply, a reply that opens with just a name: "Rapid Plumbing", "Acme roofers", "Green irrigation. Been with them..."
    if reply:
        body = PHONE_RE.sub("", URL_RE.sub("", text)).strip()
        first_sent = re.split(r"[.!\n]|\s[-–]\s", body)[0].strip(" ,")
        ws = first_sent.split()
        if 1 <= len(ws) <= 4 and ws[0][:1].isupper() and not any(w.lower().strip(",") in STOP - {"and", "&"} for w in ws) and not re.search(r"\d", first_sent):
            add(first_sent, text.find(ws[0]), "short-reply")
    # business-looking capitalised spans anywhere: "Clearview Glass", "Holly's Helpers, Inc."
    for m in re.finditer(r"\b[A-Z][\w&'.-]*(?:(?:[ \t]+|[ \t]*&[ \t]*|,[ \t]+(?=Inc|LLC))(?:[A-Z][\w&'.-]*|&|and|of))*", text):
        span = m.group(0)
        ws = [w.lower().strip(".,") for w in span.split()]
        if len(ws) >= 2 and any(w in BUSINESS_WORDS for w in ws[1:]) and not all(w in BUSINESS_WORDS | {"&", "and", "of"} for w in ws):
            add(span, m.start(), "biz-span")
    # name right before a phone number: "Mike (425)555-0148", "Joe- +1 (206) ..."
    for m in PHONE_RE.finditer(text):
        before = text[max(0, m.start() - 60): m.start()]
        if SELF_PHONE_RE.search(before):
            continue
        mm = re.search(r"([A-Z][\w'.-]*(?:,?[ \t]+[A-Z][\w'.-]*){0,4})[ \t\-:(]*\+?1?[ \t(]*$", before)
        if mm:
            add(mm.group(1), m.start() - len(before) + mm.start(), "before-phone")
        after = text[m.end(): m.end() + 40]
        mm = re.match(r"[ \t]*(?:\n|[-–:])[ \t]*([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+)?|[A-Z]{2,4})[ \t]*(?:\n|$)", after)
        if mm:
            add(mm.group(1), m.end() + mm.start(1), "after-phone")

    # de-duplicate: same name, or one contained in another from a weaker rule
    out.sort(key=lambda c: c.pos)
    seen: dict[str, Cand] = {}
    for c in out:
        k = c.name.lower()
        if k in seen:
            if c.partner and not seen[k].partner:
                seen[k].partner = c.partner
            continue
        contained = [o for ok, o in seen.items() if k in ok or ok in k]
        if contained:
            o = contained[0]
            if len(k) > len(o.name) and o.rule not in ("person-at",):  # keep the longer, fuller name
                del seen[o.name.lower()]
                c.partner = c.partner or o.partner
                seen[k] = c
            continue
        seen[k] = c
    return list(seen.values())


# --------------------------------------------------------------------------- main loop

def bundle_messages(msgs: list[Message]) -> list[Bundle]:
    out: list[Bundle] = []
    for m in msgs:
        if m.is_system:
            continue
        if out and out[-1].sender == m.sender and m.ts - out[-1].msgs[-1].ts <= BUNDLE_GAP and "reply_to" not in m.extra:
            cur = out[-1]
            if cur.has_card and not m.is_media and trades_in(cur.text) and trades_in(m.text) and not set(trades_in(m.text)) & set(trades_in(cur.text)):
                out.append(Bundle(m.sender, [m]))  # "here's our contractor [card]" then "also our cook ..." [card]
                continue
            cur.msgs.append(m)
        else:
            out.append(Bundle(m.sender, [m]))
    return out


@dataclass
class Thread:
    ts: datetime
    sender: str
    trades: list[str]
    ref: str
    text: str


OFFER_RE = re.compile(r"\blooking (?:for|to take on) (?:more |new |additional |an? )?(?:work|opportunit\w*|homes?|houses?|clients?|families|jobs?)\b|\bhas openings\b|\bis available\b", re.I)


def is_request(text: str) -> bool:
    if not text:
        return False
    if OFFER_RE.search(text):
        return False  # "our cleaner is looking for more homes" is an offer, not a question
    if ENDORSE_RE.search(text) and "?" not in text:
        return False
    return bool(REQUEST_RE.search(text)) or text.rstrip().endswith("?")


def _group(cands: list[Cand], text: str = "") -> list[tuple[Cand, list[Cand]]]:
    """Group candidates that describe one pro (a business plus its person)."""
    groups: list[tuple[Cand, list[Cand]]] = []
    used: set[int] = set()
    by_name = {c.name: c for c in cands}
    for i, c in enumerate(cands):
        if i in used:
            continue
        members = [c]
        used.add(i)
        if c.partner and c.partner in by_name:
            j = cands.index(by_name[c.partner])
            if j not in used:
                members.append(cands[j])
                used.add(j)
        groups.append((c, members))
    # a lone business and a lone person on the same or neighbouring lines are one pro:
    # "We used Acme HVAC ... Lee was very helpful", "Pat Kim / Summit Roof Cleaning & Repairs"
    line_of = lambda pos: text.count("\n", 0, pos)
    singles = [g for g in groups if len(g[1]) == 1]
    biz = [g for g in singles if g[0].kind == "business"]
    per = [g for g in singles if g[0].kind == "person"]
    if len(biz) == 1 and len(per) == 1 and (line_of(biz[0][0].pos) == line_of(per[0][0].pos) or (
            biz[0][0].rule == per[0][0].rule == "phone-line" and abs(line_of(biz[0][0].pos) - line_of(per[0][0].pos)) <= 1)):
        groups = [g for g in groups if g is not per[0] and g is not biz[0]] + [(biz[0][0], [biz[0][0], per[0][0]])]
    return groups


def extract(msgs: list[Message]) -> list[Mention]:
    bundles = bundle_messages(msgs)
    threads: list[Thread] = []
    by_id: dict[str, Thread] = {}
    mentions: list[Mention] = []
    pending: list[tuple[Mention, Thread | None]] = []  # trade unknown yet; resolve via known pro names

    for b in bundles:
        text = b.text
        own_trades = trades_in(text)
        if is_request(text) and not b.has_card and PHONE_RE.search(text) and own_trades and not ENDORSE_RE.search(text):
            for c in candidates(text):
                if c.rule in ("before-phone", "lead-verb", "biz-span"):
                    ph = [norm_phone(*m.groups()) for m in PHONE_RE.finditer(text)]
                    mentions.append(Mention(b.ts, b.sender, b.ref, own_trades[0], own_trades, "neutral", sanitize_quote(text),
                                            pro_name=c.name, phones=ph, via="asked-about"))
            continue
        if is_request(text) and not b.has_card and not PHONE_RE.search(text) and not URL_RE.search(text):
            if re.search(r"\b(?:stores?|shops?|buy|purchase|brands?|models?|options for|which (?:brand|one))\b", text, re.I) and not re.search(r"repair|fix|install|service", text, re.I):
                own_trades = []  # "suggest appliance stores" is shopping, not a pro
            t = Thread(b.ts, b.sender, own_trades, b.ref, text)
            threads.append(t)
            for m in b.msgs:
                if "id" in m.extra:
                    by_id[str(m.extra["id"])] = t
            continue

        # which thread is this answering?
        thread = None
        reply_to = next((str(m.extra["reply_to"]) for m in b.msgs if m.extra.get("reply_to")), None)
        if reply_to:
            thread = by_id.get(reply_to)
        else:
            live = [t for t in threads if b.ts - t.ts <= THREAD_WINDOW and t.sender != b.sender and b.ts >= t.ts]
            if own_trades:
                match = [t for t in live if set(t.trades) & set(own_trades)]
                thread = match[-1] if match else None
            elif live:
                thread = live[-1]
                home_live = [t for t in live if t.trades]
                mentioned = [t for t in live if re.search(rf"@~?\s*{re.escape(first_name(t.sender) or '#')}\b", text, re.I)]
                if mentioned:
                    thread = mentioned[-1]
                elif not thread.trades and home_live and "?" not in text and (
                        ((b.has_card or PHONE_RE.search(text)) and b.ts - home_live[-1].ts <= timedelta(hours=6))
                        or (POS_RE.search(text) and b.ts - home_live[-1].ts <= timedelta(hours=3)
                            and any(c.rule in STRONG_RULES for c in candidates(text, reply=False)))):
                    thread = home_live[-1]  # concrete pro info under a chatty question: answer to the open service request
        if thread and thread.sender == b.sender:
            continue  # the asker talking in their own thread
        if re.match(r"^\W*(?:thanks?|thank\s*you|thabk|thx|ty|tysm|got it|will do|sure)\b", text, re.I) and len(text) < 80:
            continue
        if SELF_PROMO_RE.search(text) and not b.has_card:
            continue
        if re.search(r"\b(?:join us|rsvp|tickets?|register|sign ?up|webinar|workshop|fundraiser|contest|you'?re (?:warmly )?invited)\b", text, re.I):
            continue  # event announcements
        pending_only = False
        home_thread = bool(thread and thread.trades)
        reply_like = home_thread and (len(text) <= 300 or bool(own_trades) or b.has_card or bool(PHONE_RE.search(text)))
        if not reply_like:
            # unsolicited post (or a reply to a non-home question): needs a trade, an endorsement and solid evidence
            strong_ok = own_trades and (ENDORSE_RE.search(text) or (POS_RE.search(text) and (PHONE_RE.search(text) or b.has_card or NAME_IS_RE.search(text) or URL_RE.search(text))))
            if not strong_ok and not (thread and not own_trades):  # bare reply under an unrelated question -> pending lookup
                continue
            if not own_trades:
                thread = None  # keep only names that turn out to be known pros (second pass)
                pending_only = True

        if own_trades and thread and set(thread.trades) & set(own_trades):
            trades = [t for t in thread.trades if t in own_trades]
        elif own_trades:
            trades = trades_in(text, major_only=True)
        elif thread:
            trades = thread.trades
        else:
            trades = []
        if not trades and not thread and not pending_only:
            continue  # unsolicited chatter with no trade in sight

        # "+1", "Same ⬆️" -> endorse the latest recommendation in this thread
        pm = PLUS_ONE_RE.match(text) if len(text) < 60 else None
        if pm and not b.has_card and not re.search(r"[A-Za-z]{3,}", re.sub(r"\b(?:for|him|her|them|this|that|one|on|it)\b", "", pm["rest"], flags=re.I)):
            prev = next((x for x in reversed(mentions) if b.ts - x.posted_at <= timedelta(hours=6) and x.sender != b.sender), None)
            if prev:
                mentions.append(Mention(b.ts, b.sender, b.ref, prev.trade, prev.trades, "positive", sanitize_quote(text),
                                        prev.pro_name, prev.person_name, list(prev.phones), list(prev.websites), prev.is_card,
                                        thread.ref if thread else None,
                                        card_owner=(prev.card_owner or prev.sender) if prev.is_card else None, via="plus-one",
                                        request_text=prev.request_text, request_by=prev.request_by))
            continue

        cands = candidates(text, reply=reply_like or pending_only or thread is not None)
        if not reply_like:
            cands = [c for c in cands if c.rule in STRONG_RULES | {"subject"}] if own_trades else [c for c in cands if c.rule == "short-reply"]
        phones = [(m.start(), norm_phone(*m.groups())) for m in PHONE_RE.finditer(text)
                  if not SELF_PHONE_RE.search(text[max(0, m.start() - 40): m.start()])]
        no_mail = EMAIL_RE.sub(" ", text)
        emails = ["mailto:" + e.lower() for e in EMAIL_RE.findall(text)]
        urls = [(m.start(), u) for m in URL_RE.finditer(no_mail) if (u := norm_url(m.group(0)))]
        if not cands and not phones and not urls and not b.has_card:
            continue
        if not (cands or phones or b.has_card) and urls and all(is_share_link(u) for _, u in urls) and not thread:
            continue

        senti = sentiment(text)
        quote = sanitize_quote(text) if text and not (b.has_card and len(text) > 300 and not own_trades) else "(shared a contact card)"
        groups = _group(cands, text) if cands else []
        if not groups:
            groups = [(None, [])]  # phone / url / contact card with no readable name

        built: list[Mention] = []
        bundle_trades = trades
        for lead, members in groups:
            nm = (lead.name if lead else "") + " " + " ".join(c.name for c in members)
            trades = list(dict.fromkeys(trades_in(nm) + bundle_trades))
            biz = next((c for c in members if c.kind == "business"), None)
            per = next((c for c in members if c.kind == "person"), None)
            name = (biz or per).name if (biz or per) else None
            mention = Mention(b.ts, b.sender, b.ref, trades[0] if trades else None, trades, senti, quote,
                              pro_name=name, person_name=per.name if per and biz else None,
                              is_card=b.has_card, request_ref=thread.ref if thread else None,
                              card_owner=b.sender if b.has_card else None,
                              via="+".join(sorted({c.rule for c in members})) or ("card" if b.has_card else "contact-only"))
            if thread:
                mention.request_text, mention.request_by = sanitize_quote(thread.text, 180), thread.sender
            built.append(mention)
        # hand out phones and urls: one pro gets them all, otherwise nearest name wins
        named = [(g[0].pos if g[0] else 0, mm) for g, mm in zip(groups, built)]
        for pos, ph in phones:
            target = min(named, key=lambda x: abs(x[0] - pos))[1] if len(named) > 1 else built[0]
            if ph not in target.phones:
                target.phones.append(ph)
        for pos, u in urls:
            target = built[0]
            if len(built) > 1:
                dom = re.sub(r"[^a-z]", "", site_domain(u).split(".")[0])
                hit = [mm for mm in built if mm.pro_name and re.sub(r"[^a-z]", "", mm.pro_name.lower())[:6] in dom]
                target = hit[0] if hit else min(named, key=lambda x: abs(x[0] - pos))[1]
            if u not in target.websites:
                target.websites.append(u)
        for e in emails:
            target = built[0] if len(built) == 1 else next((x for x in built if x.pro_name and re.sub(r"[^a-z]", "", x.pro_name.lower())[:6] in e), None)
            if target and e not in target.websites:
                target.websites.append(e)
        for mm in built:
            if not mm.pro_name and not mm.phones and not mm.websites and not mm.is_card:
                continue
            if not mm.pro_name and not mm.phones and not mm.is_card:
                dom = site_domain(mm.websites[0])
                words = {a.replace(" ", "") for t in mm.trades for a in TRADES[t]["aliases"]} | {t.split()[0] for t in mm.trades}
                if dom.endswith((".org", ".edu", ".gov")) or not (any(w in dom for w in words) or POS_RE.search(text)):
                    continue
            if mm.pro_name is None and mm.websites and not is_share_link(mm.websites[0]):
                mm.pro_name = site_domain(mm.websites[0])
            if not mm.trades:
                home_live = [t for t in threads if b.ts - t.ts <= THREAD_WINDOW and t.trades and t.sender != b.sender]
                pending.append((mm, home_live[-1] if home_live else None))
            else:
                mentions.append(mm)

    # second pass: a bare "Acme roofers" under an unrelated question inherits the trade(s) it is known for
    known: dict[str, list[str]] = {}
    for mm in mentions:
        if mm.pro_name:
            known.setdefault(_key(mm.pro_name), []).extend(mm.trades)
    for mm, home in pending:
        if mm.pro_name and _key(mm.pro_name) in known:
            tr = list(dict.fromkeys(home.trades if home else known[_key(mm.pro_name)]))
            if home:
                mm.request_ref = home.ref
                mm.request_text, mm.request_by = sanitize_quote(home.text, 180), home.sender
            mm.trade, mm.trades = tr[0], tr
            mm.via += "+known-pro"
            mentions.append(mm)
    mentions.sort(key=lambda x: x.posted_at)
    return mentions


def _key(name: str) -> str:
    from .merge import name_key
    return name_key(name)
