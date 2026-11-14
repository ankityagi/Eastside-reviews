"""Trade taxonomy: which words in a message point to which kind of service pro.

Edit freely. Each trade has
  patterns  - regexes matched (case-insensitive) against message text
  aliases   - words you can type on the command line: `recs plumber`
Order matters only for display. A message can match several trades.
"""
from __future__ import annotations

import re

TRADES: dict[str, dict[str, list[str]]] = {
    "plumbing": {
        "patterns": [r"plumb\w*", r"\bleak(s|ing|y)?\b", r"water heater", r"\bpipes?\b", r"\bdrains?\b", r"faucet", r"toilet", r"under (our|the) sink"],
        "aliases": ["plumber", "plumbing", "plumbers", "leak", "water heater"],
    },
    "electrical": {
        "patterns": [r"\belectrician\w*", r"electrical", r"\bwiring\b", r"\boutlets?\b", r"light switch|change a switch", r"ev charg\w*", r"(tesla|ev) charger", r"charging install\w*"],
        "aliases": ["electrician", "electrical", "electric", "ev charger", "wiring"],
    },
    "handyman": {
        "patterns": [r"handy ?m[ae]n", r"handy/? ?help", r"mount(ing)? (wall|tv|art|frame)", r"shelving", r"garage storage", r"storage closets?", r"carpentry", r"folding station", r"bunk bed"],
        "aliases": ["handyman", "handymen", "handy", "carpenter", "odd jobs"],
    },
    "roofing": {
        "patterns": [r"\broof\w*"],
        "aliases": ["roofer", "roofing", "roof", "roofers"],
    },
    "hvac": {
        "patterns": [r"\bhvac\b", r"furnace", r"heat pump", r"\bac repair", r"\ba/?c\b (unit|repair|service)", r"air condition\w*", r"heating"],
        "aliases": ["hvac", "furnace", "heating", "ac", "air conditioning", "heat pump", "cooling"],
    },
    "duct & vent cleaning": {
        "patterns": [r"\bducts?\b", r"dryer vent", r"laundry vent", r"vent cleaning"],
        "aliases": ["duct", "ducts", "duct cleaning", "dryer vent", "vent"],
    },
    "appliance repair": {
        "patterns": [r"appliance", r"dishwasher", r"\bwasher\b", r"washing machine", r"\bdryer\b(?! vent)", r"\boven\b", r"microwave", r"fridge|refrigerator", r"range hood", r"cooktop|burners?", r"sub ?zeros?", r"\bviking\b"],
        "aliases": ["appliance", "appliances", "appliance repair", "dishwasher", "washer", "dryer", "oven", "fridge"],
    },
    "general contractor": {
        "patterns": [r"general contractor", r"\bcontractors?\b", r"remodel\w*", r"renovat\w*", r"tub to shower", r"full bathroom", r"kitchen cabinets"],
        "aliases": ["contractor", "general contractor", "gc", "remodel", "remodeling", "renovation"],
    },
    "landscaping": {
        "patterns": [r"landscap\w*", r"gardeners?\b", r"yard (maintenance|clean\w*|work)", r"\blawn\b", r"pavers", r"retaining walls?"],
        "aliases": ["landscaper", "landscaping", "gardener", "yard", "lawn", "yard maintenance"],
    },
    "irrigation": {
        "patterns": [r"irrigation", r"sprinklers?", r"winteriz\w*", r"blow ?outs?"],
        "aliases": ["irrigation", "sprinkler", "sprinklers", "winterization"],
    },
    "backflow testing": {
        "patterns": [r"back ?flow"],
        "aliases": ["backflow", "backflow testing", "backflow tester"],
    },
    "tree service": {
        "patterns": [r"tree (removal|service|chopping|trimming|cutting)", r"arborist", r"take down (old )?trees"],
        "aliases": ["tree", "trees", "tree service", "arborist", "tree removal"],
    },
    "pest control": {
        "patterns": [r"\bpests?\b", r"\brats?\b", r"\bmice\b", r"rodent", r"wasps?", r"termite", r"exterminat\w*", r"terminix"],
        "aliases": ["pest", "pest control", "exterminator", "rodent", "wasp"],
    },
    "house cleaning": {
        "patterns": [r"(?:house|home) ?clean\w*", r"cleaning lad(?:y|ies)", r"(?:house|home) cleaning service",
                     r"(?<!dry )(?<!carpet )(?<!window )(?<!duct )(?<!vent )(?<!gutter )\bcleaners?\b", r"\bmaid\b",
                     r"household chores", r"cleaning our place"],
        "aliases": ["cleaner", "cleaners", "house cleaning", "house cleaner", "maid", "cleaning"],
    },
    "window cleaning": {
        "patterns": [r"window clean\w*"],
        "aliases": ["window cleaning", "window cleaner"],
    },
    "pressure washing": {
        "patterns": [r"pressure wash\w*", r"power wash\w*"],
        "aliases": ["pressure washing", "pressure wash", "power washing"],
    },
    "gutters": {
        "patterns": [r"gutters?"],
        "aliases": ["gutter", "gutters", "gutter cleaning"],
    },
    "windows & glass": {
        "patterns": [r"window replacement", r"replac\w* (our |the )?windows?", r"new windows", r"glass repair", r"\bglass (panel|door)", r"door glass", r"window screens?"],
        "aliases": ["windows", "window", "glass", "window replacement", "glass repair"],
    },
    "fencing": {
        "patterns": [r"\bfenc(e|es|ing)\b"],
        "aliases": ["fence", "fencing"],
    },
    "painting": {
        "patterns": [r"\bpainters?\b", r"(indoor|interior|exterior|house) paint\w*", r"painting work"],
        "aliases": ["painter", "painting", "painters"],
    },
    "carpet cleaning": {
        "patterns": [r"carpet clean\w*"],
        "aliases": ["carpet", "carpet cleaning", "carpet cleaner"],
    },
    "holiday lights": {
        "patterns": [r"(holiday|christmas|diwali|outdoor|permanent|festive) light\w*", r"\bsanta'?s\b", r"light up the house", r"put the lights", r"lighting service", r"lights? (install\w*|around the house)"],
        "aliases": ["lights", "holiday lights", "christmas lights", "diwali lights"],
    },
    "locksmith": {
        "patterns": [r"locksmith", r"\brekey"],
        "aliases": ["locksmith"],
    },
    "garage door": {
        "patterns": [r"garage door"],
        "aliases": ["garage door"],
    },
    "mold & moisture": {
        "patterns": [r"\bmold\b", r"moisture", r"crawl ?space", r"\battic\b", r"leak detection"],
        "aliases": ["mold", "moisture", "crawl space", "attic"],
    },
    "trash & junk hauling": {
        "patterns": [r"\bjunk\b", r"trash pickup", r"\bhaul(ing)?\b", r"pick up all 3 bins"],
        "aliases": ["junk", "trash", "hauling", "junk removal"],
    },
    "fireplace & chimney": {
        "patterns": [r"fireplace", r"chimney"],
        "aliases": ["fireplace", "chimney"],
    },
    "home inspection": {
        "patterns": [r"home inspect\w*"],
        "aliases": ["inspector", "home inspector", "inspection"],
    },
    "auto detailing": {
        "patterns": [r"\bdetail(ing|er)\b", r"car detail", r"wind ?screen", r"windshield"],
        "aliases": ["detailing", "auto detailing", "car detailing", "detailer", "windshield"],
    },
    "cook": {
        "patterns": [r"\bcooks?\b(?! with)", r"cooking for", r"for cooking", r"help with cooking", r"cook for me", r"mother.s helper"],
        "aliases": ["cook", "chef", "cooking", "meals"],
    },
    "nanny & childcare": {
        "patterns": [r"\bnann(y|ies)\b", r"child ?care", r"babysit\w*", r"care provider", r"summer care"],
        "aliases": ["nanny", "childcare", "babysitter", "sitter"],
    },
    "pet care": {
        "patterns": [r"pet ?sit\w*", r"cat-?sitters?", r"dog walk\w*", r"dog groom\w*", r"pet care"],
        "aliases": ["pet", "pet sitter", "dog walker", "groomer", "pet care"],
    },
}

_COMPILED = {t: re.compile("|".join(f"(?:{p})" for p in d["patterns"]), re.I) for t, d in TRADES.items()}


def trades_in(text: str, major_only: bool = False) -> list[str]:
    """Trades mentioned in a piece of text, most-mentioned first (ties keep taxonomy order).
    major_only drops passing mentions: in a long post about sprinklers one "leaking" is not plumbing."""
    hits = sorted(((len(rx.findall(text)), i, t) for i, (t, rx) in enumerate(_COMPILED.items())), key=lambda h: (-h[0], h[1]))
    hits = [h for h in hits if h[0]]
    if major_only and hits:
        top = hits[0][0]
        hits = [h for h in hits if h[0] * 2 >= top]
    return [t for _, _, t in hits]


def resolve_query(q: str) -> list[str]:
    """Map what the user typed (`plumber`, `hvac`, `roofer`) to trade keys."""
    q = q.strip().lower()
    if q in TRADES:
        return [q]
    hits = [t for t, d in TRADES.items() if q in d["aliases"]]
    if hits:
        return hits
    # singular/plural and prefix forgiveness: "electricians", "landscap"
    q2 = q.rstrip("s")
    hits = [t for t, d in TRADES.items() if any(a.rstrip("s") == q2 for a in d["aliases"])]
    if hits:
        return hits
    return [t for t, d in TRADES.items() if t.startswith(q) or any(a.startswith(q) for a in d["aliases"])]
