"""The referrals page behind a login. A plain WSGI app with no dependencies.

Local:       recs serve                       (http://127.0.0.1:8000)
Production:  gunicorn -w 2 -b 0.0.0.0:8000 'recs.web:application'   behind HTTPS

Environment:
  RECS_HOME / RECS_DB      where recs.db, secret.key and session.key live
  RECS_SESSION_SECRET      cookie signing secret (default: generated into $RECS_HOME/session.key)
  RECS_TITLE               page title (default "<first word of group> Referrals")
  RECS_INSECURE_COOKIES=1  allow cookies over plain http (local testing only)
  RECS_TRUST_PROXY=1       take the client IP from X-Forwarded-For (when behind a reverse proxy)
"""
from __future__ import annotations

import html
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from http import cookies
from urllib.parse import parse_qs, urlencode

from . import auth
from .db import DB, home
from .site import render

COOKIE = "recs_session"
CSRF_COOKIE = "recs_csrf"
MAX_BODY = 8 * 1024
FAIL_WINDOW = 15 * 60
FAIL_LIMIT = 5


class RateLimiter:
    """Failed sign-ins per IP and per username; 5 failures in 15 minutes blocks further tries."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fails: dict[str, deque] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque:
        q = self.fails[key]
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        now = time.time()
        with self.lock:
            return any(len(self._trim(k, now)) >= FAIL_LIMIT for k in keys)

    def fail(self, *keys: str) -> None:
        now = time.time()
        with self.lock:
            for k in keys:
                self._trim(k, now).append(now)

    def clear(self, *keys: str) -> None:
        with self.lock:
            for k in keys:
                self.fails.pop(k, None)


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>Sign in · __TITLE__</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=Inter:wght@400;500;600&display=swap">
<style>
:root { --bg: #f3f5f4; --surface: #fff; --line: #d5ddda; --fg: #17211e; --muted: #5b6b66; --accent: #1f5f8b; --bad: #b23a2e; --bad-soft: #f6dedb;
  --display: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif; --body: "Inter", system-ui, sans-serif }
@media (prefers-color-scheme: dark) { :root { --bg: #111816; --surface: #18211e; --line: #2a3632; --fg: #e4ece9; --muted: #93a49e;
  --accent: #7db8e0; --bad: #ef8b7f; --bad-soft: #3a1d1a; color-scheme: dark } }
* { box-sizing: border-box }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--fg); font: 15px/1.5 var(--body);
  padding: 24px max(16px, env(safe-area-inset-right, 0px)) 24px max(16px, env(safe-area-inset-left, 0px)) }
main { width: 100%; max-width: 360px; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 28px; display: grid; gap: 16px }
h1 { font: 700 26px/1.1 var(--display); margin: 0; letter-spacing: -0.02em }
p { margin: 0; color: var(--muted); font-size: 14px }
form { display: grid; gap: 12px }
label { display: grid; gap: 5px; font-size: 13px; font-weight: 500 }
input { font: inherit; padding: 10px 12px; border: 1px solid var(--line); border-radius: 9px; background: var(--bg); color: var(--fg) }
input:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }
button { font: 600 15px/1 var(--body); padding: 12px; border: 0; border-radius: 9px; background: var(--accent); color: var(--surface); cursor: pointer }
.err { color: var(--bad); background: var(--bad-soft); border-radius: 8px; padding: 9px 11px; font-size: 13.5px; margin: 0 }
</style></head>
<body><main>
  <h1>__TITLE__</h1>
  <p>Neighbor recommendations are private to the group. Sign in with the account you were given.</p>
  __ERROR__
  <form method="post" action="/login">
    <input type="hidden" name="csrf" value="__CSRF__">
    <input type="hidden" name="next" value="__NEXT__">
    <label for="username">Username<input id="username" name="username" autocomplete="username" required autofocus></label>
    <label for="password">Password<input id="password" name="password" type="password" autocomplete="current-password" required></label>
    <button type="submit">Sign in</button>
  </form>
</main></body></html>
"""


class App:
    def __init__(self, db_path=None, title: str | None = None):
        self.db_path = db_path
        self.title = title or os.environ.get("RECS_TITLE")
        self.secure = os.environ.get("RECS_INSECURE_COOKIES") != "1"
        self.trust_proxy = os.environ.get("RECS_TRUST_PROXY") == "1"
        self.secret = auth.session_secret(home() if db_path is None else _home_of(db_path))
        self.limiter = RateLimiter()
        db = DB(self.db_path)  # create tables up front
        auth.Users(db.con)
        db.con.close()

    # ----------------------------------------------------------- plumbing
    def _db(self):
        db = DB(self.db_path)
        return db, auth.Users(db.con)

    def _ip(self, env) -> str:
        if self.trust_proxy and env.get("HTTP_X_FORWARDED_FOR"):
            return env["HTTP_X_FORWARDED_FOR"].split(",")[0].strip()
        return env.get("REMOTE_ADDR", "?")

    def _cookies(self, env) -> dict:
        c = cookies.SimpleCookie()
        try:
            c.load(env.get("HTTP_COOKIE", ""))
        except cookies.CookieError:
            return {}
        return {k: v.value for k, v in c.items()}

    def _set_cookie(self, name: str, value: str, max_age: int, path: str = "/") -> tuple[str, str]:
        parts = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
        if self.secure:
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def _headers(self, ctype: str, nonce: str | None = None, extra=()) -> list[tuple[str, str]]:
        n = f"'nonce-{nonce}'" if nonce else "'none'"
        csp = (f"default-src 'none'; script-src {n}; style-src {n} https://fonts.googleapis.com; "
               "font-src https://fonts.gstatic.com; img-src 'self' data:; form-action 'self'; "
               "frame-ancestors 'none'; base-uri 'none'")
        h = [("Content-Type", ctype), ("Content-Security-Policy", csp), ("X-Content-Type-Options", "nosniff"),
             ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"), ("X-Robots-Tag", "noindex, nofollow"),
             ("Cache-Control", "no-store")]
        if self.secure:
            h.append(("Strict-Transport-Security", "max-age=31536000"))
        return h + list(extra)

    def _form(self, env) -> dict:
        try:
            n = int(env.get("CONTENT_LENGTH") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return {}
        body = env["wsgi.input"].read(n).decode("utf-8", "replace") if n else ""
        return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}

    def _session(self, env, users):
        data = auth.unsign(self._cookies(env).get(COOKIE, ""), self.secret)
        if not data:
            return None, None
        row = users.by_id(data.get("u"))
        if not row or row["session_version"] != data.get("v"):
            return None, None
        return row, data

    @staticmethod
    def _redirect(start, to: str, extra=()):
        start("303 See Other", [("Location", to), ("Cache-Control", "no-store")] + list(extra))
        return [b""]

    def _login_page(self, start, status="200 OK", error="", nxt="/", db=None):
        token = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(16)
        title = self.title
        if not title and db is not None:
            from .site import default_title
            groups = [r[0] for r in db.con.execute("SELECT DISTINCT group_name FROM sources ORDER BY group_name")]
            title = default_title(groups)
        page = (LOGIN_PAGE.replace("__TITLE__", html.escape(title or "Referrals"))
                .replace("__CSRF__", token).replace("__NEXT__", html.escape(nxt))
                .replace("__ERROR__", f'<p class="err" role="alert">{html.escape(error)}</p>' if error else "")
                .replace("<style>", f'<style nonce="{nonce}">'))
        start(status, self._headers("text/html; charset=utf-8", nonce, [self._set_cookie(CSRF_COOKIE, token, 3600)]))
        return [page.encode()]

    # ----------------------------------------------------------- routes
    def __call__(self, env, start):
        method, path = env.get("REQUEST_METHOD", "GET"), env.get("PATH_INFO", "/")
        if path == "/healthz":
            start("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]
        db, users = self._db()
        try:
            if path == "/login" and method == "GET":
                nxt = parse_qs(env.get("QUERY_STRING", "")).get("next", ["/"])[0]
                return self._login_page(start, nxt=nxt if nxt.startswith("/") and not nxt.startswith("//") else "/", db=db)
            if path == "/login" and method == "POST":
                return self._do_login(env, start, db, users)
            if path == "/logout" and method == "POST":
                user, sess = self._session(env, users)
                form = self._form(env)
                if not (user and secrets.compare_digest(form.get("csrf", ""), sess.get("c", ""))):
                    return self._redirect(start, "/")  # forged or stale sign-out request: ignore it
                return self._redirect(start, "/login", [self._set_cookie(COOKIE, "", 0)])
            if path in ("/", "/index.html") and method == "GET":
                user, sess = self._session(env, users)
                if not user:
                    return self._redirect(start, "/login?" + urlencode({"next": path}))
                nonce = secrets.token_urlsafe(16)
                page = render(db, self.title, standalone=True, nonce=nonce, user=user["username"], csrf=sess["c"])
                start("200 OK", self._headers("text/html; charset=utf-8", nonce))
                return [page.encode()]
            start("404 Not Found", self._headers("text/plain; charset=utf-8"))
            return [b"Not found"]
        finally:
            db.con.close()

    def _do_login(self, env, start, db, users):
        form = self._form(env)
        nxt = form.get("next", "/")
        nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
        expected = self._cookies(env).get(CSRF_COOKIE, "")
        if not expected or not secrets.compare_digest(form.get("csrf", ""), expected):
            return self._login_page(start, "400 Bad Request", "Your sign-in form expired. Please try again.", nxt, db)
        username = form.get("username", "").strip()[:100]
        ip = self._ip(env)
        keys = (f"ip:{ip}", f"user:{username.lower()}")
        if self.limiter.blocked(*keys):
            return self._login_page(start, "429 Too Many Requests", "Too many failed attempts. Wait 15 minutes and try again.", nxt, db)
        row = users.check(username, form.get("password", "")[:200])
        if not row:
            self.limiter.fail(*keys)
            return self._login_page(start, "401 Unauthorized", "That username and password don't match.", nxt, db)
        self.limiter.clear(*keys)
        token = auth.sign(auth.new_session(row), self.secret)
        return self._redirect(start, nxt, [self._set_cookie(COOKIE, token, auth.SESSION_DAYS * 86400),
                                           self._set_cookie(CSRF_COOKIE, "", 0)])


def _home_of(db_path):
    from pathlib import Path
    return Path(db_path).expanduser().resolve().parent


class _LazyApp:
    """`recs.web:application` for gunicorn; built on first request so importing never touches disk."""

    def __init__(self):
        self._app = None
        self._lock = threading.Lock()

    def __call__(self, env, start):
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = App(os.environ.get("RECS_DB"))
        return self._app(env, start)


application = _LazyApp()


def serve(host: str = "127.0.0.1", port: int = 8000, db_path=None) -> None:
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server

    class Threaded(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    if host in ("127.0.0.1", "localhost", "::1"):
        os.environ.setdefault("RECS_INSECURE_COOKIES", "1")  # plain http on your own machine
    app = App(db_path)
    with make_server(host, port, app, server_class=Threaded) as httpd:
        print(f"Serving on http://{host}:{port}  (Ctrl+C to stop)")
        httpd.serve_forever()
