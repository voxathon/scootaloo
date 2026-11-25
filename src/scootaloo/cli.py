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

out = Console()
err = Console(stderr=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def when(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def session(args) -> core.Session:
    sess = core.Session.current(args.origin, args.as_, args.password)
    if sess.needs_password() and not sess.password and sys.stdin.isatty():
        sess.password = getpass.getpass(f"password for {sess.username}: ")
    return sess


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
        body = Text(f"[body {v.body_state}]", style="dim italic")
    elif raw or "markdown" not in (v.content_type or ""):
        body = Text(item.body)
    else:
        body = Markdown(item.body)
    pad = "  " * item.depth
    panel = Panel(body, title=title, subtitle=meta, title_align="left", subtitle_align="right", border_style="blue" if item.depth == 0 else "grey50")
    if item.depth:
        tbl = Table.grid()
        tbl.add_column(width=len(pad))
        tbl.add_column()
        tbl.add_row(pad, panel)
        out.print(tbl)
    else:
        out.print(panel)


async def _connect_flow(url: str, verify, yes: bool) -> dict:
    try:
        return await core.connect(url, verify)
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
                core.decline_pin(p)
                raise core.ScootalooError("key needs confirming and there's no terminal to ask on (use --yes for first contact)")
            ok = Confirm.ask("trust this key?", default=False, console=err)
        if not ok:
            core.decline_pin(p)
            raise core.ScootalooError("key not trusted; nothing was pinned")
        core.accept_pin(p)
        return await core.connect(url, verify)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


async def cmd_connect(args):
    r = await _connect_flow(args.url, False if args.insecure else None, args.yes)
    out.print(f"connected to [bold]{r['origin']}[/] ({r['url']}), {r['boards']} boards")
    ids = core.local_identities(r["origin"])
    if ids:
        out.print("local identities here: " + ", ".join(i["username"] for i in ids))
    else:
        out.print("no identity here yet. Reads work anonymously; `scootaloo register <name>` to post.")


async def cmd_register(args):
    sess = core.Session.current(args.origin)
    pw = None
    if args.password_prompt:
        pw = getpass.getpass("new password (blank for none): ") or None
    r = await core.register(sess, args.name, pw)
    if r["registered_seq"] is None:
        out.print(f"'{r['username']}' was already registered with this key; selected it.")
    else:
        out.print(f"registered [bold]{r['username']}[/] on {sess.origin} (seq {r['registered_seq']})")
    out.print(f"key: {r['pubkey']}")


async def cmd_whoami(args):
    origins = core.joined_origins()
    if not origins:
        out.print("not connected anywhere. `scootaloo connect` to start.")
        return
    active = core.active_origin()
    for o in origins:
        star = "*" if active and o["origin"] == active["origin"] else " "
        out.print(f"{star} [bold]{o['origin']}[/]  {o['url']}")
        for i in core.local_identities(o["origin"]):
            me = "→" if i["username"] == o["identity"] else " "
            lock = " (password)" if i["wrapped"] else ""
            unreg = "" if i["registered"] else " [yellow](not registered)[/]"
            out.print(f"    {me} {i['username']}{lock}{unreg}  [dim]{i['public_key'][:16]}…[/]")


async def cmd_use(args):
    sess = core.Session.current(args.origin)
    if args.name not in {i["username"] for i in core.local_identities(sess.origin)}:
        raise core.ScootalooError(f"no local identity '{args.name}' on {sess.origin}")
    core.use_identity(sess.origin, args.name)
    out.print(f"now acting as [bold]{args.name}[/] on {sess.origin}")


async def cmd_boards(args):
    sess = session(args)
    rows = await core.boards(sess, args.all)
    t = Table(box=None, pad_edge=False)
    if args.all:
        t.add_column("origin", style="dim")
    t.add_column("board", style="bold cyan")
    t.add_column("name")
    t.add_column("")
    for b in rows:
        flag = "closed" if b.closed else ("bridge" if b.name.startswith("~") else "")
        cells = [b.name, b.display_name or "", flag]
        t.add_row(*([b.origin] + cells if args.all else cells))
    out.print(t)


async def cmd_mkboard(args):
    sess = session(args)
    r = await core.create_board(sess, args.name, args.title or "")
    out.print(f"created board [bold]{args.name}[/] on {sess.origin} (seq {r.origin_seq}), owned by {sess.username}")


async def cmd_ls(args):
    sess = session(args)
    rows = await core.articles(sess, args.board, args.n)
    if not rows:
        out.print(f"[dim]{args.board} is empty[/]")
        return
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
    sess = session(args)
    if args.only:
        print_item(await core.article(sess, args.board, args.num), args.raw)
        return
    th = await core.thread(sess, args.board, args.num)
    for item in th.items:
        print_item(item, args.raw)
    if th.truncated:
        out.print("[yellow]thread truncated at 200 articles[/]")


async def cmd_post(args):
    sess = session(args)
    body = read_body(args)
    r = await core.post(sess, args.board, args.subject, body, args.content_type, args.tag)
    out.print(f"posted [bold]#{r.article_num}[/] to {args.board} (seq {r.origin_seq})")


async def cmd_reply(args):
    sess = session(args)
    header = ""
    if args.message is None and not args.file and sys.stdin.isatty():
        parent = await core.article(sess, args.board, args.num)
        quoted = "\n".join("> " + ln for ln in (parent.body or "").splitlines())
        header = f"\nReplying to #{args.num}: {parent.view.subject}\n\n{quoted}\n"
    body = read_body(args, header)
    r = await core.reply(sess, args.board, args.num, body, args.subject, args.content_type)
    out.print(f"replied as [bold]#{r.article_num}[/] in {args.board}")


async def cmd_tail(args):
    sess = session(args)
    last = await core.latest_num(sess, args.board) - args.back
    out.print(f"[dim]watching {args.board} on {sess.origin}, every {args.interval}s. Ctrl-C to stop.[/]")
    while True:
        try:
            new = await core.since(sess, args.board, last)
        except (core.ProtocolError, core.FirehoseClientError, OSError) as e:
            err.print(f"[yellow]{e}; retrying[/]")
            new = []
        for a in new:
            root = "" if core.is_zero(a.root_article_id) else "  ↳ reply"
            out.print(f"[cyan]#{a.article_num}[/] [dim]{when(a.created_at)}[/] [bold]{a.subject}[/] — {core.author_label(a)}{root}")
            last = max(last, a.article_num)
        await asyncio.sleep(args.interval)


async def cmd_tui(args):
    from scootaloo.tui import ScootalooApp

    await ScootalooApp(args.origin, args.as_, args.password).run_async()


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _body_args(p):
    p.add_argument("-m", "--message", help="body text (else -f, stdin, or $EDITOR)")
    p.add_argument("-f", "--file", help="read the body from a file")
    p.add_argument("--content-type", default="text/markdown")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scootaloo", description="A terminal client for Bonnet. Run with no command for the TUI.")
    ap.add_argument("-V", "--version", action="version", version=f"scootaloo {__version__}")
    ap.add_argument("--origin", help="act on this joined origin instead of the active one")
    ap.add_argument("--as", dest="as_", metavar="NAME", help="act as this local identity")
    ap.add_argument("--password", help="password for a protected identity (or $SCOOTALOO_PASSWORD)")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("connect", help="join an origin and pin its key")
    p.add_argument("url", nargs="?", default=core.DEFAULT_URL)
    p.add_argument("-y", "--yes", action="store_true", help="accept a first-contact key without asking (never a changed one)")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    p.set_defaults(fn=cmd_connect)

    p = sub.add_parser("register", help="create an identity on the active origin")
    p.add_argument("name")
    p.add_argument("-p", "--password-prompt", action="store_true", help="protect the local key with a password")
    p.set_defaults(fn=cmd_register)

    sub.add_parser("whoami", help="joined origins and local identities").set_defaults(fn=cmd_whoami)

    p = sub.add_parser("use", help="switch the default identity")
    p.add_argument("name")
    p.set_defaults(fn=cmd_use)

    p = sub.add_parser("boards", help="list boards")
    p.add_argument("-a", "--all", action="store_true", help="every origin this relay knows")
    p.set_defaults(fn=cmd_boards)

    p = sub.add_parser("mkboard", help="create a board you own")
    p.add_argument("name")
    p.add_argument("--title", help="display name")
    p.set_defaults(fn=cmd_mkboard)

    p = sub.add_parser("ls", help="list threads on a board, newest first")
    p.add_argument("board")
    p.add_argument("-n", type=int, default=30)
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("read", help="read a thread (any article in it)")
    p.add_argument("board")
    p.add_argument("num", type=int)
    p.add_argument("--only", action="store_true", help="just this article, not the thread")
    p.add_argument("--raw", action="store_true", help="don't render markdown")
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("post", help="start a thread")
    p.add_argument("board")
    p.add_argument("-s", "--subject", required=True)
    p.add_argument("-t", "--tag", action="append", help="tag (repeatable)")
    _body_args(p)
    p.set_defaults(fn=cmd_post)

    p = sub.add_parser("reply", help="reply to an article")
    p.add_argument("board")
    p.add_argument("num", type=int)
    p.add_argument("-s", "--subject", help="default: Re: <parent subject>")
    _body_args(p)
    p.set_defaults(fn=cmd_reply)

    p = sub.add_parser("tail", help="watch a board for new posts")
    p.add_argument("board")
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
        err.print(f"[red]{p.origin} presented a key you haven't accepted ({p.kind}).[/] Run `scootaloo connect {p.origin}` to decide.")
        return 2
    except core.ProtocolError as e:
        err.print(f"[red]server said:[/] {e.detail or e}")
        return 1
    except (core.ScootalooError, core.FirehoseClientError, ValueError) as e:
        err.print(f"[red]{e}[/]")
        return 1
    return 0
