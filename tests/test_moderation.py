"""Moderator actions: an ordinary user is refused with a clear message, and punishments need a reason."""

import pytest

from conftest import run, unique
from scootaloo import core
from scootaloo.accounts import Users


@pytest.fixture
def post(servers):
    with Users() as u:
        u.add("moxxie", "hunter222")
        mox = u.login("moxxie", "hunter222")
    run(core.connect(mox, servers["alpha.test"]))
    run(core.register(core.Session.open(mox, "alpha.test"), unique("mox")))
    board = unique("mod")
    run(core.create_board(core.Session.open(mox, "alpha.test"), board))
    ref = core.BoardRef("alpha.test", board, "alpha.test")
    return mox, ref, run(core.post(mox, ref, "pin me", "x")).article_num


@pytest.mark.parametrize("action", ["pin", "close", "warn"])
def test_ordinary_users_are_refused(post, action):
    mox, ref, num = post
    with pytest.raises(core.ScootalooError, match="isn't a moderator"):
        run(core.moderate(mox, ref, num, action, reason="spam"))


def test_punishments_need_a_reason_and_posts_must_exist(post):
    mox, ref, num = post
    with pytest.raises(core.ScootalooError, match="needs a reason"):
        run(core.moderate(mox, ref, num, "ban", days=1))
    with pytest.raises(core.ScootalooError, match="no article #999"):
        run(core.moderate(mox, ref, 999, "pin"))


def test_tui_mod_menu_refusal(post):
    import asyncio
    from scootaloo.tui import ModMenu, ScootalooApp
    from test_tui import settle, wait_screen

    mox, ref, num = post

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(150, 45)) as pilot:
            await settle(pilot, app)
            app.open_thread(ref, num)
            await settle(pilot, app)
            await pilot.pause(0.3)
            await pilot.press("M")
            await wait_screen(pilot, app, ModMenu)
            await pilot.press("enter")  # "Pin this post"
            await settle(pilot, app)
            assert not isinstance(app.screen, ModMenu)
            msgs = [n.message for n in app._notifications]
            assert any("isn't a moderator" in m for m in msgs), msgs

    asyncio.run(go())


def test_owner_closes_and_reopens_own_board_and_unban_needs_a_punishment(post):
    mox, ref, num = post
    try:
        run(core.board_state(mox, ref, True))
    except core.ScootalooError as e:  # stock ACL may not let owners close: then the message must say so
        assert "can't close" in str(e)
    else:
        with pytest.raises(core.ScootalooError, match="already closed"):
            run(core.board_state(mox, ref, True))
        run(core.board_state(mox, ref, False))
    with pytest.raises(core.ScootalooError, match="no punishments"):
        run(core.lift_punishments(mox, ref, num))


def test_report_then_reports_tab(post):
    import asyncio
    from textual.widgets import Input, TabbedContent
    from scootaloo.tui import Ask, ScootalooApp
    from test_tui import settle, wait_screen

    mox, ref, num = post
    with pytest.raises(core.ScootalooError, match="needs a reason"):
        run(core.report(mox, ref, num, "  "))
    assert run(core.report(mox, ref, num, "spam, probably")).origin_seq > 0

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(150, 45)) as pilot:
            await settle(pilot, app)
            app.open_thread(ref, num)
            await settle(pilot, app)
            await pilot.pause(0.3)
            await pilot.press("exclamation_mark")
            await wait_screen(pilot, app, Ask)
            app.screen.query_one("#answer", Input).value = "filed from the TUI"
            await pilot.press("enter")
            await settle(pilot, app)
            assert any("reported" in n.message for n in app._notifications)
            # an ordinary user on a stock server can't read reports: no tab, and R does nothing
            assert app.can_reports is False
            assert app.query_one("#tabs", TabbedContent).get_tab("tab-reports").display is False
            await pilot.press("R")
            await settle(pilot, app)
            assert app.query_one("#tabs", TabbedContent).active == "tab-boards"
            assert not app.check_action("toggle_reports", ())

    asyncio.run(go())
