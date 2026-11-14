# Eastside Reviews

A private index of the plumbers, electricians, handymen and other service pros that neighbors recommend in group chats, with a sign-in-protected web page to browse it.

- **Import** WhatsApp "Export chat" files (`.zip` or `.txt`, iPhone or Android). Facebook groups can be added through a JSON-lines importer.
- **Extract** each recommendation: the pro's name and contact person, trade, phone/website/email, the recommender's first name, group, date, a one-line quote, whether it was positive, negative or unverified, and the question it was answering.
- **Merge** the same pro across spellings, phone formats and websites.
- **Browse** from the command line (`recs plumber`) or on the web page. Every pro is shown with the neighbors' own words, not just a name.

**This repository holds code only.** Chat exports, the database and the generated page contain your neighbors' messages and must never be committed. `.gitignore` blocks them.

## Quick start

```bash
pip install -e .
recs import "WhatsApp Chat - Maple Street.zip"
recs plumber
```

```
PLUMBING  ·  2 pros from 1 group

 1. Rapid Rooter (Joe)  —  3 neighbors recommend, last 2026-03-14
    (425) 555-0101
    + Raj, Maple Street, 2026-03-14: "Rapid-Rooter (Joe) did a good job for us"
    + Tom, Maple Street, 2026-03-01: "Joe at Rapid Rooter is great. Very fair price"
```

Pros are ranked by **how many distinct neighbors vouched for them**, then by **most recent recommendation**. `+` recommends, `-` warns, `~` mentioned without first-hand experience.

## The web page (with sign-in)

```bash
recs user add jane          # prints a generated password once; send it to Jane privately
recs serve                  # http://127.0.0.1:8000
```

The server shows a sign-in page. Signed-in users get the referrals page: trades down the side, search, and for each pro who vouched, their quotes with group and date, and what they were answering. See **[DEPLOY.md](DEPLOY.md)** to put it on your own domain.

To get a plain HTML file instead (no sign-in, for your own computer): `recs site -o referrals.html`.

## Commands

| | |
|---|---|
| `recs <trade>` | Ranked pros. Common words work: `plumber`, `roofer`, `hvac`, `furnace`, `cleaner`, `landscaper`, `sprinkler`, `lights`… Options: `-n 20`, `-q 5`, `--since 2026-01-01`, `--json` |
| `recs import FILE... [--group NAME] [--reimport]` | Add exports. Re-importing the same file does nothing; a newer export of the same group adds only new messages |
| `recs trades` / `recs stats` / `recs pro NAME` | What's indexed; everything about one pro (also searches by phone) |
| `recs merge "A" "B"` / `rename` / `hide` / `unhide` | Manual fixes, kept across re-imports |
| `recs site [-o FILE]` | Self-contained HTML page |
| `recs serve` | Web page behind sign-in |
| `recs user add\|list\|remove\|passwd\|signout NAME` | Who can sign in. `signout` ends all of that person's sessions |

By default data lives in `~/.recs/`. Set `RECS_HOME` (or `RECS_DB`) to change that.

## Privacy

- Neighbors are stored as **first name + an opaque key** (an HMAC of the sender name made with `secret.key`, a separate file). Phone numbers of people in the chat are never stored; senders shown only as a number appear as "a neighbor".
- No raw chat is stored: just a one-line quote per recommendation, with `@mentions` cut to first names. Numbers following phrases like "Zelle to", "call me" or "my husband at" are dropped.
- The web page sends `noindex` and strict security headers. Accounts are created only from the command line. Passwords are hashed with scrypt, sign-in attempts are rate-limited, and sessions can be revoked.
- Keep `secret.key` with `recs.db`; without it, re-importing counts everyone twice.

## Neighborhood words

Subdivision names, local organizations and business-name words specific to your area go in `$RECS_HOME/local_words.json` (kept out of this public repository):

```json
{"places": ["maplewood", "cedar ridge"], "business_words": ["brothers"], "not_pros": ["maplewood hoa"]}
```

Trades and their keywords are in `recs/trades.py`.

## How extraction works

`recs/extract.py` groups each sender's back-to-back messages, links replies to the request they answer, then reads names from patterns like "Sam at Acme Electric", "I used abc for AC repair", a reply that is just a business name, a name before a phone number, "Mike is great!" and "+1"/"Same". It skips questions, thank-yous, event posts and people advertising their own business. `recs/merge.py` joins mentions that share a phone, website domain or normalized name. A bare first name merges only within one trade.

WhatsApp leaves shared contact cards out of exports. Those recommendations still appear, as "Jane's contact card (name not in export)", so you know whom to ask.

## Facebook groups

The schema is platform-neutral. Convert posts and comments to JSON lines and import with `--platform facebook`:

```json
{"id": "c1", "reply_to": "p1", "ts": "2026-05-01T10:30:00", "author": "Fay Moss", "text": "Summit Roofing did ours, excellent work.", "group": "Oak FB"}
```

`reply_to` gives exact threading. Meta's terms prohibit automated scraping, so use your own *Download Your Information* export or pages you save from your browser.

## Tests

```bash
python -m unittest discover -s tests
```
