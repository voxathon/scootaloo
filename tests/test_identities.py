"""Identity export / import: the bonnet-identities format, core logic, CLI and TUI."""

import asyncio
import base64
import json
import os

import pytest
from textual.widgets import Input, OptionList, Static

from conftest import run, unique
from scootaloo import core
from scootaloo import idarchive as ia
from scootaloo.accounts import Stores, Users, home
from scootaloo.cli import main
from scootaloo.tui import Ask, Identities, ScootalooApp, Show, YesNo
from test_tui import settle, wait_screen


def entry(origin="alpha.test", username="moxxie", origin_key=""):
    seed = os.urandom(32).hex()
    return ia.Entry(origin, username, seed, ia.derive_public(seed), True, ia.Relay("https://x", True, origin_key))


# -- the format ------------------------------------------------------------------


def test_round_trip_and_privacy():
    e = entry(origin_key="ab" * 32)
    data = ia.build([e], "correct horse", "test")
    env = json.loads(data)
    assert env["format"] == "bonnet-identities" and env["version"] == 1
    assert env["kdf"]["name"] == "argon2id" and env["cipher"] == "xsalsa20poly1305"
    assert b"moxxie" not in data and e.private_key_hex.encode() not in data  # nothing readable outside
    opened = ia.open_archive(data, "correct horse")
    assert opened.entries == [e] and opened.exported_by == "test" and opened.rejected == []


@pytest.mark.parametrize("mutate,msg", [
    (lambda env: env.update(format="something-else"), "isn't a bonnet-identities"),
    (lambda env: env.update(version=2), "format version 2"),
    (lambda env: env["kdf"].update(memlimit=8 * 1024 ** 3), "outside what scootaloo will run"),
    (lambda env: env["kdf"].update(name="scrypt"), "key derivation"),
    (lambda env: env.update(cipher="aes"), "cipher"),
    (lambda env: env.update(nonce=base64.b64encode(b"short").decode()), "nonce"),
])
def test_envelope_checks(mutate, msg):
    env = json.loads(ia.build([entry()], "pw", "test"))
    mutate(env)
    with pytest.raises(ia.ArchiveError, match=msg):
        ia.open_archive(json.dumps(env).encode(), "pw")


def test_wrong_passphrase_and_tampering_fail_alike():
    data = ia.build([entry()], "right", "test")
    with pytest.raises(ia.ArchiveError, match="wrong passphrase, or the archive was changed"):
        ia.open_archive(data, "wrong")
    env = json.loads(data)
    ct = bytearray(base64.b64decode(env["payload"]))
    ct[-1] ^= 1
    env["payload"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(ia.ArchiveError, match="wrong passphrase, or the archive was changed"):
        ia.open_archive(json.dumps(env).encode(), "right")


def test_not_json_at_all():
    with pytest.raises(ia.ArchiveError, match="not JSON"):
        ia.open_archive(b"\x00\x01 binary junk", "x")


def test_refuses_to_build_a_broken_identity():
    e = entry()
    e.public_key_hex = "00" * 32
    with pytest.raises(ia.ArchiveError, match="doesn't match"):
        ia.build([e], "pw", "test")


def test_parse_manual_forms():
    e = entry()
    gw = {"origin": e.origin, "username": e.username, "private_key_hex": e.private_key_hex,
          "public_key_hex": e.public_key_hex, "wrapped": True, "venue_accounts": []}
    got = ia.parse_manual(json.dumps(gw))
    assert (got.origin, got.username, got.public_key_hex) == (e.origin, e.username, e.public_key_hex)
    bare = ia.parse_manual("  " + e.private_key_hex.upper() + "\n", "alpha.test", "moxxie")
    assert bare.public_key_hex == e.public_key_hex and bare.intact
    with pytest.raises(ia.ArchiveError, match="64 hex"):
        ia.parse_manual("abc", "alpha.test", "moxxie")
    with pytest.raises(ia.ArchiveError, match="origin"):
        ia.parse_manual(e.private_key_hex)
    with pytest.raises(ia.ArchiveError, match="whole archive"):
        ia.parse_manual(ia.build([e], "pw", "t").decode())


def test_seed_json_is_gateway_shaped():
    e = entry()
    d = json.loads(ia.seed_json(e))
    assert d["private_key_hex"] == e.private_key_hex and d["public_key_hex"] == e.public_key_hex
    assert ia.parse_manual(ia.seed_json(e)) == ia.Entry(e.origin, e.username, e.private_key_hex, e.public_key_hex)


# -- core, against the test servers --------------------------------------------------


@pytest.fixture
def two_users(servers):
    """moxxie holds a registered identity on alpha and a board there; pinkie has nothing yet."""
    with Users() as u:
        u.add("moxxie", "hunter222")
        mox = u.login("moxxie", "hunter222")
        u.add("pinkie", "partypony")
        pinkie = u.login("pinkie", "partypony")
    run(core.connect(mox, servers["alpha.test"]))
    name = unique("mox")
    run(core.register(core.Session.open(mox, "alpha.test"), name))
    board = unique("idb")
    run(core.create_board(core.Session.open(mox, "alpha.test"), board))
    return mox, pinkie, name, core.BoardRef("alpha.test", board, "alpha.test")


def test_export_import_round_trip_and_post(two_users):
    mox, pinkie, name, ref = two_users
    entries = core.collect_identities(mox)
    assert [(e.origin, e.username, e.registered) for e in entries] == [("alpha.test", name, True)]
    assert entries[0].relay.origin_key  # the pin travels with it
    data = ia.build(entries, "archive pass", "test")

    opened = ia.open_archive(data, "archive pass")
    plans = core.plan_import(pinkie, opened.entries)
    assert [p.action for p in plans] == ["import"]
    results = run(core.import_identities(pinkie, plans))
    assert results[0].outcome == "imported"
    assert any("joined alpha.test" in n for n in results[0].notes)

    # re-wrapped under pinkie's password, and it posts
    row = core.identities(pinkie, "alpha.test")[0]
    assert row["username"] == name and row["wrapped"] and row["registered"]
    assert core.Session.open(pinkie, "alpha.test").username == name
    posted = run(core.post(pinkie, ref, "from the imported identity", "works"))
    th = run(core.thread(mox, ref, posted.article_num))
    assert th.items[0].view.author_username == name and th.items[0].view.author_check == "registry"


def test_skip_conflict_and_replace_with_backup(two_users):
    mox, pinkie, name, ref = two_users
    mine = core.collect_identities(mox)
    # same identity again: skipped
    assert [p.action for p in core.plan_import(mox, mine)] == ["skip"]
    # same origin + name, different key: refused without --replace
    other = entry(origin="alpha.test", username=name)
    plans = core.plan_import(mox, [other])
    assert [p.action for p in plans] == ["conflict"]
    res = run(core.import_identities(mox, plans, check_live=False))
    assert res[0].outcome == "refused"
    assert core.identities(mox, "alpha.test")[0]["public_key"] == mine[0].public_key_hex
    with pytest.raises(core.ScootalooError, match="backup"):
        run(core.import_identities(mox, plans, replace=True, check_live=False))

    # replace: the old key is backed up first, and the backup opens
    res = run(core.import_identities(mox, plans, replace=True, backup_passphrase="backup pass", check_live=False))
    assert res[0].outcome == "replaced" and os.path.exists(res[0].backup)
    assert res[0].backup.startswith(os.path.join(home(), "backups"))
    backed = ia.open_archive(open(res[0].backup, "rb").read(), "backup pass")
    assert backed.entries[0].private_key_hex == mine[0].private_key_hex
    assert core.identities(mox, "alpha.test")[0]["public_key"] == other.public_key_hex


def test_corrupt_entries_are_rejected():
    with Users() as u:
        u.add("x1", "password")
        acct = u.login("x1", "password")
    bad = entry()
    bad.public_key_hex = "11" * 32
    plans = core.plan_import(acct, [bad, bad])
    assert [p.action for p in plans] == ["corrupt", "corrupt"]
    assert run(core.import_identities(acct, plans))[0].outcome == "rejected"
    assert core.identities(acct, "alpha.test") == []


def test_liveness_warns_about_unknown_keys(two_users):
    mox, pinkie, name, ref = two_users
    run(core.connect(pinkie, core.joined(mox)[0]["url"]))
    stranger = entry(origin="alpha.test", username=unique("ghost"))
    res = run(core.import_identities(pinkie, core.plan_import(pinkie, [stranger])))
    assert res[0].outcome == "imported" and any("doesn't know this key" in n for n in res[0].notes)


def test_relay_pin_from_archive(two_users, monkeypatch):
    """A relay key that matches the archive is trusted without asking; one that doesn't is asked about."""
    mox, pinkie, name, ref = two_users
    monkeypatch.setattr(core, "pin_mode_for", lambda url: "confirm")  # loopback is normally exempt
    asked = []

    async def confirm(p, url):
        asked.append(p.origin)
        return False

    # matching key: no question
    good = core.collect_identities(mox)
    res = run(core.import_identities(pinkie, core.plan_import(pinkie, good), confirm_pin=confirm))
    assert asked == [] and any("trusted without asking" in n for n in res[0].notes)

    # a third user, and an archive whose recorded key is wrong: asked, declined, not joined
    with Users() as u:
        u.add("rarity", "fabulous")
        rarity = u.login("rarity", "fabulous")
    wrong = core.collect_identities(mox)
    wrong[0].relay.origin_key = "cd" * 32
    res = run(core.import_identities(rarity, core.plan_import(rarity, wrong), confirm_pin=confirm))
    assert asked == ["alpha.test"] and core.joined(rarity) == []
    assert any("wasn't trusted" in n for n in res[0].notes)
    assert res[0].outcome == "imported"  # the identity itself is still imported


def test_gateway_store_round_trip(two_users):
    """An identity from the gateway store (unwrapped) imports into a scootaloo user, wrapped."""
    mox, pinkie, name, ref = two_users
    gw = Stores.gateway().identities()
    seed = os.urandom(32)
    gw.register("alpha.test", "gwuser", None, private_key=seed)
    gw.close()
    from scootaloo.accounts import Account

    gateway = Account(Stores.gateway(), "gateway store", None, gateway=True)
    entries = core.collect_identities(gateway)
    assert entries[0].private_key_hex == seed.hex()
    res = run(core.import_identities(pinkie, core.plan_import(pinkie, entries), check_live=False))
    assert res[0].outcome == "imported" and core.identities(pinkie, "alpha.test")[0]["wrapped"]


# -- CLI ---------------------------------------------------------------------------------


def test_cli_export_import(two_users, tmp_path, monkeypatch, capsys):
    mox, pinkie, name, ref = two_users
    monkeypatch.setenv("SCOOTALOO_EXPORT_PASSPHRASE", "cli pass")
    monkeypatch.setenv("SCOOTALOO_IMPORT_PASSPHRASE", "cli pass")

    def sh(*argv, user="moxxie", pw="hunter222"):
        monkeypatch.setenv("SCOOTALOO_USER", user)
        monkeypatch.setenv("SCOOTALOO_PASSWORD", pw)
        code = main(list(argv))
        o = capsys.readouterr()
        return code, o.out + o.err

    out = str(tmp_path / "mine")
    code, text = sh("identity", "export", "--all", "-o", out)
    assert code == 0 and "exported 1 identity" in text and os.path.exists(out + ".bnid")
    code, text = sh("identity", "export", "--all", "-o", out)
    assert code == 1 and "already exists" in text

    code, text = sh("identity", "import", out + ".bnid", "--dry-run", user="pinkie", pw="partypony")
    assert code == 0 and "import" in text and core.identities(pinkie, "alpha.test") == []
    code, text = sh("identity", "import", out + ".bnid", user="pinkie", pw="partypony")
    assert code == 0 and "imported" in text and core.identities(pinkie, "alpha.test")
    code, text = sh("identity", "import", out + ".bnid", user="pinkie", pw="partypony")
    assert "already have it" in text

    code, text = sh("identity", "export", name, "--seed", "--yes")
    seed_doc, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])  # stdout's JSON; the warning is on stderr
    assert code == 0 and seed_doc["public_key_hex"] == core.collect_identities(mox)[0].public_key_hex

    with Users() as u:
        u.add("applejack", "honesty1")
    seed_text = json.dumps(seed_doc)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(seed_text))
    code, text = sh("identity", "import", "--manual", user="applejack", pw="honesty1")
    assert code == 0 and "imported" in text

    monkeypatch.setenv("SCOOTALOO_IMPORT_PASSPHRASE", "not it")
    code, text = sh("identity", "import", out + ".bnid", user="applejack", pw="honesty1")
    assert code == 1 and "wrong passphrase" in text


# -- TUI ------------------------------------------------------------------------------------


def test_tui_identity_manager_export_then_import(two_users, tmp_path):
    mox, pinkie, name, ref = two_users
    path = str(tmp_path / "from-tui.bnid")

    async def answer(pilot, app, value):
        await wait_screen(pilot, app, Ask)
        app.screen.query_one("#answer", Input).value = value
        await pilot.press("enter")

    async def export():
        app = ScootalooApp(user="moxxie", password="hunter222")
        async with app.run_test(size=(150, 45)) as pilot:
            await settle(pilot, app)
            await pilot.press("i")
            await wait_screen(pilot, app, Identities)
            ol = app.screen.query_one("#id-list", OptionList)
            labels = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count) if ol.get_option_at_index(i).id]
            assert any(name in t and "posting" in t for t in labels)
            assert any("Import from a .bnid" in t for t in labels)
            await pilot.press("x")  # export the highlighted identity
            await answer(pilot, app, path)
            await answer(pilot, app, "tui pass")
            await answer(pilot, app, "tui pass")
            await settle(pilot, app)
        assert ia.open_archive(open(path, "rb").read(), "tui pass").entries[0].username == name

    async def import_():
        app = ScootalooApp(user="pinkie", password="partypony")
        async with app.run_test(size=(150, 45)) as pilot:
            for _ in range(40):
                await pilot.pause(0.1)
                if isinstance(app.screen, Ask):  # pinkie has no relays: the app asks to connect first
                    await pilot.press("escape")
                    break
            await pilot.pause(0.3)
            await pilot.press("i")
            await wait_screen(pilot, app, Identities)
            ol = app.screen.query_one("#id-list", OptionList)
            idx = next(i for i in range(ol.option_count) if ol.get_option_at_index(i).id == "import-file")
            ol.highlighted = idx
            await pilot.press("enter")
            await answer(pilot, app, path)
            await answer(pilot, app, "tui pass")
            await wait_screen(pilot, app, YesNo)
            await pilot.press("y")
            await wait_screen(pilot, app, Show)
            assert "imported" in str(app.screen.query_one(Static).render())
            await pilot.press("escape")
            await settle(pilot, app)
        assert core.identities(pinkie, "alpha.test")[0]["username"] == name



    asyncio.run(export())
    asyncio.run(import_())
