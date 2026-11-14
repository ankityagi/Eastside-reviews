"""Accounts and sessions for the web server. Standard library only.

* Accounts are invite-only: created from the command line (`recs user add NAME`). There is no
  public sign-up page.
* Passwords are stored as scrypt hashes with a per-user random salt.
* Sessions are signed cookies (HMAC-SHA256 with a server secret). Each user has a session_version;
  bumping it (`recs user signout NAME`, a password change, or removing the user) invalidates
  every cookie already handed out.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash         TEXT NOT NULL,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login      TEXT
);
"""

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SESSION_DAYS = 30
MIN_PASSWORD = 10


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(h)}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(h)))
        return hmac.compare_digest(got, _unb64(h))
    except (ValueError, TypeError):
        return False


# a hash to check against when the username doesn't exist, so timing doesn't reveal valid names
_DUMMY = hash_password(secrets.token_hex(8))


def generate_password() -> str:
    words = secrets.token_urlsafe(12)
    return "-".join(words[i:i + 4] for i in range(0, 16, 4))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Users:
    def __init__(self, con):
        self.con = con
        con.executescript(USERS_SCHEMA)
        con.commit()

    def add(self, username: str, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        self.con.execute("INSERT INTO users (username, pw_hash, created_at) VALUES (?,?,?)",
                         (username.strip(), hash_password(password), now()))
        self.con.commit()

    def set_password(self, username: str, password: str) -> bool:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        cur = self.con.execute("UPDATE users SET pw_hash = ?, session_version = session_version + 1 WHERE username = ?",
                               (hash_password(password), username))
        self.con.commit()
        return cur.rowcount == 1

    def remove(self, username: str) -> bool:
        cur = self.con.execute("DELETE FROM users WHERE username = ?", (username,))
        self.con.commit()
        return cur.rowcount == 1

    def sign_out_everywhere(self, username: str) -> bool:
        cur = self.con.execute("UPDATE users SET session_version = session_version + 1 WHERE username = ?", (username,))
        self.con.commit()
        return cur.rowcount == 1

    def list(self):
        return self.con.execute("SELECT username, created_at, last_login FROM users ORDER BY username").fetchall()

    def check(self, username: str, password: str):
        row = self.con.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        ok = verify_password(password, row["pw_hash"] if row else _DUMMY)
        if not (row and ok):
            return None
        self.con.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), row["id"]))
        self.con.commit()
        return row

    def by_id(self, uid: int):
        return self.con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


# ---------------------------------------------------------------- signed session cookies

def session_secret(home: Path) -> bytes:
    env = os.environ.get("RECS_SESSION_SECRET")
    if env:
        return env.encode()
    p = home / "session.key"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return p.read_text().strip().encode()


def sign(payload: dict, secret: bytes) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def unsign(token: str, secret: bytes) -> dict | None:
    try:
        body, mac = token.split(".")
        want = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, want):
            return None
        data = json.loads(_unb64(body))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except (ValueError, TypeError):
        return None


def new_session(user_row) -> dict:
    return {"u": user_row["id"], "v": user_row["session_version"], "c": secrets.token_urlsafe(16),
            "exp": int(time.time()) + SESSION_DAYS * 86400}
