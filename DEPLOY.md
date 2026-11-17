# Putting the referrals page on your site

## Option A: Cloudflare Workers (current deployment)

The Worker in `worker/` serves the page behind the sign-in. Your data lives in a private KV namespace in your Cloudflare account, never in this repository.

**One-time setup** (already done for this deployment):

```bash
export CLOUDFLARE_API_TOKEN=...      # token from the "Edit Cloudflare Workers" template
export CLOUDFLARE_ACCOUNT_ID=...
cd worker && npm install
npx wrangler kv namespace create eastside-reviews-data   # put the id in wrangler.toml
npx wrangler deploy
python3 -c "import secrets; print(secrets.token_urlsafe(48))" | npx wrangler secret put SESSION_SECRET
```

**Every time you import new chats**, from the repository root:

```bash
recs import "WhatsApp Chat - ....zip"
recs cf publish
```

**Accounts** (no public sign-up; send passwords privately):

```bash
recs cf user add jane        # prints a generated password once
recs cf user list
recs cf user passwd jane     # new password; also ends Jane's existing sessions
recs cf user signout jane    # ends Jane's sessions on every device
recs cf user remove jane     # revokes access immediately
```

To use your own domain, add a Custom Domain to the Worker in the Cloudflare dashboard (Workers & Pages → eastside-reviews → Settings → Domains & Routes), e.g. `reviews.example.com`.

Tests for the Worker: `cd worker && npm test`.

## Option B: Docker on any host

The page has to run as a small server, because the sign-in has to be checked somewhere the data is kept. A static host like GitHub Pages can't do that: anything it serves is readable by anyone who finds the URL.

You need:
1. **A host that runs a Docker container with a persistent disk.** Any of these works: Fly.io, Render, Railway, or a small VPS (DigitalOcean, Hetzner, Lightsail).
2. **HTTPS in front of it.** These hosts provide it; on a VPS use Caddy or nginx. Sign-in cookies are marked `Secure` and won't work over plain http.
3. **Your data on the server's disk**, at `/data`.

### 1. Build and run

```bash
docker build -t eastside-reviews .
docker run -d --name reviews -p 8000:8000 -v reviews-data:/data eastside-reviews
```

`RECS_TRUST_PROXY=1` is set in the image so rate limiting sees real client IPs behind the host's proxy. Don't expose port 8000 directly to the internet without a proxy in front.

### 2. Put your data on the server

The database is built on your computer, where your chat exports are. Copy these files from `~/.recs/` into the container's `/data` volume:

| File | Why |
|---|---|
| `recs.db` | The recommendations |
| `secret.key` | Keeps neighbor keys stable when you import again later |
| `local_words.json` | Your neighborhood vocabulary (optional) |

```bash
docker cp ~/.recs/recs.db reviews:/data/
docker cp ~/.recs/secret.key reviews:/data/
```

On Fly.io use `fly ssh sftp shell` and `put`. On Render, use the shell on a service with a disk. After a new import, copy `recs.db` up again. The page rebuilds from it on every request.

### 3. Create accounts

There's no public sign-up. Create each account yourself and send the password privately:

```bash
docker exec -it reviews recs user add jane
docker exec -it reviews recs user list
docker exec -it reviews recs user remove jane        # revokes access immediately
docker exec -it reviews recs user signout jane       # ends Jane's sessions on every device
```

### 4. Link it from your site

Point a subdomain (e.g. `reviews.example.com`) at the host and add a link from your main site. Search engines are told not to index the page (`noindex`).

### Settings

| Variable | Default | |
|---|---|---|
| `RECS_HOME` | `/data` in Docker | Where `recs.db`, `secret.key` and `session.key` live |
| `RECS_TITLE` | from the group name | Page title |
| `RECS_SESSION_SECRET` | generated into `session.key` | Set it to share sessions across several instances |
| `RECS_TRUST_PROXY` | `1` in Docker | Read client IP from `X-Forwarded-For` |
| `RECS_INSECURE_COOKIES` | unset | `1` only for testing over plain http |

Sessions last 30 days. Five failed sign-ins in 15 minutes, from one IP or for one username, block further attempts for that window.
