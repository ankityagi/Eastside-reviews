// Cloudflare Worker: the referrals page behind an invite-only sign-in.
//
// KV namespace (binding DATA) holds everything private; nothing private is in this repository:
//   page            the referrals page HTML built by `recs cf publish` (with __NONCE__/__USER__/__CSRF__ slots)
//   title           page title for the sign-in screen
//   user:<name>     {"name", "hash": "pbkdf2$<iter>$<salt>$<hash>", "v": session_version}
//   fail:<key>      failed sign-in counter (expires after 15 minutes)
// Secret SESSION_SECRET signs the session cookie (`wrangler secret put SESSION_SECRET`).

const COOKIE = "recs_session";
const CSRF_COOKIE = "recs_csrf";
const SESSION_DAYS = 30;
const FAIL_LIMIT = 5;
const FAIL_TTL = 15 * 60;
const enc = new TextEncoder();

// ------------------------------------------------------------------ helpers
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
const randomToken = (n = 24) => b64u(crypto.getRandomValues(new Uint8Array(n)));
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

function cookies(req) {
  const out = {};
  for (const part of (req.headers.get("Cookie") || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

const setCookie = (name, value, maxAge) => `${name}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;

function headers(type, nonce, extra = []) {
  const n = nonce ? `'nonce-${nonce}'` : "'none'";
  const h = new Headers({
    "Content-Type": type,
    "Content-Security-Policy": `default-src 'none'; script-src ${n}; style-src ${n} https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
    "Strict-Transport-Security": "max-age=31536000",
  });
  for (const [k, v] of extra) h.append(k, v);
  return h;
}

const redirect = (to, extra = []) => {
  const h = new Headers({ Location: to, "Cache-Control": "no-store" });
  for (const [k, v] of extra) h.append(k, v);
  return new Response(null, { status: 303, headers: h });
};

const safeNext = (n) => (typeof n === "string" && n.startsWith("/") && !n.startsWith("//") ? n : "/");

// ------------------------------------------------------------------ passwords and sessions
export async function verifyPassword(pw, stored) {
  const [algo, iter, salt, hash] = (stored || "").split("$");
  if (algo !== "pbkdf2") return false;
  const key = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: unb64u(salt), iterations: Number(iter) }, key, 256);
  return safeEqual(b64u(bits), hash);
}

async function hmacKey(secret) {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
}

async function sign(payload, secret) {
  const body = b64u(enc.encode(JSON.stringify(payload)));
  const mac = b64u(await crypto.subtle.sign("HMAC", await hmacKey(secret), enc.encode(body)));
  return `${body}.${mac}`;
}

async function unsign(token, secret) {
  const [body, mac] = (token || "").split(".");
  if (!body || !mac) return null;
  const want = b64u(await crypto.subtle.sign("HMAC", await hmacKey(secret), enc.encode(body)));
  if (!safeEqual(mac, want)) return null;
  try {
    const data = JSON.parse(new TextDecoder().decode(unb64u(body)));
    return data.exp > Date.now() / 1000 ? data : null;
  } catch {
    return null;
  }
}

async function currentUser(req, env) {
  const s = await unsign(cookies(req)[COOKIE], env.SESSION_SECRET);
  if (!s) return null;
  const user = await env.DATA.get(`user:${s.u}`, "json");
  if (!user || user.v !== s.v) return null; // removed, or signed out everywhere
  return { user, session: s };
}

// ------------------------------------------------------------------ rate limiting (KV counters)
async function blocked(env, keys) {
  const counts = await Promise.all(keys.map((k) => env.DATA.get(`fail:${k}`)));
  return counts.some((c) => Number(c || 0) >= FAIL_LIMIT);
}

async function recordFail(env, keys) {
  await Promise.all(keys.map(async (k) => {
    const n = Number((await env.DATA.get(`fail:${k}`)) || 0) + 1;
    await env.DATA.put(`fail:${k}`, String(n), { expirationTtl: FAIL_TTL });
  }));
}

// ------------------------------------------------------------------ pages
const LOGIN = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>Sign in · __TITLE__</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=Inter:wght@400;500;600&display=swap">
<style nonce="__NONCE__">
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
</main></body></html>`;

async function loginPage(env, { status = 200, error = "", next = "/" } = {}) {
  const csrf = randomToken();
  const nonce = randomToken(16);
  const title = (await env.DATA.get("title")) || "Referrals";
  const html = LOGIN.replaceAll("__TITLE__", esc(title)).replace("__NONCE__", nonce).replace("__CSRF__", csrf)
    .replace("__NEXT__", esc(safeNext(next)))
    .replace("__ERROR__", error ? `<p class="err" role="alert">${esc(error)}</p>` : "");
  return new Response(html, { status, headers: headers("text/html; charset=utf-8", nonce, [["Set-Cookie", setCookie(CSRF_COOKIE, csrf, 3600)]]) });
}

async function readForm(req) {
  const len = Number(req.headers.get("Content-Length") || 0);
  if (len > 8192) return new URLSearchParams();
  return new URLSearchParams(await req.text());
}

async function doLogin(req, env) {
  const form = await readForm(req);
  const next = safeNext(form.get("next"));
  const expected = cookies(req)[CSRF_COOKIE];
  if (!expected || !safeEqual(form.get("csrf") || "", expected)) {
    return loginPage(env, { status: 400, error: "Your sign-in form expired. Please try again.", next });
  }
  const name = (form.get("username") || "").trim().toLowerCase().slice(0, 100);
  const ip = req.headers.get("CF-Connecting-IP") || "?";
  const keys = [`ip:${ip}`, `user:${name}`];
  if (await blocked(env, keys)) {
    return loginPage(env, { status: 429, error: "Too many failed attempts. Wait 15 minutes and try again.", next });
  }
  const user = name ? await env.DATA.get(`user:${name}`, "json") : null;
  const ok = await verifyPassword((form.get("password") || "").slice(0, 200), user ? user.hash : DUMMY_HASH);
  if (!user || !ok) {
    await recordFail(env, keys);
    return loginPage(env, { status: 401, error: "That username and password don't match.", next });
  }
  await Promise.all(keys.map((k) => env.DATA.delete(`fail:${k}`)));
  const token = await sign({ u: name, v: user.v, c: randomToken(16), exp: Math.floor(Date.now() / 1000) + SESSION_DAYS * 86400 }, env.SESSION_SECRET);
  return redirect(next, [["Set-Cookie", setCookie(COOKIE, token, SESSION_DAYS * 86400)], ["Set-Cookie", setCookie(CSRF_COOKIE, "", 0)]]);
}

// checked when the username doesn't exist, so response time doesn't reveal which names are real
const DUMMY_HASH = "pbkdf2$100000$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const { pathname: path } = url;
    if (!env.SESSION_SECRET) return new Response("Server not configured: SESSION_SECRET is missing.", { status: 500 });
    if (path === "/healthz") return new Response("ok");
    if (path === "/login" && req.method === "GET") return loginPage(env, { next: url.searchParams.get("next") || "/" });
    if (path === "/login" && req.method === "POST") return doLogin(req, env);
    if (path === "/logout" && req.method === "POST") {
      const cur = await currentUser(req, env);
      const form = await readForm(req);
      if (!cur || !safeEqual(form.get("csrf") || "", cur.session.c)) return redirect("/");
      return redirect("/login", [["Set-Cookie", setCookie(COOKIE, "", 0)]]);
    }
    if ((path === "/" || path === "/index.html") && req.method === "GET") {
      const cur = await currentUser(req, env);
      if (!cur) return redirect(`/login?next=${encodeURIComponent(path)}`);
      const page = await env.DATA.get("page");
      if (!page) return new Response("No data published yet. Run `recs cf publish`.", { status: 503, headers: headers("text/plain; charset=utf-8") });
      const nonce = randomToken(16);
      const html = page.replaceAll("__NONCE__", nonce).replaceAll("__USER__", esc(cur.user.name)).replaceAll("__CSRF__", cur.session.c);
      return new Response(html, { headers: headers("text/html; charset=utf-8", nonce) });
    }
    return new Response("Not found", { status: 404, headers: headers("text/plain; charset=utf-8") });
  },
};
