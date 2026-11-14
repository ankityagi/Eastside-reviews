"""Login and access tests for the web server, driven through WSGI directly (no network)."""
import io
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import urlencode

from recs import cli
from recs.auth import Users, hash_password, verify_password
from recs.db import DB

CHAT = (
    "3/1/26, 9:01 AM - Priya Shah: Any recommendations for a plumber?\n"
    "3/1/26, 9:05 AM - Tom Lee: Joe at Rapid Rooter is great. (425) 555-0101\n"
)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        os.environ.update(RECS_HOME=str(d))
        for k in ("RECS_DB", "RECS_INSECURE_COOKIES", "RECS_TRUST_PROXY", "RECS_SESSION_SECRET", "RECS_TITLE"):
            os.environ.pop(k, None)
        z = d / "WhatsApp Chat - Maple Street.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("_chat.txt", CHAT)
        with open(os.devnull, "w") as null:
            import contextlib
            with contextlib.redirect_stdout(null):
                cli.main(["import", str(z)])
        db = DB()
        Users(db.con).add("jane", "correct horse battery")
        db.con.close()
        from recs.web import App
        self.app = App()
        self.jar: dict[str, str] = {}

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, method, path, form=None, ip="10.0.0.1"):
        body = urlencode(form or {}).encode()
        env = {"REQUEST_METHOD": method, "PATH_INFO": path.split("?")[0], "QUERY_STRING": path.partition("?")[2],
               "REMOTE_ADDR": ip, "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body)),
               "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in self.jar.items())}
        out = {}

        def start(status, headers):
            out["status"], out["headers"] = status, headers

        out["body"] = b"".join(self.app(env, start)).decode()
        for k, v in out["headers"]:
            if k == "Set-Cookie":
                name, _, rest = v.partition("=")
                val = rest.split(";")[0]
                if "Max-Age=0" in v:
                    self.jar.pop(name, None)
                else:
                    self.jar[name] = val
        out["code"] = int(out["status"].split()[0])
        out["h"] = dict(out["headers"])
        return out

    def login(self, user="jane", pw="correct horse battery", ip="10.0.0.1"):
        page = self.req("GET", "/login", ip=ip)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page["body"]).group(1)
        return self.req("POST", "/login", {"csrf": csrf, "username": user, "password": pw, "next": "/"}, ip=ip)

    def test_page_requires_login(self):
        r = self.req("GET", "/")
        self.assertEqual((r["code"], r["h"]["Location"]), (303, "/login?next=%2F"))
        self.assertNotIn("Rapid Rooter", r["body"])

    def test_login_then_page(self):
        r = self.login()
        self.assertEqual(r["code"], 303)
        page = self.req("GET", "/")
        self.assertEqual(page["code"], 200)
        self.assertIn("Rapid Rooter", page["body"])
        self.assertIn("Signed in as <b>jane</b>", page["body"])
        self.assertIn("noindex", page["h"]["X-Robots-Tag"])
        nonce = re.search(r"'nonce-([^']+)'", page["h"]["Content-Security-Policy"]).group(1)
        self.assertIn(f'<script nonce="{nonce}">', page["body"])

    def test_cookie_flags(self):
        self.login()
        r = self.req("GET", "/login")
        cookie = next(v for k, v in r["headers"] if k == "Set-Cookie")
        for flag in ("HttpOnly", "SameSite=Lax", "Secure"):
            self.assertIn(flag, cookie)

    def test_wrong_password_and_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.login(pw="nope nope nope")["code"], 401)
        self.assertEqual(self.login()["code"], 429)            # right password, still locked out
        self.assertEqual(self.login(ip="10.9.9.9", user="jane")["code"], 429)  # per-username too

    def test_csrf_required(self):
        self.req("GET", "/login")
        r = self.req("POST", "/login", {"csrf": "forged", "username": "jane", "password": "correct horse battery"})
        self.assertEqual(r["code"], 400)

    def test_tampered_cookie(self):
        self.login()
        self.jar["recs_session"] = self.jar["recs_session"][:-3] + "abc"
        self.assertEqual(self.req("GET", "/")["code"], 303)

    def test_removed_user_and_signout_everywhere(self):
        self.login()
        db = DB()
        Users(db.con).sign_out_everywhere("jane")
        self.assertEqual(self.req("GET", "/")["code"], 303)
        self.login()
        Users(db.con).remove("jane")
        self.assertEqual(self.req("GET", "/")["code"], 303)

    def test_logout(self):
        self.login()
        page = self.req("GET", "/")["body"]
        csrf = re.search(r'action="/logout"><span>.*?name="csrf" value="([^"]+)"', page).group(1)
        self.assertEqual(self.req("POST", "/logout", {"csrf": csrf})["h"]["Location"], "/login")
        self.assertEqual(self.req("GET", "/")["code"], 303)

    def test_open_redirect_blocked(self):
        page = self.req("GET", "/login?next=//evil.example")
        self.assertIn('name="next" value="/"', page["body"])

    def test_password_hashing(self):
        h = hash_password("correct horse battery")
        self.assertTrue(verify_password("correct horse battery", h))
        self.assertFalse(verify_password("wrong", h))
        self.assertNotIn("correct", h)
        with self.assertRaises(ValueError):
            Users(DB().con).add("bob", "short")


if __name__ == "__main__":
    unittest.main()
