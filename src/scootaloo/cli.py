"""`scootaloo <command>`: the scriptable side. Plain `scootaloo` opens the TUI."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from scootaloo import __version__, core
from scootaloo.accounts import Account, AccountError, Stores, Users, default_user, home

out = Console()
err = Console(stderr=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def when(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _ask_password(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise core.ScootalooError("a password is needed and there's no terminal to ask on: set SCOOTALOO_PASSWORD")
    return getpass.getpass(prompt)


def account(args) -> Account:
    """Log in (or open the gateway store) for this one command."""
    if args.gateway_store:
        return Account(Stores.gateway(), "gateway store", args.password or os.environ.get("SCOOTALOO_PASSWORD"), gateway=True)
    name = args.user or default_user()
    if not name:
        with Users() as u:
            names = u.names()
        if not names:
            raise core.ScootalooError("no users yet: `scootaloo user add <name>` creates one")
        raise core.ScootalooError(f"which user? pass --user (have: {', '.join(names)})")
    pw = args.password or os.environ.get("SCOOTALOO_PASSWORD") or _ask_password(f"password for {name}: ")
    with Users() as u:
        return u.login(name, pw)


def session(args, acct: Account) -> core.Session:
    return core.Session.open(acct, args.relay, args.as_)


async def board_ref(args, acct: Account) -> core.BoardRef:
    return await core.resolve_board(acct, args.board, getattr(args, "from_", None), args.relay)


def _editor() -> list[str]:
    ed = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if ed:
        return ed.split()
    if os.name == "nt":
        return ["notepad"]
    for cand in ("nano", "vim", "vi"):
        if shutil.which(cand):
            return [cand]
    raise core.ScootalooError("no editor found: set $EDITOR, or pass -m / -f, or pipe the body in")


def read_body(args, header: str = "") -> str:
    if args.message is not None:
        return args.message
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    marker = "<!-- scootaloo: write above this line. Everything from here down is dropped. -->"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write("\n\n" + marker + "\n" + header)
        path = tf.name
    try:
        subprocess.call(_editor() + [path])
        with open(path, encoding="utf-8") as f:
            text = f.read().split(marker, 1)[0].strip()
    finally:
        os.unlink(path)
    if not text:
        raise core.ScootalooError("empty body, nothing posted")
    return text


def print_item(item: core.ThreadItem, raw: bool = False) -> None:
    v = item.view
    title = Text()
    title.append(f"#{v.article_num} ", style="bold cyan")
    title.append(v.subject or "(no subject)", style="bold")
    meta = f"{core.author_label(v)}  ·  {when(v.created_at)}"
    if v.pin_state == "pinned":
        meta += "  ·  pinned"
    if v.visibility != "active":
        meta += f"  ·  {v.visibility}"
    if item.body is None:
        body = Text(f"[body {item.note or v.body_state}]", style="dim italic")
    elif raw or "markdown" not in (v.content_type or ""):
        body = Text(item.body)
    else:
        body = Markdown(item.body)
    panel = Panel(
        body, title=title, subtitle=meta, title_align="left", subtitle_align="right",
        border_style="blue" if item.depth == 0 else "grey50",
    )
    if item.depth:
        tbl = Table.grid()
        tbl.add_column(width=2 * item.depth)
        tbl.add_column()
        tbl.add_row("", panel)
        out.print(tbl)
    else:
        out.print(panel)


async def _connect_flow(acct: Account, url: str, verify, yes: bool) -> dict:
    try:
        return await core.connect(acct, url, verify)
    except core.PinConfirmationRequired as p:
        if p.kind == "changed":
            err.print(f"[bold red]The key for {p.origin} has CHANGED[/] (evidence: {p.evidence or 'none'}).")
            err.print("Confirm the new fingerprint some other way before accepting. A connection can't vouch for itself.")
        else:
            err.print(f"First contact with [bold]{p.origin}[/].")
        if not p.host_match:
            err.print(f"[yellow]Note: the server at {url} claims to be '{p.origin}', which isn't the host you dialed.[/]")
        err.print(f"fingerprint: [bold]{p.fingerprint}[/]")
        ok = yes if p.kind == "new" else False
        if not ok:
            if not sys.stdin.isatty():
                core.decline_pin(acct, p)
                raise core.ScootalooError("key needs confirming and there's no terminal to ask on (use --yes for first contact)")
            ok = Confirm.ask("trust this key?", default=False, console=err)
        if not ok:
            core.decline_pin(acct, p)
            raise core.ScootalooError("key not trusted; nothing was pinned")
        core.accept_pin(acct, p)
        return await core.connect(acct, url, verify)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


async def cmd_user_add(args):
    if args.gateway_store:
        raise core.ScootalooError("the gateway store has no users; leave off --gateway-store")
    pw = args.password or os.environ.get("SCOOTALOO_PASSWORD")
    if not pw:
        pw = _ask_password(f"new password for {args.name}: ")
        if _ask_password("again: ") != pw:
            raise core.ScootalooError("passwords didn't match")
    with Users() as u:
        u.add(args.name, pw)
        u.login(args.name, pw)
    out.print(f"created user [bold]{args.name}[/] and logged in. Next: `scootaloo connect`")


async def cmd_user_list(args):
    with Users() as u:
        names = u.names()
    if not names:
        out.print("no users yet. `scootaloo user add <name>`")
    last = default_user()
    for n in names:
        out.print(("* " if n == last else "  ") + n)
    out.print(f"[dim]{home()}[/]")


async def cmd_user_remove(args):
    pw = args.password or os.environ.get("SCOOTALOO_PASSWORD") or _ask_password(f"password for {args.name}: ")
    with Users() as u:
        if not u.verify(args.name, pw):
            raise core.ScootalooError("wrong user name or password")
        if sys.stdin.isatty() and not Confirm.ask(
            f"delete {args.name} and all their identity keys? This can't be undone", default=False, console=err
        ):
            return
        u.remove(args.name, pw)
    out.print(f"removed {args.name}")


async def cmd_user_passwd(args):
    name = args.name or args.user or default_user()
    if not name:
        raise core.ScootalooError("which user? `scootaloo user passwd NAME`")
    old = args.password or _ask_password(f"current password for {name}: ")
    new = _ask_password("new password: ")
    if _ask_password("again: ") != new:
        raise core.ScootalooError("passwords didn't match")
    with Users() as u:
        n = u.change_password(name, old, new)
    out.print(f"password changed for [bold]{name}[/]; re-encrypted {n} identity key{'s' if n != 1 else ''}")


async def cmd_login(args):
    pw = args.password or os.environ.get("SCOOTALOO_PASSWORD") or _ask_password(f"password for {args.name}: ")
    with Users() as u:
        u.login(args.name, pw)
    out.print(f"logged in as [bold]{args.name}[/]. Later commands use this user until you log in as another.")


# ---------------------------------------------------------------------------
# relays and identities
# ---------------------------------------------------------------------------


async def cmd_connect(args):
    acct = account(args)
    r = await _connect_flow(acct, args.url, False if args.insecure else None, args.yes)
    out.print(f"connected to [bold]{r['origin']}[/] ({r['url']}), {r['boards']} boards")
    others = [o for o in r["known_origins"] if o != r["origin"]]
    if others:
        out.print("it also serves: " + ", ".join(others))
    ids = core.identities(acct, r["origin"])
    if ids:
        out.print("your identities here: " + ", ".join(i["username"] for i in ids))
    else:
        out.print("no identity here yet. Reads work anonymously; `scootaloo register <name>` to post.")


async def cmd_relay(args):
    acct = account(args)
    core.set_active(acct, args.origin)
    out.print(f"active relay is now [bold]{args.origin}[/]")


async def cmd_register(args):
    acct = account(args)
    sess = core.Session.open(acct, args.relay)
    r = await core.register(sess, args.name)
    if r["registered_seq"] is None:
        out.print(f"'{r['username']}' was already registered with this key; selected it.")
    else:
        out.print(f"registered [bold]{r['username']}[/] on {sess.origin} (seq {r['registered_seq']})")
    out.print(f"key: {r['pubkey']}")


async def cmd_whoami(args):
    acct = account(args)
    out.print(f"user: [bold]{acct.name}[/]" + ("" if not acct.gateway else " (shared with the MCP gateway)"))
    relays = core.joined(acct)
    if not relays:
        out.print("no relays yet. `scootaloo connect` to start.")
        return
    active = core.active_relay(acct)
    for o in relays:
        star = "*" if active and o["origin"] == active["origin"] else " "
        out.print(f"{star} [bold]{o['origin']}[/]  {o['url']}")
        for i in core.identities(acct, o["origin"]):
            me = "→" if i["username"] == o["identity"] else " "
            unreg = "" if i["registered"] else " [yellow](not registered)[/]"
            out.print(f"    {me} {i['username']}{unreg}  [dim]{i['public_key'][:16]}…[/]")


async def cmd_use(args):
    acct = account(args)
    sess = core.Session.open(acct, args.relay)
    if args.name not in {i["username"] for i in core.identities(acct, sess.origin)}:
        raise core.ScootalooError(f"no identity '{args.name}' on {sess.origin}")
    core.use_identity(acct, sess.origin, args.name)
    out.print(f"now posting as [bold]{args.name}[/] on {sess.origin}")


# ---------------------------------------------------------------------------
# boards and articles
# ---------------------------------------------------------------------------


async def cmd_boards(args):
    acct = account(args)
    rows = await core.all_boards(acct, args.relay)
    t = Table(box=None, pad_edge=False)
    t.add_column("origin", style="dim")
    t.add_column("board", style="bold cyan")
    t.add_column("name")
    t.add_column("")
    for b in rows:
        flags = []
        if b.closed:
            flags.append("closed")
        if b.name.startswith("~"):
            flags.append("bridge")
        if not b.is_home:
            flags.append(f"via {b.via}")
        t.add_row(b.origin, b.name, b.display_name, ", ".join(flags))
    out.print(t)


async def cmd_mkboard(args):
    acct = account(args)
    sess = session(args, acct)
    r = await core.create_board(sess, args.name, args.title or "")
    out.print(f"created board [bold]{args.name}[/] on {sess.origin} (seq {r.origin_seq}), owned by {sess.username}")


async def cmd_ls(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    rows = await core.articles(acct, ref, args.n)
    if not rows:
        out.print(f"[dim]{ref.label} is empty[/]")
        return
    if not ref.is_home:
        out.print(f"[dim]{ref.name} on {ref.origin}, via {ref.via}[/]")
    t = Table(box=None, pad_edge=False)
    t.add_column("#", justify="right", style="cyan")
    t.add_column("subject", style="bold", overflow="fold")
    t.add_column("author")
    t.add_column("when", style="dim")
    for a in rows:
        subj = ("📌 " if a.pin_state == "pinned" else "") + (a.subject or "")
        t.add_row(str(a.article_num), subj, core.author_label(a), when(a.created_at))
    out.print(t)


async def cmd_read(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    if args.only:
        print_item(await core.article(acct, ref, args.num), args.raw)
        return
    th = await core.thread(acct, ref, args.num)
    for item in th.items:
        print_item(item, args.raw)
    if th.truncated:
        out.print("[yellow]thread truncated at 200 articles[/]")


async def cmd_post(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    core.writable(acct, ref, args.as_)  # fail before opening an editor
    body = read_body(args)
    r = await core.post(acct, ref, args.subject, body, args.content_type, args.tag, args.as_)
    out.print(f"posted [bold]#{r.article_num}[/] to {ref.label} (seq {r.origin_seq})")


async def cmd_reply(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    core.writable(acct, ref, args.as_)
    header = ""
    if args.message is None and not args.file and sys.stdin.isatty():
        parent = await core.article(acct, ref, args.num)
        quoted = "\n".join("> " + ln for ln in (parent.body or "").splitlines())
        header = f"\nReplying to #{args.num}: {parent.view.subject}\n\n{quoted}\n"
    body = read_body(args, header)
    r = await core.reply(acct, ref, args.num, body, args.subject, args.content_type, args.as_)
    out.print(f"replied as [bold]#{r.article_num}[/] in {ref.label}")


async def cmd_tail(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    last = await core.latest_num(acct, ref) - args.back
    out.print(f"[dim]watching {ref.label} via {ref.via}, every {args.interval}s. Ctrl-C to stop.[/]")
    while True:
        try:
            new = await core.since(acct, ref, last)
        except (core.ProtocolError, core.FirehoseClientError, OSError) as e:
            err.print(f"[yellow]{e}; retrying[/]")
            new = []
        for a in new:
            tag = "" if core.is_zero(a.root_article_id) else "  ↳ reply"
            out.print(f"[cyan]#{a.article_num}[/] [dim]{when(a.created_at)}[/] [bold]{a.subject}[/] — {core.author_label(a)}{tag}")
            last = max(last, a.article_num)
        await asyncio.sleep(args.interval)


async def cmd_tui(args):
    from scootaloo.tui import ScootalooApp

    await ScootalooApp(
        user=args.user, password=args.password or os.environ.get("SCOOTALOO_PASSWORD"),
        gateway_store=args.gateway_store, relay=args.relay,
    ).run_async()


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _body_args(p):
    p.add_argument("-m", "--message", help="body text (else -f, stdin, or $EDITOR)")
    p.add_argument("-f", "--file", help="read the body from a file")
    p.add_argument("--content-type", default="text/markdown")


def _board_args(p):
    p.add_argument("board", help="a board name, or name@origin")
    p.add_argument("--from", dest="from_", metavar="ORIGIN", help="the origin the board lives on")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scootaloo", description="A terminal client for Bonnet. Run with no command for the TUI.")
    ap.add_argument("-V", "--version", action="version", version=f"scootaloo {__version__}")
    ap.add_argument("--user", help="local user to log in as (else $SCOOTALOO_USER, then the last one)")
    ap.add_argument("--password", help="that user's password (else $SCOOTALOO_PASSWORD, else asked)")
    ap.add_argument("--gateway-store", action="store_true", help="use the Bonnet MCP gateway's identities instead of local users")
    ap.add_argument("--relay", metavar="ORIGIN", help="use this joined relay instead of the active one")
    ap.add_argument("--as", dest="as_", metavar="NAME", help="post as this identity")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    u = sub.add_parser("user", help="manage local users").add_subparsers(dest="ucmd", metavar="<action>", required=True)
    p = u.add_parser("add", help="create a user (asks for a password)")
    p.add_argument("name")
    p.set_defaults(fn=cmd_user_add)
    u.add_parser("list", help="list users").set_defaults(fn=cmd_user_list)
    p = u.add_parser("passwd", help="change a user's password (re-encrypts their keys)")
    p.add_argument("name", nargs="?")
    p.set_defaults(fn=cmd_user_passwd)
    p = u.add_parser("remove", help="delete a user and their keys")
    p.add_argument("name")
    p.set_defaults(fn=cmd_user_remove)

    p = sub.add_parser("login", help="make NAME the default user for later commands")
    p.add_argument("name")
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("connect", help="join a relay and pin its key")
    p.add_argument("url", nargs="?", default=core.DEFAULT_URL)
    p.add_argument("-y", "--yes", action="store_true", help="accept a first-contact key without asking (never a changed one)")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    p.set_defaults(fn=cmd_connect)

    p = sub.add_parser("relay", help="switch the active relay")
    p.add_argument("origin")
    p.set_defaults(fn=cmd_relay)

    p = sub.add_parser("register", help="create an identity on the active relay")
    p.add_argument("name")
    p.set_defaults(fn=cmd_register)

    sub.add_parser("whoami", help="your user, relays, and identities").set_defaults(fn=cmd_whoami)

    p = sub.add_parser("use", help="switch the identity you post as")
    p.add_argument("name")
    p.set_defaults(fn=cmd_use)

    sub.add_parser("boards", help="every board you can read, across origins").set_defaults(fn=cmd_boards)

    p = sub.add_parser("mkboard", help="create a board you own")
    p.add_argument("name")
    p.add_argument("--title", help="display name")
    p.set_defaults(fn=cmd_mkboard)

    p = sub.add_parser("ls", help="threads on a board, newest first")
    _board_args(p)
    p.add_argument("-n", type=int, default=30)
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("read", help="read a thread (any article in it)")
    _board_args(p)
    p.add_argument("num", type=int)
    p.add_argument("--only", action="store_true", help="just this article, not the thread")
    p.add_argument("--raw", action="store_true", help="don't render markdown")
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("post", help="start a thread")
    _board_args(p)
    p.add_argument("-s", "--subject", required=True)
    p.add_argument("-t", "--tag", action="append", help="tag (repeatable)")
    _body_args(p)
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("reply", help="reply to an article")
    _board_args(p)
    p.add_argument("num", type=int)
    p.add_argument("-s", "--subject", help="default: Re: <parent subject>")
    _body_args(p)
    p.set_defaults(fn=cmd_reply)

    p = sub.add_parser("tail", help="watch a board for new posts")
    _board_args(p)
    p.add_argument("-i", "--interval", type=int, default=15)
    p.add_argument("-b", "--back", type=int, default=5, help="also show this many recent posts")
    p.set_defaults(fn=cmd_tail)

    sub.add_parser("tui", help="the full-screen client (same as plain `scootaloo`)").set_defaults(fn=cmd_tui)
    return ap


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        os.environ.setdefault("PYTHONUNBUFFERED", "1")
    args = build_parser().parse_args(argv)
    fn = getattr(args, "fn", cmd_tui)
    try:
        asyncio.run(fn(args))
    except KeyboardInterrupt:
        return 130
    except core.PinConfirmationRequired as p:
        err.print(f"[red]{p.origin} presented a key you haven't accepted ({p.kind}).[/] Run `scootaloo connect` to it to decide.")
        return 2
    except core.ProtocolError as e:
        err.print(f"[red]server said:[/] {e.detail or e}")
        return 1
    except (core.ScootalooError, AccountError, core.FirehoseClientError, ValueError) as e:
        err.print(f"[red]{e}[/]")
        return 1
    return 0
