"""Reading an origin's signed log: fetching, chain checking, plain-English lines, filtering, raw layout.

Every record arrives as the exact bytes the relay served (`raw`); everything
shown is decoded from those bytes, and the raw window shows them unchanged.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime

from bonnet.core.record import (
    ZERO_HASH,
    compute_event_hash,
    decode_record,
    encode_record,
    encode_unsigned_head,
    verify_head_signature,
)
from bonnet.net.firehose_wire import (
    _read_blob32,
    _read_u16,
    _read_witness_set,
    build_event_head,
    build_event_range,
    parse_event_head_response_raw,
    parse_response,
)

BATCH = 100

# -- categories ---------------------------------------------------------------

CATEGORIES = {
    "posts": ("bonnet.article",),
    "moderation": ("bonnet.article.cancel", "bonnet.article.restore", "bonnet.article.purge", "bonnet.article.pin",
                   "bonnet.article.unpin", "bonnet.thread.close", "bonnet.thread.reopen", "bonnet.report",
                   "bonnet.punishment.warn", "bonnet.punishment.ban", "bonnet.punishment.permaban",
                   "bonnet.punishment.revoke", "bonnet.punishment.ack", "bonnet.rule.publish", "bonnet.rule.revoke"),
    "users": ("bonnet.user.register", "bonnet.user.revoke", "bonnet.user.key.rotate"),
    "boards": ("bonnet.board.create", "bonnet.board.close", "bonnet.board.reopen", "bonnet.board.purge"),
    "keys": ("bonnet.origin.key.rotate", "bonnet.route.announce", "bonnet.route.withdraw"),
    "bridges": ("bonnet.bridge.link", "bonnet.bridge.observation", "bonnet.bridge.binding", "bonnet.bridge.unbind"),
}


def category(rec) -> str:
    for cat, kinds in CATEGORIES.items():
        if rec.kind in kinds:
            # a mirrored venue post is an article, but it's bridge traffic
            if cat == "posts" and _meta(rec, 0x0101) is not None:
                return "bridges"
            return cat
    return "other"


# -- metadata helpers ---------------------------------------------------------

META_NAMES = {
    1: "field 1", 2: "field 2", 3: "field 3", 4: "content type", 5: "root article", 6: "reply to",
    7: "supersedes", 0x0100: "bridge_version", 0x0101: "bridge_role", 0x0102: "venue", 0x0103: "channel",
    0x0104: "foreign_id", 0x0105: "foreign_author", 0x0109: "foreign_url",
}
KIND_FIELD1 = {
    "bonnet.article": "subject", "bonnet.board.create": "owner key", "bonnet.user.register": "name",
    "bonnet.user.revoke": "revoked key", "bonnet.user.key.rotate": "new key", "bonnet.report": "culprit key",
    "bonnet.punishment.warn": "punished key", "bonnet.punishment.ban": "punished key",
    "bonnet.punishment.permaban": "punished key", "bonnet.punishment.ack": "punishment event",
    "bonnet.article.pin": "rank", "bonnet.origin.key.rotate": "new origin key", "bonnet.rule.publish": "rule name",
    "bonnet.route.announce": "hostname",
}
KIND_FIELD2 = {"bonnet.article": "tags", "bonnet.user.register": "key", "bonnet.punishment.ban": "expires",
               "bonnet.user.key.rotate": "proof", "bonnet.origin.key.rotate": "proof", "bonnet.route.announce": "port"}
TYPES = {1: "bytes", 2: "text", 3: "u64", 4: "i64", 5: "bool", 6: "id list", 7: "text list"}


def meta_name(kind: str, fid: int) -> str:
    if fid == 1 and kind in KIND_FIELD1:
        return KIND_FIELD1[kind]
    if fid == 2 and kind in KIND_FIELD2:
        return KIND_FIELD2[kind]
    if fid == 3 and kind == "bonnet.user.register":
        return "flags"
    return META_NAMES.get(fid, f"field 0x{fid:04x}")


def meta_value(f) -> str:
    v = f.value
    if f.value_type == 2:
        return v.decode("utf-8", "replace")
    if f.value_type == 3:
        return str(int.from_bytes(v, "big"))
    if f.value_type == 4:
        return str(int.from_bytes(v, "big", signed=True))
    if f.value_type == 5:
        return "true" if v == b"\x01" else "false"
    if f.value_type == 7 and len(v) >= 2:
        out, n, off = [], int.from_bytes(v[:2], "big"), 2
        for _ in range(n):
            ln = int.from_bytes(v[off:off + 2], "big")
            out.append(v[off + 2:off + 2 + ln].decode("utf-8", "replace"))
            off += 2 + ln
        return ", ".join(out)
    return v.hex()


def _meta(rec, fid):
    return next((f for f in rec.metadata.fields if f.field_id == fid), None)


def _meta_text(rec, fid) -> str:
    f = _meta(rec, fid)
    return meta_value(f) if f is not None else ""


# -- events -------------------------------------------------------------------


@dataclass
class Event:
    raw: bytes  # exactly as served
    rec: object  # decoded Record
    witnesses: list
    chain: str = "unchecked"  # ok | break | gap | start | fork | unchecked
    canonical: bool = True  # re-encoding the decoded record gives back the served bytes
    sigs: dict = field(default_factory=dict)
    alts: list = field(default_factory=list)  # other records served for this same seq: the origin forked

    @property
    def seq(self) -> int:
        return self.rec.origin_seq

    @property
    def hash(self) -> bytes:
        return compute_event_hash(self.raw)


def parse_range(resp: bytes) -> list[Event]:
    """EVENT_RANGE, keeping each record's served bytes."""
    _, payload = parse_response(resp)
    count, off = _read_u16(payload, 0)
    out = []
    for _ in range(count):
        raw, off = _read_blob32(payload, off)
        witnesses, off = _read_witness_set(payload, off)
        rec = decode_record(raw)
        out.append(Event(raw, rec, witnesses, canonical=encode_record(rec) == raw))
    return out


async def fetch(client, origin: str, start: int, count: int = BATCH) -> list[Event]:
    events = parse_range(await client._send_command(build_event_range(origin, max(1, start), count)))
    for e in events:
        try:
            e.sigs = client.verify_record(e.rec)
        except Exception:  # noqa: BLE001 — a record we can't check is shown as such
            e.sigs = {"author": "invalid", "origin": "unverifiable"}
    return events


@dataclass
class HeadCheck:
    """The origin's signed head, checked here: where the log ends, and the hash it ends on."""

    seq: int
    hash: bytes
    sig: str  # valid | invalid | unverifiable (no key for that sequence: accept the origin's key)


async def fetch_head(client, origin: str) -> HeadCheck:
    """EVENT_HEAD, with its signature checked under the key that was the origin's at that sequence.

    The pubkey inside the head is the head's own claim, so it isn't what we check against.
    """
    head = parse_event_head_response_raw(await client._send_command(build_event_head(origin)))
    if head.origin != origin:
        return HeadCheck(head.latest_origin_seq, head.latest_event_hash, "invalid")
    key = client.origin_key_for_seq(origin, head.latest_origin_seq) if head.latest_origin_seq else None
    if key is None:
        sig = "unverifiable"
    else:
        sig = "valid" if verify_head_signature(key, encode_unsigned_head(head), head.origin_signature) else "invalid"
    return HeadCheck(head.latest_origin_seq, head.latest_event_hash, sig)


def merge(held: dict[int, Event], events: list[Event]) -> list[int]:
    """Add fetched events to what's held. A different record for a seq we already hold is kept beside it,
    never swapped in: two signed histories for one sequence is the thing a log reader most needs to see.

    Returns the sequences that newly turned out to be forked.
    """
    forked = []
    for e in events:
        old = held.get(e.seq)
        if old is None:
            held[e.seq] = e
        elif old.hash != e.hash and all(a.hash != e.hash for a in old.alts):
            old.alts.append(e)
            forked.append(e.seq)
    return forked


def check_chain(events: dict[int, Event]) -> None:
    """Mark every held event by whether its link to the one before checks out."""
    lowest = min(events, default=0)
    for seq, e in events.items():
        if seq == 1:
            e.chain = "ok" if e.rec.previous_event_hash == ZERO_HASH else "break"
        else:
            prev = events.get(seq - 1)
            if prev is None:
                # nothing below loaded yet, or a hole between two loaded stretches
                e.chain = "start" if seq == lowest else "gap"
            else:
                e.chain = "ok" if e.rec.previous_event_hash == prev.hash else "break"
        if e.alts:
            e.chain = "fork"


def runs(events: dict[int, Event]) -> list[tuple[int, int]]:
    """The contiguous stretches of held sequence numbers, newest first."""
    out = []
    for seq in sorted(events, reverse=True):
        if out and out[-1][0] == seq + 1:
            out[-1] = (seq, out[-1][1])
        else:
            out.append((seq, seq))
    return out


def anchor(events: dict[int, Event], head: HeadCheck | None) -> tuple[str, int]:
    """Tie the held chain to the signed head: (state, lowest anchored seq).

    ok: the record at the head's sequence hashes to the head's hash, and every link from there down to
    the returned seq checks out, so that stretch is the history the origin signed (given a valid head sig).
    mismatch: the record held at the head's sequence is not the one the head names.
    unloaded: the head's record isn't held, so nothing is anchored.
    """
    if head is None or head.seq not in events:
        return "unloaded", 0
    top = events[head.seq]
    if top.hash != head.hash:
        return "mismatch", 0
    lo = head.seq
    while lo in events and events[lo].chain == "ok" and (lo - 1) in events:
        lo -= 1
    return "ok", lo


# -- sentences ------------------------------------------------------------------


def who(rec, names: dict[str, str]) -> str:
    return rec.actor_username or names.get(rec.actor_pubkey.hex()) or rec.actor_pubkey.hex()[:12]


def _key_name(hexkey: str, names) -> str:
    return names.get(hexkey) or hexkey[:12]


def sentence(rec, names: dict[str, str] | None = None) -> str:
    names = names or {}
    a, k, board = who(rec, names), rec.kind, rec.board or rec.target_board
    t = f"#{rec.target_article_id.hex()[:8]}" if rec.target_article_id != b"\x00" * 32 else ""
    f1 = _meta_text(rec, 1)
    if k == "bonnet.article":
        sub = f"“{f1}”" if f1 else "an article"
        if _meta(rec, 0x0101) is not None:
            return f"mirrored {sub} from {_meta_text(rec, 0x0102)} into {board} (#{rec.article_num})"
        if _meta(rec, 7) is not None:
            return f"{a} edited a post: now {sub} in {board} (#{rec.article_num})"
        if _meta(rec, 6) is not None:
            return f"{a} replied {sub} in {board} (#{rec.article_num})"
        return f"{a} posted {sub} in {board} (#{rec.article_num})"
    simple = {
        "bonnet.article.cancel": f"{a} removed {t} in {board}", "bonnet.article.restore": f"{a} restored {t} in {board}",
        "bonnet.article.purge": f"{a} purged the body of {t} in {board}",
        "bonnet.article.pin": f"{a} pinned {t} in {board}", "bonnet.article.unpin": f"{a} unpinned {t} in {board}",
        "bonnet.thread.close": f"{a} closed the thread {t} in {board}",
        "bonnet.thread.reopen": f"{a} reopened the thread {t} in {board}",
        "bonnet.board.create": f"{a} created the board {board}", "bonnet.board.close": f"{a} closed the board {board}",
        "bonnet.board.reopen": f"{a} reopened the board {board}", "bonnet.board.purge": f"{a} purged the board {board}",
        "bonnet.user.register": f"{a} registered “{f1}”" if f1 != rec.actor_username else f"“{f1}” registered",
        "bonnet.user.revoke": f"{a} revoked {_key_name(f1, names)}",
        "bonnet.user.key.rotate": f"{a} rotated their key", "bonnet.origin.key.rotate": f"{a} rotated the origin key",
        "bonnet.report": f"{a} reported {_key_name(f1, names)}" + (f" ({t} in {board})" if t else ""),
        "bonnet.punishment.warn": f"{a} warned {_key_name(f1, names)}",
        "bonnet.punishment.permaban": f"{a} permanently banned {_key_name(f1, names)}",
        "bonnet.punishment.revoke": f"{a} lifted a punishment",
        "bonnet.punishment.ack": f"{a} acknowledged a punishment",
        "bonnet.rule.publish": f"{a} published the rule “{f1}”", "bonnet.rule.revoke": f"{a} revoked a rule",
        "bonnet.route.announce": f"{a} announced the route {f1}:{_meta_text(rec, 2)}",
        "bonnet.route.withdraw": f"{a} withdrew a route",
        "bonnet.bridge.binding": f"bound a venue channel ({_meta_text(rec, 0x0102)}) to {rec.target_board}",
        "bonnet.bridge.unbind": "unbound a venue channel",
        "bonnet.bridge.observation": f"observed a venue post on {_meta_text(rec, 0x0102)}",
        "bonnet.bridge.link": f"linked {t} to its copy on {_meta_text(rec, 0x0102)}",
    }
    if k == "bonnet.punishment.ban":
        exp = _meta(rec, 2)
        until = datetime.fromtimestamp(int.from_bytes(exp.value, "big", signed=True)).strftime("%Y-%m-%d") if exp else "?"
        return f"{a} banned {_key_name(f1, names)} until {until}"
    return simple.get(k, f"{a}: {k}")


# -- the query language ---------------------------------------------------------

KEYS = ("kind", "cat", "actor", "board", "target", "seq", "since", "until", "has", "text", "verify", "chain")


class QueryError(ValueError):
    pass


@dataclass
class Term:
    key: str
    values: list[str]
    negate: bool = False


def parse_query(q: str) -> list[Term]:
    """`key:value[,value]` terms, AND between them, `-` negates. Quotes allowed: text:"two words"."""
    terms = []
    for tok in re.findall(r'-?\w+:(?:"[^"]*"|\S+)|\S+', q or ""):
        neg = tok.startswith("-")
        body = tok[1:] if neg else tok
        if ":" not in body:
            raise QueryError(f"“{tok}” isn't key:value (keys: {', '.join(KEYS)})")
        key, val = body.split(":", 1)
        key = key.lower()
        if key not in KEYS:
            raise QueryError(f"unknown key “{key}” (keys: {', '.join(KEYS)})")
        val = val.strip('"')
        if not val:
            raise QueryError(f"“{key}:” needs a value")
        if key == "cat":
            for v in val.split(","):
                if v not in CATEGORIES and v != "other":
                    raise QueryError(f"unknown category “{v}” ({', '.join([*CATEGORIES, 'other'])})")
        if key == "seq" and not re.fullmatch(r"\d*(\.\.\d*)?", val):
            raise QueryError("seq takes N or A..B")
        if key in ("since", "until"):
            _when(val)
        terms.append(Term(key, val.split(",") if key != "text" else [val], neg))
    return terms


def _when(v: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M"):
        try:
            d = datetime.strptime(v, fmt)
            if fmt == "%H:%M":
                now = datetime.now()
                d = d.replace(year=now.year, month=now.month, day=now.day)
            return d.timestamp()
        except ValueError:
            continue
    raise QueryError(f"“{v}” isn't a date (YYYY-MM-DD, or HH:MM for today)")


def _hit(t: Term, e: Event, names: dict, body: str | None) -> bool:
    r = e.rec
    vs = [v.lower() for v in t.values]
    if t.key == "kind":
        return any(r.kind == v or r.kind == f"bonnet.{v}" for v in vs)
    if t.key == "cat":
        return category(r) in vs
    if t.key == "actor":
        name, key = (r.actor_username or names.get(r.actor_pubkey.hex(), "")).lower(), r.actor_pubkey.hex()
        return any(v == name or key.startswith(v) for v in vs)
    if t.key == "board":
        return any(v in (r.board.lower(), r.target_board.lower()) for v in vs)
    if t.key == "target":
        ids = (r.target_article_id.hex(), r.target_event_id.hex())
        return any(r.article_num == int(v[1:]) if v[1:].isdigit() and v.startswith("#")
                   else any(i.startswith(v.lstrip("#")) and i != "0" * 64 for i in ids) for v in vs)
    if t.key == "seq":
        v = vs[0]
        if ".." in v:
            lo, hi = v.split("..")
            return (not lo or r.origin_seq >= int(lo)) and (not hi or r.origin_seq <= int(hi))
        return r.origin_seq == int(v)
    if t.key == "since":
        return r.created_at >= _when(t.values[0])
    if t.key == "until":
        return r.created_at <= _when(t.values[0])
    if t.key == "has":
        return all({"body": r.body_size > 0, "target": r.target_article_id != b"\x00" * 32}.get(v, False) for v in vs)
    if t.key == "text":
        hay = " ".join([sentence(r, names), _meta_text(r, 1), body or ""]).lower()
        return vs[0] in hay
    if t.key == "verify":
        bad = e.sigs.get("author") != "valid" or e.sigs.get("origin") == "invalid" or not e.canonical
        unverified = not bad and e.sigs.get("origin") != "valid"
        return any(bad if v == "fail" else unverified if v == "unverified" else not bad for v in vs)
    if t.key == "chain":
        return any(e.chain == v for v in vs)
    return False


def matches(terms: list[Term], e: Event, names: dict, body: str | None = None) -> bool:
    return all(_hit(t, e, names, body) != t.negate for t in terms)


# -- raw layout -------------------------------------------------------------------

RECORD_LAYOUT = [
    ("record_format", "u8"), ("origin", "text16"), ("origin_seq", "u64"), ("previous_event_hash", "id32"),
    ("event_id", "id32"), ("kind", "text16"), ("schema_version", "u16"), ("created_at", "i64"),
    ("actor_pubkey", "key32"), ("actor_username", "text16"), ("actor_registrar", "text16"), ("board", "text16"),
    ("article_id", "id32"), ("article_num", "u64"), ("target_origin", "text16"), ("target_board", "text16"),
    ("target_article_id", "id32"), ("target_event_id", "id32"), ("metadata", "blob32"), ("body_hash", "id32"),
    ("body_size", "u64"), ("actor_signature", "sig64"), ("origin_signature", "sig64"),
]
_FIXED = {"u8": 1, "u16": 2, "u64": 8, "i64": 8, "id32": 32, "key32": 32, "sig64": 64}


@dataclass
class Span:
    name: str
    type: str
    start: int
    end: int
    value: str


def layout(raw: bytes) -> list[Span]:
    """Walk the served bytes field by field in wire order (Bonnet guide, Part I: record)."""
    spans, off = [], 0
    for name, typ in RECORD_LAYOUT:
        start = off
        if typ in _FIXED:
            n = _FIXED[typ]
            chunk = raw[off:off + n]
            off += n
            if typ in ("u8", "u16", "u64"):
                val = str(int.from_bytes(chunk, "big"))
            elif typ == "i64":
                val = str(struct.unpack(">q", chunk)[0]) + (
                    f"  ({datetime.fromtimestamp(struct.unpack('>q', chunk)[0]):%Y-%m-%d %H:%M:%S})"
                    if name == "created_at" else "")
            else:
                val = chunk.hex()
        elif typ == "text16":
            n = int.from_bytes(raw[off:off + 2], "big")
            val = raw[off + 2:off + 2 + n].decode("utf-8", "replace")
            off += 2 + n
            val = f"“{val}”"
        else:  # blob32
            n = int.from_bytes(raw[off:off + 4], "big")
            off += 4 + n
            val = f"{n} bytes (decoded below)"
        spans.append(Span(name, typ, start, off, val))
    if off != len(raw):
        spans.append(Span("(trailing bytes!)", "?", off, len(raw), raw[off:].hex()))
    return spans


def hexdump(raw: bytes, spans: list[Span], width: int = 16) -> list[tuple[int, list[tuple[int, int]]]]:
    """Rows of (offset, [(byte, span index)]) for a tinted hex view."""
    owner = [0] * len(raw)
    for i, s in enumerate(spans):
        for b in range(s.start, min(s.end, len(raw))):
            owner[b] = i
    return [(o, [(raw[o + j], owner[o + j]) for j in range(min(width, len(raw) - o))]) for o in range(0, len(raw), width)]
