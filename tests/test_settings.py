"""config.json, preferences.json, and the login screen's different faces."""

import asyncio
import json
import os

import pytest
from textual.widgets import Input, OptionList

from scootaloo import settings
from scootaloo.accounts import Stores, Users, home
from scootaloo.settings import SettingsError
from scootaloo.tui import Login, ScootalooApp


# -- files -----------------------------------------------------------------------


def test_config_defaults_set_and_persist():
    c = settings.config()
    assert not c.exists and c.get("login.list_users") is True
    c.set("login.list_users", "off")  # strings from the CLI are parsed to the default's type
    c.save()
    assert json.load(open(os.path.join(home(), "config.json")))["login"]["list_users"] is False
    assert settings.config().get("login.list_users") is False


@pytest.mark.parametrize(
    "key,value,msg",
    [
        ("login.list_users", "maybe", "true or false"),
        ("login.list_users", 1, "takes a bool"),
        ("nope", True, "unknown setting"),
        ("login", True, "group"),
        ("version", 2, "managed"),
    ],
)
def test_config_rejects(key, value, msg):
    with pytest.raises(SettingsError, match=msg):
        settings.config().set(key, value)


def test_preferences_validate_choices_and_ranges():
    with Users() as u:
        u.add("rarity", "fabulous")
    p = settings.preferences(Stores.for_user("rarity"))
    p.set("layout.name", "two")
    p.set("layout.list_share", "70")
    with pytest.raises(SettingsError, match="one of"):
        p.set("layout.name", "diagonal")
    with pytest.raises(SettingsError, match="between"):
        p.set("layout.boards_width", 500)
    p.save()
    again = settings.preferences(Stores.for_user("rarity"))
    assert again.get("layout.name") == "two" and again.get("layout.list_share") == 70


def test_unknown_keys_survive_and_bad_types_fall_back():
    path = os.path.join(home(), "config.json")
    os.makedirs(home(), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"version": 1, "login": {"list_users": "yes please"}, "from_the_future": {"x": 1}}, f)
    c = settings.config()
    assert c.get("login.list_users") is True  # wrong type in the file: the default is used
    c.save()
    assert json.load(open(path))["from_the_future"] == {"x": 1}


def test_broken_file_is_kept_aside():
    path = os.path.join(home(), "config.json")
    os.makedirs(home(), exist_ok=True)
    with open(path, "w") as f:
        f.write("{ not json")
    c = settings.config()
    assert c.get("login.list_users") is True and "broken" in c.warning
    assert os.path.exists(path + ".broken") and not os.path.exists(path)


# -- the login screen ----------------------------------------------------------------


def visible(screen, sel):
    return screen.query_one(sel).display


async def login_screen(pilot, app):
    for _ in range(30):
        await pilot.pause(0.1)
        if isinstance(app.screen, Login):
            return app.screen
    raise AssertionError("no login screen")


def test_no_users_goes_straight_to_create():
    async def go():
        app = ScootalooApp()
        async with app.run_test(size=(120, 35)) as pilot:
            s = await login_screen(pilot, app)
            assert s.mode == "create" and visible(s, "#login-pw2") and not visible(s, "#login-back")

    asyncio.run(go())


def test_single_user_only_asks_for_the_password():
    with Users() as u:
        u.add("moxxie", "hunter222")

    async def go():
        app = ScootalooApp()
        async with app.run_test(size=(120, 35)) as pilot:
            s = await login_screen(pilot, app)
            assert s.mode == "password" and s.chosen == "moxxie"
            assert not visible(s, "#login-name") and not visible(s, "#login-users")
            assert "moxxie" in str(s.query_one("#login-mode").render())
            await pilot.press(*"hunter222", "enter")
            await pilot.pause(0.5)
            assert app.acct and app.acct.name == "moxxie"

    asyncio.run(go())


def test_several_users_menu_then_password_then_back():
    with Users() as u:
        u.add("moxxie", "hunter222")
        u.add("pinkie", "partypony")
        u.login("pinkie", "partypony")  # last to log in is highlighted

    async def go():
        app = ScootalooApp()
        async with app.run_test(size=(120, 35)) as pilot:
            s = await login_screen(pilot, app)
            ol = s.query_one("#login-users", OptionList)
            assert s.mode == "menu" and not visible(s, "#login-pw")
            assert [ol.get_option_at_index(i).id for i in range(ol.option_count)][:2] == ["moxxie", "pinkie"]
            assert ol.highlighted == 1
            await pilot.press("up", "enter")
            assert s.mode == "password" and s.chosen == "moxxie" and visible(s, "#login-back")
            await pilot.press("escape")
            assert s.mode == "menu"
            # "New user…" is the last entry
            await pilot.press("end", "enter")
            assert s.mode == "create"
            await pilot.press("escape")
            assert s.mode == "menu"
            await pilot.press("enter", *"partypony", "enter")  # pinkie is highlighted again
            await pilot.pause(0.5)
            assert app.acct and app.acct.name == "pinkie"

    asyncio.run(go())


def test_listing_off_shows_a_blank_form():
    with Users() as u:
        u.add("moxxie", "hunter222")
        u.add("pinkie", "partypony")
    c = settings.config()
    c.set("login.list_users", False)
    c.save()

    async def go():
        app = ScootalooApp()
        async with app.run_test(size=(120, 35)) as pilot:
            s = await login_screen(pilot, app)
            assert s.mode == "blank" and not visible(s, "#login-users")
            assert s.query_one("#login-name", Input).value == ""
            await pilot.press(*"moxxie", "enter", *"hunter222", "enter")
            await pilot.pause(0.5)
            assert app.acct and app.acct.name == "moxxie"

    asyncio.run(go())


def test_wrong_password_stays_on_the_screen():
    with Users() as u:
        u.add("moxxie", "hunter222")

    async def go():
        app = ScootalooApp()
        async with app.run_test(size=(120, 35)) as pilot:
            s = await login_screen(pilot, app)
            await pilot.press(*"nope", "enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, Login) and "wrong" in str(s.query_one("#login-error").render())

    asyncio.run(go())


def test_layout_migrates_from_state_db():
    from scootaloo import core

    with Users() as u:
        u.add("moxxie", "hunter222")
        acct = u.login("moxxie", "hunter222")
    with core.read_state(acct) as st:  # how 0.5.x stored it
        st.set("layout.name", "stacked")
        st.set("layout.list_share", "35")

    async def go():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(160, 40)) as pilot:
            for _ in range(30):
                await pilot.pause(0.1)
                if app.acct:
                    break
            await pilot.pause(0.3)
            assert app.layout_name == "stacked" and app.list_share == 35

    asyncio.run(go())
    p = settings.preferences(acct.stores)
    assert p.exists and p.get("layout.name") == "stacked" and p.get("layout.list_share") == 35
