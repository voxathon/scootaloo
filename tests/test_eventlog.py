"""The event log: sentences, chain checking, the query language, the raw layout, and the Log tab."""

import asyncio

import pytest
from textual.widgets import DataTable, Input, Static, TabbedContent

from conftest import run, unique
from scootaloo import core, eventlog as ev, settings
from scootaloo.accounts import Users
from scootaloo.tui import Ask, Pick, RawRecord, ScootalooApp
from test_tui import settle, wait_screen


def test_query_errors():
    for bad, msg in [("acter:x", "unknown key"), ("cat:nope", "unknown category"), ("seq:x", "seq takes"),
                     ("since:yesterday", "isn't a date"), ("loose", "isn't key:value"), ("kind:", "needs a value")]:
        with pytest.raises(ev.QueryError, match=msg):
            ev.parse_query(bad)
    terms = ev.parse_query('cat:posts,moderation -actor:spammer text:"two words" seq:5..')
    assert [(t.key, t.negate) for t in terms] == [("cat", False), ("actor", True), ("text", False), ("seq", False)]
    assert terms[2].values == ["two words"]


@pytest.fixture
def logworld(servers):
    with Users() as u:
        u.add("moxxie", "hunter222")
        mox = u.login("moxxie", "hunter222")
    run(core.connect(mox, servers["alpha.test"]))
    name = unique("mox")
    run(core.register(core.Session.open(mox, "alpha.test"), name))
    board = unique("log")
    run(core.create_board(core.Session.open(mox, "alpha.test"), board))
    ref = core.BoardRef("alpha.test", board, "alpha.test")
    p = run(core.post(mox, ref, "hello log", "first"))
    run(core.reply(mox, ref, p.article_num, "second"))
    return mox, ref, name, p.article_num


def test_fetch_chain_sentences_layout(logworld):
    mox, ref, name, num = logworld

    async def go():
        async with core.Session.open(mox, "alpha.test").reader() as c:
            await c.refresh_epoch_cache("alpha.test")
            head = (await c.get_head("alpha.test")).latest_origin_seq
            return await ev.fetch(c, "alpha.test", max(1, head - 9), 10)

    evs = run(go())
    held = {e.seq: e for e in evs}
    ev.check_chain(held)
    lowest = min(held)
    assert all(e.chain == "ok" for s, e in held.items() if s != lowest) and held[lowest].chain in ("ok", "start")
    assert all(e.canonical and e.sigs["author"] == "valid" and e.sigs["origin"] == "valid" for e in evs)
    texts = [ev.sentence(e.rec) for e in evs]
    assert f"{name} posted “hello log” in {ref.name} (#{num})" in texts
    assert any(t.startswith(f"{name} replied") for t in texts)
    spans = ev.layout(evs[-1].raw)
    assert spans[0].start == 0 and spans[-1].end == len(evs[-1].raw) and spans[-1].name == "origin_signature"
    # tamper with a link: the chain check notices
    prev = held[max(held) - 1]
    prev.raw = prev.raw[:-1] + bytes([prev.raw[-1] ^ 1])
    ev.check_chain(held)
    assert held[max(held)].chain == "break"


def test_head_anchor_forks_gaps(logworld):
    mox, ref, name, num = logworld

    async def go():
        async with core.Session.open(mox, "alpha.test").reader() as c:
            await c.refresh_epoch_cache("alpha.test")
            head = await ev.fetch_head(c, "alpha.test")
            evs = await ev.fetch(c, "alpha.test", max(1, head.seq - 9), 10)
            c.origin_key_for_seq = lambda origin, seq: None  # as if we'd never accepted the origin's key
            blind = await ev.fetch_head(c, "alpha.test")
            return head, evs, blind

    head, evs, blind = run(go())
    assert head.sig == "valid" and blind.sig == "unverifiable"
    held = {}
    assert ev.merge(held, evs) == []
    ev.check_chain(held)
    lo = min(held)
    state, anchored = ev.anchor(held, head)
    assert state == "ok" and anchored == lo and ev.runs(held) == [(lo, head.seq)]

    # the head names a different record than the one held there
    assert ev.anchor(held, ev.HeadCheck(head.seq, b"\x01" * 32, "valid"))[0] == "mismatch"
    # the head's record isn't loaded: nothing is anchored
    assert ev.anchor({s: e for s, e in held.items() if s != head.seq}, head) == ("unloaded", 0)

    # a second, different record for a seq we hold is kept beside it, never swapped in
    mid = head.seq - 2
    orig = held[mid]
    other = ev.Event(orig.raw[:-1] + bytes([orig.raw[-1] ^ 1]), orig.rec, [], sigs={"author": "valid", "origin": "invalid"})
    assert ev.merge(held, [other]) == [mid] and ev.merge(held, [other]) == []  # reported once
    assert held[mid] is orig and held[mid].alts == [other]
    ev.check_chain(held)
    assert held[mid].chain == "fork"
    assert [e.seq for e in held.values() if ev.matches(ev.parse_query("chain:fork"), e, {})] == [mid]
    orig.alts.clear()

    # a hole between two loaded stretches is a gap, not a start
    del held[mid]
    ev.check_chain(held)
    assert held[mid + 1].chain == "gap" and held[lo].chain in ("ok", "start")
    assert ev.runs(held) == [(mid + 1, head.seq), (lo, mid - 1)]
    assert ev.anchor(held, head) == ("ok", mid + 1)

    # unverifiable origin signatures are their own filter, apart from failures
    e = held[head.seq]
    e.sigs = {**e.sigs, "origin": "unverifiable"}
    unv = [x.seq for x in held.values() if ev.matches(ev.parse_query("verify:unverified"), x, {})]
    assert unv == [head.seq]
    assert not ev.matches(ev.parse_query("verify:fail"), e, {})


def test_log_tab(logworld, monkeypatch):
    mox, ref, name, num = logworld
    copied = []

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        monkeypatch.setattr(app, "copy_to_clipboard", lambda t: copied.append(t))
        async with app.run_test(size=(170, 50)) as pilot:
            await settle(pilot, app)
            app.open_board(ref)
            await settle(pilot, app)
            await pilot.press("L")
            await settle(pilot, app)
            assert app.query_one("#tabs", TabbedContent).active == "tab-log"
            assert app.query_one("#log-filter", Input).value == "-cat:bridges"
            lt = app.query_one("#log", DataTable)
            status = str(app.query_one("#log-status", Static).render())
            assert lt.row_count >= 4 and "chain ✓" in status and "BROKEN" not in status
            assert f"#{app.log_head}" in status
            assert "anchored to the signed head" in status and "unverified" not in status
            assert app.log_heads[app.log_at].sig == "valid"

            # filter
            app.query_one("#log-filter", Input).value = "cat:boards"
            await pilot.press("slash")
            await pilot.press("enter")
            await settle(pilot, app)
            whats = [str(lt.get_row_at(i)[3]) for i in range(lt.row_count)]
            assert whats and all("board" in w for w in whats)
            app.query_one("#log-filter", Input).value = "acter:x"
            app.query_one("#log-filter", Input).focus()
            await pilot.press("enter")
            await settle(pilot, app)
            assert "unknown key" in str(app.query_one("#log-status", Static).render())
            app.query_one("#log-filter", Input).value = f"actor:{name}"
            app.query_one("#log-filter", Input).focus()
            await pilot.press("enter")
            await settle(pilot, app)
            assert lt.row_count >= 3

            # raw window, stepping, copy
            lt.focus()
            lt.move_cursor(row=0)
            await pilot.press("x")
            await wait_screen(pilot, app, RawRecord)
            first = app.screen.event.seq
            assert "canonical" in str(app.screen.query(Static).first().render())
            await pilot.press("c")
            assert copied and copied[-1] == app.screen.event.raw.hex()
            await pilot.press("right")
            await wait_screen(pilot, app, RawRecord)
            assert app.screen.event.seq < first
            await pilot.press("escape")
            await settle(pilot, app)

            # go to a sequence number
            await pilot.press("g")
            await wait_screen(pilot, app, Ask)
            app.screen.query_one("#answer", Input).value = "2"
            await pilot.press("enter")
            await settle(pilot, app)
            assert 2 in app.logs[app.log_at]

            # saved filters (per user)
            app.query_one("#log-filter", Input).value = "cat:posts"
            lt.focus()
            await pilot.press("ctrl+s")
            await wait_screen(pilot, app, Ask)
            app.screen.query_one("#answer", Input).value = "just posts"
            await pilot.press("enter")
            await settle(pilot, app)
            assert settings.preferences(mox.stores).get("log.saved_filters") == [["just posts", "cat:posts"]]
            await pilot.press("F")
            await wait_screen(pilot, app, Pick)
            await pilot.press("enter")
            await settle(pilot, app)
            # (the relay is shared across test files: test_cli's edit is a post too)
            assert all(any(w in str(lt.get_row_at(i)[3]) for w in ("posted", "replied", "edited"))
                       for i in range(lt.row_count))

            # follow: a new post appears without a refresh
            await pilot.press("f")
            await core.post(mox, ref, "while following", "x")
            await app._poll()
            await settle(pilot, app)
            assert any("while following" in str(lt.get_row_at(i)[3]) for i in range(lt.row_count))

            # enter on a post opens it in the Boards tab
            lt.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot, app)
            assert app.query_one("#tabs", TabbedContent).active == "tab-boards"
            assert app.thread is not None

    asyncio.run(go())
