"""Pins of the Linux CI image of the TEMPLATE repository (template-ci-image.yml), and its tag.

Template repository only: `./pyt new` never copies `.github/workflows/template-*`. This file
is the only home of the pins that exist for the image alone (the base, the apt snapshot, uv,
Neovim, PowerShell, actionlint, xonsh, the user id); the pins the project already owns are read
from their files by text, as the workflows read them, never copied. The tag is a hash of every
pin and every file that decides what the image holds (`canonical`, written into the image as
/opt/ci/inputs.txt): a new pin, a changed lock or preset gives a new tag. Taken on the day of
the build, not named by the tag: ca-certificates and openssl (from the live archive, before the
snapshot: see `system`) and the unpinned dependencies of the pinned tools (xonsh's).

Usage (any Python >= 3.11, stdlib only):
  python3 pins.py env          KEY=VALUE lines, sourced by the image's `system` and `warm`
  python3 pins.py canonical    what the tag hashes (also /opt/ci/inputs.txt in the image)
  python3 pins.py ref OWNER/REPO    ghcr.io/owner/repo-ci:<16 hex>
  python3 pins.py base         the base image (FROM)
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

# The image's own pins. Bump one deliberately: the next push builds and publishes a new tag, and
# the weekly rebuild from scratch proves that every one of them still downloads.
# APT_SNAPSHOT must be later than the base's build date (`serial` in its /etc/cloud/build.info):
# the snapshot's toolchain needs the base's libc6 at the same version. A BASE bump usually needs
# an APT_SNAPSHOT bump in the same commit (`system` refuses the other order).
BASE = "ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3"  # serial 20260911
APT_SNAPSHOT = "20260920T000000Z"  # snapshot.ubuntu.com: every apt package but the first ones (system)
UV = "0.12.19"
UV_SHA256 = "23bf5552d220e0842b65c862097b2ebaeba0064b74eda5e565e77fd25969d8c8"
NVIM = "v0.12.5"
NVIM_SHA256 = {  # nvim-linux-x86_64.tar.gz of NVIM and of cmd_nvim.MIN_LAZYVIM
    "v0.12.5": "bce0f56eda1f1b1db6eee8f4133d7a38813ea07933837dd1777411ca384c6875",
    "v0.11.2": "a9b24157672eb218ff3e33ef3f8c08db26f8931c5c04bdb0e471371dd1dfe63e",
}
PWSH = "7.6.6"
PWSH_SHA256 = "ddbc4a2d113bbd46d283cfedcbcd117a70caefd7673f41f2b4e0000badf103bc"
ACTIONLINT = "1.7.12"
ACTIONLINT_SHA256 = "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
XONSH = "xonsh==0.24.2"
CI_UID = "1001"  # the hosted runner's user: the checkout it mounts belongs to it

# The files of the image itself (every tracked file of this folder but .gitattributes)
IMAGE_FILES = ("Dockerfile", "build", "pins.py", "system", "warm")
_SAFE = re.compile(r"[A-Za-z0-9._:@/+=-]+")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8-sig")


def _one(pattern: str, rel: str) -> str:
    """The single match of `pattern` (one group) in the file `rel`: two or none is an error, never
    a silent empty pin."""
    found = re.findall(pattern, _read(rel), flags=re.MULTILINE)
    if len(found) != 1:
        raise SystemExit(f"pins.py: {rel} has {len(found)} matches of {pattern!r}, not one")
    return str(found[0])


def from_code() -> dict[str, str]:
    """The pins the project owns, read where the workflows read them."""
    major, minor, patch = re.findall(r"\d+", _one(r"^MIN_LAZYVIM = (\(\d+, \d+, \d+\))", ".pytemplate/runner/cmd_nvim.py"))
    return {
        "CPYTHON": _read(".python-version").strip(),
        "PYTHON_FLOOR": _one(r'^# requires-python = ">=(\d+\.\d+)"$', ".pytemplate/pyt.py"),
        "NVIM_MIN": f"v{major}.{minor}.{patch}",
        "TAPLO": _one(r'"(taplo==[0-9.]+)"', ".pytemplate/tests/test_render_core.py"),
        "BASEDPYRIGHT": _one(r'^BASEDPYRIGHT = "(.*)"', ".pytemplate/runner/cmd_dev.py"),
        "BASEDPYRIGHT_NODE": _one(r'^BASEDPYRIGHT_NODE = "(.*)"', ".pytemplate/runner/cmd_dev.py"),
        "UPX": _one(r'^VERSION = "([0-9.]+)"', ".pytemplate/runner/upx.py"),
    }


def values() -> dict[str, str]:
    code = from_code()
    minimum = code["NVIM_MIN"]
    if NVIM not in NVIM_SHA256 or minimum not in NVIM_SHA256:
        raise SystemExit(f"pins.py: NVIM_SHA256 needs the SHA-256 of {NVIM} and {minimum}")
    out = {
        "APT_SNAPSHOT": APT_SNAPSHOT,
        "UV": UV,
        "UV_SHA256": UV_SHA256,
        "NVIM": NVIM,
        "NVIM_SHA256": NVIM_SHA256[NVIM],
        "NVIM_MIN_SHA256": NVIM_SHA256[minimum],
        "PWSH": PWSH,
        "PWSH_SHA256": PWSH_SHA256,
        "ACTIONLINT": ACTIONLINT,
        "ACTIONLINT_SHA256": ACTIONLINT_SHA256,
        "XONSH": XONSH,
        "CI_UID": CI_UID,
        **code,
    }
    bad = {k: v for k, v in out.items() if not _SAFE.fullmatch(v)}
    if bad:
        raise SystemExit(f"pins.py: values a shell cannot source as they are: {bad}")
    return out


def data_files() -> list[str]:
    """The project files whose content decides what the image downloads (lock, presets)."""
    names = ["uv.lock", "pyproject.toml", "pytemplate.toml", ".python-version"]
    for preset in sorted(p for p in (ROOT / ".pytemplate" / "presets").iterdir() if p.is_dir()):
        for rel in ("preset.toml", "constraints.txt", "files/pytemplate.toml"):
            if (preset / rel).is_file():
                names.append((preset / rel).relative_to(ROOT).as_posix())
    return names


def _digest(path: Path) -> str:
    """sha256 of the bytes without a UTF-8 BOM and with CRLF as LF: a Windows checkout
    (core.autocrlf) hashes like a Linux one."""
    data = path.read_bytes().removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def canonical() -> str:
    lines = [f"BASE={BASE}", *(f"{k}={v}" for k, v in sorted(values().items()))]
    here = HERE.relative_to(ROOT).as_posix()
    lines += [f"{_digest(HERE / name)}  {here}/{name}" for name in IMAGE_FILES]
    lines += [f"{_digest(ROOT / rel)}  {rel}" for rel in data_files()]
    return "\n".join(lines) + "\n"


def ref(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise SystemExit(f"pins.py: {repository!r} is not OWNER/REPO")
    tag = hashlib.sha256(canonical().encode()).hexdigest()[:16]
    return f"ghcr.io/{repository.lower()}-ci:{tag}"


def main(argv: list[str]) -> int:
    if argv == ["env"]:
        sys.stdout.write("".join(f"{k}={v}\n" for k, v in values().items()))
    elif argv == ["canonical"]:
        sys.stdout.write(canonical())
    elif len(argv) == 2 and argv[0] == "ref":
        print(ref(argv[1]))
    elif argv == ["base"]:
        print(BASE)
    else:
        print("usage: pins.py env | canonical | ref OWNER/REPO | base", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
