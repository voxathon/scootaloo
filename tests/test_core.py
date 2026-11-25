"""Against two real federated servers: connecting, posting, threads, cross-origin reads."""

import pytest

from conftest import run, unique, wait_for
from scootaloo import core
from scootaloo.accounts import Users


@pytest.fixture
def mox(servers):
    """A user who joined both relays and holds an identity on each, plus a board on each."""
    with Users() as u:
        u.add("moxxie", "hunter222")
        acct = u.login("moxxie", "hunter222")
    run(core.connect(acct, servers["beta.test"]))
    run(core.connect(acct, servers["alpha.test"]))
    names = {"alpha.test": unique("mox"), "beta.test": unique("moxb")}
    boards = {"alpha.test": unique("gen"), "beta.test": unique("lounge")}
    for origin in ("alpha.test", "beta.test"):
        sess = core.Session.open(acct, origin)
        run(core.register(sess, names[origin]))
        sess = core.Session.open(acct, origin)
        run(core.create_board(sess, boards[origin], "Board"))
    acct.names, acct.boards = names, boards
    return acct


def ref_for(acct, origin, via=None):
    return core.BoardRef(origin, acct.boards[origin], via or origin)


def test_connect_remembers_relay_and_active(mox):
    relays = {r["origin"] for r in core.joined(mox)}
    assert relays == {"alpha.test", "beta.test"}
    assert core.active_relay(mox)["origin"] == "alpha.test"


def test_register_on_one_relay_keeps_active(mox):
    # registering on beta (done in the fixture after connecting alpha last) must not steal "active"
    assert core.active_relay(mox)["origin"] == "alpha.test"
    assert core.Session.open(mox, "beta.test").username == mox.names["beta.test"]


def test_keys_are_encrypted(mox):
    for r in core.identities(mox, "alpha.test"):
        assert r["wrapped"]


def test_post_reply_and_thread(mox):
    ref = ref_for(mox, "alpha.test")
    root = run(core.post(mox, ref, "hello", "first **post**"))
    r1 = run(core.reply(mox, ref, root.article_num, "a reply"))
    r2 = run(core.reply(mox, ref, r1.article_num, "a reply to the reply"))
    th = run(core.thread(mox, ref, r2.article_num))  # any article in the thread works
    assert [(i.view.article_num, i.depth) for i in th.items] == [
        (root.article_num, 0), (r1.article_num, 1), (r2.article_num, 2),
    ]
    assert th.items[0].body == "first **post**"
    assert th.items[1].view.subject == "Re: hello"
    roots = run(core.articles(mox, ref))
    assert [a.article_num for a in roots] == [root.article_num]


def test_all_boards_spans_origins(mox):
    names = wait_for(lambda: {(b.origin, b.name) for b in run(core.all_boards(mox))} >= {
        ("alpha.test", mox.boards["alpha.test"]), ("beta.test", mox.boards["beta.test"]),
    })
    assert names
    by = {(b.origin, b.name): b for b in run(core.all_boards(mox))}
    # with both relays joined, each origin's boards are read through its own relay
    assert by[("beta.test", mox.boards["beta.test"])].via == "beta.test"


def test_resolve_board_forms(mox):
    b = mox.boards["beta.test"]
    wait_for(lambda: run(core.resolve_board(mox, b)))
    assert run(core.resolve_board(mox, f"{b}@beta.test")).origin == "beta.test"
    assert run(core.resolve_board(mox, b, origin="beta.test")).origin == "beta.test"
    with pytest.raises(core.ScootalooError, match="no board"):
        run(core.resolve_board(mox, "does-not-exist"))


def test_reading_a_foreign_board_through_one_relay(mox, servers):
    """A second user who only joined alpha reads beta's board through it, read-only."""
    ref = ref_for(mox, "beta.test")
    posted = run(core.post(mox, ref, "from beta", "beta body"))

    with Users() as u:
        u.add("pinkie", "partypony1")
        pinkie = u.login("pinkie", "partypony1")
    run(core.connect(pinkie, servers["alpha.test"]))

    foreign = wait_for(lambda: run(core.resolve_board(pinkie, mox.boards["beta.test"])))
    assert foreign.origin == "beta.test" and foreign.via == "alpha.test" and not foreign.is_home
    th = wait_for(lambda: run(core.thread(pinkie, foreign, posted.article_num)))
    assert th.items[0].body == "beta body"

    with pytest.raises(core.ScootalooError, match="lives on beta.test"):
        run(core.post(pinkie, foreign, "x", "y"))


def test_write_refusals(mox, servers):
    with pytest.raises(core.ScootalooError, match="bridge board"):
        core.writable(mox, core.BoardRef("alpha.test", "~flatboard", "alpha.test"))
    with Users() as u:
        u.add("anon", "anonymous1")
        anon = u.login("anon", "anonymous1")
    run(core.connect(anon, servers["alpha.test"]))
    with pytest.raises(core.ScootalooError, match="no identity"):
        core.writable(anon, ref_for(mox, "alpha.test"))


def test_register_name_taken_by_another_key(mox, servers):
    with Users() as u:
        u.add("rival", "rivalrival")
        rival = u.login("rival", "rivalrival")
    run(core.connect(rival, servers["alpha.test"]))
    with pytest.raises(core.ScootalooError, match="different key"):
        run(core.register(core.Session.open(rival, "alpha.test"), mox.names["alpha.test"]))
