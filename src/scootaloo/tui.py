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


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------


class ScootalooApp(App):
    TITLE = "scootaloo"
    CSS = """
    #panes { height: 1fr; }
    #boards { width: 30; height: 1fr; border-right: solid $primary-darken-2; }
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
    Ask, TrustKey { align: center middle; }
    .dialog { width: 80; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
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
        Binding("n", "new_thread", "New thread"),
        Binding("c", "reply", "Reply"),
        Binding("o", "connect", "Connect"),
        Binding("i", "identity", "Identity"),
        Binding("ctrl+l", "switch_user", "Switch user"),
    ]

    def __init__(self, user: str | None = None, password: str | None = None,
                 gateway_store: bool = False, relay: str | None = None):
        super().__init__()
        self._user, self._password, self._gateway, self._relay = user, password, gateway_store, relay
        self.acct: Account | None = None
        self.ref: core.BoardRef | None = None
        self.thread: core.Thread | None = None

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
        t.add_column("#", key="num", width=5)
        t.add_column("subject", key="subject", width=28)
        t.add_column("author", key="author", width=14)
        t.add_column("date", key="when", width=5)
        self.start()
        self.set_interval(60, self._auto_refresh)

    def set_status(self) -> None:
        if not self.acct:
            self.query_one("#status", Static).update("not logged in")
            return
        parts = [self.acct.name]
        if self.ref:
            parts.append(self.ref.label + ("" if self.ref.is_home else f" via {self.ref.via}"))
            try:
                sess = core.writable(self.acct, self.ref)
                parts.append(f"posting as {sess.username}")
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
        self.acct, self.ref, self.thread = acct, None, None
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
        relay = self.ref.via if self.ref else (core.active_relay(self.acct) or {}).get("origin")
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

    async def load_boards(self) -> None:
        if not self.acct:
            return
        refs = await self._guard(core.all_boards(self.acct))
        if refs is None:
            return
        tree = self.query_one("#boards", Tree)
        tree.clear()
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
                style = "dim" if ref.closed else ("magenta" if ref.name.startswith("~") else "")
                leaf = node.add_leaf(Text(ref.name, style=style), data=ref)
                if first is None and ref.is_home and ref.name == "general":
                    first = leaf
                if first is None and ref.is_home and not ref.name.startswith("~"):
                    first = leaf
        tree.focus()
        if self.ref:
            keep = next((leaf for n in tree.root.children for leaf in n.children if leaf.data == self.ref), None)
            first = keep or first
        if first is not None:
            tree.move_cursor(first)
            if self.ref != first.data:
                self.open_board(first.data)

    @on(Tree.NodeSelected, "#boards")
    def board_selected(self, ev: Tree.NodeSelected) -> None:
        if isinstance(ev.node.data, core.BoardRef):
            self.open_board(ev.node.data)

    @work(exclusive=True, group="threads")
    async def open_board(self, ref: core.BoardRef) -> None:
        if ref != self.ref:
            self.thread = None
            await self._clear_view("pick a thread")
        self.ref = ref
        self.set_status()
        rows = await self._guard(core.articles(self.acct, ref, 100))
        t = self.query_one("#threads", DataTable)
        t.clear()
        for a in rows or []:
            subj = ("📌 " if a.pin_state == "pinned" else "") + (a.subject or "")
            subj = subj if len(subj) <= 28 else subj[:27] + "…"
            t.add_row(
                str(a.article_num), subj, core.author_label(a),
                datetime.fromtimestamp(a.created_at).strftime("%m-%d"), key=str(a.article_num),
            )

    async def _clear_view(self, msg: str) -> None:
        view = self.query_one("#view", VerticalScroll)
        await view.remove_children()
        await view.mount(Static(msg, id="empty"))

    @on(DataTable.RowSelected, "#threads")
    def thread_selected(self, ev: DataTable.RowSelected) -> None:
        self.open_thread(int(ev.row_key.value))

    @work(exclusive=True, group="view")
    async def open_thread(self, num: int, focus_num: int | None = None) -> None:
        ref = self.ref
        await self._clear_view("loading…")
        th = await self._guard(core.thread(self.acct, ref, num))
        view = self.query_one("#view", VerticalScroll)
        await view.remove_children()
        if th is None or ref != self.ref:
            return
        self.thread = th
        posts = [Post(item) for item in th.items]
        await view.mount_all(posts)
        if th.truncated:
            await view.mount(Static("thread truncated at 200 articles", id="empty"))
        target = next((p for p in posts if p.item.view.article_num == focus_num), None)
        if target:
            target.focus()
            view.scroll_to_widget(target)
        else:
            view.scroll_home(animate=False)

    def action_refresh(self) -> None:
        if not self.acct:
            return
        self.run_worker(self.load_boards())
        if self.ref:
            self.open_board(self.ref)
        if self.thread:
            self.open_thread(self.thread.root.article_num)

    def _auto_refresh(self) -> None:
        if self.acct and self.ref and not isinstance(self.screen, (ModalScreen, Login)):
            self.open_board(self.ref)

    # -- writing -------------------------------------------------------------

    def _writable(self) -> core.Session | None:
        if not self.acct or not self.ref:
            return None
        try:
            return core.writable(self.acct, self.ref)
        except core.ScootalooError as e:
            self.notify(str(e), severity="warning", timeout=8)
            return None

    @work
    async def action_new_thread(self) -> None:
        sess = self._writable()
        if not sess:
            return
        res = await self.push_screen_wait(Compose(f"New thread on {self.ref.label} as {sess.username}"))
        if not res:
            return
        r = await self._guard(core.post(self.acct, self.ref, res[0], res[1]))
        if r:
            self.notify(f"posted #{r.article_num}")
            self.open_board(self.ref)
            self.open_thread(r.article_num)

    @work
    async def action_reply(self) -> None:
        if self.ref and not self.thread:
            self.notify("open a thread first", severity="warning")
            return
        sess = self._writable()
        if not sess or not self.thread:
            return
        focused = self.focused
        item = focused.item if isinstance(focused, Post) else self.thread.items[0]
        v = item.view
        quote = "\n".join("> " + ln for ln in (item.body or "").splitlines()[:12])
        subj = v.subject if v.subject.lower().startswith("re:") else "Re: " + v.subject
        res = await self.push_screen_wait(
            Compose(f"Reply to #{v.article_num} as {sess.username}", subj, quote + "\n\n" if quote else "")
        )
        if not res:
            return
        r = await self._guard(core.reply(self.acct, self.ref, v.article_num, res[1], res[0]))
        if r:
            self.notify(f"replied as #{r.article_num}")
            self.open_thread(self.thread.root.article_num, focus_num=r.article_num)
