// Run: npm test (from worker/). Exercises the Worker with an in-memory KV.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { verifyPassword } from "../src/index.js";

// produced by the Python CLI (recs.cloudflare.hash_password), so this also checks both sides agree
const PY_HASH = "pbkdf2$100000$056lf2ZSMnqx4QIZ_QK2Pg$0vTv8J48cN5qPjXOQx9LVi4Pc8pwNb1wbgoOewqznVU";

function makeEnv() {
  const store = new Map();
  const DATA = {
    async get(k, type) { const v = store.get(k); return v === undefined ? null : type === "json" ? JSON.parse(v) : v; },
    async put(k, v) { store.set(k, v); },
    async delete(k) { store.delete(k); },
  };
  store.set("page", '<!doctype html><style nonce="__NONCE__"></style><b>__USER__</b><input name="csrf" value="__CSRF__">Rapid Rooter');
  store.set("title", "Maple Referrals");
  store.set("user:jane", JSON.stringify({ name: "Jane", hash: PY_HASH, v: 1 }));
  return { DATA, SESSION_SECRET: "test-secret-0123456789", store };
}

class Browser {
  constructor(env) { this.env = env; this.jar = {}; }
  async go(method, path, form, ip = "10.0.0.1") {
    const headers = { "CF-Connecting-IP": ip, Cookie: Object.entries(this.jar).map(([k, v]) => k + "=" + v).join("; ") };
    let body;
    if (form) { body = new URLSearchParams(form).toString(); headers["Content-Type"] = "application/x-www-form-urlencoded"; headers["Content-Length"] = String(body.length); }
    const res = await worker.fetch(new Request("https://reviews.example" + path, { method, headers, body }), this.env);
    for (const c of res.headers.getSetCookie()) {
      const [kv] = c.split(";"); const i = kv.indexOf("=");
      if (/Max-Age=0/.test(c)) delete this.jar[kv.slice(0, i)]; else this.jar[kv.slice(0, i)] = kv.slice(i + 1);
    }
    return { res, text: await res.text() };
  }
  async login(user = "jane", password = "correct horse battery", ip) {
    const { text } = await this.go("GET", "/login", null, ip);
    const csrf = text.match(/name="csrf" value="([^"]+)"/)[1];
    return this.go("POST", "/login", { csrf, username: user, password, next: "/" }, ip);
  }
}

test("python hash verifies in the worker", async () => {
  assert.equal(await verifyPassword("correct horse battery", PY_HASH), true);
  assert.equal(await verifyPassword("wrong", PY_HASH), false);
});

test("page needs sign-in, then shows data with a fresh nonce", async () => {
  const b = new Browser(makeEnv());
  const r = await b.go("GET", "/");
  assert.equal(r.res.status, 303);
  assert.equal(r.res.headers.get("Location"), "/login?next=%2F");
  assert.equal((await b.login("JANE")).res.status, 303); // usernames are case-insensitive
  const page = await b.go("GET", "/");
  assert.equal(page.res.status, 200);
  assert.match(page.text, /Rapid Rooter/);
  assert.match(page.text, /<b>Jane<\/b>/);
  const nonce = page.res.headers.get("Content-Security-Policy").match(/'nonce-([^']+)'/)[1];
  assert.ok(page.text.includes('nonce="' + nonce + '"'));
  assert.ok(!page.text.includes("__NONCE__"));
  assert.match(page.res.headers.get("X-Robots-Tag"), /noindex/);
});

test("wrong password, lockout, csrf, tampering, revocation, logout", async () => {
  const env = makeEnv();
  const b = new Browser(env);
  for (let i = 0; i < 5; i++) assert.equal((await b.login("jane", "nope nope nope")).res.status, 401);
  assert.equal((await b.login()).res.status, 429);
  env.store.clear(); Object.assign(env, makeEnv());

  const c = new Browser(env);
  await c.go("GET", "/login");
  assert.equal((await c.go("POST", "/login", { csrf: "forged", username: "jane", password: "correct horse battery" })).res.status, 400);

  await c.login();
  const good = c.jar.recs_session;
  c.jar.recs_session = good.slice(0, -3) + "abc";
  assert.equal((await c.go("GET", "/")).res.status, 303);
  c.jar.recs_session = good;
  assert.equal((await c.go("GET", "/")).res.status, 200);

  const u = JSON.parse(env.store.get("user:jane")); u.v += 1; env.store.set("user:jane", JSON.stringify(u)); // recs cf user signout
  assert.equal((await c.go("GET", "/")).res.status, 303);

  await c.login();
  const page = await c.go("GET", "/");
  const csrf = page.text.match(/name="csrf" value="([^"]+)"/)[1];
  assert.equal((await c.go("POST", "/logout", { csrf })).res.headers.get("Location"), "/login");
  assert.equal((await c.go("GET", "/")).res.status, 303);
});

test("open redirect blocked", async () => {
  const b = new Browser(makeEnv());
  const { text } = await b.go("GET", "/login?next=//evil.example");
  assert.match(text, /name="next" value="\/"/);
});
