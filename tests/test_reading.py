"""Reading comfort: collapsing, show more, keyboard walking, links, info, go-to, preview, caches, layout."""

import asyncio
import sqlite3

import pytest
from textual.widgets import DataTable, Input, OptionList

from conftest import run, unique
from scootaloo import core
from scootaloo.accounts import Stores, Users
from scootaloo.bodycache import BodyCache
from scootaloo.links import find_links, is_safe_web_url
from scootaloo.tui import SHOW_LINES, Info, LinkPicker, OpenLink, Post, ScootalooApp
from test_tui import settle, wait_screen


# -- pure pieces ---------------------------------------------------------------


def test_find_links():
    body = (
        "See [the spec](https://knolastna.me/bonnet/) and #12, also <https://example.org/a?b=1>.\n"
        "Bare: https://pypi.org/project/scootaloo/. Not `#99` or issue#5 or &#38;\n"
        "```\nhttps://in.code.block #77\n```\n"
        "Again https://pypi.org/project/scootaloo/ and #12"
    )
    got = [(link.kind, link.target) for link in find_links(body)]
    assert got == [
        ("web", "https://knolastna.me/bonnet/"),
        ("post", "12"),
        ("web", "https://example.org/a?b=1"),
        ("web", "https://pypi.org/project/scootaloo/"),
    ]


def test_only_http_links_are_followed():
    assert is_safe_web_url("https://x.example/")
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,hi", "https:///nohost"):
        assert not is_safe_web_url(bad)


def test_body_cache_checks_hashes(tmp_path):
    from bonnet.core.record import compute_body_hash

    raw = b"hello pony"
    h = compute_body_hash(raw).hex()
    path = str(tmp_path / "cache.db")
    with BodyCache(path) as c:
        assert not c.put(h, b"not the same bytes")  # refuses bytes that don't match
        assert c.put(h, raw)
        assert c.get(h) == raw
    db = sqlite3.connect(path)  # tamper with the stored copy
    db.execute("UPDATE bodies SET body = ? WHERE hash = ?", (b"evil", h))
    db.commit()
    db.close()
    with BodyCache(path) as c:
        assert c.get(h) is None  # a body that fails its hash is dropped, not served
        assert c.get(h) is None


def test_trusted_domains_and_settings_persist():
    with Users() as u:
        u.add("zecora", "rhymes")
        acct = u.login("zecora", "rhymes")
    with core.read_state(acct) as st:
        st.trust("Example.org")
        st.set("layout.name", "two")
    with core.read_state(acct) as st:
        assert st.trusts("example.org") and not st.trusts("evil.example")
        assert st.trusted_domains() == ["example.org"]
        assert st.get("layout.name") == "two"
        assert st.untrust("example.org") and not st.trusts("example.org")


# -- the app -------------------------------------------------------------------


@pytest.fixture
def deep(servers):
    """A thread: OP > reply > reply-to-reply > deeper, plus a long side reply."""
    with Users() as u:
        u.add("moxxie", "hunter222")
        mox = u.login("moxxie", "hunter222")
    run(core.connect(mox, servers["alpha.test"]))
    run(core.register(core.Session.open(mox, "alpha.test"), unique("mox")))
    board = unique("read")
    run(core.create_board(core.Session.open(mox, "alpha.test"), board))
    ref = core.BoardRef("alpha.test", board, "alpha.test")
    op = run(core.post(mox, ref, "OP", "the opening post, see https://example.org/x and #1"))
    r1 = run(core.reply(mox, ref, op.article_num, "reply one"))
    r2 = run(core.reply(mox, ref, r1.article_num, "reply two (depth 2)"))
    r3 = run(core.reply(mox, ref, r2.article_num, "reply three (depth 3)"))
    long_body = "\n".join(f"line {i}" for i in range(1, 31))
    lng = run(core.reply(mox, ref, op.article_num, long_body))
    nums = dict(op=op.article_num, r1=r1.article_num, r2=r2.article_num, r3=r3.article_num, long=lng.article_num)
    return mox, ref, nums


def by_num(app, n) -> Post:
    return next(p for p in app.posts if p.item.view.article_num == n)


async def open_thread(pilot, app, ref, num):
    app.open_thread(ref, num)
    await settle(pilot, app)
    await pilot.pause(0.2)


def test_collapse_defaults_and_keyboard_walk(deep):
    mox, ref, n = deep

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 50)) as pilot:
            await settle(pilot, app)
            await open_thread(pilot, app, ref, n["op"])
            # OP and direct replies open; depth 2 collapsed, which hides depth 3 under it
            assert not by_num(app, n["op"]).collapsed and not by_num(app, n["r1"]).collapsed
            assert by_num(app, n["r2"]).collapsed and by_num(app, n["r2"]).display
            assert not by_num(app, n["r3"]).display
            assert "1 reply hidden" in str(by_num(app, n["r2"]).query_one(".post-meta").render())

            # down walks visible posts only: OP, r1, r2 (collapsed), long
            assert app.focused is by_num(app, n["op"])
            seen = [n["op"]]
            for _ in range(3):
                await pilot.press("down")
                seen.append(app.focused.item.view.article_num)
            assert seen == [n["op"], n["r1"], n["r2"], n["long"]]

            # right on the collapsed r2 expands it, revealing r3 (which is depth 3, so starts collapsed)
            await pilot.press("up")
            assert app.focused is by_num(app, n["r2"])
            await pilot.press("right")
            assert not by_num(app, n["r2"]).collapsed and by_num(app, n["r3"]).display
            # left collapses again; left on a collapsed post steps out to its parent
            await pilot.press("left")
            assert by_num(app, n["r2"]).collapsed and not by_num(app, n["r3"]).display
            await pilot.press("left")
            assert app.focused is by_num(app, n["r1"])
            # escape goes back to the list
            await pilot.press("escape")
            assert app.focused is app.query_one("#threads", DataTable)

    asyncio.run(go())


def test_show_more(deep):
    mox, ref, n = deep

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 60)) as pilot:
            await settle(pilot, app)
            await open_thread(pilot, app, ref, n["long"])
            p = by_num(app, n["long"])
            assert app.focused is p and p.long
            more = p.query(".more").first()
            assert f"{30 - SHOW_LINES} more lines" in str(more.render())
            await pilot.press("space")
            assert p.show_all and "show less" in str(p.query(".more").first().render())
            await pilot.press("space")
            assert not p.show_all

    asyncio.run(go())


def test_links_trust_and_post_jump(deep, monkeypatch):
    mox, ref, n = deep
    opened = []

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
        async with app.run_test(size=(160, 50)) as pilot:
            await settle(pilot, app)
            await open_thread(pilot, app, ref, n["op"])

            # l lists both links; pick the web one -> confirm screen, cancel: nothing opens
            await pilot.press("l")
            await wait_screen(pilot, app, LinkPicker)
            assert app.screen.query_one("#links", OptionList).option_count == 2
            await pilot.press("enter")
            await wait_screen(pilot, app, OpenLink)
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert opened == []

            # trust the domain: opens now, and next time without asking
            await pilot.press("l")
            await wait_screen(pilot, app, LinkPicker)
            await pilot.press("enter")
            await wait_screen(pilot, app, OpenLink)
            await pilot.press("t")
            await pilot.pause(0.3)
            assert opened == ["https://example.org/x"]
            with core.read_state(mox) as st:
                assert st.trusts("example.org")
            await pilot.press("l")
            await wait_screen(pilot, app, LinkPicker)
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert opened == ["https://example.org/x"] * 2 and not isinstance(app.screen, OpenLink)

            # the #1 mention jumps inside scootaloo
            await pilot.press("l")
            await wait_screen(pilot, app, LinkPicker)
            await pilot.press("down", "enter")
            await settle(pilot, app)
            assert app.thread.items[0].view.article_num == 1 or app.focused.item.view.article_num == 1

    asyncio.run(go())


def test_info_popup_jumps_and_goto(deep):
    mox, ref, n = deep

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 50)) as pilot:
            await settle(pilot, app)
            await open_thread(pilot, app, ref, n["r3"])  # deep target: its collapsed ancestors open up
            assert app.focused is by_num(app, n["r3"]) and by_num(app, n["r3"]).display
            await pilot.press("I")
            await wait_screen(pilot, app, Info)
            text = str(app.screen.query_one("#info-table").render())
            assert by_num(app, n["r3"]).item.view.event_id in text and "body hash" in text
            # first action is "go to the thread's root"
            await pilot.press("enter")
            await settle(pilot, app)
            assert app.focused.item.view.article_num == n["op"]

            # g: go to a post by number
            await pilot.press("g")
            await pilot.pause(0.3)
            app.screen.query_one("#answer", Input).value = f"#{n['r2']}"
            await pilot.press("enter")
            await settle(pilot, app)
            await pilot.pause(0.2)
            assert app.focused.item.view.article_num == n["r2"]

    asyncio.run(go())


def test_preview_and_caches(deep, monkeypatch):
    mox, ref, n = deep

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 50)) as pilot:
            await settle(pilot, app)
            app.open_board(ref)
            await settle(pilot, app)
            t = app.query_one("#threads", DataTable)
            t.focus()
            t.move_cursor(row=0)
            await pilot.pause(0.5)
            await settle(pilot, app)
            assert app.previewing == (ref, n["op"])
            assert [p.item.view.article_num for p in app.posts] == [n["op"]]
            assert app.focused is t  # skimming leaves you in the list

            # the preview filled the body cache, checked against the hash
            with BodyCache(Stores.for_user("moxxie").cache_path) as c:
                row = next(r for r in app.index.rows(ref) if r.article_num == n["op"])
                assert c.get(row.body_hash) is not None

            # open, then reopen: the second time comes from the thread cache, no network
            await pilot.press("enter")
            await settle(pilot, app)

            async def no_network(*a, **k):
                raise AssertionError("thread fetched again despite an unchanged head")

            monkeypatch.setattr(core, "thread", no_network)
            app.open_thread(ref, n["r1"])  # another article of the same thread
            await settle(pilot, app)
            assert app.thread.root.article_num == n["op"]

    asyncio.run(go())


def test_layout_maximize_resize_persist_and_help(deep):
    mox, ref, n = deep

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 50)) as pilot:
            await settle(pilot, app)
            await open_thread(pilot, app, ref, n["op"])
            await pilot.press("z")
            assert app.screen.maximized is app.query_one("#view")
            await pilot.press("z")
            assert app.screen.maximized is None

            share = app.list_share
            await pilot.press("left_square_bracket")  # in the thread pane: thread grows, list shrinks
            assert app.list_share == share + 5
            await pilot.press("backslash")
            assert app.layout_name == "two" and app.query_one("#panes").has_class("-two")

            await pilot.press("question_mark")
            await pilot.pause(0.2)
            assert app.screen.query("HelpPanel")
            await pilot.press("question_mark")
            await pilot.pause(0.2)
            assert not app.screen.query("HelpPanel")
        from scootaloo import settings

        prefs = settings.preferences(mox.stores)
        assert prefs.get("layout.name") == "two" and prefs.get("layout.list_share") == share + 5

        # narrow windows stack the three-pane layout
        prefs.set("layout.name", "three")
        prefs.save()
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(80, 40)) as pilot:
            await settle(pilot, app)
            assert app._effective_layout() == "stacked" and app.query_one("#panes").has_class("-stacked")
            await pilot.press("b")
            assert app.query_one("#panes").has_class("-show-boards")

    asyncio.run(go())


def test_enter_from_preview_keeps_the_thread(deep):
    """Regression: opening a thread re-renders the list, which must not re-trigger a preview."""
    mox, ref, n = deep
    run(core.post(mox, ref, "another thread", "so the list has two rows"))

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 50)) as pilot:
            await settle(pilot, app)
            app.open_board(ref)
            await settle(pilot, app)
            t = app.query_one("#threads", DataTable)
            t.focus()
            await pilot.pause(0.5)
            await pilot.press("down")  # skim to the older thread (the OP with replies)
            await pilot.pause(0.5)
            await settle(pilot, app)
            assert app.previewing == (ref, n["op"])
            hint = str(app.query_one("#preview-hint").render())
            assert "4 replies" in hint and "ENTER" in hint

            await pilot.press("enter")
            await settle(pilot, app)
            await pilot.pause(0.6)  # longer than the preview delay
            await settle(pilot, app)
            assert app.previewing is None and app.thread is not None
            assert len(app.posts) == 5 and isinstance(app.focused, Post)

            # back to the list and down again: previews resume
            await pilot.press("escape")
            await pilot.press("up")
            await pilot.pause(0.5)
            await settle(pilot, app)
            assert app.previewing is not None and app.previewing[1] != n["op"]

    asyncio.run(go())


def test_arrows_scroll_through_a_tall_post_before_moving_on(deep):
    """Regression: an expanded post taller than the pane, last in the thread, must be readable by ↓ alone."""
    mox, ref, n = deep

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(140, 24)) as pilot:  # short window: the long reply can't fit
            await settle(pilot, app)
            await open_thread(pilot, app, ref, n["long"])
            view = app.query_one("#view")
            p = by_num(app, n["long"])
            assert app.focused is p
            await pilot.press("space")  # show all 30 lines
            await pilot.pause(0.3)
            assert p.show_all

            def bottom_visible():
                return p.virtual_region.bottom <= view.scroll_y + view.scrollable_content_region.height

            presses = 0
            while not bottom_visible() and presses < 30:
                before = view.scroll_y
                await pilot.press("down")
                await pilot.pause(0.05)
                presses += 1
                assert app.focused is p, "↓ left the post before its end was on screen"
                assert view.scroll_y > before, "↓ did nothing while the post continued below"
            assert bottom_visible() and presses > 0

            # ↑ scrolls back through it before moving to the post above
            while p.virtual_region.y < view.scroll_y:
                await pilot.press("up")
                await pilot.pause(0.05)
                assert app.focused is p
            await pilot.press("up")
            await pilot.pause(0.1)
            assert app.focused is not p and isinstance(app.focused, Post)

    asyncio.run(go())
