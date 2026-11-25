"""The CLI end to end, through main(argv), with output captured."""

import pytest

from conftest import unique
from scootaloo.cli import main


@pytest.fixture
def cli(servers, monkeypatch, capsys):
    def sh(*argv, user="moxxie", pw="hunter222"):
        monkeypatch.setenv("SCOOTALOO_USER", user)
        monkeypatch.setenv("SCOOTALOO_PASSWORD", pw)
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out + out.err

    for user, pw in (("moxxie", "hunter222"), ("pinkie", "partypony1")):
        monkeypatch.setenv("SCOOTALOO_PASSWORD", pw)
        assert main(["user", "add", user]) == 0
        assert sh("connect", servers["alpha.test"], user=user, pw=pw)[0] == 0
        assert sh("register", unique(user[:4]), user=user, pw=pw)[0] == 0
    capsys.readouterr()
    return sh


def test_full_round_trip(cli):
    sh = cli
    board = unique("cli")
    assert sh("mkboard", board)[0] == 0
    code, out = sh("post", board, "-s", "hello world", "-m", "first")
    assert code == 0 and "posted #1" in out

    # both have looked at the board once (that sets each one's unread floor)
    sh("unread")
    sh("unread", user="pinkie", pw="partypony1")
    # pinkie replies; moxxie's inbox and unread pick it up
    code, out = sh("reply", board, "1", "-m", "hi back", user="pinkie", pw="partypony1")
    assert code == 0 and "#2" in out
    code, out = sh("inbox")
    assert code == 0 and "Re: hello world" in out and "●" in out
    code, out = sh("ls", board)
    assert "●" in out  # pinkie's reply came after moxxie's first look
    code, out = sh("read", board, "2")
    assert "first" in out and "hi back" in out
    code, out = sh("unread")
    assert "nothing unread" in out

    # edit and verify
    code, out = sh("edit", board, "1", "-m", "first (edited)")
    assert code == 0 and "edited #1" in out
    code, out = sh("read", board, "2")
    assert "first (edited)" in out
    code, out = sh("search", "hello", board)
    assert code == 0 and "hello world" in out
    code, out = sh("verify", board, "2")
    assert code == 0 and "author signature" in out and "verified" in out

    # can't edit or delete someone else's
    code, out = sh("edit", board, "2", "-m", "nope")
    assert code == 1 and "only change your own" in out
    code, out = sh("delete", board, "2", "-y")
    assert code == 1 and "only change your own" in out

    # delete own (alpha grants cancel in the test config)
    code, out = sh("delete", board, "2", "-y", user="pinkie", pw="partypony1")
    assert code == 0 and "deleted #2" in out


def test_wrong_password(cli):
    code, out = cli("whoami", pw="not-it-at-all")
    assert code == 1 and "wrong user name or password" in out


def test_gateway_store_is_separate(cli, servers, monkeypatch, capsys):
    monkeypatch.delenv("SCOOTALOO_PASSWORD", raising=False)
    code = main(["--gateway-store", "whoami"])
    out = capsys.readouterr().out
    assert code == 0 and "gateway store" in out and "no relays yet" in out
