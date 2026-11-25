import os

import pytest

from scootaloo.accounts import AccountError, Stores, Users, current, default_user


def test_add_login_and_verify():
    with Users() as u:
        u.add("moxxie", "correct horse")
        assert u.names() == ["moxxie"]
        assert u.verify("moxxie", "correct horse")
        assert not u.verify("moxxie", "wrong horse")
        assert not u.verify("nobody", "correct horse")
        acct = u.login("moxxie", "correct horse")
    assert acct.name == "moxxie" and acct.wraps_keys
    assert current() == "moxxie"
    assert default_user() == "moxxie"


@pytest.mark.parametrize(
    "name,pw,msg",
    [
        ("Bad Name", "longenough", "user names"),
        ("", "longenough", "user names"),
        ("ok", "", "needs a password"),
    ],
)
def test_add_rejects(name, pw, msg):
    with Users() as u, pytest.raises(AccountError, match=msg):
        u.add(name, pw)


def test_short_passwords_are_allowed():
    with Users() as u:
        u.add("tiny", "x")
        assert u.verify("tiny", "x")


def test_duplicate_user():
    with Users() as u:
        u.add("pinkie", "partypony")
        with pytest.raises(AccountError, match="already"):
            u.add("pinkie", "partypony")


def test_login_wrong_password():
    with Users() as u:
        u.add("rarity", "fabulous!")
        with pytest.raises(AccountError, match="wrong"):
            u.login("rarity", "nope nope")


def test_users_are_isolated(scootaloo_home):
    with Users() as u:
        u.add("a1", "password1")
        u.add("b1", "password2")
    a, b = Stores.for_user("a1"), Stores.for_user("b1")
    assert os.path.dirname(a.identities_path) != os.path.dirname(b.identities_path)
    s = a.identities()
    s.register("x.test", "someone", "password1")
    s.close()
    s = b.identities()
    assert s.list_users() == []
    s.close()


def test_default_user_ambiguous(monkeypatch):
    with Users() as u:
        u.add("one", "password1")
        u.add("two", "password2")
    from scootaloo import accounts

    accounts.set_current("")
    assert default_user() is None
    monkeypatch.setenv("SCOOTALOO_USER", "two")
    assert default_user() == "two"


def test_change_password_reencrypts_keys():
    with Users() as u:
        u.add("applejack", "honesty1")
        s = Stores.for_user("applejack").identities()
        _, pub1 = s.register("a.test", "aj", "honesty1")
        _, pub2 = s.register("b.test", "aj2", "honesty1")
        s.mark_registered("a.test", "aj")
        seed1 = s.get_private_key("a.test", "aj", "honesty1")
        s.close()

        assert u.change_password("applejack", "honesty1", "applesauce") == 2
        assert not u.verify("applejack", "honesty1")
        assert u.verify("applejack", "applesauce")

    s = Stores.for_user("applejack").identities()
    try:
        assert s.get_private_key("a.test", "aj", "applesauce") == seed1
        with pytest.raises(ValueError):
            s.get_private_key("a.test", "aj", "honesty1")
        rows = {r["username"]: r for r in s.list_users()}
        assert rows["aj"]["registered"] and not rows["aj2"]["registered"]
        assert bytes.fromhex(rows["aj2"]["public_key"]) == pub2
    finally:
        s.close()
    d = os.path.dirname(Stores.for_user("applejack").identities_path)
    leftovers = set(os.listdir(d)) & {"identities.db.bak", "identities.db.new"}
    assert not leftovers


@pytest.mark.parametrize(
    "old,new,msg",
    [("wrongpass", "newpass99", "wrong"), ("flutters1", "", "needs a password"), ("flutters1", "flutters1", "same")],
)
def test_change_password_refusals_change_nothing(old, new, msg):
    with Users() as u:
        u.add("fluttershy", "flutters1")
        s = Stores.for_user("fluttershy").identities()
        s.register("a.test", "fs", "flutters1")
        s.close()
        with pytest.raises(AccountError, match=msg):
            u.change_password("fluttershy", old, new)
        assert u.verify("fluttershy", "flutters1")
    s = Stores.for_user("fluttershy").identities()
    assert len(s.get_private_key("a.test", "fs", "flutters1")) == 32
    s.close()


def test_remove_user_deletes_keys():
    with Users() as u:
        u.add("trixie", "greatandpowerful")
        d = os.path.dirname(Stores.for_user("trixie").identities_path)
        assert os.path.isdir(d)
        with pytest.raises(AccountError):
            u.remove("trixie", "wrong")
        u.remove("trixie", "greatandpowerful")
        assert u.names() == []
    assert not os.path.exists(d)
