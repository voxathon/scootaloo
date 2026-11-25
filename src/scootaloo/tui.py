"""The full-screen client: log in, then boards (by origin) | threads | the open thread."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    Tree,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from scootaloo import core
from scootaloo.accounts import Account, AccountError, Stores, Users, default_user


def when(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


# ---------------------------------------------------------------------------
# modals
# ---------------------------------------------------------------------------


class Compose(ModalScreen[tuple[str, str] | None]):
    """Subject + body. Returns (subject, body), or None on cancel."""

    BINDINGS = [Binding("ctrl+s", "send", "Send"), Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, subject: str = "", quote: str = "", subject_editable: bool = True):
        super().__init__()
        self.title_text = title
        self.subject = subject
        self.quote = quote
        self.subject_editable = subject_editable

    def compose(self) -> ComposeResult:
        with Vertical(id="compose"):
            yield Label(self.title_text, id="compose-title")
            yield Input(self.subject, placeholder="subject", id="subject", disabled=not self.subject_editable)
            yield TextArea(self.quote, id="body", language="markdown", soft_wrap=True, show_line_numbers=False)
            with Horizontal(id="compose-buttons"):
                yield Button("Send (ctrl+s)", variant="primary", id="send")
                yield Button("Cancel (esc)", id="cancel")

    def on_mount(self) -> None:
        if self.subject_editable and not self.subject:
            self.query_one("#subject", Input).focus()
        else:
            ta = self.query_one("#body", TextArea)
            ta.focus()
            ta.move_cursor(ta.document.end)

    @on(Button.Pressed, "#send")
    def action_send(self) -> None:
        subject = self.query_one("#subject", Input).value.strip()
        body = self.query_one("#body", TextArea).text.strip()
        if not subject:
            self.notify("needs a subject", severity="warning")
            return
        if not body:
            self.notify("needs a body", severity="warning")
            return
        self.dismiss((subject, body))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class Ask(ModalScreen[str | None]):
    """One line of input (a URL, a password, a name)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, value: str = "", password: bool = False):
        super().__init__()
        self.prompt, self.value, self.password = prompt, value, password

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt)
            yield Input(self.value, password=self.password, id="answer")

    @on(Input.Submitted)
    def submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TrustKey(ModalScreen[bool]):
    """The pin decision. Defaults to No, and says loudly when a key changed."""

    BINDINGS = [Binding("escape", "no", "No")]

    def __init__(self, pending: core.PinConfirmationRequired, url: str):
        super().__init__()
        self.pending, self.url = pending, url

    def compose(self) -> ComposeResult:
        p = self.pending
        with Vertical(classes="dialog"):
            if p.kind == "changed":
                yield Label(Text(f"The key for {p.origin} has CHANGED", style="bold red"))
                yield Label(f"evidence offered: {p.evidence or 'none'}. Confirm the fingerprint another way first.")
            else:
                yield Label(Text(f"First contact with {p.origin}", style="bold"))
            if not p.host_match:
                yield Label(Text(f"{self.url} claims to be '{p.origin}', which isn't the host you dialed.", style="yellow"))
            yield Static(Text(p.fingerprint, style="bold"), id="fingerprint")
            with Horizontal(id="compose-buttons"):
                yield Button("Trust", variant="warning" if p.kind == "changed" else "primary", id="yes")
                yield Button("Don't trust", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    @on(Button.Pressed, "#yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# thread view
# ---------------------------------------------------------------------------


class Post(Vertical, can_focus=True):
    """One article in the open thread. Focus one and press c to reply to it."""

    def __init__(self, item: core.ThreadItem):
        super().__init__(classes=f"post depth{min(item.depth, 6)}")
        self.item = item

    def compose(self) -> ComposeResult:
        v = self.item.view
        head = Text()
        head.append(f"#{v.article_num} ", style="bold cyan")
        head.append(v.subject or "(no subject)", style="bold")
        meta = Text(f"{core.author_label(v)} · {when(v.created_at)}")
        if v.pin_state == "pinned":
            meta.append(" · pinned")
        if v.visibility != "active":
            meta.append(f" · {v.visibility}", style="yellow")
        yield Static(head, classes="post-head")
        yield Static(meta, classes="post-meta")
        if self.item.body is None:
            yield Static(Text(f"[body {self.item.note or v.body_state}]", style="dim italic"))
        elif "markdown" in (v.content_type or ""):
            yield Markdown(self.item.body)
        else:
            yield Static(Text(self.item.body))


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


class Login(Screen[Account]):
    """Pick a user and give their password, or make a new user. Every user has a password."""

    BINDINGS = [Binding("ctrl+n", "toggle_new", "New user"), Binding("ctrl+q", "app.quit", "Quit")]

    def __init__(self, user: str | None = None):
        super().__init__()
        self.preset = user
        self.creating = False

    def compose(self) -> ComposeResult:
        with Vertical(id="login"):
            yield Label(Text("scootaloo", style="bold"), id="login-title")
            yield Label("", id="login-mode")
            yield OptionList(id="login-users")
            yield Input(placeholder="user name", id="login-name")
            yield Input(placeholder="password", password=True, id="login-pw")
            yield Input(placeholder="password again", password=True, id="login-pw2")
            with Horizontal(id="compose-buttons"):
                yield Button("Log in", variant="primary", id="login-go")
                yield Button("New user (ctrl+n)", id="login-new")
            yield Label("", id="login-error")
        yield Footer()

    def on_mount(self) -> None:
        with Users() as u:
            names = u.names()
        ol = self.query_one("#login-users", OptionList)
        for n in names:
            ol.add_option(Option(n, id=n))
        self.creating = not names
        guess = self.preset or default_user()
        if guess in names:
            ol.highlighted = names.index(guess)
            self.query_one("#login-name", Input).value = guess
        self._render_mode()

    def _render_mode(self) -> None:
        ol = self.query_one("#login-users", OptionList)
        has_users = ol.option_count > 0
        self.query_one("#login-mode", Label).update(
            "Create a user. Their password also encrypts their Bonnet keys." if self.creating else "Log in"
        )
        ol.display = has_users and not self.creating
        self.query_one("#login-pw2", Input).display = self.creating
        self.query_one("#login-go", Button).label = "Create" if self.creating else "Log in"
        self.query_one("#login-new", Button).label = "Back to log in" if self.creating else "New user (ctrl+n)"
        self.query_one("#login-new", Button).display = has_users or not self.creating
        name = self.query_one("#login-name", Input)
        if self.creating:
            name.value = ""
            name.focus()
        elif name.value:
            self.query_one("#login-pw", Input).focus()
        else:
            name.focus()

    @on(OptionList.OptionHighlighted, "#login-users")
    def picked(self, ev: OptionList.OptionHighlighted) -> None:
        self.query_one("#login-name", Input).value = ev.option.id

    @on(OptionList.OptionSelected, "#login-users")
    def chosen(self, ev: OptionList.OptionSelected) -> None:
        self.query_one("#login-name", Input).value = ev.option.id
        self.query_one("#login-pw", Input).focus()

    @on(Button.Pressed, "#login-new")
    def action_toggle_new(self) -> None:
        self.creating = not self.creating
        self._render_mode()

    @on(Input.Submitted)
    def submitted(self, ev: Input.Submitted) -> None:
        if ev.input.id == "login-name":
            self.query_one("#login-pw", Input).focus()
        elif ev.input.id == "login-pw" and self.creating:
            self.query_one("#login-pw2", Input).focus()
        else:
            self.go()

    @on(Button.Pressed, "#login-go")
    def go(self) -> None:
        name = self.query_one("#login-name", Input).value.strip()
        pw = self.query_one("#login-pw", Input).value
        error = self.query_one("#login-error", Label)
        try:
            with Users() as u:
                if self.creating:
                    if pw != self.query_one("#login-pw2", Input).value:
                        raise AccountError("passwords didn't match")
                    u.add(name, pw)
                acct = u.login(name, pw)
        except AccountError as e:
            error.update(Text(str(e), style="red"))
            self.query_one("#login-pw", Input).value = ""
            self.query_one("#login-pw2", Input).value = ""
            self.query_one("#login-pw", Input).focus()
            return
        self.dismiss(acct)


class YesNo(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "No"), Binding("y", "yes", "Yes")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question)
            with Horizontal(id="compose-buttons"):
                yield Button("Yes (y)", variant="error", id="yes")
                yield Button("No (esc)", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    @on(Button.Pressed, "#yes")
    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


class Show(ModalScreen[None]):
    """Read-only text (verification results). Any of esc / enter / q closes it."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("enter", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, markup: str):
        super().__init__()
        self.markup = markup

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="show"):
            yield Static(self.markup)

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

INBOX = "inbox"


class ScootalooApp(App):
    TITLE = "scootaloo"
    CSS = """
    #panes { height: 1fr; }
    #boards { width: 32; height: 1fr; border-right: solid $primary-darken-2; }
    #threads { width: 1fr; height: 1fr; }
    #view { width: 1fr; border-left: solid $primary-darken-2; padding: 0 1; }
    #status { height: 1; background: $boost; color: $text-muted; padding: 0 1; }
    .post { height: auto; margin: 0 0 1 0; padding: 0 1; border-left: tall $primary; }
    .post:focus { border-left: tall $accent; background: $boost; }
    .post-head { height: auto; }
    .post-meta { color: $text-muted; height: auto; }
    .depth1 { margin-left: 2; } .depth2 { margin-left: 4; } .depth3 { margin-left: 6; }
    .depth4 { margin-left: 8; } .depth5 { margin-left: 10; } .depth6 { margin-left: 12; }
    #empty { color: $text-muted; padding: 1; }
    Compose { align: center middle; }
    #compose { width: 90%; height: 85%; border: thick $primary; background: $surface; padding: 1 2; }
    #compose-title { text-style: bold; margin-bottom: 1; }
    #body { height: 1fr; }
    #compose-buttons { height: auto; margin-top: 1; }
    #compose-buttons Button { margin-right: 2; }
    Ask, TrustKey, YesNo, Show { align: center middle; }
    .dialog { width: 90; height: auto; max-height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    #fingerprint { margin: 1 0; }
    Login { align: center middle; }
    #login { width: 60; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #login-title { margin-bottom: 1; }
    #login-users { height: auto; max-height: 8; margin-bottom: 1; }
    #login Input { margin-bottom: 1; }
    #login-error { margin-top: 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("n", "new_thread", "New"),
        Binding("c", "reply", "Reply"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("slash", "search", "Search"),
        Binding("v", "verify", "Verify"),
        Binding("m", "mark_read", "Mark read"),
        Binding("o", "connect", "Connect"),
        Binding("i", "identity", "Identity"),
        Binding("ctrl+l", "switch_user", "User"),
        Binding("ctrl+k", "change_password", "Password", show=False),
    ]

    REFRESH_SECONDS = 20

    def __init__(self, user: str | None = None, password: str | None = None,
                 gateway_store: bool = False, relay: str | None = None):
        super().__init__()
        self._user, self._password, self._gateway, self._relay = user, password, gateway_store, relay
        self.acct: Account | None = None
        self.index = core.Index()
        self.refs: list[core.BoardRef] = []
        self.unread_map: dict[core.BoardRef, set[int]] = {}
        self.inbox_items: list[core.InboxItem] = []
        self.nodes: dict[core.BoardRef, object] = {}
        self.inbox_node = None
        self.ref: core.BoardRef | None = None  # board selected in the tree
        self.mode = "board"  # board | inbox | search
        self.search_label = ""
        self.rowmap: dict[str, tuple[core.BoardRef, int]] = {}
        self.thread: core.Thread | None = None
        self._busy = False

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            with Horizontal(id="panes"):
                yield Tree("relays", id="boards")
                yield DataTable(id="threads", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="view"):
                    yield Static("pick a thread", id="empty")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#boards", Tree)
        tree.show_root = False
        t = self.query_one("#threads", DataTable)
        t.add_column("", key="new", width=3)
        t.add_column("#", key="num", width=5)
        t.add_column("subject", key="subject", width=28)
        t.add_column("author", key="author", width=14)
        t.add_column("date", key="when", width=5)
        self.start()
        self.set_interval(self.REFRESH_SECONDS, self._tick)

    @property
    def target_ref(self) -> core.BoardRef | None:
        """The board actions apply to: the open thread's, else the selected board."""
        if self.thread:
            return self.thread.ref
        return self.ref if self.mode == "board" else None

    def set_status(self) -> None:
        if not self.acct:
            self.query_one("#status", Static).update("not logged in")
            return
        parts = [self.acct.name]
        if self.mode == "inbox":
            parts.append("inbox")
        elif self.mode == "search":
            parts.append(f"search: {self.search_label}")
        ref = self.target_ref
        if ref:
            parts.append(ref.label + ("" if ref.is_home else f" via {ref.via}"))
            try:
                parts.append(f"posting as {core.writable(self.acct, ref).username}")
            except core.ScootalooError:
                parts.append("read-only here")
        elif not core.joined(self.acct):
            parts.append("no relays yet · press o")
        self.query_one("#status", Static).update(" · ".join(parts))
        self.sub_title = self.acct.name

    # -- login, relays, identities -------------------------------------------

    @work
    async def start(self) -> None:
        acct = None
        if self._gateway:
            acct = Account(Stores.gateway(), "gateway store", self._password, gateway=True)
        elif self._user and self._password:
            try:
                with Users() as u:
                    acct = u.login(self._user, self._password)
            except AccountError as e:
                self.notify(str(e), severity="error")
        if acct is None:
            acct = await self.push_screen_wait(Login(self._user))
        await self._enter(acct)

    async def _enter(self, acct: Account) -> None:
        self.acct, self.ref, self.thread, self.mode = acct, None, None, "board"
        self.index = core.Index()
        self.query_one("#threads", DataTable).clear()
        await self._clear_view("pick a thread")
        if self._relay:
            try:
                core.set_active(acct, self._relay)
            except core.ScootalooError as e:
                self.notify(str(e), severity="warning")
        self.set_status()
        if not core.joined(acct):
            await self._connect_flow(core.DEFAULT_URL)
        else:
            await self.load_boards()

    @work
    async def action_switch_user(self) -> None:
        if isinstance(self.screen, Login):
            return
        if self._gateway:
            self.notify("using the gateway store: there are no users to switch between", severity="warning")
            return
        acct = await self.push_screen_wait(Login())
        await self._enter(acct)
        self.notify(f"logged in as {acct.name}")

    @work
    async def action_change_password(self) -> None:
        if not self.acct or self.acct.gateway or isinstance(self.screen, Login):
            return
        old = await self.push_screen_wait(Ask(f"current password for {self.acct.name}", password=True))
        if not old:
            return
        new = await self.push_screen_wait(Ask("new password", password=True))
        if not new:
            return
        if await self.push_screen_wait(Ask("new password again", password=True)) != new:
            self.notify("passwords didn't match; nothing changed", severity="warning")
            return
        try:
            with Users() as u:
                n = u.change_password(self.acct.name, old, new)
        except AccountError as e:
            self.notify(str(e), severity="error", timeout=8)
            return
        self.acct.password = new
        self.notify(f"password changed; re-encrypted {n} key{'s' if n != 1 else ''}")

    async def _connect_flow(self, default_url: str) -> None:
        url = await self.push_screen_wait(Ask("connect to a relay (URL)", default_url))
        if not url:
            return
        try:
            try:
                r = await core.connect(self.acct, url)
            except core.PinConfirmationRequired as p:
                ok = await self.push_screen_wait(TrustKey(p, url))
                if not ok:
                    core.decline_pin(self.acct, p)
                    self.notify("key not trusted; nothing pinned", severity="warning")
                    return
                core.accept_pin(self.acct, p)
                r = await core.connect(self.acct, url)
        except Exception as e:  # noqa: BLE001 — show anything to the user
            self.notify(str(e), severity="error", timeout=8)
            return
        self.notify(f"connected to {r['origin']}")
        await self.load_boards()

    @work
    async def action_connect(self) -> None:
        if self.acct:
            await self._connect_flow(core.DEFAULT_URL)

    @work
    async def action_identity(self) -> None:
        """Pick or register the identity you post as on the relay you're looking at."""
        if not self.acct:
            return
        ref = self.target_ref
        relay = ref.via if ref else (core.active_relay(self.acct) or {}).get("origin")
        if not relay:
            self.notify("connect to a relay first (o)", severity="warning")
            return
        ids = [i["username"] for i in core.identities(self.acct, relay)]
        current = core.Session.open(self.acct, relay).username or ""
        hint = f"have: {', '.join(ids)}" if ids else "a new name registers it"
        name = await self.push_screen_wait(Ask(f"post as, on {relay} ({hint})", current))
        if not name:
            return
        try:
            if name in ids:
                core.use_identity(self.acct, relay, name)
                self.notify(f"posting as {name} on {relay}")
            else:
                r = await core.register(core.Session.open(self.acct, relay), name)
                self.notify(f"registered {r['username']} on {relay}")
        except Exception as e:  # noqa: BLE001
            self.notify(str(e), severity="error", timeout=8)
        self.set_status()

    # -- loading -------------------------------------------------------------

    async def _guard(self, coro):
        try:
            return await coro
        except core.PinConfirmationRequired as p:
            self.notify(f"{p.origin} presented a key you haven't accepted; press o to reconnect", severity="error", timeout=10)
        except core.ProtocolError as e:
            self.notify(f"server: {e.detail or e}", severity="error", timeout=8)
        except Exception as e:  # noqa: BLE001
            self.notify(str(e), severity="error", timeout=8)
        return None

    def _recount(self) -> None:
        self.unread_map = core.unread(self.acct, self.index, self.refs)
        self.inbox_items = core.inbox(self.acct, self.index)

    def _relabel(self) -> None:
        for ref, node in self.nodes.items():
            n = len(self.unread_map.get(ref, ()))
            style = "dim" if ref.closed else ("magenta" if ref.name.startswith("~") else "")
            label = Text(ref.name + (f" ({n})" if n else ""), style=style)
            if n:
                label.stylize("bold")
            node.set_label(label)
        if self.inbox_node is not None:
            n = sum(1 for i in self.inbox_items if i.unread)
            label = Text("Inbox" + (f" ({n})" if n else ""), style="bold yellow" if n else "")
            self.inbox_node.set_label(label)

    async def load_boards(self) -> None:
        if not self.acct:
            return
        refs = await self._guard(core.all_boards(self.acct))
        if refs is None:
            return
        self.refs = refs
        await self._guard(self.index.refresh(self.acct, refs))
        self._recount()

        tree = self.query_one("#boards", Tree)
        tree.clear()
        self.nodes = {}
        self.inbox_node = tree.root.add_leaf("Inbox", data=INBOX)
        by_origin: dict[str, list[core.BoardRef]] = {}
        for ref in refs:
            by_origin.setdefault(ref.origin, []).append(ref)
        first = None
        for origin, rows in by_origin.items():
            via = rows[0].via
            label = Text(origin if via == origin else f"{origin}  via {via}")
            label.stylize("bold", 0, len(origin))
            label.stylize("dim", len(origin))
            node = tree.root.add(label, expand=True)
            for ref in rows:
                leaf = node.add_leaf(ref.name, data=ref)
                self.nodes[ref] = leaf
                if ref.is_home and not ref.name.startswith("~") and (first is None or ref.name == "general"):
                    first = leaf
        self._relabel()
        tree.focus()
        if self.ref and self.ref in self.nodes:
            first = self.nodes[self.ref]
        if first is not None:
            tree.move_cursor(first)
            if self.mode == "board" and self.ref != first.data:
                self.open_board(first.data)

    @on(Tree.NodeSelected, "#boards")
    def board_selected(self, ev: Tree.NodeSelected) -> None:
        if ev.node.data == INBOX:
            self.show_inbox()
        elif isinstance(ev.node.data, core.BoardRef):
            self.open_board(ev.node.data)

    # -- the middle pane: a board, the inbox, or search results ----------------

    def _fill(self, rows: list[tuple[str, int, int, str, str, int, core.BoardRef]]) -> None:
        """rows: (key, unread count, num, subject, author, timestamp, ref)."""
        t = self.query_one("#threads", DataTable)
        t.clear()
        self.rowmap = {}
        for key, n, num, subject, author, ts, ref in rows:
            subj = subject if len(subject) <= 28 else subject[:27] + "…"
            t.add_row(
                Text(f"●{n}" if n > 1 else ("●" if n else ""), style="yellow"),
                str(num), Text(subj, style="bold" if n else ""), author,
                datetime.fromtimestamp(ts).strftime("%m-%d"), key=key,
            )
            self.rowmap[key] = (ref, num)

    def _render_board(self) -> None:
        ref = self.ref
        rows = self.index.rows(ref)
        per_thread = core.unread_threads(self.index, ref, self.unread_map.get(ref, set()))
        roots = sorted((r for r in rows if core.is_zero(r.root_article_id)),
                       key=lambda r: (r.pin_state != "pinned", -r.created_at, -r.article_num))
        self._fill([
            (f"b{r.article_num}", per_thread.get(r.article_id, 0), r.article_num,
             ("📌 " if r.pin_state == "pinned" else "") + (r.subject or ""), core.author_label(r), r.created_at, ref)
            for r in roots
        ])

    def _render_inbox(self) -> None:
        self._fill([
            (f"i{k}", 1 if it.unread else 0, it.row.article_num, f"{it.ref.name}: {it.row.subject}",
             core.author_label(it.row), it.row.created_at, it.ref)
            for k, it in enumerate(self.inbox_items)
        ])

    @work(exclusive=True, group="threads")
    async def open_board(self, ref: core.BoardRef) -> None:
        if ref != self.ref or self.mode != "board":
            self.thread = None
            await self._clear_view("pick a thread")
        self.ref, self.mode = ref, "board"
        self.set_status()
        await self._guard(self.index.refresh(self.acct, [ref]))
        self.unread_map[ref] = core.unread(self.acct, self.index, [ref])[ref]
        self._render_board()
        self._relabel()

    @work(exclusive=True, group="threads")
    async def show_inbox(self) -> None:
        self.mode, self.thread = "inbox", None
        await self._clear_view("replies to your posts, newest first")
        self.set_status()
        self._render_inbox()

    @on(DataTable.RowSelected, "#threads")
    def thread_selected(self, ev: DataTable.RowSelected) -> None:
        ref, num = self.rowmap[ev.row_key.value]
        self.open_thread(ref, num)

    async def _clear_view(self, msg: str) -> None:
        view = self.query_one("#view", VerticalScroll)
        await view.remove_children()
        await view.mount(Static(msg, id="empty"))

    @work(exclusive=True, group="view")
    async def open_thread(self, ref: core.BoardRef, num: int, focus_num: int | None = None) -> None:
        await self._clear_view("loading…")
        th = await self._guard(core.thread(self.acct, ref, num))
        view = self.query_one("#view", VerticalScroll)
        await view.remove_children()
        if th is None:
            return
        self.thread = th
        posts = [Post(item) for item in th.items]
        await view.mount_all(posts)
        if th.truncated:
            await view.mount(Static("thread truncated at 200 articles", id="empty"))
        target = next((p for p in posts if p.item.view.article_num in (focus_num, num)), None)
        if target and (focus_num or target.item.depth):
            target.focus()
            view.scroll_to_widget(target)
        else:
            view.scroll_home(animate=False)
        core.mark_read(self.acct, ref, [i.view.article_num for i in th.items])
        self._recount()
        self._relabel()
        self._rerender_list()
        self.set_status()

    def _rerender_list(self) -> None:
        t = self.query_one("#threads", DataTable)
        cursor = t.cursor_row
        if self.mode == "board" and self.ref:
            self._render_board()
        elif self.mode == "inbox":
            self._render_inbox()
        else:
            return
        if t.row_count:
            t.move_cursor(row=min(cursor, t.row_count - 1), animate=False)

    # -- refreshing ----------------------------------------------------------

    def action_refresh(self) -> None:
        if not self.acct:
            return

        async def full():
            await self.load_boards()
            if self.thread:
                self.open_thread(self.thread.ref, self.thread.root.article_num)

        self.run_worker(full(), exclusive=True, group="refresh")

    def _tick(self) -> None:
        if self.acct and self.refs and not self._busy and not isinstance(self.screen, (ModalScreen, Login)):
            self.run_worker(self._poll(), exclusive=True, group="refresh")

    async def _poll(self) -> None:
        """Cheap: one head request per origin; boards are re-listed only when their origin moved."""
        self._busy = True
        try:
            before = sum(len(v) for v in self.unread_map.values())
            changed = await self.index.refresh(self.acct, self.refs)
            if not changed:
                return
            self._recount()
            self._relabel()
            self._rerender_list()
            gained = sum(len(v) for v in self.unread_map.values()) - before
            if gained > 0:
                self.notify(f"{gained} new post{'s' if gained != 1 else ''}")
        except Exception:  # noqa: BLE001 — a failed background poll just waits for the next one
            pass
        finally:
            self._busy = False

    # -- reading tools -------------------------------------------------------

    def _focused_item(self) -> core.ThreadItem | None:
        if not self.thread:
            return None
        f = self.focused
        return f.item if isinstance(f, Post) else self.thread.items[0]

    @work
    async def action_search(self) -> None:
        if not self.acct or not self.refs:
            return
        scope = [self.ref] if self.mode == "board" and self.ref else [r for r in self.refs if not r.name.startswith("~")]
        where = scope[0].label if len(scope) == 1 else "all boards"
        q = await self.push_screen_wait(Ask(f"search {where}"))
        if not q:
            return
        hits = await self._guard(core.search(self.acct, scope, q, 100, self.index))
        if hits is None:
            return
        self.mode, self.thread, self.search_label = "search", None, f"{q} in {where}"
        await self._clear_view(f"{len(hits)} match{'es' if len(hits) != 1 else ''}")
        self._fill([
            (f"s{k}", 0, h.article_num, (f"{h.ref.name}: " if len(scope) > 1 else "") + (h.subject or ""),
             h.author, h.created_at, h.ref)
            for k, h in enumerate(hits)
        ])
        self.set_status()
        self.query_one("#threads", DataTable).focus()

    @work
    async def action_verify(self) -> None:
        item = self._focused_item()
        if not item:
            self.notify("open a thread first", severity="warning")
            return
        self.notify(f"checking #{item.view.article_num}…")
        v = await self._guard(core.verify(self.acct, self.thread.ref, item.view.article_num))
        if v:
            from scootaloo.cli import verification_markup

            await self.push_screen_wait(Show(verification_markup(v)))

    def action_mark_read(self) -> None:
        if self.mode == "board" and self.ref:
            core.mark_board_read(self.acct, self.index, self.ref)
        elif self.mode == "inbox":
            for it in self.inbox_items:
                core.mark_read(self.acct, it.ref, [it.row.article_num])
        else:
            return
        self._recount()
        self._relabel()
        self._rerender_list()

    # -- writing -------------------------------------------------------------

    def _writable(self, ref: core.BoardRef | None) -> core.Session | None:
        if not self.acct or not ref:
            if self.mode != "board":
                self.notify("open a board or thread first", severity="warning")
            return None
        try:
            return core.writable(self.acct, ref)
        except core.ScootalooError as e:
            self.notify(str(e), severity="warning", timeout=8)
            return None

    async def _after_write(self, ref: core.BoardRef, thread_num: int | None, focus: int | None) -> None:
        await self._guard(self.index.refresh(self.acct, [ref]))
        if thread_num is not None:
            core.mark_read(self.acct, ref, [n for n in (thread_num, focus) if n])
        self._recount()
        self._relabel()
        self._rerender_list()
        if thread_num is not None:
            self.open_thread(ref, thread_num, focus_num=focus)

    @work
    async def action_new_thread(self) -> None:
        ref = self.ref if self.mode == "board" else None
        sess = self._writable(ref)
        if not sess:
            return
        res = await self.push_screen_wait(Compose(f"New thread on {ref.label} as {sess.username}"))
        if not res:
            return
        r = await self._guard(core.post(self.acct, ref, res[0], res[1]))
        if r:
            self.notify(f"posted #{r.article_num}")
            await self._after_write(ref, r.article_num, None)

    @work
    async def action_reply(self) -> None:
        if not self.thread:
            self.notify("open a thread first", severity="warning")
            return
        ref = self.thread.ref
        sess = self._writable(ref)
        if not sess:
            return
        item = self._focused_item()
        v = item.view
        quote = "\n".join("> " + ln for ln in (item.body or "").splitlines()[:12])
        subj = v.subject if v.subject.lower().startswith("re:") else "Re: " + v.subject
        res = await self.push_screen_wait(
            Compose(f"Reply to #{v.article_num} as {sess.username}", subj, quote + "\n\n" if quote else "")
        )
        if not res:
            return
        r = await self._guard(core.reply(self.acct, ref, v.article_num, res[1], res[0]))
        if r:
            self.notify(f"replied as #{r.article_num}")
            await self._after_write(ref, self.thread.root.article_num, r.article_num)

    @work
    async def action_edit(self) -> None:
        item = self._focused_item()
        if not item:
            self.notify("open a thread and focus your post first", severity="warning")
            return
        ref = self.thread.ref
        sess = self._writable(ref)
        if not sess:
            return
        v = item.view
        res = await self.push_screen_wait(Compose(f"Edit #{v.article_num} as {sess.username}", v.subject, item.body or ""))
        if not res:
            return
        if res[0] == v.subject and res[1].strip() == (item.body or "").strip():
            self.notify("no changes")
            return
        r = await self._guard(core.edit(self.acct, ref, v.article_num, res[1], res[0]))
        if r:
            self.notify(f"edited: #{v.article_num} is now #{r.article_num}")
            root = r.article_num if item.depth == 0 else self.thread.root.article_num
            await self._after_write(ref, root, r.article_num)

    @work
    async def action_delete(self) -> None:
        item = self._focused_item()
        if not item:
            self.notify("open a thread and focus your post first", severity="warning")
            return
        ref = self.thread.ref
        if not self._writable(ref):
            return
        v = item.view
        if not await self.push_screen_wait(YesNo(f"Delete #{v.article_num} “{v.subject}”?")):
            return
        r = await self._guard(core.delete(self.acct, ref, v.article_num))
        if r:
            self.notify(f"deleted #{v.article_num}")
            if item.depth == 0:
                self.thread = None
                await self._clear_view("deleted")
                await self._after_write(ref, None, None)
            else:
                await self._after_write(ref, self.thread.root.article_num, None)
