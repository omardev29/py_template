"""Get the Flet desktop client into flet_desktop's cache as `flet pack` does, trying a transient
failure again. Runs INSIDE .venv (it needs flet_desktop), started by methods/exe._flet_pack.

flet pack (flet_cli 1.0.1) bundles the client from flet_desktop's cache (~/.flet/client), which
flet_desktop.ensure_client_cached() fills the first time on a machine with one download of about
40 MB from GitHub and no retry: a transient HTTP 500 of GitHub's release download failed whole exe
builds (CLAUDE.md 15.1). This script runs first, in flet pack's folder and environment, so
flet_desktop picks the same client (flavor, version, FLET_CLIENT_URL) and cache folder, and flet
pack then finds it there. Nothing is downloaded when the cache already holds it.

Usage: python flet_client.py PAUSE...   (the seconds to wait before each new attempt)
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import sys
import tarfile
import time
import urllib.error
import zipfile
import zlib

# methods/common._TRANSIENT_ERRORS: a dropped, refused or timed-out connection, a name the
# resolver could not look up now, a body cut short
TRANSIENT_ERRORS = (ConnectionError, TimeoutError, socket.gaierror, http.client.HTTPException)
# what a download cut short raises when flet_desktop extracts it, right after the download
UNREADABLE = (EOFError, tarfile.TarError, zipfile.BadZipFile, zlib.error)


def transient(e: BaseException) -> bool:
    """methods/common.transient (this script cannot import the runner), plus an archive that a
    download cut short left unreadable."""
    if isinstance(e, UNREADABLE):
        return True
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code in (408, 429)
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, BaseException):
        e = e.reason
    if isinstance(e, ssl.SSLError):
        return not isinstance(e, ssl.SSLCertVerificationError)
    return isinstance(e, TRANSIENT_ERRORS)


def main(argv: list[str]) -> int:
    view = os.environ.get("FLET_VIEW_PATH")
    if view and os.path.exists(view):
        return 0  # flet pack copies the client from there (flet_cli's get_flet_bin_path)
    try:
        import flet_desktop  # type: ignore[import-not-found]  # .venv's, not the runner's
    except ImportError:
        return 0  # flet pack says what is missing itself
    ensure = getattr(flet_desktop, "ensure_client_cached", None)
    if ensure is None:
        return 0  # another Flet ([preset.flet] version) gets its client its own way: never in its way

    pauses = [float(a) for a in argv]
    for attempt in range(len(pauses) + 1):
        try:
            ensure()
            return 0
        except Exception as e:
            if attempt == len(pauses) or not transient(e):
                raise
            why = str(e) or type(e).__name__
            print(f"the Flet client download failed ({why}): trying again in {pauses[attempt]:g} s", file=sys.stderr, flush=True)
            time.sleep(pauses[attempt])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
