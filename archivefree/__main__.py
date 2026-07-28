"""Entry point: ``python3 -m archivefree``."""

from __future__ import annotations

import sys


def main() -> int:
    from .app import main as run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
