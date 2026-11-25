"""`scootaloo <command>`: the scriptable side. Plain `scootaloo` opens the TUI."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
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


def _show_or_set(st, args, what: str) -> None:
    from scootaloo.settings import SettingsError

    if st.warning:
        err.print(f"[yellow]{st.warning}[/]")
    if args.key is None:
        for k, v in st.items().items():
            out.print(f"{k} = [bold]{json.dumps(v)}[/]")
        out.print(f"[dim]{st.path}[/]")
        return
    try:
        if args.value is None:
            out.print(json.dumps(st.get(args.key)))
            return
        st.set(args.key, args.value)
    except SettingsError as e:
        raise core.ScootalooError(str(e)) from None
    st.save()
    out.print(f"{what}: {args.key} = [bold]{json.dumps(st.get(args.key))}[/]")


async def cmd_config(args):
    from scootaloo import settings

    _show_or_set(settings.config(), args, "config")


async def cmd_prefs(args):
    from scootaloo import settings

    _show_or_set(settings.preferences(account(args).stores), args, "preferences")


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
    idx = core.Index()
    await idx.refresh(acct, [ref])
    per_thread = core.unread_threads(idx, ref, core.unread(acct, idx, [ref])[ref])
    if not rows:
        out.print(f"[dim]{ref.label} is empty[/]")
        return
    if not ref.is_home:
        out.print(f"[dim]{ref.name} on {ref.origin}, via {ref.via}[/]")
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="yellow")
    t.add_column("#", justify="right", style="cyan")
    t.add_column("subject", overflow="fold")
    t.add_column("author")
    t.add_column("when", style="dim")
    for a in rows:
        n = per_thread.get(a.article_id, 0)
        subj = Text(("📌 " if a.pin_state == "pinned" else "") + (a.subject or ""), style="bold" if n else "")
        t.add_row(f"●{n}" if n else "", str(a.article_num), subj, core.author_label(a), when(a.created_at))
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
    core.mark_read(acct, ref, [i.view.article_num for i in th.items])
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


async def cmd_unread(args):
    acct = account(args)
    refs = await core.all_boards(acct, args.relay)
    idx = core.Index()
    await idx.refresh(acct, refs)
    counts = {r: n for r, n in core.unread(acct, idx, refs).items() if n}
    if not counts:
        out.print("[dim]nothing unread[/]")
        return
    t = Table(box=None, pad_edge=False)
    t.add_column("unread", justify="right", style="yellow")
    t.add_column("board", style="bold cyan")
    t.add_column("threads", style="dim")
    for ref, nums in sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0].label)):
        threads = core.unread_threads(idx, ref, nums)
        t.add_row(str(len(nums)), ref.label, f"{len(threads)} thread{'s' if len(threads) != 1 else ''}")
    out.print(t)


async def cmd_inbox(args):
    acct = account(args)
    refs = await core.all_boards(acct, args.relay)
    idx = core.Index()
    await idx.refresh(acct, refs)
    items = [i for i in core.inbox(acct, idx) if args.all or i.unread]
    if not items:
        out.print("[dim]no " + ("" if args.all else "unread ") + "replies to you[/]")
        return
    t = Table(box=None, pad_edge=False)
    t.add_column("", style="yellow")
    t.add_column("board", style="cyan")
    t.add_column("#", justify="right", style="cyan")
    t.add_column("reply")
    t.add_column("to your")
    t.add_column("when", style="dim")
    for i in items:
        t.add_row("●" if i.unread else "", i.ref.label, str(i.row.article_num),
                  f"{core.author_label(i.row)}: {i.row.subject}", f"#{i.parent.article_num}", when(i.row.created_at))
    out.print(t)
    out.print("[dim]`scootaloo read BOARD NUM` opens one (and marks it read)[/]")


async def cmd_mark(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    idx = core.Index()
    await idx.refresh(acct, [ref])
    core.mark_board_read(acct, idx, ref)
    out.print(f"marked everything on {ref.label} read")


async def cmd_edit(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    core.writable(acct, ref, args.as_)
    subject, old = await core.current_body(acct, ref, args.num)
    if args.message is None and not args.file and sys.stdin.isatty():
        body = _edit_text(old)
    else:
        body = read_body(args)
    if body.strip() == old.strip() and not args.subject:
        out.print("[dim]no changes[/]")
        return
    r = await core.edit(acct, ref, args.num, body, args.subject, args.as_)
    out.print(f"edited #{args.num}; it's now [bold]#{r.article_num}[/] (the old version is kept as superseded)")


def _edit_text(text: str) -> str:
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(text)
        path = tf.name
    try:
        subprocess.call(_editor() + [path])
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    finally:
        os.unlink(path)


async def cmd_delete(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    core.writable(acct, ref, args.as_)
    if not args.yes:
        if not sys.stdin.isatty():
            raise core.ScootalooError("pass --yes to delete without a prompt")
        if not Confirm.ask(f"delete #{args.num} on {ref.label}?", default=False, console=err):
            return
    await core.delete(acct, ref, args.num, args.reason or "", args.as_)
    out.print(f"deleted #{args.num}. Bonnet keeps the signed record; the article no longer shows.")


async def cmd_search(args):
    acct = account(args)
    if args.board:
        refs = [await core.resolve_board(acct, args.board, args.from_, args.relay)]
    else:
        refs = [r for r in await core.all_boards(acct, args.relay) if not r.name.startswith("~") or args.bridges]
    hits = await core.search(acct, refs, args.query, args.n)
    if not hits:
        out.print("[dim]no matches[/]")
        return
    t = Table(box=None, pad_edge=False)
    t.add_column("board", style="cyan")
    t.add_column("#", justify="right", style="cyan")
    t.add_column("subject", style="bold", overflow="fold")
    t.add_column("author")
    t.add_column("when", style="dim")
    for h in hits:
        t.add_row(h.ref.label, str(h.article_num), h.subject, h.author, when(h.created_at))
    out.print(t)


async def cmd_verify(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    v = await core.verify(acct, ref, args.num)
    print_verification(v)


def verification_markup(v: core.Verification) -> str:
    """Rich markup describing a verification, shared by the CLI and the TUI."""

    def mark(state, good=("valid", "matches", "absent")):
        if state in good:
            return f"[green]✓ {state}[/]"
        if state in ("unverifiable", "unavailable"):
            return f"[yellow]? {state}[/]"
        return f"[bold red]✗ {state}[/]"

    lines = [f"[bold]#{v.article_num}[/] on {v.ref.label} · event {v.event_id[:16]}… · seq {v.origin_seq} on {v.ref.origin}", ""]
    lines.append(f"  author signature   {mark(v.author_sig)}  [dim]key {v.author_key[:16]}…[/]")
    hint = f"  [dim]accept {v.ref.origin}'s key (connect to it) to check this[/]" if v.origin_sig == "unverifiable" else ""
    lines.append(f"  origin signature   {mark(v.origin_sig)}{hint}")
    lines.append(f"  body               {mark(v.body)}")
    lines.append("  relay's summary    " + ("[green]✓ matches the signed record[/]" if v.view_matches
                                            else f"[bold red]✗ differs: {', '.join(v.mismatches)}[/]"))
    lines.append("  path")
    for h in v.hops:
        ok = "[green]✓[/]" if h["signature_valid"] else "[bold red]✗ bad signature[/]"
        what = "published it" if h["is_origin"] else f"got it from {h['received_from_hostname'] or h['received_from_pubkey'][:12]}"
        link = "" if h["linked"] else " [yellow](not linked to the chain)[/]"
        lines.append(f"    {ok} {h['relay_origin'] or h['relay_hostname']} {what} · {when(h['seen_at'])}{link}")
    lines.append("")
    lines.append("[bold green]verified[/]" if v.ok else "[bold yellow]not fully verified, see above[/]")
    return "\n".join(lines)


def print_verification(v: core.Verification) -> None:
    out.print(verification_markup(v))


def _passphrase(env: str, prompt: str, twice: bool) -> str:
    pw = os.environ.get(env)
    if pw:
        return pw
    pw = _ask_password(prompt)
    if not pw:
        raise core.ScootalooError("an archive needs a passphrase")
    if twice and _ask_password("again: ") != pw:
        raise core.ScootalooError("passphrases didn't match")
    return pw


async def cmd_identity_export(args):
    from scootaloo import idarchive

    acct = account(args)
    if args.all or not args.name:
        if args.seed:
            raise core.ScootalooError("--seed prints one identity: name it")
        which = None
    else:
        which = core.find_identities(acct, args.name, args.origin)
        if not which:
            raise core.ScootalooError(f"no identity '{args.name}'" + (f" on {args.origin}" if args.origin else ""))
        if len(which) > 1:
            raise core.ScootalooError(
                f"'{args.name}' exists on several origins ({', '.join(o for o, _ in which)}): add --origin"
            )
    entries = core.collect_identities(acct, which)
    if not entries:
        raise core.ScootalooError("no identities to export")

    if args.seed:
        e = entries[0]
        err.print(f"[bold red]This prints {e.label}'s private key in plain text.[/] Anyone who sees it can post as you.")
        if not args.yes:
            if not sys.stdin.isatty():
                raise core.ScootalooError("pass --yes to print a raw seed without a prompt")
            if acct.gateway:
                if not Confirm.ask("print it?", default=False, console=err):
                    return
            else:
                again = _ask_password(f"password for {acct.name}, again: ")
                with Users() as u:
                    if not u.verify(acct.name, again):
                        raise core.ScootalooError("wrong password")
        print(idarchive.seed_json(e))
        return

    passphrase = _passphrase("SCOOTALOO_EXPORT_PASSPHRASE", "passphrase for the archive: ", twice=True)
    if args.output:
        path = args.output
    elif len(entries) == 1:
        path = f"{entries[0].username}@{entries[0].origin}"
    else:
        path = "identities-" + datetime.now().strftime("%Y%m%d-%H%M")
    data = idarchive.build(entries, passphrase, f"scootaloo {__version__}")
    path = core.write_archive(path, data, overwrite=args.force)
    out.print(f"exported {len(entries)} identit{'y' if len(entries) == 1 else 'ies'} to [bold]{path}[/]")
    for e in entries:
        out.print(f"  {e.username} on {e.origin}")
    out.print("[dim]Keep the passphrase somewhere other than the file: without it the archive can't be opened.[/]")


async def cmd_identity_import(args):
    from scootaloo import idarchive

    acct = account(args)
    if args.manual:
        if args.file:
            raise core.ScootalooError("--manual reads a pasted seed or gateway JSON, not a file")
        text = sys.stdin.read() if not sys.stdin.isatty() else _ask_password(
            "paste the seed (64 hex) or the gateway's export_identity JSON: ")
        entry = idarchive.parse_manual(text, args.origin or "", args.name or "")
        if args.url:
            entry.relay.url = args.url
        entries, rejected, source = [entry], [], "pasted identity"
    else:
        if not args.file:
            raise core.ScootalooError("name an archive file, or use --manual")
        with open(args.file, "rb") as f:
            data = f.read()
        idarchive.check_envelope(data)
        passphrase = _passphrase("SCOOTALOO_IMPORT_PASSPHRASE", f"passphrase for {args.file}: ", twice=False)
        opened = idarchive.open_archive(data, passphrase)
        entries, rejected = opened.entries, opened.rejected
        source = f"{args.file} (made {opened.exported_at or 'at an unknown time'} by {opened.exported_by or 'unknown'})"
        if args.only:
            wanted = {tuple(x.split("/", 1)) if "/" in x else ("", x) for x in args.only}
            entries = [e for e in entries if (e.origin, e.username) in wanted or ("", e.username) in wanted]
            if not entries:
                raise core.ScootalooError("--only matched nothing in the archive")

    plans = core.plan_import(acct, entries)
    out.print(f"[bold]{source}[/]")
    words = {"import": "[green]import[/]", "skip": "[dim]skip[/]", "conflict": "[yellow]conflict[/]", "corrupt": "[red]corrupt[/]"}
    for p in plans:
        out.print(f"  {words[p.action]:<22} {p.entry.label}" + (f" [dim]({p.reason})[/]" if p.reason else ""))
    for what, why in rejected:
        out.print(f"  [red]unreadable[/]  {what} [dim]({why})[/]")
    if args.dry_run:
        return

    backup_pw = None
    if args.replace and any(p.action == "conflict" for p in plans):
        n = sum(p.action == "conflict" for p in plans)
        if not args.yes:
            if not sys.stdin.isatty():
                raise core.ScootalooError("pass --yes to replace identities without a prompt")
            if not Confirm.ask(f"replace {n} identit{'y' if n == 1 else 'ies'} you hold? They'll be backed up first",
                               default=False, console=err):
                return
        backup_pw = _passphrase("SCOOTALOO_EXPORT_PASSPHRASE", "passphrase for the backup of what's replaced: ", twice=True)

    async def confirm(p, url):
        if not sys.stdin.isatty():
            return False
        kind = "has a DIFFERENT key than the archive recorded" if p.kind == "new" else "presents a CHANGED key"
        err.print(f"[yellow]{p.origin} ({url}) {kind}.[/] fingerprint: [bold]{p.fingerprint}[/]")
        return Confirm.ask("trust this key?", default=False, console=err)

    results = await core.import_identities(acct, plans, replace=args.replace, backup_passphrase=backup_pw, confirm_pin=confirm)
    for r in results:
        colour = {"imported": "green", "replaced": "yellow", "skipped": "dim", "refused": "yellow", "rejected": "red"}[r.outcome]
        out.print(f"[{colour}]{r.outcome}[/] {r.entry.label}")
        if r.backup:
            out.print(f"    the identity it replaced is backed up in {r.backup}")
        for note in r.notes:
            out.print(f"    [dim]{note}[/]")


async def cmd_mod(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    if args.action == "ban" and args.days <= 0 and not args.yes:
        if not sys.stdin.isatty() or not Confirm.ask("no --days: that's a PERMANENT ban. Go ahead?", default=False, console=err):
            raise core.ScootalooError("not banned (pass --days N for a temporary ban)")
    r = await core.moderate(acct, ref, args.num, args.action, args.reason or "", args.rank, args.days, args.as_)
    out.print(f"{args.action}: #{args.num} on {ref.label} (seq {r.origin_seq})")


async def cmd_unban(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    n = await core.lift_punishments(acct, ref, args.num, args.reason or "", args.as_)
    out.print(f"lifted {n} punishment{'s' if n != 1 else ''} from the author of #{args.num}")


async def cmd_board_state(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    close = args.fn is cmd_board_close
    await core.board_state(acct, ref, close, args.reason or "", args.as_)
    out.print(f"{ref.label} is now {'closed' if close else 'open'}")


async def cmd_board_close(args):
    await cmd_board_state(args)


async def cmd_board_reopen(args):
    await cmd_board_state(args)


async def cmd_report(args):
    acct = account(args)
    ref = await board_ref(args, acct)
    reason = args.reason or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    await core.report(acct, ref, args.num, reason, args.as_)
    out.print(f"reported #{args.num} on {ref.label} to its moderators")


async def cmd_reports(args):
    acct = account(args)
    rows = await core.reports(acct, args.relay, args.n)
    if not rows:
        out.print("[dim]no reports[/]")
        return
    t = Table(box=None, pad_edge=False)
    t.add_column("when", style="dim")
    t.add_column("by")
    t.add_column("about")
    t.add_column("where", style="cyan")
    t.add_column("reason", overflow="fold")
    for r in rows:
        i = r.info
        where = f"{i.target_board}@{i.target_origin}" if i.target_kind == "article" else i.target_kind
        t.add_row(when(i.created_at), i.reporter_username or i.reporter_pubkey[:12], i.culprit_pubkey[:12], where, r.reason)
    out.print(t)


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

    p = sub.add_parser("config", help="app-wide settings (config.json): list, read, or set one")
    p.add_argument("key", nargs="?", help="e.g. login.list_users")
    p.add_argument("value", nargs="?", help="new value, e.g. false")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("prefs", help="your own preferences (preferences.json): list, read, or set one")
    p.add_argument("key", nargs="?", help="e.g. layout.name")
    p.add_argument("value", nargs="?", help="new value, e.g. stacked")
    p.set_defaults(fn=cmd_prefs)

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

    ident = sub.add_parser("identity", help="export or import Bonnet identities").add_subparsers(
        dest="icmd", metavar="<action>", required=True)
    p = ident.add_parser("export", help="write identities to an encrypted .bnid archive")
    p.add_argument("name", nargs="?", help="one identity (default: all of them)")
    p.add_argument("--all", action="store_true", help="every identity you hold")
    p.add_argument("--origin", help="which origin, when the name is on several")
    p.add_argument("-o", "--output", help="file to write (default: NAME@ORIGIN.bnid or identities-DATE.bnid)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--seed", action="store_true", help="print one identity's raw seed, gateway-style, instead")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before printing a raw seed")
    p.set_defaults(fn=cmd_identity_export)
    p = ident.add_parser("import", help="import identities from a .bnid archive, or a pasted seed")
    p.add_argument("file", nargs="?", help="the .bnid archive")
    p.add_argument("--only", action="append", metavar="ORIGIN/NAME", help="import just this one (repeatable)")
    p.add_argument("--replace", action="store_true", help="replace an identity you hold under the same name (backed up first)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("--manual", action="store_true", help="paste a 64-hex seed or the gateway's export_identity JSON")
    p.add_argument("--origin", help="with --manual and a bare seed: the identity's origin")
    p.add_argument("--name", help="with --manual and a bare seed: the identity's username")
    p.add_argument("--url", help="with --manual: the relay URL (default https://ORIGIN)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before replacing")
    p.set_defaults(fn=cmd_identity_import)

    p = sub.add_parser("mod", help="moderator actions: pin, unpin, close, reopen, remove a post; warn or ban its author")
    p.add_argument("action", choices=core.MOD_ACTIONS)
    _board_args(p)
    p.add_argument("num", type=int)
    p.add_argument("--reason", help="why (required for warn and ban; shown to the user)")
    p.add_argument("--rank", type=int, default=0, help="pin order")
    p.add_argument("--days", type=int, default=0, help="ban length; 0 = permanent (asks first)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before a permanent ban")
    p.set_defaults(fn=cmd_mod)

    p = sub.add_parser("report", help="report a post to its origin's moderators")
    _board_args(p)
    p.add_argument("num", type=int)
    p.add_argument("--reason", help="what's wrong (else read from stdin)")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("reports", help="moderators: the report queue of the active relay")
    p.add_argument("-n", type=int, default=100)
    p.set_defaults(fn=cmd_reports)

    p = sub.add_parser("unban", help="moderators: lift the punishments on a post's author")
    _board_args(p)
    p.add_argument("num", type=int)
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_unban)
    for verb, fn, what in (("close-board", cmd_board_close, "close a board to new posts"),
                           ("reopen-board", cmd_board_reopen, "reopen a closed board")):
        p = sub.add_parser(verb, help=f"board owners and moderators: {what}")
        _board_args(p)
        p.add_argument("--reason")
        p.set_defaults(fn=fn)

    sub.add_parser("unread", help="boards with unread posts").set_defaults(fn=cmd_unread)

    p = sub.add_parser("inbox", help="replies to your posts")
    p.add_argument("-a", "--all", action="store_true", help="include ones you've read")
    p.set_defaults(fn=cmd_inbox)

    p = sub.add_parser("mark", help="mark everything on a board read")
    _board_args(p)
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("edit", help="edit your own post (opens $EDITOR with the current text)")
    _board_args(p)
    p.add_argument("num", type=int)
    p.add_argument("-s", "--subject", help="new subject")
    _body_args(p)
    p.set_defaults(fn=cmd_edit)

    p = sub.add_parser("delete", help="delete your own post")
    _board_args(p)
    p.add_argument("num", type=int)
    p.add_argument("--reason", help="optional reason, stored with the cancel")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask")
    p.set_defaults(fn=cmd_delete)

    p = sub.add_parser("search", help="search subjects (and bodies, where the server indexes them)")
    p.add_argument("query")
    p.add_argument("board", nargs="?", help="one board (name or name@origin); default: all")
    p.add_argument("--from", dest="from_", metavar="ORIGIN")
    p.add_argument("-n", type=int, default=50)
    p.add_argument("--bridges", action="store_true", help="include ~ bridge boards")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("verify", help="check the signatures behind an article")
    _board_args(p)
    p.add_argument("num", type=int)
    p.set_defaults(fn=cmd_verify)

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
    except FileNotFoundError as e:
        err.print(f"[red]no such file: {e.filename}[/]")
        return 1
    except (core.ScootalooError, AccountError, core.FirehoseClientError, ValueError) as e:
        err.print(f"[red]{e}[/]")
        return 1
    return 0
