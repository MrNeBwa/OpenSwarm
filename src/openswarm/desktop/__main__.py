import sys
from .app import desktop_main

if __name__ == "__main__":
    raise SystemExit(desktop_main(sys.argv[1:]))
