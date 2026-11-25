"""The full-screen client: boards | threads | the open thread."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from scootaloo import core


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
            yield Static(Text(f"[body {v.body_state}]", style="dim italic"))
        elif "markdown" in (v.content_type or ""):
            yield Markdown(self.item.body)
        else:
            yield Static(Text(self.item.body))


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------


class ScootalooApp(App):
    TITLE = "scootaloo"
    CSS = """
    #panes { height: 1fr; }
    #boards { width: 24; height: 1fr; border-right: solid $primary-darken-2; }
    #threads { width: 1fr; height: 1fr; }
    #view { width: 1fr; border-left: solid $primary-darken-2; padding: 0 1; }
    #status { height: 1; background: $boost; color: $text-muted; padding: 0 1; }
    .post-meta { color: $text-muted; height: auto; }
    .post { height: auto; margin: 0 0 1 0; padding: 0 1; border-left: tall $primary; }
    .post:focus { border-left: tall $accent; background: $boost; }
    .post-head { height: auto; }
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
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("n", "new_thread", "New thread"),
        Binding("c", "reply", "Reply"),
        Binding("o", "connect", "Connect"),
        Binding("u", "identity", "Identity"),
    ]

    def __init__(self, origin: str | None = None, username: str | None = None, password: str | None = None):
        super().__init__()
        self._want = (origin, username, password)
        self.sess: core.Session | None = None
        self.board: str | None = None
        self.thread: core.Thread | None = None

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            with Horizontal(id="panes"):
                yield OptionList(id="boards")
                yield DataTable(id="threads", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="view"):
                    yield Static("pick a thread", id="empty")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#threads", DataTable)
        t.add_column("#", key="num", width=5)
        t.add_column("subject", key="subject", width=34)
        t.add_column("author", key="author", width=14)
        t.add_column("date", key="when", width=5)
        self.start()
        self.set_interval(60, self._auto_refresh)

    def set_status(self) -> None:
        if not self.sess:
            msg = "not connected · press o"
        else:
            who = self.sess.username or "anonymous (read-only) · press u"
            msg = f"{self.sess.origin} · {who}" + (f" · {self.board}" if self.board else "")
        self.query_one("#status", Static).update(msg)
        self.sub_title = self.sess.origin if self.sess else ""

    # -- startup / connecting ------------------------------------------------

    @work
    async def start(self) -> None:
        origin, username, password = self._want
        try:
            self.sess = core.Session.current(origin, username, password)
        except core.ScootalooError:
            self.sess = None
        if self.sess is None:
            self.set_status()
            await self._connect_flow(core.DEFAULT_URL)
            return
        if self.sess.needs_password() and not self.sess.password:
            pw = await self.push_screen_wait(Ask(f"password for {self.sess.username}", password=True))
            if pw is None:
                self.sess.username = None  # read anonymously instead
                self.notify("reading anonymously", severity="warning")
            self.sess.password = pw
        self.set_status()
        await self.load_boards()

    async def _connect_flow(self, default_url: str) -> None:
        url = await self.push_screen_wait(Ask("connect to (URL)", default_url))
        if not url:
            return
        try:
            try:
                r = await core.connect(url)
            except core.PinConfirmationRequired as p:
                ok = await self.push_screen_wait(TrustKey(p, url))
                if not ok:
                    core.decline_pin(p)
                    self.notify("key not trusted; nothing pinned", severity="warning")
                    return
                core.accept_pin(p)
                r = await core.connect(url)
        except Exception as e:  # noqa: BLE001 — show anything to the user
            self.notify(str(e), severity="error", timeout=8)
            return
        self.sess = core.Session.current(r["origin"])
        self.board, self.thread = None, None
        self.set_status()
        self.notify(f"connected to {r['origin']}")
        await self.load_boards()

    @work
    async def action_connect(self) -> None:
        await self._connect_flow(self.sess.url if self.sess else core.DEFAULT_URL)

    @work
    async def action_identity(self) -> None:
        if not self.sess:
            return
        ids = [i["username"] for i in core.local_identities(self.sess.origin)]
        hint = f" (have: {', '.join(ids)})" if ids else " (new name registers it)"
        name = await self.push_screen_wait(Ask("act as" + hint, self.sess.username or ""))
        if not name:
            return
        try:
            if name in ids:
                core.use_identity(self.sess.origin, name)
                self.sess = core.Session.current(self.sess.origin)
                if self.sess.needs_password():
                    self.sess.password = await self.push_screen_wait(Ask(f"password for {name}", password=True))
                self.notify(f"acting as {name}")
            else:
                r = await core.register(self.sess, name)
                self.sess = core.Session.current(self.sess.origin)
                self.notify(f"registered {r['username']}")
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
        if not self.sess:
            return
        rows = await self._guard(core.boards(self.sess))
        if rows is None:
            return
        ol = self.query_one("#boards", OptionList)
        ol.clear_options()
        for b in rows:
            label = Text(b.name, style="dim" if b.closed else ("magenta" if b.name.startswith("~") else ""))
            ol.add_option(Option(label, id=b.name))
        ol.focus()
        if rows and not self.board:
            first = next((b.name for b in rows if b.name == "general"), rows[0].name)
            ol.highlighted = [o.id for o in ol.options].index(first)
            self.open_board(first)

    @on(OptionList.OptionSelected, "#boards")
    def board_selected(self, ev: OptionList.OptionSelected) -> None:
        self.open_board(ev.option.id)

    @work(exclusive=True, group="threads")
    async def open_board(self, board: str) -> None:
        self.board = board
        self.set_status()
        rows = await self._guard(core.articles(self.sess, board, 100))
        t = self.query_one("#threads", DataTable)
        t.clear()
        for a in rows or []:
            subj = ("📌 " if a.pin_state == "pinned" else "") + (a.subject or "")
            subj = subj if len(subj) <= 34 else subj[:33] + "…"
            t.add_row(str(a.article_num), subj, core.author_label(a), datetime.fromtimestamp(a.created_at).strftime("%m-%d"), key=str(a.article_num))

    @on(DataTable.RowSelected, "#threads")
    def thread_selected(self, ev: DataTable.RowSelected) -> None:
        self.open_thread(int(ev.row_key.value))

    @work(exclusive=True, group="view")
    async def open_thread(self, num: int, focus_num: int | None = None) -> None:
        view = self.query_one("#view", VerticalScroll)
        await view.remove_children()
        await view.mount(Static("loading…", id="empty"))
        th = await self._guard(core.thread(self.sess, self.board, num))
        await view.remove_children()
        if th is None:
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
        if self.board:
            self.open_board(self.board)
        if self.thread:
            self.open_thread(self.thread.root.article_num)
        elif not self.board:
            self.run_worker(self.load_boards())

    def _auto_refresh(self) -> None:
        if self.board and not isinstance(self.screen, ModalScreen):
            self.open_board(self.board)

    # -- writing -------------------------------------------------------------

    def _can_write(self) -> bool:
        if not self.sess or not self.board:
            return False
        if not self.sess.username:
            self.notify("you're anonymous: press u to pick or register an identity", severity="warning")
            return False
        if self.board.startswith("~"):
            self.notify("bridge boards are crossposts; scootaloo doesn't do those yet", severity="warning")
            return False
        return True

    @work
    async def action_new_thread(self) -> None:
        if not self._can_write():
            return
        res = await self.push_screen_wait(Compose(f"New thread on {self.board} as {self.sess.username}"))
        if not res:
            return
        r = await self._guard(core.post(self.sess, self.board, res[0], res[1]))
        if r:
            self.notify(f"posted #{r.article_num}")
            self.open_board(self.board)
            self.open_thread(r.article_num)

    @work
    async def action_reply(self) -> None:
        if not self._can_write() or not self.thread:
            if self.sess and self.board and not self.thread:
                self.notify("open a thread first", severity="warning")
            return
        focused = self.focused
        item = focused.item if isinstance(focused, Post) else self.thread.items[0]
        v = item.view
        quote = "\n".join("> " + ln for ln in (item.body or "").splitlines()[:12])
        subj = v.subject if v.subject.lower().startswith("re:") else "Re: " + v.subject
        res = await self.push_screen_wait(
            Compose(f"Reply to #{v.article_num} as {self.sess.username}", subj, quote + "\n\n" if quote else "")
        )
        if not res:
            return
        r = await self._guard(core.reply(self.sess, self.board, v.article_num, res[1], res[0]))
        if r:
            self.notify(f"replied as #{r.article_num}")
            self.open_thread(self.thread.root.article_num, focus_num=r.article_num)
