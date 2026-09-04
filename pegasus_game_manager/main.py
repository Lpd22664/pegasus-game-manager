from __future__ import annotations

import sys

from .app import GameManagerApplication


def main() -> int:
    app = GameManagerApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
