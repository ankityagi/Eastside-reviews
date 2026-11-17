"""recs - neighbourhood service-pro recommendations from your group chats.

  recs plumber                     ranked plumbers with quotes
  recs import FILE... [--group G]  add WhatsApp exports (.zip or .txt) or JSON-lines files
  recs trades                      what is in the index
  recs pro NAME                    everything about one pro
  recs merge "A" "B"               tell recs two names are the same pro
  recs rename "Old" "New" | recs hide NAME | recs unhide NAME
  recs stats
  recs site [-o referrals.html]    browsable page with every quote behind each pro
  recs serve [--port 8000]         the page behind a login (see DEPLOY.md for production)
  recs user add|list|remove|passwd|signout NAME   manage who can sign in
  recs cf publish | recs cf user add|list|remove|passwd|signout NAME   Cloudflare Worker (DEPLOY.md)
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

from . import sources
from .db import DB, default_db
from .extract import extract, fmt_phone
from .trades import TRADES, resolve_query
from .whatsapp import first_name

COMMANDS = {"cf", "user", "serve", "site", "import", "trades", "pro", "merge", "rename", "hide", "unhide", "stats", "rebuild", "help"}
MARK = {"positive": "+", "negative": "-", "neutral": "~"}


def _who(m) -> str:
    return m["first_name"] or "a neighbor"


def _contact_line(contacts) -> str:
    out = []
    for c in contacts:
        if c["kind"] == "phone":
            out.append(fmt_phone(c["value"]))
        else:
            out.append(c["value"].replace("https://", "").replace("http://", "").rstrip("/")[:70])
    return "  ·  ".join(out)


def cmd_import(db: DB, args) -> int:
    total = 0
    for f in args.files:
        path = Path(f).expanduser()
        if not path.exists():
            print(f"skip: {f} not found", file=sys.stderr)
            continue
        platform = args.platform or sources.guess_platform(path)
        for loaded in sources.IMPORTERS[platform](path, args.group):
            if loaded.group in ("_chat", "chat") and not args.group:
                print(f"skip: {f}: can't tell the group name from '_chat.txt' - rerun with --group NAME", file=sys.stderr)
                continue
            if db.source_exists(loaded.sha256):
                if not args.reimport:
                    print(f"already imported: {loaded.file_label} ({loaded.group})")
                    continue
                db.drop_source(loaded.sha256)
            mentions = extract(loaded.messages)
            sid = db.add_source(loaded.platform, loaded.group, loaded.file_label, loaded.sha256, len(loaded.messages))
            added = db.add_mentions(sid, loaded.platform, loaded.group, mentions, first_name)
            db.con.commit()
            total += added
            print(f"{loaded.group}: {len(loaded.messages)} messages -> {len(mentions)} recommendations ({added} new)")
    n = db.rebuild()
    print(f"index: {n} pros, {db.stats()['mentions']} recommendations  [{db.path}]")
    return 0


def show_trade(db: DB, query: str, args) -> int:
    trades = resolve_query(query)
    if not trades:
        hits = db.find_pros(query)
        if hits:
            for h in hits:
                show_pro(db, h["id"], args)
            return 0
        print(f"No trade or pro matches '{query}'. Try one of: {', '.join(sorted(TRADES))}")
        return 1
    pros = db.ranked(trades, include_hidden=args.all, since=args.since)
    if args.json:
        print(json.dumps([{k: (sorted(v) if isinstance(v, set) else v) for k, v in p.items()} for p in pros[: args.limit]],
                         indent=2, default=str))
        return 0
    title = " / ".join(trades)
    if not pros:
        print(f"No recommendations for {title} yet.")
        return 0
    groups = {m["group_name"] for p in pros for m in p["mentions"]}
    print(f"{title.upper()}  ·  {len(pros)} pro{'s' * (len(pros) != 1)} from {len(groups)} group{'s' * (len(groups) != 1)}\n")
    width = 100
    for i, p in enumerate(pros[: args.limit], 1):
        n = p["score"]
        bits = [(f"1 neighbor recommends" if n == 1 else f"{n} neighbors recommend") if n else "no clear recommendation"]
        if p["neg"]:
            bits.append(f"{len(p['neg'])} warned")
        if p["neu"]:
            bits.append(f"{len(p['neu'])} unverified")
        bits.append(f"last {p['last'][:10]}")
        print(f"{i:>2}. {p['name']}  —  {', '.join(bits)}")
        cl = _contact_line(p["contacts"])
        if cl:
            print(f"    {cl}")
        elif p["kind"] == "unnamed":
            print("    (contact card not included in the export - ask the neighbor below)")
        for m in p["mentions"][: args.quotes]:
            head = f"    {MARK[m['sentiment']]} {_who(m)}, {m['group_name']}, {m['posted_at'][:10]}: "
            body = textwrap.fill(f'"{m["quote"]}"', width=width, initial_indent=head,
                                 subsequent_indent=" " * 8)
            print(body)
        if len(p["mentions"]) > args.quotes:
            print(f"      … {len(p['mentions']) - args.quotes} more (recs pro \"{p['name']}\")")
        other = sorted(set(p["trades"]) - set(trades))
        if other:
            print(f"    also recommended for: {', '.join(other)}")
        print()
    if len(pros) > args.limit:
        print(f"… {len(pros) - args.limit} more; use --limit {len(pros)}")
    return 0


def show_pro(db: DB, pro_id: int, args) -> None:
    p = db.con.execute("SELECT * FROM pros WHERE id = ?", (pro_id,)).fetchone()
    contacts = db.con.execute("SELECT kind, value FROM pro_contacts WHERE pro_id = ? ORDER BY kind DESC", (pro_id,)).fetchall()
    trades = [r[0] for r in db.con.execute("SELECT trade FROM pro_trades WHERE pro_id = ?", (pro_id,))]
    aliases = [r[0] for r in db.con.execute("SELECT alias FROM pro_aliases WHERE pro_id = ?", (pro_id,))]
    print(f"{p['display_name']}{'  [hidden]' if p['hidden'] else ''}")
    print(f"  trades:   {', '.join(trades) or '-'}")
    if aliases:
        print(f"  also as:  {', '.join(aliases)}")
    if contacts:
        print(f"  contact:  {_contact_line(contacts)}")
    for m in db.con.execute("""SELECT m.*, n.first_name FROM mentions m JOIN neighbors n ON n.id = m.neighbor_id
                               WHERE pro_id = ? ORDER BY posted_at DESC""", (pro_id,)):
        print(textwrap.fill(f'"{m["quote"]}"', width=100,
                            initial_indent=f"  {MARK[m['sentiment']]} {_who(m)}, {m['group_name']}, {m['posted_at'][:10]} [{m['trade']}]: ",
                            subsequent_indent=" " * 6))
    print()


def cmd_user(db: DB, args) -> int:
    from .auth import Users, generate_password
    users = Users(db.con)
    if args.action == "list":
        rows = users.list()
        if not rows:
            print("No accounts yet. Add one: recs user add NAME")
        for r in rows:
            print(f"  {r['username']:<24} created {r['created_at'][:10]}  last sign-in {(r['last_login'] or 'never')[:10]}")
        return 0
    if not args.name:
        print("Give a username, e.g. recs user add jane", file=sys.stderr)
        return 2
    if args.action in ("add", "passwd"):
        pw = sys.stdin.readline().rstrip("\n") if args.password_stdin else generate_password()
        try:
            if args.action == "add":
                users.add(args.name, pw)
            elif not users.set_password(args.name, pw):
                print(f"No account named {args.name}.", file=sys.stderr)
                return 1
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        except Exception as e:  # sqlite3.IntegrityError
            print(f"Could not save {args.name}: {e}", file=sys.stderr)
            return 1
        if not args.password_stdin:
            print(f"{args.name}'s password: {pw}\n(shown once; send it to them privately)")
        else:
            print(f"Saved {args.name}.")
        return 0
    if args.action == "remove":
        ok = users.remove(args.name)
    else:
        ok = users.sign_out_everywhere(args.name)
    print(("Done." if ok else f"No account named {args.name}."))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help=f"database file (default {default_db()}, or $RECS_DB)")

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] not in COMMANDS:  # `recs plumber ...`
        ap = argparse.ArgumentParser(prog="recs", parents=[common], description="Ranked pros for a trade")
        ap.add_argument("trade", nargs="+")
        ap.add_argument("-n", "--limit", type=int, default=10)
        ap.add_argument("-q", "--quotes", type=int, default=3, help="quotes per pro (default 3)")
        ap.add_argument("--since", help="only recommendations on/after YYYY-MM-DD")
        ap.add_argument("--all", action="store_true", help="include hidden pros")
        ap.add_argument("--json", action="store_true")
        args = ap.parse_args(argv)
        return show_trade(DB(args.db), " ".join(args.trade), args)

    cmd, rest = argv[0], argv[1:]
    ap = argparse.ArgumentParser(prog=f"recs {cmd}", parents=[common])
    if cmd == "import":
        ap.add_argument("files", nargs="+")
        ap.add_argument("--group", help="group name (default: from the WhatsApp file name)")
        ap.add_argument("--platform", choices=sorted(sources.IMPORTERS), help="default: whatsapp for .txt/.zip, jsonl for .jsonl")
        ap.add_argument("--reimport", action="store_true", help="re-extract files that were already imported")
    elif cmd == "pro":
        ap.add_argument("name", nargs="+")
    elif cmd in ("merge", "rename"):
        ap.add_argument("a")
        ap.add_argument("b")
    elif cmd in ("hide", "unhide"):
        ap.add_argument("name", nargs="+")
    elif cmd == "user":
        ap.add_argument("action", choices=["add", "list", "remove", "passwd", "signout"])
        ap.add_argument("name", nargs="?")
        ap.add_argument("--password-stdin", action="store_true", help="read the password from stdin instead of generating one")
    elif cmd == "cf":
        ap.add_argument("what", choices=["publish", "user"])
        ap.add_argument("action", nargs="?", choices=["add", "list", "remove", "passwd", "signout"])
        ap.add_argument("name", nargs="?")
        ap.add_argument("--title")
        ap.add_argument("--password-stdin", action="store_true")
    elif cmd == "serve":
        ap.add_argument("--host", default="127.0.0.1")
        ap.add_argument("--port", type=int, default=8000)
    elif cmd == "site":
        ap.add_argument("-o", "--out", default="referrals.html")
        ap.add_argument("--title", help="page title (default: '<first word of group> Referrals')")
        ap.add_argument("--fragment", action="store_true", help="body-only HTML (for hosts that add their own <head>)")
    args = ap.parse_args(rest)
    db = DB(args.db)

    if cmd == "import":
        return cmd_import(db, args)
    if cmd == "trades":
        rows = db.trade_counts()
        if not rows:
            print("Index is empty. Run: recs import <WhatsApp export .zip/.txt>")
        for r in rows:
            print(f"  {r['trade']:<22} {r['pros']:>3} pros  {r['neighbors']:>3} neighbors  last {r['last'][:10]}")
        return 0
    if cmd == "pro":
        hits = db.find_pros(" ".join(args.name))
        if not hits:
            print("No pro matches that name or number.")
            return 1
        for h in hits:
            show_pro(db, h["id"], args)
        return 0
    if cmd == "merge":
        for n in (args.a, args.b):
            if not db.find_pros(n):
                print(f"No pro matches '{n}'.")
                return 1
        db.curate("merge", args.a, args.b)
        print(f"Merged '{args.a}' and '{args.b}'.")
        return 0
    if cmd == "rename":
        db.curate("rename", args.a, args.b)
        print(f"'{args.a}' will show as '{args.b}'.")
        return 0
    if cmd in ("hide", "unhide"):
        db.curate(cmd, " ".join(args.name))
        print(f"{cmd}: {' '.join(args.name)}")
        return 0
    if cmd == "user":
        return cmd_user(db, args)
    if cmd == "cf":
        from . import cloudflare
        if args.what == "publish":
            cloudflare.publish(db, args.title)
            return 0
        if not args.action:
            print("Usage: recs cf user add|list|remove|passwd|signout NAME", file=sys.stderr)
            return 2
        return cloudflare.user(args.action, args.name, args.password_stdin)
    if cmd == "serve":
        from .web import serve
        db.con.close()
        serve(args.host, args.port, args.db)
        return 0
    if cmd == "site":
        from .site import render
        Path(args.out).write_text(render(db, args.title, standalone=not args.fragment), encoding="utf-8")
        print(f"wrote {args.out}  (open it in a browser; it works offline)")
        return 0
    if cmd == "rebuild":
        print(f"{db.rebuild()} pros")
        return 0
    if cmd == "stats":
        s = db.stats()
        for src in s["sources"]:
            print(f"  {src['platform']:<9} {src['group_name']:<32} {src['message_count']:>5} msgs  ({src['file_name']})")
        print(f"  {s['mentions']} recommendations of {s['pros']} pros by {s['neighbors']} neighbors  [{db.path}]")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
