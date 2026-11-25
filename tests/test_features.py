"""Unread tracking, inbox, edit/delete, search, verification, head-cached index."""

import pytest

from conftest import run, unique, wait_for
from scootaloo import core
from scootaloo.accounts import Users


@pytest.fixture
def pair(servers):
    """moxxie and pinkie, both with identities on alpha, plus a fresh board there."""
    accts = {}
    for user, pw in (("moxxie", "hunter222"), ("pinkie", "partypony1")):
        with Users() as u:
            u.add(user, pw)
            accts[user] = u.login(user, pw)
        run(core.connect(accts[user], servers["alpha.test"]))
        run(core.register(core.Session.open(accts[user], "alpha.test"), unique(user[:4])))
    board = unique("feat")
    run(core.create_board(core.Session.open(accts["moxxie"], "alpha.test"), board))
    ref = core.BoardRef("alpha.test", board, "alpha.test")
    return accts["moxxie"], accts["pinkie"], ref


def test_index_only_relists_when_head_moves(pair):
    mox, _, ref = pair
    idx = core.Index()
    assert ref in run(idx.refresh(mox, [ref])) or idx.rows(ref) == []
    assert run(idx.refresh(mox, [ref])) == set()  # nothing new: head unchanged, no re-list
    run(core.post(mox, ref, "new", "body"))
    assert run(idx.refresh(mox, [ref])) == {ref}
    assert [r.subject for r in idx.rows(ref)] == ["new"]


def test_unread_floor_and_marking(pair):
    mox, pinkie, ref = pair
    old = run(core.post(pinkie, ref, "before you looked", "old"))
    idx = core.Index()
    run(idx.refresh(mox, [ref]))
    assert core.unread(mox, idx)[ref] == set()  # first sight: the floor covers what's there

    root = run(core.post(pinkie, ref, "after", "new"))
    reply = run(core.reply(pinkie, ref, root.article_num, "more"))
    run(idx.refresh(mox, [ref]))
    un = core.unread(mox, idx)[ref]
    assert un == {root.article_num, reply.article_num}
    assert core.unread_threads(idx, ref, un) == {idx.rows(ref)[-2].article_id: 2}

    th = run(core.thread(mox, ref, reply.article_num))
    core.mark_read(mox, ref, [i.view.article_num for i in th.items])
    assert core.unread(mox, idx)[ref] == set()

    run(core.post(pinkie, ref, "third", "x"))
    run(idx.refresh(mox, [ref]))
    assert len(core.unread(mox, idx)[ref]) == 1
    core.mark_board_read(mox, idx, ref)
    assert core.unread(mox, idx)[ref] == set()
    assert old.article_num < root.article_num


def test_unread_is_per_user(pair):
    mox, pinkie, ref = pair
    idx_m, idx_p = core.Index(), core.Index()
    run(idx_m.refresh(mox, [ref]))
    run(idx_p.refresh(pinkie, [ref]))
    core.unread(mox, idx_m), core.unread(pinkie, idx_p)  # both have now looked at the board
    run(core.post(mox, ref, "hi", "x"))
    run(idx_m.refresh(mox, [ref]))
    run(idx_p.refresh(pinkie, [ref]))
    core.mark_board_read(mox, idx_m, ref)
    assert core.unread(mox, idx_m)[ref] == set()
    assert len(core.unread(pinkie, idx_p)[ref]) == 1


def test_inbox_shows_replies_from_others(pair):
    mox, pinkie, ref = pair
    root = run(core.post(mox, ref, "will anyone reply?", "hello"))
    run(core.reply(mox, ref, root.article_num, "bumping my own post"))  # not inbox material
    theirs = run(core.reply(pinkie, ref, root.article_num, "yes!"))
    idx = core.Index()
    run(idx.refresh(mox, [ref]))
    items = core.inbox(mox, idx)
    assert [(i.row.article_num, i.parent.article_num) for i in items] == [(theirs.article_num, root.article_num)]
    assert core.inbox(pinkie, idx) == []


def test_edit_supersedes_in_place(pair):
    mox, pinkie, ref = pair
    root = run(core.post(mox, ref, "draft", "typo here"))
    reply = run(core.reply(pinkie, ref, root.article_num, "nice"))
    new = run(core.edit(mox, ref, root.article_num, "fixed", "final"))
    th = run(core.thread(mox, ref, reply.article_num))
    assert [(i.view.article_num, i.depth, i.view.subject) for i in th.items][0] == (new.article_num, 0, "final")
    assert th.items[0].body == "fixed"
    assert any(i.view.article_num == reply.article_num and i.depth == 1 for i in th.items)
    assert [a.subject for a in run(core.articles(mox, ref))] == ["final"]

    with pytest.raises(core.ScootalooError, match="only change your own"):
        run(core.edit(mox, ref, reply.article_num, "hijack"))
    with pytest.raises(core.ScootalooError, match="only a live article"):
        run(core.edit(mox, ref, root.article_num, "again"))


def test_edit_a_reply_keeps_it_threaded(pair):
    mox, pinkie, ref = pair
    root = run(core.post(pinkie, ref, "question", "?"))
    mine = run(core.reply(mox, ref, root.article_num, "answr"))
    new = run(core.edit(mox, ref, mine.article_num, "answer"))
    th = run(core.thread(mox, ref, root.article_num))
    assert [(i.view.article_num, i.depth, i.body) for i in th.items] == [
        (root.article_num, 0, "?"), (new.article_num, 1, "answer"),
    ]


def test_delete_own_only(pair):
    mox, pinkie, ref = pair
    mine = run(core.post(mox, ref, "oops", "delete me"))
    theirs = run(core.post(pinkie, ref, "keep", "not yours"))
    with pytest.raises(core.ScootalooError, match="only change your own"):
        run(core.delete(mox, ref, theirs.article_num))
    run(core.delete(mox, ref, mine.article_num))
    assert [a.subject for a in run(core.articles(mox, ref))] == ["keep"]


def test_search(pair):
    mox, pinkie, ref = pair
    run(core.post(pinkie, ref, "Cutie mark crusaders", "scootaloo apple bloom sweetie belle"))
    run(core.post(mox, ref, "unrelated", "nothing to see"))
    hits = run(core.search(mox, [ref], "cutie"))
    assert [h.subject for h in hits] == ["Cutie mark crusaders"]
    with pytest.raises(core.ScootalooError):
        run(core.search(mox, [ref], "   "))


def test_verify_checks_signatures_and_body(pair):
    mox, pinkie, ref = pair
    a = run(core.post(pinkie, ref, "signed", "check me"))
    v = run(core.verify(mox, ref, a.article_num))
    assert v.author_sig == "valid"
    assert v.body == "matches"
    assert v.view_matches, v.mismatches
    assert v.origin_sig in ("valid", "unverifiable")
    assert v.hops and all(h["signature_valid"] for h in v.hops)
    assert v.hops[0]["is_origin"]


def test_verify_foreign_article_through_relay(servers):
    with Users() as u:
        u.add("rd", "rainbowdash")
        rd = u.login("rd", "rainbowdash")
    run(core.connect(rd, servers["beta.test"]))
    run(core.register(core.Session.open(rd, "beta.test"), unique("rd")))
    board = unique("cloud")
    run(core.create_board(core.Session.open(rd, "beta.test"), board))
    a = run(core.post(rd, core.BoardRef("beta.test", board, "beta.test"), "20% cooler", "fast"))

    with Users() as u:
        u.add("tw", "twilight1")
        tw = u.login("tw", "twilight1")
    run(core.connect(tw, servers["alpha.test"]))
    ref = wait_for(lambda: run(core.resolve_board(tw, board)))
    assert ref.via == "alpha.test"
    v = wait_for(lambda: run(core.verify(tw, ref, a.article_num)))
    assert v.author_sig == "valid" and v.body == "matches" and v.view_matches
    # the record reached alpha from beta: an origin witness by beta, then alpha's own
    origins = [h["relay_origin"] for h in v.hops if h["linked"]]
    assert origins[0] == "beta.test" and "alpha.test" in origins


def test_delete_refused_by_stock_acl(servers):
    with Users() as u:
        u.add("spike", "dragonfire")
        spike = u.login("spike", "dragonfire")
    run(core.connect(spike, servers["beta.test"]))
    run(core.register(core.Session.open(spike, "beta.test"), unique("spk")))
    board = unique("gems")
    run(core.create_board(core.Session.open(spike, "beta.test"), board))
    ref = core.BoardRef("beta.test", board, "beta.test")
    a = run(core.post(spike, ref, "mine", "x"))
    with pytest.raises(core.ScootalooError, match="doesn't let users delete"):
        run(core.delete(spike, ref, a.article_num))
