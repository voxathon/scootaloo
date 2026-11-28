"""scootaloo: a terminal client for Bonnet."""

__version__ = "0.11.1"


def main() -> int:
    from scootaloo.cli import main as _main

    return _main()
