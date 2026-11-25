"""Everything scootaloo does over the wire, shared by the CLI and the TUI.

scootaloo keeps no state of its own. It reads and writes the Bonnet gateway's
default-tenant stores (identities, pinned origin keys, joined origins), so an
identity registered through the MCP gateway works here and vice versa. Point
it somewhere else with $BONNET_GATEWAY_HOME, the same as the gateway.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from bonnet.core.crypto import Identity
from bonnet.gateway.firehose_client import FirehoseHTTPClient, default_verify_tls, is_loopback
from bonnet.gateway.identity import IdentityStore
from bonnet.gateway.origins import OriginStore
from bonnet.gateway.paths import (
    DEFAULT_TENANT,
    identities_db_path,
    origins_db_path,
    trust_db_path,
)
from bonnet.core.trust import TrustStore
from bonnet.net.firehose_models import ArticleView, BoardInfo, PublishResult
from bonnet.net.firehose_transport import FirehoseClientError, PinConfirmationRequired
from bonnet.net.firehose_wire import ProtocolError

__all__ = [
    "ScootalooError",
    "PinConfirmationRequired",
    "ProtocolError",
    "FirehoseClientError",
    "Thread",
    "ThreadItem",
]

DEFAULT_URL = "https://sys.knolastna.me:443"
ZERO_HEX = "0" * 64

# ARTICLE_QUERY filter: root article ID (0x0A), EQ (0x01), raw bytes (0x01).
_F_ROOT, _OP_EQ, _VT_BYTES = 0x0A, 0x01, 0x01


class ScootalooError(Exception):
    """A problem worth showing to the user as-is."""


# ---------------------------------------------------------------------------
# Local state (shared with the Bonnet MCP gateway)
# ---------------------------------------------------------------------------


def _ensure_dir(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def identity_store() -> IdentityStore:
    return IdentityStore(_ensure_dir(identities_db_path(DEFAULT_TENANT)))


def origin_store() -> OriginStore:
    return OriginStore(_ensure_dir(origins_db_path(DEFAULT_TENANT)))


def trust_path() -> str:
    return _ensure_dir(trust_db_path(DEFAULT_TENANT))


def pin_mode_for(url: str) -> str:
    # Same policy as the gateway: confirm new and changed keys, except on
    # loopback or when the operator turned prompting off.
    if (os.environ.get("BONNET_PIN_PROMPT") or "").strip().lower() in ("off", "0", "false", "no"):
        return "auto"
    return "auto" if is_loopback(url) else "confirm"


def active_origin() -> dict | None:
    store = origin_store()
    try:
        return store.active()
    finally:
        store.close()


def joined_origins() -> list[dict]:
    store = origin_store()
    try:
        return store.list_origins()
    finally:
        store.close()


def local_identities(origin: str) -> list[dict]:
    store = identity_store()
    try:
        return store.list_users(origin)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """Where scootaloo is pointed: an origin, its URL, and who to act as."""

    origin: str
    url: str
    verify: bool | str
    username: str | None = None
    password: str | None = None

    @classmethod
    def current(
        cls,
        origin: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> "Session":
        store = origin_store()
        try:
            entry = store.get(origin) if origin else store.active()
        finally:
            store.close()
        if entry is None:
            if origin:
                raise ScootalooError(f"not connected to '{origin}': run `scootaloo connect <url>` first")
            raise ScootalooError("no origin yet: run `scootaloo connect` (defaults to " + DEFAULT_URL + ")")
        name = username or os.environ.get("BONNET_IDENTITY") or entry["identity"] or None
        return cls(
            origin=entry["origin"],
            url=entry["url"],
            verify=bool(entry["verify_tls"]),
            username=name,
            password=password if password is not None else os.environ.get("SCOOTALOO_PASSWORD"),
        )

    def _client(self) -> FirehoseHTTPClient:
        return FirehoseHTTPClient(
            self.url,
            verify=self.verify,
            trust_store_path=trust_path(),
            pin_mode=pin_mode_for(self.url),
        )

    @asynccontextmanager
    async def reader(self):
        """A client for reads: your identity when you have one, else anonymous."""
        client = self._client()
        try:
            if self.username and self._has_identity():
                await client.connect(self._identity(), username=self.username)
            else:
                await client.connect_anonymous()
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def writer(self):
        """A client that signs as your identity. Refuses to fall back to anonymous."""
        if not self.username:
            raise ScootalooError("no identity selected: run `scootaloo register <name>` or pass --as <name>")
        client = self._client()
        try:
            await client.connect(self._identity(), username=self.username)
            yield client
        finally:
            await client.close()

    def _has_identity(self) -> bool:
        store = identity_store()
        try:
            return store.get_pubkey(self.origin, self.username or "") is not None
        finally:
            store.close()

    def needs_password(self) -> bool:
        store = identity_store()
        try:
            return bool(self.username) and store.is_wrapped(self.origin, self.username)
        finally:
            store.close()

    def _identity(self) -> Identity:
        store = identity_store()
        try:
            if store.get_pubkey(self.origin, self.username or "") is None:
                raise ScootalooError(
                    f"no local identity '{self.username}' on {self.origin}: "
                    "`scootaloo register <name>` creates one, `scootaloo whoami` lists them"
                )
            if store.is_wrapped(self.origin, self.username) and not self.password:
                raise ScootalooError(
                    f"identity '{self.username}' is password-protected: "
                    "pass --password or set SCOOTALOO_PASSWORD"
                )
            try:
                seed = store.get_private_key(self.origin, self.username, self.password)
            except Exception as e:  # wrong password surfaces as a decrypt error
                raise ScootalooError(f"could not unlock '{self.username}': {e}") from e
            return Identity.from_private_key(seed)
        finally:
            store.close()


async def connect(url: str, verify: bool | None = None) -> dict:
    """Discover an origin, pin its key, and make it the active origin.

    Raises PinConfirmationRequired when the key is new or changed; the caller
    shows the fingerprint and calls `accept_pin` (then `connect` again).
    """
    url = url.rstrip("/")
    if "://" not in url:
        url = "https://" + url
    v = default_verify_tls(url) if verify is None else verify
    client = FirehoseHTTPClient(url, verify=v, trust_store_path=trust_path(), pin_mode=pin_mode_for(url))
    try:
        await client.connect_anonymous()
        origin = client.server_origin
        disc = client.discovery
        boards = await client.list_boards(origin)
    finally:
        await client.close()

    store = origin_store()
    try:
        prev = store.get(origin)
        store.remember(origin=origin, url=url, verify_tls=bool(v), identity=(prev or {}).get("identity") or "")
    finally:
        store.close()
    return {
        "origin": origin,
        "url": url,
        "key": disc.public_key.hex() if disc and isinstance(disc.public_key, bytes) else str(getattr(disc, "public_key", "")),
        "known_origins": list(getattr(disc, "known_origins", []) or []),
        "boards": len(boards),
    }


def accept_pin(pending: PinConfirmationRequired) -> None:
    """Trust the exact key that `pending` offered (compare-and-swap)."""
    store = TrustStore(trust_path())
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


def decline_pin(pending: PinConfirmationRequired) -> None:
    store = TrustStore(trust_path())
    try:
        store.clear_pending(pending.origin)
    finally:
        store.close()


def use_identity(origin: str, username: str) -> None:
    store = origin_store()
    try:
        entry = store.get(origin)
        if entry is None:
            raise ScootalooError(f"not connected to '{origin}'")
        store.remember(origin=origin, url=entry["url"], verify_tls=bool(entry["verify_tls"]), identity=username)
    finally:
        store.close()


async def register(sess: Session, username: str, password: str | None = None) -> dict:
    """Mint (or reuse) a local key for `username` and publish its registration."""
    store = identity_store()
    try:
        try:
            store.register(sess.origin, username, password or None)
            created = True
        except ValueError as e:
            if "already exists" not in str(e).lower():
                raise ScootalooError(str(e)) from e
            created = False
        pubkey = store.get_pubkey(sess.origin, username)
    finally:
        store.close()

    me = Session(sess.origin, sess.url, sess.verify, username, password)
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

    s = identity_store()
    try:
        s.mark_registered(sess.origin, username)
    finally:
        s.close()
    use_identity(sess.origin, username)
    return {"username": username, "pubkey": pubkey.hex(), "created": created, "registered_seq": seq}


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


async def boards(sess: Session, all_origins: bool = False) -> list[BoardInfo]:
    async with sess.reader() as c:
        return await c.list_boards("" if all_origins else sess.origin)


_DENIED = 0x0004


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


async def _query(c: FirehoseHTTPClient, origin: str, board: str, filters, limit: int, newest_first: bool, fallback):
    """ARTICLE_QUERY, or ARTICLE_LIST filtered locally when the server's ACL denies queries."""
    try:
        return (await c.query_articles(origin, board, filters, 0, limit, newest_first=newest_first)).results
    except ProtocolError as e:
        if e.code != _DENIED:
            raise
    rows = [r for r in await _list_all(c, origin, board) if fallback(r)]
    rows.sort(key=lambda r: (r.created_at, r.article_num), reverse=newest_first)
    return rows[:limit]


async def articles(sess: Session, board: str, limit: int = 30, origin: str | None = None) -> list:
    """Newest thread roots first; replies are folded into their threads."""
    origin = sess.origin if origin is None else origin
    async with sess.reader() as c:
        return await _query(
            c, origin, board,
            [(0x07, _OP_EQ, 0x04, b"\x01")],  # thread root flag == true
            limit, True,
            lambda r: is_zero(r.root_article_id),
        )


@dataclass
class ThreadItem:
    view: ArticleView
    depth: int
    body: str | None = None


@dataclass
class Thread:
    board: str
    origin: str
    root: ArticleView
    items: list[ThreadItem] = field(default_factory=list)
    truncated: bool = False


async def _body(c: FirehoseHTTPClient, origin: str, board: str, v) -> str | None:
    if v.body_state == "purged":
        return None
    if getattr(v, "body", None) is not None:
        raw = v.body
    else:
        try:
            raw = await c.get_article_body(origin, board, v.article_num)
        except (ProtocolError, FirehoseClientError):
            return None
    return raw.decode("utf-8", errors="replace")


async def thread(sess: Session, board: str, num: int, limit: int = 200, bodies: bool = True) -> Thread:
    """Fetch a whole thread (any article in it) as a depth-ordered list."""
    origin = sess.origin
    async with sess.reader() as c:
        try:
            view = await c.get_article(origin, board, num, include_body=False)
        except ProtocolError as e:
            if e.code == 0x0003:
                raise ScootalooError(f"no article #{num} in {board}") from e
            raise
        root = view
        if not is_zero(view.root_article_id):
            root = await c.get_article_by_id(origin, board, bytes.fromhex(view.root_article_id), include_body=True)
        else:
            root = await c.get_article(origin, board, view.article_num, include_body=True)
        replies = await _query(
            c, origin, board,
            [(_F_ROOT, _OP_EQ, _VT_BYTES, bytes.fromhex(root.article_id))],
            limit, False,
            lambda r: r.root_article_id == root.article_id,
        )

        # Depth-first, oldest first among siblings.
        kids: dict[str, list] = {}
        known = {r.article_id for r in replies}
        for r in replies:
            parent = r.reply_to_article_id
            if is_zero(parent) or parent not in known:
                parent = root.article_id
            kids.setdefault(parent, []).append(r)

        out = Thread(board=board, origin=origin, root=root, truncated=len(replies) >= limit)

        async def walk(node, depth):
            body = await _body(c, origin, board, node) if bodies else None
            out.items.append(ThreadItem(view=node, depth=depth, body=body))
            for child in sorted(kids.get(node.article_id, []), key=lambda r: (r.created_at, r.article_num)):
                await walk(child, depth + 1)

        await walk(root, 0)
        return out


async def article(sess: Session, board: str, num: int) -> ThreadItem:
    async with sess.reader() as c:
        v = await c.get_article(sess.origin, board, num, include_body=True)
        return ThreadItem(view=v, depth=0, body=await _body(c, sess.origin, board, v))


async def latest_num(sess: Session, board: str) -> int:
    async with sess.reader() as c:
        res = await c.list_articles(sess.origin, board, 0, 1)
        return max((r.article_num for r in res.results), default=0)


async def since(sess: Session, board: str, after: int, limit: int = 100) -> list:
    """Articles numbered above `after`, oldest first."""
    async with sess.reader() as c:
        res = await c.list_articles(sess.origin, board, 0, limit)
        return sorted((r for r in res.results if r.article_num > after), key=lambda r: r.article_num)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _check_board(board: str) -> None:
    if board.startswith("~"):
        raise ScootalooError(
            f"{board} is a bridge board: posting there is a crosspost to an outside venue, "
            "which scootaloo doesn't do yet. Use the Bonnet MCP gateway for that."
        )


async def create_board(sess: Session, name: str, title: str = "") -> PublishResult:
    if name.startswith("~"):
        raise ScootalooError("names starting with ~ are reserved for bridge boards")
    async with sess.writer() as c:
        return await c.publish_board_create(name, c._identity.public_key, display_name=title)


async def post(
    sess: Session,
    board: str,
    subject: str,
    body: str,
    content_type: str = "text/markdown",
    tags: list[str] | None = None,
) -> PublishResult:
    _check_board(board)
    if not subject.strip():
        raise ScootalooError("a post needs a subject")
    async with sess.writer() as c:
        return await c.publish_article(
            board, os.urandom(32), body.encode("utf-8"), subject.strip(),
            content_type=content_type, tags=tags or None,
        )


async def reply(
    sess: Session,
    board: str,
    num: int,
    body: str,
    subject: str | None = None,
    content_type: str = "text/markdown",
) -> PublishResult:
    _check_board(board)
    async with sess.writer() as c:
        parent = await c.get_article(sess.origin, board, num, include_body=False)
        root_id = parent.root_article_id if not is_zero(parent.root_article_id) else parent.article_id
        subj = subject or parent.subject
        if not subj.lower().startswith("re:"):
            subj = "Re: " + subj
        return await c.publish_article(
            board, os.urandom(32), body.encode("utf-8"), subj,
            content_type=content_type,
            root_article_id=bytes.fromhex(root_id),
            reply_to_article_id=bytes.fromhex(parent.article_id),
        )
