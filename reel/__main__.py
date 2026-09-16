"""Entry point so `python3 -m reel` works straight from the repo."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
