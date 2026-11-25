"""The TUI, driven headlessly through Textual's pilot against the test servers."""

import asyncio

import pytest
from textual.widgets import DataTable, Input, Static, TextArea

from conftest import run, unique
from scootaloo import core
from scootaloo.accounts import Users
from scootaloo.tui import Compose, Login, Post, ScootalooApp, Show, YesNo


@pytest.fixture
def world(servers):
    accts = {}
    for user, pw in (("moxxie", "hunter222"), ("pinkie", "partypony1")):
        with Users() as u:
            u.add(user, pw)
            accts[user] = u.login(user, pw)
        run(core.connect(accts[user], servers["alpha.test"]))
        run(core.register(core.Session.open(accts[user], "alpha.test"), unique(user[:4])))
    board = unique("tui")
    run(core.create_board(core.Session.open(accts["moxxie"], "alpha.test"), board))
    return accts["moxxie"], accts["pinkie"], core.BoardRef("alpha.test", board, "alpha.test")


async def settle(pilot, app, rounds=60):
    """Wait until no workers are running (network calls finished)."""
    for _ in range(rounds):
        await pilot.pause(0.1)
        if not any(w.is_running for w in app.workers):
            await pilot.pause(0.1)
            return
    running = [(w.name, w.group, w.description) for w in app.workers if w.is_running]
    raise AssertionError(f"workers never settled: {running}; screen={type(app.screen).__name__}")


async def wait_screen(pilot, app, cls, rounds=80):
    for _ in range(rounds):
        await pilot.pause(0.1)
        if isinstance(app.screen, cls):
            return
    raise AssertionError(f"never reached {cls.__name__}; screen={type(app.screen).__name__}")


def leaf(app, ref):
    return app.nodes[ref]


def table_rows(app):
    t = app.query_one("#threads", DataTable)
    return [[str(c) for c in t.get_row_at(i)] for i in range(t.row_count)]


def test_login_screen_then_board(world):
    mox, pinkie, ref = world

    async def go():
        app = ScootalooApp()
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.3)
            assert isinstance(app.screen, Login)
            app.screen.query_one("#login-name", Input).value = "moxxie"
            app.screen.query_one("#login-pw", Input).value = "wrong-password"
            app.screen.go()
            await pilot.pause(0.2)
            assert "wrong" in str(app.screen.query_one("#login-error").render())
            app.screen.query_one("#login-pw", Input).value = "hunter222"
            app.screen.go()
            await settle(pilot, app)
            assert app.acct.name == "moxxie"
            assert ref in app.nodes and app.inbox_node is not None

    asyncio.run(go())


def test_unread_reply_edit_search_verify_delete(world):
    mox, pinkie, ref = world

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app)
            app.open_board(ref)
            await settle(pilot, app)
            assert app.unread_map[ref] == set()  # first sight

            # pinkie posts; a poll picks it up as unread
            theirs = await core.post(pinkie, ref, "hello moxxie", "are you there?")
            await app._poll()
            assert app.unread_map[ref] == {theirs.article_num}
            assert "(1)" in str(leaf(app, ref).label)
            assert table_rows(app)[0][0] == "●"

            # opening the thread marks it read
            t = app.query_one("#threads", DataTable)
            t.focus()
            t.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot, app)
            assert [p.item.view.article_num for p in app.query(Post)] == [theirs.article_num]
            assert app.unread_map[ref] == set()
            assert "(" not in str(leaf(app, ref).label)

            # reply
            await pilot.press("c")
            await pilot.pause(0.3)
            assert isinstance(app.screen, Compose)
            app.screen.query_one("#body", TextArea).load_text("yes, here")
            await pilot.press("ctrl+s")
            await settle(pilot, app)
            posts = list(app.query(Post))
            assert len(posts) == 2 and posts[1].item.body == "yes, here"
            mine = posts[1].item.view.article_num

            # edit my reply
            posts[1].focus()
            await pilot.press("e")
            await pilot.pause(0.3)
            assert isinstance(app.screen, Compose)
            app.screen.query_one("#body", TextArea).load_text("yes, I'm here")
            await pilot.press("ctrl+s")
            await settle(pilot, app)
            posts = list(app.query(Post))
            assert posts[1].item.body == "yes, I'm here" and posts[1].item.view.article_num != mine

            # can't edit pinkie's post
            posts[0].focus()
            await pilot.press("e")
            await pilot.pause(0.3)
            assert isinstance(app.screen, Compose)
            app.screen.query_one("#body", TextArea).load_text("hijacked")
            await pilot.press("ctrl+s")
            await settle(pilot, app)
            assert list(app.query(Post))[0].item.body == "are you there?"

            # verify
            list(app.query(Post))[0].focus()
            await pilot.press("v")
            await wait_screen(pilot, app, Show)
            text = str(app.screen.query_one(Static).render())
            assert "author signature" in text and "verified" in text
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, Show)

            # search
            await pilot.press("slash")
            await pilot.pause(0.3)
            app.screen.query_one("#answer", Input).value = "hello"
            await pilot.press("enter")
            await settle(pilot, app)
            assert app.mode == "search"
            found = [r[1] for r in table_rows(app)]
            assert str(theirs.article_num) in found and len(found) == 2  # the post and my "Re: hello…" reply

            # delete my reply (alpha's ACL allows cancel)
            app.open_thread(ref, theirs.article_num)
            await settle(pilot, app)
            list(app.query(Post))[1].focus()
            await pilot.press("d")
            await pilot.pause(0.3)
            assert isinstance(app.screen, YesNo)
            await pilot.press("y")
            await settle(pilot, app)
            assert len(list(app.query(Post))) == 1

    asyncio.run(go())


def test_inbox_lists_replies_to_me(world):
    mox, pinkie, ref = world
    root = run(core.post(mox, ref, "my question", "anyone?"))

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 45)) as pilot:
            await settle(pilot, app)
            await core.reply(pinkie, ref, root.article_num, "me!")
            await app._poll()
            assert "Inbox (1)" in str(app.inbox_node.label)
            app.show_inbox()
            await settle(pilot, app)
            rows = table_rows(app)
            assert rows and rows[0][0] == "●" and ref.name in rows[0][2]
            t = app.query_one("#threads", DataTable)
            t.focus()
            t.move_cursor(row=0)
            await pilot.press("enter")
            await settle(pilot, app)
            assert [p.item.depth for p in app.query(Post)] == [0, 1]
            assert str(app.inbox_node.label) == "Inbox"

    asyncio.run(go())


def test_read_only_board_refuses_writes(world, servers):
    _, _, ref = world
    with Users() as u:
        u.add("lurker", "justlooking")

    async def go():
        app = ScootalooApp(user="lurker", password="justlooking")
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.5)
            # no relays yet: the app asks for a URL
            app.screen.query_one("#answer", Input).value = servers["alpha.test"]
            await pilot.press("enter")
            await settle(pilot, app)
            app.open_board(core.BoardRef("alpha.test", ref.name, "alpha.test"))
            await settle(pilot, app)
            assert "read-only here" in str(app.query_one("#status", Static).render())
            await pilot.press("n")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, Compose)

    asyncio.run(go())
