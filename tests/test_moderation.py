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
    with pytest.raises(core.ScootalooError, match=r"refused .* for .*\(") as err:
        run(core.moderate(mox, ref, num, action, reason="spam"))
    assert "press i" in str(err.value)


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
            assert any("refused pin" in m for m in msgs), msgs

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


def test_reports_tab_is_cached_until_a_log_moves(post, monkeypatch):
    """Stand-in queue (the test servers have no moderators): count real fetches."""
    import asyncio
    from types import SimpleNamespace
    from textual.widgets import DataTable, TabbedContent
    from scootaloo.tui import ScootalooApp
    from test_tui import settle

    mox, ref, num = post
    calls = []

    async def fake_reports(acct, relay=None, limit=100):
        calls.append(relay)
        info = SimpleNamespace(created_at=1, reporter_username="pinkie", reporter_pubkey="ab" * 32,
                               culprit_pubkey="cd" * 32, target_kind="article", target_origin=ref.origin,
                               target_board=ref.name, target_article_id="00" * 32)
        return [core.Report(info, f"report #{len(calls)}", relay)]

    async def yes(*a, **k):
        return True

    monkeypatch.setattr(core, "reports", fake_reports)
    monkeypatch.setattr(core, "can_read_reports", yes)

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(150, 45)) as pilot:
            await settle(pilot, app)
            await settle(pilot, app)
            assert app.can_reports
            await pilot.press("R")
            await settle(pilot, app)
            rt = app.query_one("#reports", DataTable)
            assert len(calls) == 1 and rt.row_count == 1
            await pilot.press("R", "R")  # away and back: served from the cache
            await settle(pilot, app)
            assert len(calls) == 1 and rt.row_count == 1
            await core.post(mox, ref, "something new", "x")  # a log moved
            app.load_reports()
            await settle(pilot, app)
            assert len(calls) == 2
            await pilot.press("r")  # refresh on the tab forces a fetch
            await settle(pilot, app)
            assert len(calls) == 3
            assert app.query_one("#tabs", TabbedContent).active == "tab-reports"

    asyncio.run(go())
