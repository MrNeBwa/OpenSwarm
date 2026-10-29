#!/usr/bin/env python3
"""Build dist/OpenSwarm-Mobile.apk.

The previous hand-assembled DEX/AXML/ARSC writer produced packages that Android's installer rejected
("package appears to be invalid"), so the APK is now built by the official toolchain
(aapt2 + javac + d8 + zipalign + apksigner) in build_apk_sdk.py.
"""

from build_apk_sdk import main

if __name__ == "__main__":
    main()
