"""Decide which mentions refer to the same pro.

Evidence that joins two mentions (union-find):
  * the same phone number (any formatting)
  * the same business website domain (share links like facebook.com/share/... only if identical)
  * the same business name after normalising ("Expert Irrigation Services" == "Expert irrigation")
  * near-identical business names ("Rapid Rooter" ~ "rapid rooters"), or a name matching a domain
    ("Rapid Rooter" ~ rapidrooterinc.com)
  * a single first name ("Joe", "Mike") only within the same trade
  * an unnamed contact card is joined to the one named pro the same neighbour recommended
    for the same trade; otherwise all of that neighbour's unnamed cards for that trade are one pro
  * manual curation: `recs merge "A" "B"`
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

_SUFFIXES = {"inc", "llc", "co", "company", "corp", "services", "service", "the", "and", "ltd", "pllc", "group"}


def name_key(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[’']s\b", "", s)
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _SUFFIXES]
    return "".join(words)


def domain_key(domain: str) -> str:
    label = domain.split(".")[0].lower()
    label = re.sub(r"[^a-z0-9]", "", label)
    for suf in ("services", "service", "inc", "llc", "co", "wa", "nw"):
        if label.endswith(suf) and len(label) - len(suf) >= 5:
            label = label[: -len(suf)]
    return label


def _similar(a: str, b: str) -> bool:
    if len(a) < 6 or len(b) < 6:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= 8
    return SequenceMatcher(None, a, b).ratio() >= 0.88


class DSU:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb, key=str)] = min(ra, rb, key=str)


def is_single_person(name: str | None) -> bool:
    if not name:
        return False
    from .extract import looks_business
    return len(name.split()) == 1 and not looks_business(name)


def cluster(rows: list[dict], merges: list[tuple[str, str]] = ()) -> dict[int, int]:
    """rows: dicts with id, neighbor_id, trade, pro_name, person_name, phones, websites, is_card, card_owner_id.
    Returns {mention_id: cluster_root_mention_id}."""
    from .extract import is_share_link, site_domain

    d = DSU()
    keyed: dict[str, list[int]] = defaultdict(list)
    name_keys: dict[int, str] = {}
    for r in rows:
        mid = r["id"]
        d.find(mid)
        for ph in r["phones"]:
            keyed[f"phone:{ph}"].append(mid)
        for u in r["websites"]:
            keyed[f"url:{u}" if is_share_link(u) else f"site:{site_domain(u)}"].append(mid)
        for n in filter(None, [r["pro_name"], r["person_name"]]):
            k = name_key(n)
            if not k:
                continue
            if is_single_person(n):
                keyed[f"pname:{r['trade']}:{k}"].append(mid)
            else:
                keyed[f"name:{k}"].append(mid)
                if n == r["pro_name"]:
                    name_keys[mid] = k
        if r["is_card"] and not (r["pro_name"] or r["phones"] or r["websites"]):
            keyed[f"card:{r['card_owner_id'] or r['neighbor_id']}:{r['trade']}"].append(mid)
    for ids in keyed.values():
        for other in ids[1:]:
            d.union(ids[0], other)

    # fuzzy business names, and names vs. website domains
    dom_keys = {r["id"]: domain_key(site_domain(u)) for r in rows for u in r["websites"] if not is_share_link(u)}
    items = list(name_keys.items()) + [(i, k) for i, k in dom_keys.items()]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if d.find(items[i][0]) != d.find(items[j][0]) and _similar(items[i][1], items[j][1]):
                d.union(items[i][0], items[j][0])

    # unnamed cards -> the one named pro that neighbour recommended in that trade
    by_id = {r["id"]: r for r in rows}
    groups: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        groups[d.find(r["id"])].append(r["id"])
    # only clusters where this neighbour *shared a card and named it* count: then their other
    # unnamed cards for the same trade are very likely the same person (a neighbour's handyman "Mike")
    named_by_owner: dict[tuple, set[int]] = defaultdict(set)
    for root, ids in groups.items():
        for i in ids:
            r = by_id[i]
            if r["is_card"] and (r["pro_name"] or r["phones"]):
                named_by_owner[(r["card_owner_id"] or r["neighbor_id"], r["trade"])].add(root)
    for root, ids in list(groups.items()):
        if all(by_id[i]["is_card"] and not (by_id[i]["pro_name"] or by_id[i]["phones"] or by_id[i]["websites"]) for i in ids):
            r0 = by_id[ids[0]]
            targets = named_by_owner.get((r0["card_owner_id"] or r0["neighbor_id"], r0["trade"]), set())
            if len(targets) == 1:
                d.union(root, next(iter(targets)))

    # manual merges by name
    name_to_ids: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        for n in filter(None, [r["pro_name"], r["person_name"]]):
            name_to_ids[name_key(n)].append(r["id"])
    for a, b in merges:
        ia, ib = name_to_ids.get(name_key(a)), name_to_ids.get(name_key(b))
        if ia and ib:
            d.union(ia[0], ib[0])
    return {r["id"]: d.find(r["id"]) for r in rows}


def display_name(members: list[dict], first_names: dict[int, str]) -> tuple[str, str]:
    """Pick a name for a cluster. Returns (name, kind) with kind in business/person/unnamed."""
    from .extract import fmt_phone, looks_business, site_domain

    names = Counter()
    for m in members:
        bonus = 1 if m["sentiment"] == "positive" else 0  # prefer the spelling used by people who vouched
        if m["pro_name"]:
            names[m["pro_name"]] += (3 if looks_business(m["pro_name"]) else 2) + bonus
        if m["person_name"]:
            names[m["person_name"]] += 1
    if names:
        best = max(names.items(), key=lambda kv: (kv[1], looks_business(kv[0]), any(c.isupper() for c in kv[0][1:]), len(kv[0])))[0]
        persons = sorted({m["person_name"] or m["pro_name"] for m in members
                          if (m["person_name"] or (m["pro_name"] and is_single_person(m["pro_name"]))) and (m["person_name"] or m["pro_name"]) != best})
        if looks_business(best) and persons:
            return f"{best} ({', '.join(persons[:2])})", "business"
        if not looks_business(best):
            others = sorted({m["pro_name"] for m in members if m["pro_name"] and m["pro_name"].lower() != best.lower()
                             and not looks_business(m["pro_name"])})
            if others:  # same phone, different spellings: show them so you can recognise either
                return f"{best} / {' / '.join(others[:2])}", "person"
        return best, "business" if looks_business(best) else "person"
    phones = [p for m in members for p in m["phones"]]
    if phones:
        return f"Unnamed pro {fmt_phone(phones[0])}", "unnamed"
    sites = [u for m in members for u in m["websites"]]
    if sites:
        return site_domain(sites[0]), "unnamed"
    owners = sorted({first_names.get(m["card_owner_id"] or m["neighbor_id"]) or "a neighbor" for m in members if m["is_card"]})
    return f"{' / '.join(owners)}'s contact card (name not in export)", "unnamed"
