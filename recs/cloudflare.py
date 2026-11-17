"""Publish the referrals page and manage accounts for the Cloudflare Worker (see worker/ and DEPLOY.md).

  recs cf publish                      upload the page built from your local database
  recs cf user add|passwd NAME         create an account / reset its password (prints a generated password)
  recs cf user remove|signout NAME     revoke access / end all of that person's sessions
  recs cf user list

Needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in the environment. The KV namespace id is
read from worker/wrangler.toml (or RECS_KV_ID). Everything is stored in your Cloudflare account,
never in this repository.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
PBKDF2_ITER = 100_000  # the Workers runtime caps PBKDF2 at 100k iterations
MIN_PASSWORD = 10


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER, 32)
    return f"pbkdf2${PBKDF2_ITER}${_b64u(salt)}${_b64u(h)}"


def _kv_id() -> str:
    if os.environ.get("RECS_KV_ID"):
        return os.environ["RECS_KV_ID"]
    toml = Path(__file__).resolve().parent.parent / "worker" / "wrangler.toml"
    m = re.search(r'binding\s*=\s*"DATA"\s*\n\s*id\s*=\s*"([0-9a-f]{32})"', toml.read_text()) if toml.exists() else None
    if not m:
        raise SystemExit("Set RECS_KV_ID, or put the KV namespace id in worker/wrangler.toml.")
    return m.group(1)


class KV:
    def __init__(self):
        self.token = os.environ.get("CLOUDFLARE_API_TOKEN")
        self.account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if not self.token or not self.account:
            raise SystemExit("Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID first.")
        self.base = f"{API}/accounts/{self.account}/storage/kv/namespaces/{_kv_id()}"

    def _req(self, method: str, path: str, body: bytes | None = None, ctype: str = "text/plain"):
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "GET":
                return None
            raise SystemExit(f"Cloudflare API {method} {path}: HTTP {e.code} {e.read()[:300]!r}")

    @staticmethod
    def _key(k: str) -> str:
        return urllib.request.quote(k, safe="")

    def get(self, key: str):
        return self._req("GET", f"/values/{self._key(key)}")

    def put(self, key: str, value: str | bytes):
        self._req("PUT", f"/values/{self._key(key)}", value.encode() if isinstance(value, str) else value)

    def delete(self, key: str):
        self._req("DELETE", f"/values/{self._key(key)}")

    def keys(self, prefix: str) -> list[str]:
        out = json.loads(self._req("GET", f"/keys?prefix={self._key(prefix)}") or b"{}")
        return [k["name"] for k in out.get("result", [])]


def publish(db, title: str | None) -> None:
    from .site import build_data, default_title, render
    title = title or default_title(build_data(db)["groups"])
    # placeholders the Worker fills per request: CSP nonce, signed-in user, sign-out CSRF token
    page = render(db, title, standalone=True, nonce="__NONCE__", user="__USER__", csrf="__CSRF__")
    kv = KV()
    kv.put("page", page)
    kv.put("title", title)
    n = db.con.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    print(f"Published '{title}': {len(page) // 1024} KB, {n} recommendations.")


def user(action: str, name: str | None, password_stdin: bool = False) -> int:
    kv = KV()
    if action == "list":
        names = [k.split(":", 1)[1] for k in kv.keys("user:")]
        print("\n".join(f"  {n}" for n in names) if names else "No accounts yet. Add one: recs cf user add NAME")
        return 0
    if not name:
        print("Give a username, e.g. recs cf user add jane", file=sys.stderr)
        return 2
    key = f"user:{name.strip().lower()}"
    existing = json.loads(kv.get(key) or b"null")
    if action in ("add", "passwd"):
        if action == "add" and existing:
            print(f"{name} already exists; use `recs cf user passwd {name}` to reset the password.", file=sys.stderr)
            return 1
        if action == "passwd" and not existing:
            print(f"No account named {name}.", file=sys.stderr)
            return 1
        pw = sys.stdin.readline().rstrip("\n") if password_stdin else "-".join(secrets.token_urlsafe(12)[i:i + 4] for i in range(0, 16, 4))
        if len(pw) < MIN_PASSWORD:
            print(f"Password must be at least {MIN_PASSWORD} characters.", file=sys.stderr)
            return 2
        v = (existing or {}).get("v", 0) + 1  # a password reset also ends existing sessions
        kv.put(key, json.dumps({"name": name.strip(), "hash": hash_password(pw), "v": v}))
        print(f"Saved {name}." if password_stdin else f"{name}'s password: {pw}\n(shown once; send it to them privately)")
        return 0
    if not existing:
        print(f"No account named {name}.", file=sys.stderr)
        return 1
    if action == "remove":
        kv.delete(key)
    else:  # signout
        existing["v"] += 1
        kv.put(key, json.dumps(existing))
    print("Done.")
    return 0
