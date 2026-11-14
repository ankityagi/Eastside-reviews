"""Parser for WhatsApp "Export chat" files (.txt, or the .zip WhatsApp produces).

Handles both layouts WhatsApp uses:
  iOS:     [4/12/25, 10:33:08 AM] ~ Jane Doe: message
  Android: 4/12/25, 10:33 AM - Jane Doe: message
           12/04/2025, 22:33 - Jane Doe: message
Multi-line messages are joined; media placeholders ("image omitted",
"<Media omitted>", "Contact card omitted" ...) are flagged; system lines
(joins, security codes, ...) are flagged so callers can skip them.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Invisible marks WhatsApp sprinkles in: LRM/RLM, LRE/PDF/LRO..., narrow nbsp, nbsp, BOM.
_INVISIBLE = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮﻿⁦⁧⁨⁩"), None)
_SPACES = {0x202F: " ", 0x00A0: " ", 0x2011: "-", 0x2019: "'", 0x2018: "'", 0x201C: '"', 0x201D: '"'}

_IOS = re.compile(
    r"^\[(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[APap]\.?[Mm]\.?)?)\]\s(?P<rest>.*)$"
)
_ANDROID = re.compile(
    r"^(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[APap]\.?[Mm]\.?)?)\s[-–]\s(?P<rest>.*)$"
)

MEDIA_RE = re.compile(
    r"^(?:<attached: .*>|<Media omitted>|(?:image|video|audio|sticker|GIF|document|Contact card|contact card)\s+omitted"
    r"|.*\.(?:pdf|docx?|xlsx?|vcf|jpe?g|png|mp4|opus)\s+•.*\s+omitted|.*document omitted|null)$",
    re.I,
)
_SYSTEM_PATTERNS = [
    r"security code .*changed",
    r"joined using (?:this|a) group(?:'s invite)? link",
    r"\badded\b",
    r"\bremoved\b",
    r"^.{0,80}\bleft$",
    r"created (?:this )?group",
    r"changed (?:the |this )?(?:group|subject|description|settings|icon)",
    r"changed their phone number",
    r"Messages and calls are end-to-end encrypted",
    r"Disappearing messages",
    r"Waiting for this message",
    r"^This message was deleted\.?$",
    r"^You deleted this message\.?$",
    r"pinned a message",
    r"is now an admin|no longer an admin",
    r"started a call|Missed (?:voice|video) call",
    r"POLL:",
]
_SYSTEM_RE = re.compile("|".join(_SYSTEM_PATTERNS), re.I)
_INLINE_MEDIA_RE = re.compile(r"\s+(image|video|audio|sticker|GIF|document|Contact card) omitted$", re.I)
_EDITED_RE = re.compile(r"\s*<This message was edited>\s*$")


@dataclass
class Message:
    ts: datetime
    sender: str  # cleaned display name or phone number, exactly as WhatsApp shows it
    text: str
    is_media: bool = False
    is_system: bool = False
    line_no: int = 0
    extra: dict = field(default_factory=dict)


def clean(s: str) -> str:
    return s.translate(_INVISIBLE).translate(_SPACES)


def read_export(path: str | Path) -> list[tuple[str, str]]:
    """Return [(chat_file_name, text)] for a .txt or a WhatsApp .zip export."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        out = []
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".txt") and not name.startswith("__MACOSX"):
                    out.append((name, zf.read(name).decode("utf-8", errors="replace")))
        return out
    return [(path.name, path.read_text(encoding="utf-8", errors="replace"))]


def group_name_from_filename(path: str | Path) -> str:
    stem = Path(path).stem
    stem = re.sub(r"^[0-9a-f]{6,}-", "", stem)  # upload prefixes like "283ad0c4-"
    stem = re.sub(r"^WhatsApp[ _]Chat[ _]+(?:-[ _]+|with[ _]+)?", "", stem, flags=re.I)
    return stem.replace("_", " ").strip(" -") or stem


def _date_order(lines: list[str]) -> str:
    """Guess 'MDY' or 'DMY' (or 'YMD') from every date in the file."""
    firsts, seconds = [], []
    for ln in lines:
        m = _IOS.match(ln) or _ANDROID.match(ln)
        if m:
            parts = re.split(r"[./-]", m["date"])
            if len(parts[0]) == 4:
                return "YMD"
            firsts.append(int(parts[0]))
            seconds.append(int(parts[1]))
    if any(f > 12 for f in firsts):
        return "DMY"
    return "MDY"  # also the right call when both are always <= 12 for a US-locale phone


def _parse_ts(date: str, time: str, order: str) -> datetime:
    a, b, c = (int(x) for x in re.split(r"[./-]", date))
    if order == "YMD":
        y, mo, d = a, b, c
    elif order == "DMY":
        d, mo, y = a, b, c
    else:
        mo, d, y = a, b, c
    if y < 100:
        y += 2000
    t = time.replace(".", ":").upper().replace(" ", "").replace("A:M:", "AM").replace("P:M:", "PM")
    t = t.replace("A:M", "AM").replace("P:M", "PM")
    ampm = None
    if t.endswith(("AM", "PM")):
        ampm, t = t[-2:], t[:-2]
    hms = [int(x) for x in t.split(":") if x]
    h, mi, s = (hms + [0, 0])[:3]
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return datetime(y, mo, d, h, mi, s)


def parse_text(text: str) -> list[Message]:
    # Messages end with CRLF on iOS; continuation lines inside a message use bare LF.
    # Splitting on any newline and re-joining non-header lines handles both platforms.
    originals = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw_lines = [clean(l) for l in originals]
    order = _date_order(raw_lines)
    msgs: list[Message] = []
    for i, (ln, orig) in enumerate(zip(raw_lines, originals), 1):
        m = _IOS.match(ln) or _ANDROID.match(ln)
        if m:
            rest = m["rest"]
            sender, sep, body = rest.partition(": ")
            if not sep:  # system line with no sender ("You were added", Android style)
                sender, body = "", rest
            msg = Message(_parse_ts(m["date"], m["time"], order), sender.strip(), body, line_no=i)
            # iOS marks system notices and media placeholders with a leading U+200E in the body.
            _, _, obody = orig.partition(": ")
            msg.extra["lrm"] = obody.startswith("\u200e")
            msgs.append(msg)
        elif msgs:
            msgs[-1].text += "\n" + ln
        # text before the first header is ignored
    for msg in msgs:
        msg.sender = re.sub(r"^~\s*", "", msg.sender).strip()
        msg.text = _EDITED_RE.sub("", msg.text.strip())
        msg.is_media = bool(MEDIA_RE.match(msg.text))
        if not msg.is_media:
            # iOS appends the attachment to a caption: "Look at this image omitted"
            m2 = _INLINE_MEDIA_RE.search(msg.text)
            if m2:
                msg.text = msg.text[: m2.start()].rstrip()
                msg.extra["media"] = m2.group(1).lower()
        else:
            msg.extra["media"] = msg.text.lower()
        if msg.is_media:
            msg.is_system = False
        elif not msg.sender:
            msg.is_system = True
        else:
            # Trust the iOS marker when present; otherwise fall back to the phrase list on short lines.
            msg.is_system = bool(_SYSTEM_RE.search(msg.text)) and (msg.extra.get("lrm") or len(msg.text) < 120)
            if msg.is_system and not msg.extra.get("lrm") and re.search(r"\b(I|we|you)\b", msg.text, re.I):
                msg.is_system = False  # "I added the link above" is a person talking
    # iOS sometimes writes a captioned attachment twice; drop exact repeats a few minutes apart.
    out: list[Message] = []
    seen: dict[tuple[str, str], datetime] = {}
    for msg in msgs:
        k = (msg.sender, msg.text)
        prev = seen.get(k)
        if prev and not msg.is_media and abs((msg.ts - prev).total_seconds()) < 600:
            continue
        seen[k] = msg.ts
        out.append(msg)
    return out


PHONE_ONLY_RE = re.compile(r"^\+?[\d\s()\-.]{7,}$")


def first_name(sender: str) -> str | None:
    """'Jane Doe' -> 'Jane'; '+1 (206) 555-0100' -> None; "Jane's Business Number" -> 'Jane'."""
    s = sender.strip()
    if not s or PHONE_ONLY_RE.match(s):
        return None
    tok = re.split(r"\s+", s)[0]
    tok = re.sub(r"[’']s$", "", tok)
    tok = re.sub(r"[^\w\-.]", "", tok)  # emoji and punctuation
    return tok or None
