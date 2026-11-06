"""Install the official, checksum-pinned CLI without changing shell profiles."""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "1.5.2"
RELEASE = "1.5.2-4951595792859136"
BUILDS = {
    "aarch64": ("linux-arm/cli_linux_arm64.tar.gz", "9a141da7b218c640a962188b388905f065cc307425bbeff6e74c07f95266f93526f6d0dee9bad876b79431403f5b30ef4e6514f5f44fbf06dca2a077f54f6175"),
    "x86_64": ("linux-x64/cli_linux_x64.tar.gz", "aa876c0813572efaf85ed47983cc937414705d40490cbdb54f6e255656a4f8744ae38e457823e924fa8270b348124e0521d3cda3fec6824bb2d14b98a7a05baa"),
}


def main() -> None:
    if platform.system() != "Linux" or platform.machine() not in BUILDS:
        raise SystemExit("Antigravity backend installation requires Linux ARM64 or AMD64")
    relative, expected = BUILDS[platform.machine()]
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/local/bin/agy")
    with tempfile.TemporaryDirectory(prefix="raven-agy-install-") as temp:
        archive = Path(temp) / "cli.tar.gz"
        url = f"https://storage.googleapis.com/antigravity-public/antigravity-cli/{RELEASE}/{relative}"
        with urllib.request.urlopen(url, timeout=180) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        with archive.open("rb") as source:
            actual = hashlib.file_digest(source, "sha512").hexdigest()
        if actual != expected:
            raise SystemExit("Antigravity archive checksum mismatch")
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember("antigravity")
            if not member.isfile():
                raise SystemExit("Antigravity archive does not contain a regular executable")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)
        destination.chmod(0o755)
    result = subprocess.run([str(destination), "--version"], capture_output=True, text=True, check=True)
    if result.stdout.strip() != VERSION:
        raise SystemExit("Installed Antigravity version differs from pinned release")
    print(f"Installed Antigravity CLI {VERSION} ({platform.machine()}); SHA-512 verified")


if __name__ == "__main__":
    main()
