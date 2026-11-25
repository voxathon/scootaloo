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
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

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


def body_cache(acct: Account):
    from scootaloo.bodycache import BodyCache

    return BodyCache(acct.stores.cache_path)


async def _body(c: FirehoseHTTPClient, ref: BoardRef, v, cache=None) -> tuple[str | None, str]:
    """An article's body as text, from the cache when possible; (None, reason) when it can't be had."""
    if v.body_state == "purged":
        return None, "purged"
    if getattr(v, "body_size", 1) == 0:
        return "", ""
    raw = getattr(v, "body", None)
    if raw is None and cache is not None:
        raw = cache.get(v.body_hash)
    if raw is None:
        try:
            raw = await c.get_article_body(ref.origin, ref.name, v.article_num)
        except PinConfirmationRequired as p:
            return None, f"stored on {p.origin}, whose key you haven't accepted: `scootaloo connect` to it to read"
        except (ProtocolError, FirehoseClientError, OSError) as e:
            return None, f"unavailable ({getattr(e, 'detail', None) or e})"
    if cache is not None:
        cache.put(v.body_hash, raw)
    return raw.decode("utf-8", errors="replace"), ""


async def thread(acct: Account, ref: BoardRef, num: int, limit: int = 200, bodies: bool = True) -> Thread:
    """A whole thread (from any article in it), as a depth-first list. Bodies come from the cache when held."""
    with body_cache(acct) as cache:
        return await _thread(acct, ref, num, limit, bodies, cache)


async def _thread(acct, ref, num, limit, bodies, cache) -> Thread:
    async with session_for(acct, ref).reader() as c:
        try:
            view = await c.get_article(ref.origin, ref.name, num, include_body=False)
        except ProtocolError as e:
            if e.code == _MISSING:
                raise ScootalooError(f"no article #{num} in {ref.label}") from e
            raise
        if not is_zero(view.root_article_id):
            root = await c.get_article_by_id(ref.origin, ref.name, bytes.fromhex(view.root_article_id), include_body=False)
        else:
            root = view
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
            body, note = await _body(c, ref, node, cache) if bodies else (None, "")
            out.items.append(ThreadItem(view=node, depth=depth, body=body, note=note))
            for child in sorted(kids.get(node.article_id, []), key=lambda r: (r.created_at, r.article_num)):
                await walk(child, depth + 1)

        await walk(root, 0)
        return out


async def article(acct: Account, ref: BoardRef, num: int) -> ThreadItem:
    with body_cache(acct) as cache:
        async with session_for(acct, ref).reader() as c:
            v = await c.get_article(ref.origin, ref.name, num, include_body=False)
            body, note = await _body(c, ref, v, cache)
            return ThreadItem(view=v, depth=0, body=body, note=note)


async def preview(acct: Account, ref: BoardRef, row) -> str | None:
    """The body of a listed article, for skimming: the cache first, else one fetch (which fills the cache)."""
    with body_cache(acct) as cache:
        hit = cache.get(row.body_hash)
        if hit is not None:
            return hit.decode("utf-8", errors="replace")
        async with session_for(acct, ref).reader() as c:
            body, _ = await _body(c, ref, row, cache)
            return body


async def num_for_id(acct: Account, ref: BoardRef, article_id: str) -> int:
    """The article number on `ref` of an article ID (to jump from metadata to a post)."""
    async with session_for(acct, ref).reader() as c:
        try:
            v = await c.get_article_by_id(ref.origin, ref.name, bytes.fromhex(article_id), include_body=False)
        except ProtocolError as e:
            if e.code == _MISSING:
                raise ScootalooError(f"no article {article_id[:16]}… on {ref.label}") from e
            raise
        return v.article_num


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


# ---------------------------------------------------------------------------
# Board index: what's on each board, refetched only when its origin's log moved
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    ref: BoardRef
    rows: list  # every live article on the board, oldest first
    head_seq: int | None


class Index:
    """In-memory board contents, keyed by BoardRef.

    `refresh` asks each origin for its log head (one tiny request) and only
    re-lists a board when that head has moved since the last look. Any board
    on an origin whose head can't be read is simply re-listed.
    """

    def __init__(self) -> None:
        self.snaps: dict[BoardRef, Snapshot] = {}

    async def refresh(self, acct: Account, refs, force: bool = False) -> set[BoardRef]:
        """Bring `refs` up to date. Returns the boards whose contents changed."""
        changed: set[BoardRef] = set()
        by_via: dict[str, list[BoardRef]] = {}
        for r in refs:
            by_via.setdefault(r.via, []).append(r)
        for via, group in by_via.items():
            try:
                async with Session.open(acct, via).reader() as c:
                    heads: dict[str, int | None] = {}
                    for origin in {r.origin for r in group}:
                        try:
                            heads[origin] = (await c.get_head(origin)).latest_origin_seq
                        except (ProtocolError, FirehoseClientError):
                            heads[origin] = None
                    for ref in group:
                        head = heads[ref.origin]
                        old = self.snaps.get(ref)
                        if not force and old is not None and head is not None and old.head_seq == head:
                            continue
                        try:
                            rows = await _list_all(c, ref.origin, ref.name)
                        except ProtocolError:
                            continue
                        rows.sort(key=lambda r: r.article_num)
                        if old is None or [(r.article_num, r.visibility, r.subject) for r in old.rows] != [
                            (r.article_num, r.visibility, r.subject) for r in rows
                        ]:
                            changed.add(ref)
                        self.snaps[ref] = Snapshot(ref, rows, head)
            except (ProtocolError, FirehoseClientError, OSError):
                continue
        return changed

    def rows(self, ref: BoardRef) -> list:
        snap = self.snaps.get(ref)
        return snap.rows if snap else []


def thread_key(row) -> str:
    """The article ID of the thread a row belongs to."""
    return row.article_id if is_zero(row.root_article_id) else row.root_article_id


# ---------------------------------------------------------------------------
# Unread and inbox
# ---------------------------------------------------------------------------


def read_state(acct: Account):
    from scootaloo.readstate import ReadState

    return ReadState(acct.stores.state_path)


def unread(acct: Account, index: Index, refs=None) -> dict[BoardRef, set[int]]:
    """Unread article numbers per board, for boards the index holds."""
    out = {}
    with read_state(acct) as st:
        for ref in refs if refs is not None else list(index.snaps):
            rows = index.rows(ref)
            out[ref] = st.unread(ref.origin, ref.name, [r.article_num for r in rows])
    return out


def unread_threads(index: Index, ref: BoardRef, unread_nums: set[int]) -> dict[str, int]:
    """Unread count per thread (keyed by root article ID)."""
    counts: dict[str, int] = {}
    for r in index.rows(ref):
        if r.article_num in unread_nums:
            k = thread_key(r)
            counts[k] = counts.get(k, 0) + 1
    return counts


def mark_read(acct: Account, ref: BoardRef, nums) -> None:
    with read_state(acct) as st:
        st.mark(ref.origin, ref.name, nums)


def mark_board_read(acct: Account, index: Index, ref: BoardRef) -> None:
    with read_state(acct) as st:
        st.mark_all(ref.origin, ref.name, [r.article_num for r in index.rows(ref)])


def my_keys(acct: Account) -> set[str]:
    """Every public key this user holds, on any relay."""
    s = acct.stores.identities()
    try:
        return {r["public_key"] for r in s.list_users()}
    finally:
        s.close()


@dataclass
class InboxItem:
    ref: BoardRef
    row: object  # the reply
    parent: object  # your article it answers
    unread: bool


def inbox(acct: Account, index: Index) -> list[InboxItem]:
    """Replies to your articles, by anyone else, on every board the index holds. Newest first."""
    mine = my_keys(acct)
    unread_by = unread(acct, index)
    items = []
    for ref, snap in index.snaps.items():
        own = {r.article_id: r for r in snap.rows if r.author_pubkey in mine}
        if not own:
            continue
        for r in snap.rows:
            parent = own.get(r.reply_to_article_id)
            if parent is not None and r.author_pubkey not in mine:
                items.append(InboxItem(ref, r, parent, r.article_num in unread_by.get(ref, set())))
    items.sort(key=lambda i: (i.row.created_at, i.row.article_num), reverse=True)
    return items


# ---------------------------------------------------------------------------
# Editing and deleting your own articles
# ---------------------------------------------------------------------------


def _my_key_on(sess: Session) -> str:
    s = sess.acct.stores.identities()
    try:
        pub = s.get_pubkey(sess.origin, sess.username or "")
    finally:
        s.close()
    return pub.hex() if pub else ""


async def _own_article(c, sess: Session, ref: BoardRef, num: int):
    try:
        v = await c.get_article(ref.origin, ref.name, num, include_body=True)
    except ProtocolError as e:
        if e.code == _MISSING:
            raise ScootalooError(f"no article #{num} in {ref.label}") from e
        raise
    if v.author_pubkey != _my_key_on(sess):
        who = v.author_username or v.author_pubkey[:12]
        raise ScootalooError(f"#{num} is {who}'s, not {sess.username}'s: you can only change your own posts")
    if v.visibility != "active":
        raise ScootalooError(f"#{num} is {v.visibility}; only a live article can be changed")
    return v


async def edit(
    acct: Account, ref: BoardRef, num: int, body: str, subject: str | None = None, username: str | None = None,
) -> PublishResult:
    """Supersede your article #num: a new article replaces it in place, keeping its thread position."""
    sess = writable(acct, ref, username)
    async with sess.writer() as c:
        old = await _own_article(c, sess, ref, num)
        return await c.publish_article(
            ref.name, os.urandom(32), body.encode("utf-8"), (subject or old.subject).strip(),
            content_type=old.content_type or "text/markdown",
            tags=[t for t in (old.tags or "").split(",") if t] or None,
            root_article_id=None if is_zero(old.root_article_id) else bytes.fromhex(old.root_article_id),
            reply_to_article_id=None if is_zero(old.reply_to_article_id) else bytes.fromhex(old.reply_to_article_id),
            supersedes_article_id=bytes.fromhex(old.article_id),
        )


async def current_body(acct: Account, ref: BoardRef, num: int) -> tuple[str, str]:
    """(subject, body) of an article, for pre-filling an editor."""
    item = await article(acct, ref, num)
    return item.view.subject, item.body or ""


async def delete(acct: Account, ref: BoardRef, num: int, reason: str = "", username: str | None = None) -> PublishResult:
    """Cancel your article #num. Bonnet keeps the record; the article stops showing."""
    sess = writable(acct, ref, username)
    async with sess.writer() as c:
        old = await _own_article(c, sess, ref, num)
        try:
            return await c.publish_cancel("", ref.origin, ref.name, bytes.fromhex(old.article_id), reason)
        except ProtocolError as e:
            if e.code == _DENIED:
                raise ScootalooError(
                    f"{ref.origin} doesn't let users delete posts (its ACL doesn't grant "
                    "bonnet.article.cancel). You can still edit it."
                ) from e
            raise


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass
class Hit:
    ref: BoardRef
    article_num: int
    subject: str
    author: str
    created_at: int
    excerpt: str = ""


async def search(acct: Account, refs, query: str, limit: int = 50, index: Index | None = None) -> list[Hit]:
    """Search subjects (and bodies, where the server indexes them) on each board in `refs`.

    Where a server's ACL refuses ARTICLE_SEARCH, falls back to matching
    subjects and author names in the board listing, case-insensitively.
    """
    q = query.strip()
    if not q:
        raise ScootalooError("search for what?")
    hits: list[Hit] = []
    by_via: dict[str, list[BoardRef]] = {}
    for r in refs:
        by_via.setdefault(r.via, []).append(r)
    for via, group in by_via.items():
        async with Session.open(acct, via).reader() as c:
            body_search = "bonnet.per-board-body-search" in (getattr(c.discovery, "capabilities", None) or [])
            for ref in group:
                found: dict[int, Hit] = {}
                try:
                    queries = [(q, "")] + ([("", q)] if body_search else [])
                    for meta, body in queries:
                        res = await c.search_articles(ref.origin, ref.name, meta, body, 0, limit)
                        for r in res.results:
                            found.setdefault(r.article_num, Hit(
                                ref, r.article_num, r.subject, author_label(r), r.created_at, r.excerpt or "",
                            ))
                except ProtocolError as e:
                    if e.code != _DENIED:
                        raise
                    rows = index.rows(ref) if index and ref in index.snaps else await _list_all(c, ref.origin, ref.name)
                    needle = q.casefold()
                    for r in rows:
                        if needle in (r.subject or "").casefold() or needle in (r.author_username or "").casefold():
                            found.setdefault(r.article_num, Hit(ref, r.article_num, r.subject, author_label(r), r.created_at))
                hits.extend(found.values())
    hits.sort(key=lambda h: (h.created_at, h.article_num), reverse=True)
    return hits[:limit]


# ---------------------------------------------------------------------------
# Verification: check the signed record behind an article view
# ---------------------------------------------------------------------------


@dataclass
class Verification:
    ref: BoardRef
    article_num: int
    event_id: str
    origin_seq: int
    author_key: str
    author_sig: str  # valid | invalid
    origin_sig: str  # valid | invalid | unverifiable
    body: str  # matches | mismatch | absent | unavailable
    view_matches: bool  # does the relay's summary agree with the signed record?
    mismatches: list[str]
    hops: list[dict]

    @property
    def ok(self) -> bool:
        return (
            self.author_sig == "valid"
            and self.origin_sig == "valid"
            and self.body in ("matches", "absent")
            and self.view_matches
            and all(h["signature_valid"] for h in self.hops)
        )


async def verify(acct: Account, ref: BoardRef, num: int) -> Verification:
    """Fetch the signed record behind article #num and check it here, not on the relay's word.

    - the author's signature over what they published,
    - the origin's countersignature (needs that origin's key history, which
      this client only has for origins you've accepted a key for),
    - the body bytes against the hash the author signed,
    - the relay's article view against the record it claims to summarize,
    - every relay's signed witness of handing the record on.
    """
    from bonnet.core.record import compute_body_hash, ZERO_HASH

    async with session_for(acct, ref).reader() as c:
        v = await c.get_article(ref.origin, ref.name, num, include_body=True)
        try:
            await c.refresh_epoch_cache(ref.origin)
        except Exception:  # noqa: BLE001 — best effort, as in Bonnet itself
            pass
        rec, _ = await c.get_event(ref.origin, bytes.fromhex(v.event_id))
        sigs = c.verify_record(rec)
        hops = await c.trace_event(ref.origin, bytes.fromhex(v.event_id))

        if rec.body_size == 0:
            body_state = "absent"
        else:
            raw = v.body
            if raw is None and v.body_state != "purged":
                try:
                    raw = await c.get_article_body(ref.origin, ref.name, num)
                except (ProtocolError, FirehoseClientError, OSError):
                    raw = None
            if raw is None:
                body_state = "unavailable"
            else:
                body_state = "matches" if compute_body_hash(raw) == rec.body_hash else "mismatch"

    mismatches = []
    if v.author_pubkey != rec.actor_pubkey.hex():
        mismatches.append("author key")
    if v.article_id != rec.article_id.hex():
        mismatches.append("article ID")
    if v.body_hash and v.body_hash != rec.body_hash.hex() and rec.body_hash != ZERO_HASH:
        mismatches.append("body hash")
    if rec.board != ref.name or rec.origin != ref.origin:
        mismatches.append("board or origin")
    subj = next((f for f in rec.metadata.fields if f.field_id == 1), None)
    if subj is not None:
        signed_subject = subj.value.decode("utf-8", errors="replace") if isinstance(subj.value, bytes) else str(subj.value)
        if signed_subject != v.subject:
            mismatches.append("subject")
    return Verification(
        ref=ref, article_num=num, event_id=v.event_id, origin_seq=rec.origin_seq,
        author_key=rec.actor_pubkey.hex(), author_sig=sigs["author"], origin_sig=sigs["origin"],
        body=body_state, view_matches=not mismatches, mismatches=mismatches, hops=hops,
    )


# ---------------------------------------------------------------------------
# Identity export / import (bonnet-identities archives; see idarchive.py)
# ---------------------------------------------------------------------------


def _relay_info(acct: Account, origin: str):
    from scootaloo.idarchive import Relay

    o = acct.stores.origins()
    try:
        entry = o.get(origin)
    finally:
        o.close()
    t = acct.stores.trust()
    try:
        pin = t.get_pin(origin)
    finally:
        t.close()
    if entry is None:
        return Relay(url="", verify_tls=True, origin_key=pin.hex() if pin else "")
    return Relay(url=entry["url"], verify_tls=bool(entry["verify_tls"]), origin_key=pin.hex() if pin else "")


def collect_identities(acct: Account, which: list[tuple[str, str]] | None = None) -> list:
    """Unlock identities for export: all of them, or the (origin, username) pairs named."""
    from scootaloo.idarchive import Entry

    s = acct.stores.identities()
    try:
        rows = s.list_users()
        if which is not None:
            wanted = set(which)
            missing = wanted - {(r["origin"], r["username"]) for r in rows}
            if missing:
                o, u = sorted(missing)[0]
                raise ScootalooError(f"no identity '{u}' on {o}")
            rows = [r for r in rows if (r["origin"], r["username"]) in wanted]
        out = []
        for r in rows:
            pw = acct.password if r["wrapped"] else None
            if r["wrapped"] and not pw:
                raise ScootalooError(f"'{r['username']}' on {r['origin']} is password-protected: pass --password")
            try:
                seed = s.get_private_key(r["origin"], r["username"], pw)
            except Exception as e:  # noqa: BLE001 — wrong password surfaces as a decrypt error
                raise ScootalooError(f"couldn't unlock '{r['username']}' on {r['origin']}: {e}") from e
            out.append(Entry(r["origin"], r["username"], seed.hex(), r["public_key"], bool(r["registered"]),
                             _relay_info(acct, r["origin"])))
        return out
    finally:
        s.close()


def find_identities(acct: Account, name: str, origin: str | None = None) -> list[tuple[str, str]]:
    """(origin, username) pairs matching a name (and origin, if given)."""
    s = acct.stores.identities()
    try:
        rows = s.list_users(origin)
    finally:
        s.close()
    return [(r["origin"], r["username"]) for r in rows if r["username"] == name]


@dataclass
class ImportPlan:
    entry: object  # idarchive.Entry
    action: str  # import | skip | conflict | corrupt
    reason: str = ""


def plan_import(acct: Account, entries: list) -> list[ImportPlan]:
    """What importing each entry would do, without doing it."""
    s = acct.stores.identities()
    try:
        held = {(r["origin"], r["username"]): r["public_key"] for r in s.list_users()}
    finally:
        s.close()
    plans, seen = [], set()
    for e in entries:
        key = (e.origin, e.username)
        if not e.intact:
            plans.append(ImportPlan(e, "corrupt", "its key doesn't re-derive its public key"))
        elif key in seen:
            plans.append(ImportPlan(e, "skip", "listed twice in what you're importing"))
        elif key not in held:
            plans.append(ImportPlan(e, "import"))
        elif held[key] == e.public_key_hex:
            plans.append(ImportPlan(e, "skip", "you already have it"))
        else:
            plans.append(ImportPlan(e, "conflict", "you hold a different key under this name here"))
        seen.add(key)
    return plans


@dataclass
class ImportResult:
    entry: object
    outcome: str  # imported | replaced | skipped | refused | rejected
    notes: list[str] = field(default_factory=list)
    backup: str = ""


def _backup_dir() -> str:
    from scootaloo.accounts import home

    d = os.path.join(home(), "backups")
    os.makedirs(d, exist_ok=True)
    return d


def _drop_identity(acct: Account, origin: str, username: str) -> None:
    s = acct.stores.identities()
    try:
        conn = s._get_conn()
        conn.execute("DELETE FROM identities WHERE origin = ? AND username = ?", (origin, username))
        conn.execute("DELETE FROM venue_accounts WHERE origin = ? AND username = ?", (origin, username))
        conn.commit()
    finally:
        s.close()


async def _join_relay(acct: Account, e, confirm_pin) -> list[str]:
    """Make sure the identity's relay is joined; pin silently only when its key is the archive's."""
    notes = []
    if e.origin in {r["origin"] for r in joined(acct)}:
        pin = _relay_info(acct, e.origin).origin_key
        if e.relay.origin_key and pin and pin != e.relay.origin_key:
            notes.append(
                f"the key you trust for {e.origin} differs from the one recorded in the archive; "
                "if you didn't expect a key change, check it"
            )
        return notes
    url = e.relay.url or f"https://{e.origin}"
    try:
        try:
            await connect(acct, url, None if e.relay.verify_tls else False)
            notes.append(f"joined {e.origin} ({url})")
            return notes
        except PinConfirmationRequired as p:
            if p.kind == "new" and e.relay.origin_key and p.fingerprint == e.relay.origin_key:
                accept_pin(acct, p)
                await connect(acct, url, None if e.relay.verify_tls else False)
                notes.append(f"joined {e.origin}; its key matches the archive's, so it was trusted without asking")
                return notes
            ok = await confirm_pin(p, url) if confirm_pin else False
            if not ok:
                decline_pin(acct, p)
                notes.append(f"didn't join {e.origin}: its key wasn't trusted. `scootaloo connect {url}` later")
                return notes
            accept_pin(acct, p)
            await connect(acct, url, None if e.relay.verify_tls else False)
            notes.append(f"joined {e.origin}")
            return notes
    except (ProtocolError, FirehoseClientError, OSError, ScootalooError) as err:
        notes.append(f"couldn't reach {e.origin} ({err}); the identity is imported, connect to it later")
    return notes


async def _liveness(acct: Account, e) -> list[str]:
    if e.origin not in {r["origin"] for r in joined(acct)}:
        return []
    sess = Session.open(acct, e.origin)
    sess.username = None  # ask anonymously: an unregistered key may not be allowed to look anything up
    try:
        async with sess.reader() as c:
            u = await c.get_user(e.origin, bytes.fromhex(e.public_key_hex))
    except ProtocolError as err:
        if err.code == 0x0001:
            return [f"{e.origin} doesn't know this key: it may never have been registered there"]
        return [f"couldn't check the key with {e.origin} ({err.detail or err})"]
    except (FirehoseClientError, OSError) as err:
        return [f"couldn't check the key with {e.origin} ({err})"]
    if u.revoked:
        return [f"{e.origin} has REVOKED this key: it can't post there"]
    if u.superseded_by:
        return [f"this key was rotated away on {e.origin} (successor {u.superseded_by[:16]}…): it can't post there"]
    if u.username and u.username != e.username:
        return [f"{e.origin} knows this key as '{u.username}', not '{e.username}'"]
    return []


async def import_identities(
    acct: Account, plans: list[ImportPlan], *, replace: bool = False,
    backup_passphrase: str | None = None, confirm_pin=None, check_live: bool = True,
) -> list[ImportResult]:
    """Carry out a plan. Never publishes anything: the origins already know these keys.

    `replace` lets a conflicting entry replace the identity held under its name,
    after writing the held one to <home>/backups as an archive locked with
    `backup_passphrase` (required). `confirm_pin(pending, url)` is awaited when a
    relay presents a key the archive doesn't vouch for.
    """
    from scootaloo import __version__
    from scootaloo.idarchive import build

    wrap = acct.password if acct.wraps_keys else None
    results = []
    for plan in plans:
        e = plan.entry
        if plan.action == "corrupt":
            results.append(ImportResult(e, "rejected", [plan.reason]))
            continue
        if plan.action == "skip":
            results.append(ImportResult(e, "skipped", [plan.reason]))
            continue
        res = ImportResult(e, "imported")
        if plan.action == "conflict":
            if not replace:
                results.append(ImportResult(e, "refused", [plan.reason + "; --replace swaps it (after a backup)"]))
                continue
            if not backup_passphrase:
                raise ScootalooError("replacing an identity needs a passphrase for the backup of the one it replaces")
            current = collect_identities(acct, [(e.origin, e.username)])
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = os.path.join(_backup_dir(), f"replaced-{e.username}@{e.origin}-{stamp}.bnid")
            with open(path, "xb") as f:
                f.write(build(current, backup_passphrase, f"scootaloo {__version__} (backup before replace)"))
            res.backup = path
            _drop_identity(acct, e.origin, e.username)
            res.outcome = "replaced"
        s = acct.stores.identities()
        try:
            s.register(e.origin, e.username, wrap, private_key=e.seed)
            if e.registered:
                s.mark_registered(e.origin, e.username)
        finally:
            s.close()
        res.notes += await _join_relay(acct, e, confirm_pin)
        if e.origin in {r["origin"] for r in joined(acct)}:
            o = acct.stores.origins()
            try:
                if not (o.get(e.origin) or {}).get("identity"):
                    use_identity(acct, e.origin, e.username)
            finally:
                o.close()
            if check_live:
                res.notes += await _liveness(acct, e)
        results.append(res)
    return results


def write_archive(path: str, data: bytes, overwrite: bool = False) -> str:
    """Write archive bytes atomically; never over an existing file unless asked."""
    from scootaloo.idarchive import EXTENSION

    if not path.endswith(EXTENSION):
        path += EXTENSION
    if os.path.exists(path) and not overwrite:
        raise ScootalooError(f"{path} already exists (pass --force to overwrite)")
    d = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Moderation (needs a moderator or admin role on the board's origin)
# ---------------------------------------------------------------------------

MOD_ACTIONS = ("pin", "unpin", "close", "reopen", "remove", "warn", "ban")


async def moderate(acct: Account, ref: BoardRef, num: int, action: str, reason: str = "",
                   rank: int = 0, days: int = 0, username: str | None = None) -> PublishResult:
    """A moderator action on article #num, or (warn/ban) on its author."""
    if action not in MOD_ACTIONS:
        raise ScootalooError(f"unknown action '{action}'")
    sess = writable(acct, ref, username)
    async with sess.writer() as c:
        try:
            v = await c.get_article(ref.origin, ref.name, num, include_body=False)
        except ProtocolError as e:
            if e.code == _MISSING:
                raise ScootalooError(f"no article #{num} in {ref.label}") from e
            raise
        aid = bytes.fromhex(v.article_id)
        try:
            if action == "pin":
                return await c.publish_pin("", ref.origin, ref.name, aid, rank)
            if action == "unpin":
                return await c.publish_unpin("", ref.origin, ref.name, aid)
            if action == "close":
                return await c.publish_thread_close("", ref.origin, ref.name, aid, reason)
            if action == "reopen":
                return await c.publish_thread_reopen("", ref.origin, ref.name, aid, reason)
            if action == "remove":
                return await c.publish_cancel("", ref.origin, ref.name, aid, reason)
            if not reason.strip():
                raise ScootalooError(f"a {action} needs a reason (--reason); the punished user sees it")
            key = bytes.fromhex(v.author_pubkey)
            if action == "warn":
                return await c.publish_punishment_warn(key, reason, board=ref.name)
            if days <= 0:
                return await c.publish_punishment_permaban(key, reason, board=ref.name)
            return await c.publish_punishment_ban(key, reason, int(time.time()) + days * 86400, board=ref.name)
        except ProtocolError as e:
            if e.code == _DENIED:
                raise ScootalooError(f"{ref.origin} refused: {sess.username} isn't a moderator for {ref.label}") from e
            if e.code == 0x0009:
                raise ScootalooError(f"nothing to do: {e.detail or e}") from e
            raise


async def lift_punishments(acct: Account, ref: BoardRef, num: int, reason: str = "",
                           username: str | None = None) -> int:
    """Revoke every active punishment of article #num's author issued on its origin. Returns how many."""
    sess = writable(acct, ref, username)
    async with sess.writer() as c:
        v = await c.get_article(ref.origin, ref.name, num, include_body=False)
        status = await c.get_ban_status(bytes.fromhex(v.author_pubkey))
        mine = [p for p in status.punishments if p.origin == ref.origin and p.event_id]
        if not mine:
            raise ScootalooError(f"{core_label(v)} has no punishments from {ref.origin}")
        try:
            for p in mine:
                await c.publish_punishment_revoke(bytes.fromhex(p.event_id), reason)
        except ProtocolError as e:
            if e.code == _DENIED:
                raise ScootalooError(f"{ref.origin} refused: {sess.username} isn't a moderator there") from e
            raise
        return len(mine)


def core_label(v) -> str:
    return author_label(v)


async def board_state(acct: Account, ref: BoardRef, close: bool, reason: str = "", username: str | None = None) -> PublishResult:
    """Close (no new posts) or reopen a whole board."""
    sess = writable(acct, ref, username)
    async with sess.writer() as c:
        try:
            if close:
                return await c.publish_board_close(ref.name, reason)
            return await c.publish_board_reopen(ref.name, reason)
        except ProtocolError as e:
            if e.code == _DENIED:
                raise ScootalooError(f"{ref.origin} refused: {sess.username} can't close or reopen {ref.label}") from e
            if e.code == 0x0009:
                raise ScootalooError(f"{ref.label} is already {'closed' if close else 'open'}") from e
            raise


# ---------------------------------------------------------------------------
# Reports: anyone registered may file one; reading the queue needs REPORT_LIST
# ---------------------------------------------------------------------------


async def report(acct: Account, ref: BoardRef, num: int, reason: str, username: str | None = None) -> PublishResult:
    """Report article #num (and its author) to the moderators of its origin."""
    if not reason.strip():
        raise ScootalooError("a report needs a reason: moderators read it")
    sess = writable(acct, ref, username)
    async with sess.writer() as c:
        v = await c.get_article(ref.origin, ref.name, num, include_body=False)
        try:
            return await c.publish_report(
                bytes.fromhex(v.author_pubkey), reason.strip(),
                target_origin=ref.origin, target_board=ref.name, target_article_id=bytes.fromhex(v.article_id),
            )
        except ProtocolError as e:
            if e.code == _DENIED:
                raise ScootalooError(f"{ref.origin} doesn't let {sess.username} file reports") from e
            raise


@dataclass
class Report:
    info: object  # ReportInfo
    reason: str
    relay: str


async def reports(acct: Account, relay: str | None = None, limit: int = 100) -> list[Report]:
    """The moderation queue of a relay (newest first), with each report's reason.

    Reasons are signed bodies, so they come from the body cache (checked
    against their hash) when held, and go into it when fetched.
    """
    sess = Session.open(acct, relay)
    with body_cache(acct) as cache:
        async with sess.reader() as c:
            try:
                rows = await c.list_reports(b"", limit, 0)
            except ProtocolError as e:
                if e.code == _DENIED:
                    raise ScootalooError(f"only moderators can read {sess.origin}'s reports") from e
                raise
            out = []
            for r in rows:
                reason = ""
                if r.body_size:
                    raw = cache.get(r.body_hash)
                    if raw is None:
                        try:
                            raw = await c.get_event_body(r.origin, bytes.fromhex(r.event_id))
                            cache.put(r.body_hash, raw)
                        except (ProtocolError, FirehoseClientError):
                            raw = None
                    reason = raw.decode("utf-8", "replace") if raw is not None else "(reason unavailable)"
                out.append(Report(r, reason, sess.origin))
    out.sort(key=lambda x: x.info.created_at, reverse=True)
    return out


async def log_heads(acct: Account, relay: str, origins) -> tuple:
    """The latest sequence of each origin's log, as seen through `relay`: changes whenever anything new lands."""
    heads = []
    async with Session.open(acct, relay).reader() as c:
        for o in sorted(set(origins) | {relay}):
            try:
                heads.append((o, (await c.get_head(o)).latest_origin_seq))
            except (ProtocolError, FirehoseClientError):
                heads.append((o, None))
    return tuple(heads)


async def can_read_reports(acct: Account, relay: str | None = None, board: str = "") -> bool:
    """Ask the relay (PERMISSIONS) whether this user may read its report queue."""
    try:
        async with Session.open(acct, relay).reader() as c:
            for scope in dict.fromkeys(("", board)):
                if "REPORT_LIST" in (await c.get_permissions(scope)).commands:
                    return True
    except (ProtocolError, FirehoseClientError, OSError, ScootalooError):
        return False
    return False
