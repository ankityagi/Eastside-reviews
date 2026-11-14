"""SQLite storage.

Privacy rules enforced here:
  * neighbours are stored as (opaque key, first name). The key is an HMAC of the sender as the
    platform shows it, made with a secret kept in a separate file (secret.key), so the database
    alone cannot be used to recover anyone's phone number. Senders shown only as a phone number
    get no first name at all.
  * no raw chat is stored - only the recommendation quote, which mentions neighbours by first name.

Schema is platform-neutral: `sources.platform` ('whatsapp', later 'facebook', ...), free-form
`external_ref` per mention (WhatsApp line number, Facebook post/comment id), and `extra` JSON columns.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import merge
from .extract import EXTRACTOR_VERSION, Mention

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- one row per imported file / export
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY,
    platform      TEXT NOT NULL,              -- 'whatsapp', 'facebook', ...
    group_name    TEXT NOT NULL,
    file_name     TEXT,
    file_sha256   TEXT UNIQUE,
    imported_at   TEXT NOT NULL,
    message_count INTEGER,
    extra         TEXT NOT NULL DEFAULT '{}'
);

-- people who recommended someone: first name only, no phone numbers
CREATE TABLE IF NOT EXISTS neighbors (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,         -- HMAC(secret, platform|sender)
    platform    TEXT NOT NULL,
    first_name  TEXT                          -- NULL when the sender was only a phone number
);

-- every time a neighbour recommended (or warned about) a pro
CREATE TABLE IF NOT EXISTS mentions (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    neighbor_id   INTEGER NOT NULL REFERENCES neighbors(id),
    group_name    TEXT NOT NULL,
    posted_at     TEXT NOT NULL,              -- ISO timestamp from the chat
    external_ref  TEXT,                       -- line number / post id inside the source
    trade         TEXT,                       -- primary trade
    trades        TEXT NOT NULL DEFAULT '[]', -- JSON list, primary first
    sentiment     TEXT NOT NULL CHECK (sentiment IN ('positive','negative','neutral')),
    quote         TEXT NOT NULL,
    pro_name      TEXT,                       -- as written in the chat
    person_name   TEXT,                       -- contact person at a business ("Sam" at Acme Electric)
    phones        TEXT NOT NULL DEFAULT '[]', -- pro phone numbers, 10 digits
    websites      TEXT NOT NULL DEFAULT '[]',
    is_card       INTEGER NOT NULL DEFAULT 0, -- neighbour shared a WhatsApp contact card
    card_owner_id INTEGER REFERENCES neighbors(id),
    request_ref   TEXT,                       -- the question this answered, if known
    via           TEXT,                       -- extraction rule (debugging)
    extractor_version INTEGER,
    dedupe_key    TEXT NOT NULL UNIQUE,       -- same message seen in two overlapping exports counts once
    pro_id        INTEGER REFERENCES pros(id) ON DELETE SET NULL,
    extra         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS mentions_pro ON mentions(pro_id);
CREATE INDEX IF NOT EXISTS mentions_trade ON mentions(trade);

-- merged pros (rebuilt from mentions + curation after every import)
CREATE TABLE IF NOT EXISTS pros (
    id            INTEGER PRIMARY KEY,
    display_name  TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- business / person / unnamed
    hidden        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pro_trades   (pro_id INTEGER REFERENCES pros(id) ON DELETE CASCADE, trade TEXT, PRIMARY KEY (pro_id, trade));
CREATE TABLE IF NOT EXISTS pro_contacts (pro_id INTEGER REFERENCES pros(id) ON DELETE CASCADE, kind TEXT CHECK (kind IN ('phone','website','email')), value TEXT, PRIMARY KEY (pro_id, kind, value));
CREATE TABLE IF NOT EXISTS pro_aliases  (pro_id INTEGER REFERENCES pros(id) ON DELETE CASCADE, alias TEXT, PRIMARY KEY (pro_id, alias));

-- your manual fixes; replayed on every rebuild so they survive re-imports
CREATE TABLE IF NOT EXISTS curation (
    id          INTEGER PRIMARY KEY,
    action      TEXT NOT NULL CHECK (action IN ('merge','rename','hide','unhide')),
    a           TEXT NOT NULL,
    b           TEXT,
    created_at  TEXT NOT NULL
);
"""


def home() -> Path:
    return Path(os.environ.get("RECS_HOME", Path.home() / ".recs"))


def default_db() -> Path:
    return Path(os.environ.get("RECS_DB", home() / "recs.db"))


def _secret(db_path: Path) -> bytes:
    p = Path(os.environ.get("RECS_SECRET", db_path.with_name("secret.key")))
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return bytes.fromhex(p.read_text().strip())


class DB:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.executescript(SCHEMA)
        self.con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.con.commit()
        self._key = _secret(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ neighbours
    def neighbor_id(self, platform: str, sender: str, first: str | None) -> int:
        norm = " ".join(sender.lower().split())
        key = hmac.new(self._key, f"{platform}|{norm}".encode(), hashlib.sha256).hexdigest()[:32]
        row = self.con.execute("SELECT id FROM neighbors WHERE key = ?", (key,)).fetchone()
        if row:
            return row["id"]
        return self.con.execute("INSERT INTO neighbors (key, platform, first_name) VALUES (?,?,?)", (key, platform, first)).lastrowid

    # ------------------------------------------------------------------ import
    def source_exists(self, sha: str) -> bool:
        return self.con.execute("SELECT 1 FROM sources WHERE file_sha256 = ?", (sha,)).fetchone() is not None

    def drop_source(self, sha: str) -> None:
        self.con.execute("DELETE FROM sources WHERE file_sha256 = ?", (sha,))

    def add_source(self, platform: str, group: str, file_name: str, sha: str, n_messages: int) -> int:
        return self.con.execute(
            "INSERT INTO sources (platform, group_name, file_name, file_sha256, imported_at, message_count) VALUES (?,?,?,?,?,?)",
            (platform, group, file_name, sha, datetime.now(timezone.utc).isoformat(timespec="seconds"), n_messages),
        ).lastrowid

    def add_mentions(self, source_id: int, platform: str, group: str, mentions: list[Mention], first_name_fn) -> int:
        added = 0
        for m in mentions:
            nid = self.neighbor_id(platform, m.sender, first_name_fn(m.sender))
            owner = self.neighbor_id(platform, m.card_owner, first_name_fn(m.card_owner)) if m.card_owner else None
            dk = hashlib.sha256(json.dumps([platform, group, nid, m.posted_at.isoformat(), m.pro_name, sorted(m.phones),
                                            sorted(m.websites), m.is_card]).encode()).hexdigest()
            cur = self.con.execute(
                """INSERT OR IGNORE INTO mentions (source_id, neighbor_id, group_name, posted_at, external_ref, trade, trades,
                   sentiment, quote, pro_name, person_name, phones, websites, is_card, card_owner_id, request_ref, via,
                   extractor_version, dedupe_key, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source_id, nid, group, m.posted_at.isoformat(timespec="minutes"), m.external_ref, m.trade, json.dumps(m.trades),
                 m.sentiment, m.quote, m.pro_name, m.person_name, json.dumps(m.phones), json.dumps(m.websites), int(m.is_card),
                 owner, m.request_ref, m.via, EXTRACTOR_VERSION, dk,
                 json.dumps({"request": {"by": first_name_fn(m.request_by) if m.request_by else None, "text": m.request_text}}
                            if m.request_text else {})),
            )
            added += cur.rowcount
        return added

    # ------------------------------------------------------------------ merging
    def rebuild(self) -> int:
        """Recompute merged pros from every mention plus the curation log."""
        rows = []
        for r in self.con.execute("SELECT * FROM mentions"):
            d = dict(r)
            d["phones"], d["websites"], d["trades"] = json.loads(d["phones"]), json.loads(d["websites"]), json.loads(d["trades"])
            rows.append(d)
        cur_rows = self.con.execute("SELECT action, a, b FROM curation ORDER BY id").fetchall()
        merges = [(c["a"], c["b"]) for c in cur_rows if c["action"] == "merge"]
        roots = merge.cluster(rows, merges)
        first_names = {r["id"]: r["first_name"] for r in self.con.execute("SELECT id, first_name FROM neighbors")}

        self.con.execute("UPDATE mentions SET pro_id = NULL")
        self.con.execute("DELETE FROM pros")
        clusters: dict[int, list[dict]] = {}
        for r in rows:
            clusters.setdefault(roots[r["id"]], []).append(r)
        renames = {merge.name_key(c["a"]): c["b"] for c in cur_rows if c["action"] == "rename"}
        hide_state: dict[str, bool] = {}
        for c in cur_rows:
            if c["action"] in ("hide", "unhide"):
                hide_state[merge.name_key(c["a"])] = c["action"] == "hide"
        for members in clusters.values():
            name, kind = merge.display_name(members, first_names)
            aliases = sorted({n for m in members for n in (m["pro_name"], m["person_name"]) if n})
            keys = {merge.name_key(n) for n in aliases + [name]}
            for k, new in renames.items():
                if k in keys:
                    name = new
            hidden = any(hide_state.get(k) for k in keys | {merge.name_key(name)})
            pid = self.con.execute("INSERT INTO pros (display_name, kind, hidden) VALUES (?,?,?)", (name, kind, int(hidden))).lastrowid
            trades = []
            for m in sorted(members, key=lambda m: m["posted_at"]):
                trades += m["trades"] or ([m["trade"]] if m["trade"] else [])
            for t in dict.fromkeys(trades):
                self.con.execute("INSERT OR IGNORE INTO pro_trades VALUES (?,?)", (pid, t))
            for m in members:
                for ph in m["phones"]:
                    self.con.execute("INSERT OR IGNORE INTO pro_contacts VALUES (?, 'phone', ?)", (pid, ph))
                for u in m["websites"]:
                    kind, val = ("email", u[7:]) if u.startswith("mailto:") else ("website", u)
                    self.con.execute("INSERT OR IGNORE INTO pro_contacts VALUES (?, ?, ?)", (pid, kind, val))
            for a in aliases:
                self.con.execute("INSERT OR IGNORE INTO pro_aliases VALUES (?,?)", (pid, a))
            self.con.executemany("UPDATE mentions SET pro_id = ? WHERE id = ?", [(pid, m["id"]) for m in members])
        self.con.commit()
        return len(clusters)

    def curate(self, action: str, a: str, b: str | None = None) -> None:
        self.con.execute("INSERT INTO curation (action, a, b, created_at) VALUES (?,?,?,?)",
                         (action, a, b, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self.con.commit()
        self.rebuild()

    def find_pros(self, text: str) -> list[sqlite3.Row]:
        like = f"%{text.lower()}%"
        return self.con.execute(
            """SELECT DISTINCT p.* FROM pros p LEFT JOIN pro_aliases a ON a.pro_id = p.id
               LEFT JOIN pro_contacts c ON c.pro_id = p.id
               WHERE lower(p.display_name) LIKE ? OR lower(a.alias) LIKE ? OR c.value LIKE ?""",
            (like, like, f"%{''.join(ch for ch in text if ch.isdigit()) or '~'}%"),
        ).fetchall()

    # ------------------------------------------------------------------ queries
    def ranked(self, trades: list[str], include_hidden: bool = False, since: str | None = None) -> list[dict]:
        """Pros for these trades, ranked by distinct neighbours with a positive mention, then recency."""
        q = f"""
            SELECT m.*, n.first_name, p.display_name, p.kind, p.hidden
            FROM mentions m JOIN pros p ON p.id = m.pro_id JOIN neighbors n ON n.id = m.neighbor_id
            WHERE m.pro_id IN (SELECT pro_id FROM pro_trades WHERE trade IN ({','.join('?' * len(trades))}))
            {'' if include_hidden else 'AND p.hidden = 0'} {'AND m.posted_at >= ?' if since else ''}
            ORDER BY m.posted_at DESC"""
        rows = self.con.execute(q, trades + ([since] if since else [])).fetchall()
        pros: dict[int, dict] = {}
        for r in rows:
            p = pros.setdefault(r["pro_id"], {"id": r["pro_id"], "name": r["display_name"], "kind": r["kind"],
                                              "mentions": [], "other": []})
            in_trade = bool(set(json.loads(r["trades"]) or [r["trade"]]) & set(trades))
            (p["mentions"] if in_trade else p["other"]).append(dict(r))
        out = []
        for p in pros.values():
            if not p["mentions"]:
                continue
            by = lambda s: {m["neighbor_id"] for m in p["mentions"] if m["sentiment"] == s}
            p["pos"], p["neg"], p["neu"] = by("positive"), by("negative"), by("neutral")
            p["score"] = len(p["pos"] - p["neg"])  # distinct neighbours who vouched (a later warning cancels)
            p["last_positive"] = max((m["posted_at"] for m in p["mentions"] if m["sentiment"] == "positive"), default="")
            p["last"] = max(m["posted_at"] for m in p["mentions"])
            p["contacts"] = [dict(c) for c in self.con.execute(
                "SELECT kind, value FROM pro_contacts WHERE pro_id = ? ORDER BY kind DESC, value", (p["id"],))]
            p["trades"] = [r["trade"] for r in self.con.execute("SELECT trade FROM pro_trades WHERE pro_id = ?", (p["id"],))]
            out.append(p)
        # rank: distinct recommending neighbours, then most recent recommendation
        out.sort(key=lambda p: (p["score"], p["last_positive"], len(p["neu"]), p["last"]), reverse=True)
        return out

    def trade_counts(self) -> list[sqlite3.Row]:
        return self.con.execute(
            """SELECT t.trade, COUNT(DISTINCT t.pro_id) AS pros, COUNT(DISTINCT m.neighbor_id) AS neighbors, MAX(m.posted_at) AS last
               FROM pro_trades t JOIN pros p ON p.id = t.pro_id AND p.hidden = 0 JOIN mentions m ON m.pro_id = t.pro_id
               GROUP BY t.trade ORDER BY pros DESC, t.trade""").fetchall()

    def stats(self) -> dict:
        one = lambda q: self.con.execute(q).fetchone()[0]
        return {
            "sources": [dict(r) for r in self.con.execute("SELECT platform, group_name, file_name, message_count, imported_at FROM sources")],
            "mentions": one("SELECT COUNT(*) FROM mentions"),
            "pros": one("SELECT COUNT(*) FROM pros WHERE hidden = 0"),
            "neighbors": one("SELECT COUNT(DISTINCT neighbor_id) FROM mentions"),
        }
