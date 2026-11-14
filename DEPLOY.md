# Putting the referrals page on your site

The page has to run as a small server, because the sign-in has to be checked somewhere the data is kept. A static host like GitHub Pages can't do that: anything it serves is readable by anyone who finds the URL.

You need:
1. **A host that runs a Docker container with a persistent disk.** Any of these works: Fly.io, Render, Railway, or a small VPS (DigitalOcean, Hetzner, Lightsail).
2. **HTTPS in front of it.** These hosts provide it; on a VPS use Caddy or nginx. Sign-in cookies are marked `Secure` and won't work over plain http.
3. **Your data on the server's disk**, at `/data`.

## 1. Build and run

```bash
docker build -t eastside-reviews .
docker run -d --name reviews -p 8000:8000 -v reviews-data:/data eastside-reviews
```

`RECS_TRUST_PROXY=1` is set in the image so rate limiting sees real client IPs behind the host's proxy. Don't expose port 8000 directly to the internet without a proxy in front.

## 2. Put your data on the server

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

## 3. Create accounts

There's no public sign-up. Create each account yourself and send the password privately:

```bash
docker exec -it reviews recs user add jane
docker exec -it reviews recs user list
docker exec -it reviews recs user remove jane        # revokes access immediately
docker exec -it reviews recs user signout jane       # ends Jane's sessions on every device
```

## 4. Link it from your site

Point a subdomain (e.g. `reviews.example.com`) at the host and add a link from your main site. Search engines are told not to index the page (`noindex`).

## Settings

| Variable | Default | |
|---|---|---|
| `RECS_HOME` | `/data` in Docker | Where `recs.db`, `secret.key` and `session.key` live |
| `RECS_TITLE` | from the group name | Page title |
| `RECS_SESSION_SECRET` | generated into `session.key` | Set it to share sessions across several instances |
| `RECS_TRUST_PROXY` | `1` in Docker | Read client IP from `X-Forwarded-For` |
| `RECS_INSECURE_COOKIES` | unset | `1` only for testing over plain http |

Sessions last 30 days. Five failed sign-ins in 15 minutes, from one IP or for one username, block further attempts for that window.
