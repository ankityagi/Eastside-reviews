"""Importers: turn an export file into (group name, messages).

Every importer yields `Loaded` objects holding platform-neutral `Message`s, so the extractor,
database and CLI never need to know where a message came from.

Adding a platform (e.g. Facebook groups) = write a function that returns Loaded objects and
register it in IMPORTERS. The quickest route is to convert the data to the generic JSON-lines
format below and use `--platform jsonl`:

    {"id": "c123", "reply_to": "p45", "ts": "2026-10-28T19:41:00", "author": "Jane Doe",
     "text": "I've had great experience with Acme Electric.", "group": "Maple Street (FB)"}

  id / reply_to  optional; when present they give exact threading (comment -> post), which beats
                 WhatsApp's time-window guess
  author         as displayed; only the first name is kept, the rest is hashed
  group          optional; overrides --group per line
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import whatsapp
from .whatsapp import Message


@dataclass
class Loaded:
    platform: str
    group: str
    file_label: str
    sha256: str
    messages: list[Message]


def load_whatsapp(path: Path, group: str | None) -> list[Loaded]:
    out = []
    for name, text in whatsapp.read_export(path):
        msgs = whatsapp.parse_text(text)
        g = group or whatsapp.group_name_from_filename(path if name == "_chat.txt" or path.suffix.lower() == ".txt" else name)
        out.append(Loaded("whatsapp", g, f"{path.name}:{name}" if path.suffix.lower() == ".zip" else path.name,
                          hashlib.sha256(text.encode()).hexdigest(), msgs))
    return out


def load_jsonl(path: Path, group: str | None, platform: str = "jsonl") -> list[Loaded]:
    raw = path.read_text(encoding="utf-8")
    by_group: dict[str, list[Message]] = {}
    for i, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        d = json.loads(line)
        g = d.get("group") or group or path.stem
        extra = {k: str(d[k]) for k in ("id", "reply_to") if d.get(k)}
        m = Message(datetime.fromisoformat(d["ts"]), whatsapp.clean(d.get("author", "")).strip(),
                    whatsapp.clean(d.get("text", "")).strip(), line_no=i, extra=extra)
        by_group.setdefault(g, []).append(m)
    sha = hashlib.sha256(raw.encode()).hexdigest()
    return [Loaded(d_platform, g, path.name, hashlib.sha256((sha + g).encode()).hexdigest(), sorted(ms, key=lambda m: m.ts))
            for g, ms in by_group.items() for d_platform in [platform]]


def load_facebook(path: Path, group: str | None) -> list[Loaded]:
    """Facebook group data converted to the JSON-lines format above (see README)."""
    if path.suffix.lower() not in (".jsonl", ".json"):
        raise SystemExit("Facebook import expects the JSON-lines format described in recs/sources.py "
                         "(a converter for saved group pages / data downloads goes here).")
    return load_jsonl(path, group, platform="facebook")


IMPORTERS = {"whatsapp": load_whatsapp, "jsonl": load_jsonl, "facebook": load_facebook}


def guess_platform(path: Path) -> str:
    return "jsonl" if path.suffix.lower() in (".jsonl", ".json") else "whatsapp"
