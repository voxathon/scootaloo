"""Everything scootaloo does over the wire, shared by the CLI and the TUI.

Three ideas carry the rest:

- An **Account** (see accounts.py) owns a set of stores: identities, joined
  relays, accepted origin keys.
- A **Session** is one joined relay as seen by that account: where to dial it,
  and which identity (if any) to sign as there.
- A **BoardRef** names a board on some origin *and* the relay to read it
  through. A relay serves every origin it syncs, so a board on origin X can be
  read through relay Y. Posting only ever happens on the relay's own origin.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from bonnet.core.crypto import Identity
from bonnet.gateway.firehose_client import FirehoseHTTPClient, default_verify_tls, is_loopback
from bonnet.net.firehose_models import ArticleView, PublishResult
from bonnet.net.firehose_transport import FirehoseClientError, PinConfirmationRequired
from bonnet.net.firehose_wire import ProtocolError

from scootaloo.accounts import Account

__all__ = [
    "ScootalooError",
    "PinConfirmationRequired",
    "ProtocolError",
    "FirehoseClientError",
]

DEFAULT_URL = "https://sys.knolastna.me:443"
ZERO_HEX = "0" * 64

_F_ROOT, _F_IS_ROOT = 0x0A, 0x07
_OP_EQ = 0x01
_VT_BYTES, _VT_BOOL = 0x01, 0x04
_MISSING, _DENIED = 0x0003, 0x0004


class ScootalooError(Exception):
    """A problem worth showing to the user as-is."""


def pin_mode_for(url: str) -> str:
    # Same policy as the gateway: confirm new and changed keys, except on
    # loopback or when the operator turned prompting off.
    if (os.environ.get("BONNET_PIN_PROMPT") or "").strip().lower() in ("off", "0", "false", "no"):
        return "auto"
    return "auto" if is_loopback(url) else "confirm"


# ---------------------------------------------------------------------------
# Joined relays and identities (per account)
# ---------------------------------------------------------------------------


def joined(acct: Account) -> list[dict]:
    s = acct.stores.origins()
    try:
        return s.list_origins()
    finally:
        s.close()


def active_relay(acct: Account) -> dict | None:
    s = acct.stores.origins()
    try:
        return s.active()
    finally:
        s.close()


def identities(acct: Account, origin: str) -> list[dict]:
    s = acct.stores.identities()
    try:
        return s.list_users(origin)
    finally:
        s.close()


def use_identity(acct: Account, origin: str, username: str) -> None:
    s = acct.stores.origins()
    try:
        entry = s.get(origin)
        if entry is None:
            raise ScootalooError(f"not connected to '{origin}'")
        s.remember(
            origin=origin, url=entry["url"], verify_tls=bool(entry["verify_tls"]), identity=username, make_active=False
        )
    finally:
        s.close()


def set_active(acct: Account, origin: str) -> None:
    s = acct.stores.origins()
    try:
        if s.get(origin) is None:
            raise ScootalooError(f"not connected to '{origin}'")
        s.set_active(origin)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """One joined relay, as one account sees it."""

    acct: Account
    origin: str
    url: str
    verify: bool | str
    username: str | None = None

    @classmethod
    def open(cls, acct: Account, relay: str | None = None, username: str | None = None) -> "Session":
        s = acct.stores.origins()
        try:
            entry = s.get(relay) if relay else s.active()
        finally:
            s.close()
        if entry is None:
            if relay:
                raise ScootalooError(f"not connected to '{relay}': run `scootaloo connect <url>` first")
            raise ScootalooError("no relay yet: run `scootaloo connect` (defaults to " + DEFAULT_URL + ")")
        name = username or entry["identity"] or None
        return cls(acct, entry["origin"], entry["url"], bool(entry["verify_tls"]), name)

    @property
    def can_post(self) -> bool:
        return bool(self.username) and self._has_identity()

    def _client(self) -> FirehoseHTTPClient:
        return FirehoseHTTPClient(
            self.url,
            verify=self.verify,
            trust_store_path=self.acct.stores.trust_path,
            pin_mode=pin_mode_for(self.url),
        )

    @asynccontextmanager
    async def reader(self):
        """A client for reads: your identity when you have one here, else anonymous."""
        client = self._client()
        try:
            if self.can_post:
                await client.connect(self._identity(), username=self.username)
            else:
                await client.connect_anonymous()
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def writer(self):
        """A client that signs as your identity. Never falls back to anonymous."""
        if not self.username:
            raise ScootalooError(f"no identity on {self.origin}: run `scootaloo register <name>` or pass --as <name>")
        client = self._client()
        try:
            await client.connect(self._identity(), username=self.username)
            yield client
        finally:
            await client.close()

    def _has_identity(self) -> bool:
        s = self.acct.stores.identities()
        try:
            return s.get_pubkey(self.origin, self.username or "") is not None
        finally:
            s.close()

    def _identity(self) -> Identity:
        s = self.acct.stores.identities()
        try:
            if s.get_pubkey(self.origin, self.username or "") is None:
                raise ScootalooError(
                    f"no identity '{self.username}' on {self.origin}: "
                    "`scootaloo register <name>` creates one, `scootaloo whoami` lists them"
                )
            pw = self.acct.password if s.is_wrapped(self.origin, self.username) else None
            if s.is_wrapped(self.origin, self.username) and not pw:
                raise ScootalooError(f"identity '{self.username}' is password-protected: pass --password")
            try:
                seed = s.get_private_key(self.origin, self.username, pw)
            except Exception as e:  # a wrong password surfaces as a decrypt error
                raise ScootalooError(f"could not unlock '{self.username}': {e}") from e
            return Identity.from_private_key(seed)
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Connecting and registering
# ---------------------------------------------------------------------------


async def connect(acct: Account, url: str, verify: bool | None = None) -> dict:
    """Discover a relay, pin its key, and make it this account's active relay.

    Raises PinConfirmationRequired when the key is new or changed; the caller
    shows the fingerprint, calls `accept_pin`, then `connect` again.
    """
    url = url.rstrip("/")
    if "://" not in url:
        url = "https://" + url
    v = default_verify_tls(url) if verify is None else verify
    client = FirehoseHTTPClient(url, verify=v, trust_store_path=acct.stores.trust_path, pin_mode=pin_mode_for(url))
    try:
        await client.connect_anonymous()
        origin = client.server_origin
        disc = client.discovery
        boards = await client.list_boards("")
    finally:
        await client.close()

    s = acct.stores.origins()
    try:
        prev = s.get(origin)
        s.remember(origin=origin, url=url, verify_tls=bool(v), identity=(prev or {}).get("identity") or "")
    finally:
        s.close()
    known = sorted({b.origin for b in boards} | set(getattr(disc, "known_origins", []) or []))
    return {"origin": origin, "url": url, "known_origins": known, "boards": len(boards)}


def accept_pin(acct: Account, pending: PinConfirmationRequired) -> None:
    """Trust exactly the key that `pending` offered (compare-and-swap)."""
    store = acct.stores.trust()
    try:
        offer = store.get_pending(pending.origin)
        if offer is None or offer["publickey"] != pending.presented_key:
            raise ScootalooError("the key on offer changed; connect again to see the current one")
        pinned = store.get_pin(pending.origin)
        ok = (
            store.tofu_pin(pending.origin, pending.presented_key)
            if pinned is None
            else store.accept_rotation(pending.origin, pinned, pending.presented_key)
        )
        if not ok:
            raise ScootalooError("the pin moved while deciding; nothing was written. Connect again.")
        store.clear_pending(pending.origin)
    finally:
        store.close()


def decline_pin(acct: Account, pending: PinConfirmationRequired) -> None:
    store = acct.stores.trust()
    try:
        store.clear_pending(pending.origin)
    finally:
        store.close()


async def register(sess: Session, username: str, password: str | None = None) -> dict:
    """Mint (or reuse) a local key for `username` on the session's relay and register it.

    For a local user the key is always wrapped with that user's password. In
    gateway-store mode, `password` (optional) wraps it, as the gateway does.
    """
    acct = sess.acct
    wrap = acct.password if acct.wraps_keys else (password or None)
    if acct.wraps_keys and not wrap:
        raise ScootalooError("logged-in users always encrypt their keys; this account has no password in hand")
    s = acct.stores.identities()
    try:
        try:
            s.register(sess.origin, username, wrap)
            created = True
        except ValueError as e:
            if "already exists" not in str(e).lower():
                raise ScootalooError(str(e)) from e
            created = False
        pubkey = s.get_pubkey(sess.origin, username)
    finally:
        s.close()

    me = Session(acct, sess.origin, sess.url, sess.verify, username)
    if not acct.wraps_keys and password:
        me.acct = Account(acct.stores, acct.name, password, gateway=True)
    seq = None
    async with me.writer() as client:
        try:
            seq = (await client.publish_user_register(username, flags=0)).origin_seq
        except ProtocolError as refusal:
            try:
                existing = await client.get_user(sess.origin, pubkey)
            except ProtocolError:
                existing = None
            if existing is None or existing.username != username:
                raise ScootalooError(
                    f"{refusal.detail or refusal}: '{username}' belongs to a different key "
                    "on this origin. Pick another name (your new local key is kept)."
                ) from refusal

    s = acct.stores.identities()
    try:
        s.mark_registered(sess.origin, username)
    finally:
        s.close()
    use_identity(acct, sess.origin, username)
    return {"username": username, "pubkey": pubkey.hex(), "created": created, "registered_seq": seq}


# ---------------------------------------------------------------------------
# Boards across origins
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardRef:
    origin: str  # whose board
    name: str
    via: str  # the joined relay to read it through
    display_name: str = ""
    closed: bool = False

    @property
    def label(self) -> str:
        return self.name if self.origin == self.via else f"{self.name}@{self.origin}"

    @property
    def is_home(self) -> bool:
        """Only a relay's own boards accept posts from it."""
        return self.origin == self.via


async def all_boards(acct: Account, relay: str | None = None) -> list[BoardRef]:
    """Every board readable from the joined relays (or one relay), one row per (origin, board).

    When several relays serve the same origin, the origin's own relay wins,
    then the active relay, then whichever answered first.
    """
    relays = [r for r in joined(acct) if relay is None or r["origin"] == relay]
    if relay and not relays:
        raise ScootalooError(f"not connected to '{relay}'")
    active = (active_relay(acct) or {}).get("origin")
    out: dict[tuple[str, str], BoardRef] = {}
    errors = []
    for r in relays:
        sess = Session.open(acct, r["origin"])
        try:
            async with sess.reader() as c:
                rows = await c.list_boards("")
        except (ProtocolError, FirehoseClientError, OSError) as e:
            errors.append(f"{r['origin']}: {e}")
            continue
        for b in rows:
            o = b.origin or r["origin"]
            ref = BoardRef(o, b.name, r["origin"], b.display_name or "", bool(b.closed))
            prev = out.get((o, b.name))
            if prev is None or _rank(ref, active) < _rank(prev, active):
                out[(o, b.name)] = ref
    if errors and not out:
        raise ScootalooError("; ".join(errors))
    return sorted(out.values(), key=lambda b: (b.origin != b.via, b.origin, b.name))


def _rank(ref: BoardRef, active: str | None) -> int:
    if ref.origin == ref.via:
        return 0
    return 1 if ref.via == active else 2


async def resolve_board(acct: Account, board: str, origin: str | None = None, relay: str | None = None) -> BoardRef:
    """Turn what a user typed (`general`, `general@beta.test`, or --from) into a BoardRef."""
    if origin is None and "@" in board:
        board, origin = board.rsplit("@", 1)
    refs = [b for b in await all_boards(acct, relay) if b.name == board]
    if origin:
        refs = [b for b in refs if b.origin == origin]
        if not refs:
            raise ScootalooError(f"no board '{board}' on {origin} through your relays")
        return refs[0]
    if not refs:
        raise ScootalooError(f"no board '{board}' on any relay you've joined")
    sess_origin = relay or (active_relay(acct) or {}).get("origin")
    home = [b for b in refs if b.origin == sess_origin]
    if home:
        return home[0]
    if len({b.origin for b in refs}) > 1:
        names = ", ".join(sorted(f"{b.name}@{b.origin}" for b in refs))
        raise ScootalooError(f"'{board}' exists on several origins: {names}. Say which one.")
    return refs[0]


def session_for(acct: Account, ref: BoardRef, username: str | None = None) -> Session:
    return Session.open(acct, ref.via, username)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def is_zero(hex_id: str) -> bool:
    return not hex_id or hex_id == ZERO_HEX


def author_label(a) -> str:
    """Self-chosen names only mean something with author_check beside them."""
    name = a.author_username or a.author_pubkey[:12]
    check = getattr(a, "author_check", "unchecked")
    if check == "registry":
        return name
    if check == "retired":
        return f"{name} (retired key)"
    if check == "foreign":
        return f"{name}@{a.author_registrar}?"
    if check == "unregistered":
        return f"{name} (unverified)"
    return name


async def _list_all(c: FirehoseHTTPClient, origin: str, board: str, cap: int = 2000) -> list:
    """Every article on a board via ARTICLE_LIST, for servers that don't grant ARTICLE_QUERY."""
    rows, offset = [], 0
    while len(rows) < cap:
        page = (await c.list_articles(origin, board, offset, 500)).results
        rows.extend(page)
        if len(page) < 500:
            break
        offset += len(page)
    return rows


async def _query(c, origin, board, filters, limit, newest_first, fallback):
    """ARTICLE_QUERY, or ARTICLE_LIST filtered locally when the server's ACL denies queries."""
    try:
        return (await c.query_articles(origin, board, filters, 0, limit, newest_first=newest_first)).results
    except ProtocolError as e:
        if e.code != _DENIED:
            raise
    rows = [r for r in await _list_all(c, origin, board) if fallback(r)]
    rows.sort(key=lambda r: (r.created_at, r.article_num), reverse=newest_first)
    return rows[:limit]


async def articles(acct: Account, ref: BoardRef, limit: int = 30) -> list:
    """Thread roots on a board, newest first."""
    async with session_for(acct, ref).reader() as c:
        return await _query(
            c, ref.origin, ref.name, [(_F_IS_ROOT, _OP_EQ, _VT_BOOL, b"\x01")], limit, True,
            lambda r: is_zero(r.root_article_id),
        )


@dataclass
class ThreadItem:
    view: ArticleView
    depth: int
    body: str | None = None
    note: str = ""  # why the body is missing, when it is


@dataclass
class Thread:
    ref: BoardRef
    root: ArticleView
    items: list[ThreadItem] = field(default_factory=list)
    truncated: bool = False


async def _body(c: FirehoseHTTPClient, ref: BoardRef, v) -> tuple[str | None, str]:
    if v.body_state == "purged":
        return None, "purged"
    if getattr(v, "body", None) is not None:
        return v.body.decode("utf-8", errors="replace"), ""
    try:
        raw = await c.get_article_body(ref.origin, ref.name, v.article_num)
    except PinConfirmationRequired as p:
        return None, f"stored on {p.origin}, whose key you haven't accepted: `scootaloo connect` to it to read"
    except (ProtocolError, FirehoseClientError, OSError) as e:
        return None, f"unavailable ({getattr(e, 'detail', None) or e})"
    return raw.decode("utf-8", errors="replace"), ""


async def thread(acct: Account, ref: BoardRef, num: int, limit: int = 200, bodies: bool = True) -> Thread:
    """A whole thread (from any article in it), as a depth-first list."""
    async with session_for(acct, ref).reader() as c:
        try:
            view = await c.get_article(ref.origin, ref.name, num, include_body=False)
        except ProtocolError as e:
            if e.code == _MISSING:
                raise ScootalooError(f"no article #{num} in {ref.label}") from e
            raise
        if not is_zero(view.root_article_id):
            root = await c.get_article_by_id(ref.origin, ref.name, bytes.fromhex(view.root_article_id), include_body=True)
        else:
            root = await c.get_article(ref.origin, ref.name, view.article_num, include_body=True)
        replies = await _query(
            c, ref.origin, ref.name,
            [(_F_ROOT, _OP_EQ, _VT_BYTES, bytes.fromhex(root.article_id))], limit, False,
            lambda r: r.root_article_id == root.article_id,
        )
        kids: dict[str, list] = {}
        known = {r.article_id for r in replies}
        for r in replies:
            parent = r.reply_to_article_id
            if is_zero(parent) or parent not in known:
                parent = root.article_id
            kids.setdefault(parent, []).append(r)

        out = Thread(ref=ref, root=root, truncated=len(replies) >= limit)

        async def walk(node, depth):
            body, note = await _body(c, ref, node) if bodies else (None, "")
            out.items.append(ThreadItem(view=node, depth=depth, body=body, note=note))
            for child in sorted(kids.get(node.article_id, []), key=lambda r: (r.created_at, r.article_num)):
                await walk(child, depth + 1)

        await walk(root, 0)
        return out


async def article(acct: Account, ref: BoardRef, num: int) -> ThreadItem:
    async with session_for(acct, ref).reader() as c:
        v = await c.get_article(ref.origin, ref.name, num, include_body=True)
        body, note = await _body(c, ref, v)
        return ThreadItem(view=v, depth=0, body=body, note=note)


async def latest_num(acct: Account, ref: BoardRef) -> int:
    async with session_for(acct, ref).reader() as c:
        res = await c.list_articles(ref.origin, ref.name, 0, 1)
        return max((r.article_num for r in res.results), default=0)


async def since(acct: Account, ref: BoardRef, after: int, limit: int = 100) -> list:
    """Articles numbered above `after`, oldest first."""
    async with session_for(acct, ref).reader() as c:
        res = await c.list_articles(ref.origin, ref.name, 0, limit)
        return sorted((r for r in res.results if r.article_num > after), key=lambda r: r.article_num)


# ---------------------------------------------------------------------------
# Writing (only ever on a relay's own origin)
# ---------------------------------------------------------------------------


def writable(acct: Account, ref: BoardRef, username: str | None = None) -> Session:
    """The session to post on `ref` with, or a clear reason why you can't."""
    if ref.name.startswith("~"):
        raise ScootalooError(
            f"{ref.label} is a bridge board: posting there is a crosspost to an outside venue, "
            "which scootaloo doesn't do yet. Use the Bonnet MCP gateway for that."
        )
    if not ref.is_home:
        mine = {r["origin"] for r in joined(acct)}
        hint = (
            f"switch to it with `scootaloo --relay {ref.origin} ...`"
            if ref.origin in mine
            else f"connect to {ref.origin} and register there to post"
        )
        raise ScootalooError(f"{ref.label} lives on {ref.origin}, and you're reading it through {ref.via}: {hint}")
    sess = session_for(acct, ref, username)
    if not sess.username:
        raise ScootalooError(f"no identity on {ref.origin}: `scootaloo register <name>` first")
    return sess


async def create_board(sess: Session, name: str, title: str = "") -> PublishResult:
    if name.startswith("~"):
        raise ScootalooError("names starting with ~ are reserved for bridge boards")
    async with sess.writer() as c:
        return await c.publish_board_create(name, c._identity.public_key, display_name=title)


async def post(
    acct: Account, ref: BoardRef, subject: str, body: str,
    content_type: str = "text/markdown", tags: list[str] | None = None, username: str | None = None,
) -> PublishResult:
    sess = writable(acct, ref, username)
    if not subject.strip():
        raise ScootalooError("a post needs a subject")
    async with sess.writer() as c:
        return await c.publish_article(
            ref.name, os.urandom(32), body.encode("utf-8"), subject.strip(),
            content_type=content_type, tags=tags or None,
        )


async def reply(
    acct: Account, ref: BoardRef, num: int, body: str, subject: str | None = None,
    content_type: str = "text/markdown", username: str | None = None,
) -> PublishResult:
    sess = writable(acct, ref, username)
    async with sess.writer() as c:
        parent = await c.get_article(ref.origin, ref.name, num, include_body=False)
        root_id = parent.root_article_id if not is_zero(parent.root_article_id) else parent.article_id
        subj = subject or parent.subject
        if not subj.lower().startswith("re:"):
            subj = "Re: " + subj
        return await c.publish_article(
            ref.name, os.urandom(32), body.encode("utf-8"), subj,
            content_type=content_type,
            root_article_id=bytes.fromhex(root_id),
            reply_to_article_id=bytes.fromhex(parent.article_id),
        )
