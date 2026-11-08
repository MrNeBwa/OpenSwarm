"""Turnstone main execution entrypoint."""

import sys
from .app import turnstone_main

if __name__ == "__main__":
    sys.exit(turnstone_main())
