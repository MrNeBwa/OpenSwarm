#!/usr/bin/env python3
"""Build a REAL installable OpenSwarm-Mobile.apk with the official Android toolchain.

aapt2 (manifest + resources) -> javac -> d8 (classes.dex) -> zipalign -> apksigner (v1+v2+v3).
The SDK (command-line tools, build-tools 34, platform android-34) is downloaded to
~/.cache/openswarm-android on first use. Needs a JDK (javac, keytool, java) on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ROOT / "android" / "app" / "src" / "main"
CACHE = Path(os.environ.get("OPENSWARM_ANDROID_CACHE") or Path.home() / ".cache" / "openswarm-android")
SDK = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or CACHE / "sdk")
CMDLINE_URL = "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
BUILD_TOOLS, PLATFORM = "34.0.0", "android-34"
OUTS = [ROOT / "dist" / "OpenSwarm-Mobile.apk", ROOT / "src" / "openswarm" / "data" / "OpenSwarm-Mobile.apk"]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if r.returncode:
        sys.exit(f"command failed: {' '.join(map(str, cmd))}\n{r.stdout}\n{r.stderr}")
    return r


def ensure_sdk() -> tuple[Path, Path]:
    bt, jar = SDK / "build-tools" / BUILD_TOOLS, SDK / "platforms" / PLATFORM / "android.jar"
    if bt.is_dir() and jar.is_file():
        return bt, jar
    sdkm = SDK / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if not sdkm.is_file():
        print("downloading Android command-line tools ...")
        CACHE.mkdir(parents=True, exist_ok=True)
        z = CACHE / "cmdline.zip"
        urllib.request.urlretrieve(CMDLINE_URL, z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(CACHE / "_tmp")
        (SDK / "cmdline-tools").mkdir(parents=True, exist_ok=True)
        shutil.move(str(CACHE / "_tmp" / "cmdline-tools"), str(SDK / "cmdline-tools" / "latest"))
        for p in (sdkm,):
            p.chmod(0o755)
    subprocess.run(f"yes | '{sdkm}' --sdk_root='{SDK}' --licenses", shell=True, capture_output=True)
    run([sdkm, f"--sdk_root={SDK}", f"build-tools;{BUILD_TOOLS}", f"platforms;{PLATFORM}"])
    return bt, jar


def keystore() -> Path:
    ks = CACHE / "openswarm-release.keystore"
    if not ks.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        run(["keytool", "-genkeypair", "-keystore", ks, "-storepass", "openswarm", "-keypass", "openswarm",
             "-alias", "openswarm", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-dname", "CN=OpenSwarm, OU=Mobile, O=OpenSwarm, C=US"])
    return ks


def main() -> None:
    bt, android_jar = ensure_sdk()
    aapt2, zipalign, d8, apksigner = bt / "aapt2", bt / "zipalign", bt / "lib" / "d8.jar", bt / "apksigner"
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        base = tmp / "base.apk"
        run([aapt2, "link", "-o", base, "-I", android_jar, "--manifest", ANDROID / "AndroidManifest.xml",
             "--min-sdk-version", "24", "--target-sdk-version", "34", "--version-code", "100",
             "--version-name", "1.0.0", "-A", ANDROID / "assets", "--auto-add-overlay"])
        classes = tmp / "classes"
        classes.mkdir()
        srcs = [str(p) for p in (ANDROID / "java").rglob("*.java")]
        run(["javac", "--release", "8", "-g:none", "-classpath", android_jar, "-d", classes, *srcs])
        dexdir = tmp / "dex"
        dexdir.mkdir()
        class_files = [str(p) for p in classes.rglob("*.class")]
        run(["java", "-cp", d8, "com.android.tools.r8.D8", "--min-api", "24", "--lib", android_jar,
             "--output", dexdir, *class_files])
        with zipfile.ZipFile(base, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.write(dexdir / "classes.dex", "classes.dex")
        aligned = tmp / "aligned.apk"
        run([zipalign, "-f", "-p", "4", base, aligned])
        final = tmp / "OpenSwarm-Mobile.apk"
        run([apksigner, "sign", "--ks", keystore(), "--ks-pass", "pass:openswarm", "--key-pass", "pass:openswarm",
             "--ks-key-alias", "openswarm", "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
             "--v3-signing-enabled", "true", "--out", final, aligned])
        print(run([apksigner, "verify", "--verbose", "--print-certs", final]).stdout.strip())
        run([zipalign, "-c", "-p", "4", final])
        for out in OUTS:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(final, out)
            print(f"built {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
