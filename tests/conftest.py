"""Two real Bonnet servers, federated with each other, for the whole test session.

`alpha.test` and `beta.test` sync every few seconds, so tests can check
cross-origin reads against real federation instead of a mock. Each test gets
a fresh SCOOTALOO_HOME, so users, pins and identities never leak between tests.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

BONNET = [sys.executable, "-c", "import sys; from bonnet.cli import main; sys.argv[0] = 'bonnet'; sys.exit(main())"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _configure(path: Path, origin: str, port: int, peer: str, peer_port: int) -> None:
    s = path.read_text()
    s = s.replace('origin = "localhost"\nhostname = ""', f'origin = "{origin}"\nhostname = "127.0.0.1"', 1)
    s = re.sub(r"^port = \d+$", f"port = {port}", s, count=1, flags=re.M)
    s = s.replace(
        "[sync]\ninterval_seconds = 300",
        f"""[sync]
interval_seconds = 2

[[sync.peers]]
origin = "{peer}"
hostname = "127.0.0.1"
port = {peer_port}
scheme = "https"
verify_tls = false
allow_private = true""",
        1,
    )
    if origin == "alpha.test":
        # alpha lets users delete their own posts; beta keeps Bonnet's stock ACL, which doesn't
        s = s.replace(
            'kinds = ["bonnet.article", "bonnet.board.create",',
            'kinds = ["bonnet.article", "bonnet.article.cancel", "bonnet.board.create",',
            1,
        )
    path.write_text(s)


def _wait_up(port: int, timeout: float = 30) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"https://127.0.0.1:{port}/.well-known/untp", context=ctx, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"bonnet server on {port} didn't come up")


@pytest.fixture(scope="session")
def servers(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to make the test servers' certificates")
    base = tmp_path_factory.mktemp("servers")
    ports = {"alpha.test": _free_port(), "beta.test": _free_port()}
    procs = []
    try:
        for origin, peer in (("alpha.test", "beta.test"), ("beta.test", "alpha.test")):
            d = base / origin
            subprocess.run(BONNET + ["server", "--dir", str(d), "--init"], check=True, capture_output=True)
            _configure(d / "config.toml", origin, ports[origin], peer, ports[peer])
            log = open(d / "server.log", "w")
            procs.append(
                subprocess.Popen(
                    BONNET + ["server", "--config", str(d / "config.toml")],
                    stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                )
            )
        for p in ports.values():
            _wait_up(p)
        yield {o: f"https://127.0.0.1:{p}" for o, p in ports.items()}
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.fixture(autouse=True)
def scootaloo_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SCOOTALOO_HOME", str(home))
    monkeypatch.setenv("BONNET_GATEWAY_HOME", str(tmp_path / "gateway"))
    for var in ("SCOOTALOO_USER", "SCOOTALOO_PASSWORD", "BONNET_IDENTITY"):
        monkeypatch.delenv(var, raising=False)
    return home


def run(coro):
    return asyncio.run(coro)


def wait_for(pred, timeout: float = 20, every: float = 0.5):
    """Poll until pred() is truthy (federation is eventually consistent)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = pred()
            if last:
                return last
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(every)
    raise AssertionError(f"timed out waiting; last result: {last!r}")


_counter = [0]


def unique(prefix: str) -> str:
    _counter[0] += 1
    return f"{prefix}{os.getpid() % 1000}x{_counter[0]}"
