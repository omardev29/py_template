"""The steps of template-flet.yml on a project of the flet preset made with `./pyt new`.

TEMPLATE repository only: `./pyt new` never copies `.github/workflows/template-*`. PROJECT is
the project's folder: its pytemplate.toml gives `app.name` and `python.cpython`, and the build
of each target is the folder `./pyt build cpython --method flet` writes
(`dist/<name>-cpython-flet-<target>`, `cmd_build.dist_path`).

Usage (Python >= 3.11; `web` also needs Playwright and its Chromium: browser.txt):
  python check.py target PROJECT TARGET  set [deploy.flet] target in its pytemplate.toml with the
                                         project's own runner (config.update_file, the editor
                                         `./pyt mode` uses: comments and layout stay)
  python check.py flet PROJECT           print `version=<the Flet version its uv.lock holds>`
  python check.py web PROJECT OUT        serve the web build over HTTP on 127.0.0.1 and drive it
                                         in a headless Chromium: the app's Python must start in
                                         the browser (Pyodide), show the skeleton's controls and
                                         window title, and draw when its button is clicked. The
                                         browser's console log and a screenshot go to the folder
                                         OUT, pass or fail.
  python check.py apk PROJECT            the .apk (built, never run): a valid zip with the
                                         manifest, the Flutter and Python runtimes of every ABI,
                                         the app and the native modules of its dependencies.
Exit code 0 when the step passes, 1 when it fails (the reason is printed), 2 on bad usage.
"""

from __future__ import annotations

import http.server
import importlib
import io
import re
import sys
import threading
import time
import tomllib
import zipfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

# What the flet preset's skeleton shows (.pytemplate/presets/flet/files/src/__pkg__/ui/app.py);
# test_workflows.py checks that the fragments of SKELETON are still in its code.
BUTTON = "Draw"
READY = "Core: CPython. Press Draw."  # its first status: in Pyodide the core runs interpreted
DONE = re.compile(r"\b\d+ iterations in \d+\.\d\d s \(core: CPython\)")  # its status after a draw
SKELETON = (
    'page.title = "{{name}}"',
    'ft.Button("Draw", on_click=draw)',
    'f"Core: {_backend()}. Press Draw."',
    'f"{max_iter} iterations in {elapsed:.2f} s (core: {_backend()})"',
    '"Draw failed (see the console)"',
)
# python.js of Flet's web template logs the first once the app's main module ran and started its
# connection to Flutter (ft.run -> PyodideConnection), the second with the error that stopped it
PYTHON_STARTED = "Python worker initialized"
PYTHON_FAILED = "Python worker init error"
# Seconds; Pyodide, CanvasKit and the app come from the network at the first start
PYTHON_TIMEOUT = 300.0
UI_TIMEOUT = 120.0
DRAW_TIMEOUT = 300.0
CLICK_AGAIN = 20.0  # no "Computing..." this long after the click: tap the semantics node instead

# The ABIs flet build packs by default (the abiFilters of its Android template, the ABIs
# python-build publishes for every Python minor) and what each one must hold
ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64")
ABI_LIBS = ("libflutter.so", "libapp.so", "libdart_bridge.so")  # and libpython<minor>.so
APP_ZIPS = ("assets/app.zip", "assets/sitepackages.zip", "assets/stdlib.zip")


class Failed(Exception):
    """A step that did not pass: the message says what and why."""


def project_info(project: Path) -> tuple[str, str, str]:
    """(app.name, the package, python.cpython) of the project's pytemplate.toml."""
    data = tomllib.loads((project / "pytemplate.toml").read_text(encoding="utf-8-sig"))
    name = str(data["app"]["name"])
    return name, name.replace("-", "_").lower(), str(data["python"]["cpython"])


def output_dir(project: Path, target: str) -> Path:
    """Where `./pyt build cpython --method flet` puts the build of `target`."""
    name, _pkg, _minor = project_info(project)
    return project / "dist" / f"{name}-cpython-flet-{target}"


def set_target(project: Path, target: str) -> None:
    file = project / "pytemplate.toml"
    if not (project / ".pytemplate" / "runner").is_dir():
        raise Failed(f"{project}: no .pytemplate/runner (not a project made with ./pyt new)")
    sys.path.insert(0, str(project / ".pytemplate"))
    config: Any = importlib.import_module("runner.config")
    pyt_error: type[Exception] = importlib.import_module("runner.ui").PytError
    if Path(config.CONFIG_FILE).resolve() != file.resolve():  # the runner of another project
        raise Failed(f"the runner imported edits {config.CONFIG_FILE}, not {file}")
    try:
        config.update_file([("deploy.flet", "target", target)])
        edited = config.load(set()).deploy.flet.target
    except pyt_error as e:  # a pytemplate.toml the runner cannot read or edit
        raise Failed(f"{file}: {e}") from None
    if edited != target:
        raise Failed(f"{file}: [deploy.flet] target is {edited!r} after the edit, not {target!r}")
    print(f"ok   {file}: [deploy.flet] target = {target!r}")


def flet_version(project: Path) -> str:
    lock = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8-sig"))
    versions = [str(p["version"]) for p in lock.get("package", []) if p.get("name") == "flet"]
    if len(versions) != 1:
        raise Failed(f"{project / 'uv.lock'}: {len(versions)} versions of flet, not one")
    return versions[0]


# --- web -----------------------------------------------------------------------------------


class _Site(http.server.SimpleHTTPRequestHandler):
    """The web build on a plain static server, as a static host serves it: JavaScript (the module
    worker) and wasm get their types whatever the machine's mime.types says. No cross-origin
    isolation headers: `flet serve` adds them, the app needs none."""

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".wasm": "application/wasm",
        ".json": "application/json",
    }
    log: list[str] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (the base class's name)
        self.log.append("[http] " + format % args)


TEXTS_JS = """() => {
    const out = [];
    const host = document.querySelector('flt-semantics-host');
    if (!host) return out;
    for (const e of host.querySelectorAll('*')) {
        const label = e.getAttribute('aria-label');
        if (label) out.push(label);
        if (e.childElementCount === 0 && e.textContent.trim()) out.push(e.textContent.trim());
    }
    out.push(host.innerText || '');
    return out;
}"""
# The semantics node of a button: its centre (scrolled into view), and a click on it
BUTTON_JS = """([label, click]) => {
    const host = document.querySelector('flt-semantics-host');
    if (!host) return null;
    for (const e of host.querySelectorAll('[role="button"], button')) {
        if ((e.getAttribute('aria-label') || e.textContent || '').trim() !== label) continue;
        if (click) { e.click(); return {x: 0, y: 0}; }
        e.scrollIntoView({block: 'center'});
        const r = e.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }
    return null;
}"""
# Flutter draws on a canvas; its semantics tree (the accessibility DOM) holds the controls, and a
# click on its placeholder turns it on, as for a screen reader
SEMANTICS_JS = """() => {
    const placeholder = document.querySelector('flt-semantics-placeholder');
    if (placeholder) { placeholder.click(); return 'enabled'; }
    return document.querySelector('flt-semantics-host') ? 'on' : '';
}"""


def _texts(page: Any) -> list[str]:
    """Every text of the semantics tree: labels (aria-label) and the nodes' own text."""
    found: list[str] = page.evaluate(TEXTS_JS)
    return found


def _button(page: Any, click: bool = False) -> dict[str, float] | None:
    box: dict[str, float] | None = page.evaluate(BUTTON_JS, [BUTTON, click])
    return box


def _wait(page: Any, what: str, timeout: float, found: Callable[[], str | None]) -> str:
    """Poll `found` every 0.5 s (the page runs meanwhile) until it returns a text."""
    deadline = time.monotonic() + timeout
    while True:
        result = found()
        if result:
            return result
        if time.monotonic() > deadline:
            raise Failed(f"{what}: not within {timeout:.0f} s")
        page.wait_for_timeout(500)


def _drive(page: Any, url: str, name: str, console: list[str]) -> None:
    start = time.monotonic()
    try:
        page.goto(url, wait_until="load", timeout=120_000)
    except Exception as e:  # Playwright's own error types: a page that never loaded
        raise Failed(f"{url} did not load: {e}") from None

    def python_started() -> str | None:
        for line in console:
            if PYTHON_FAILED in line:
                raise Failed(f"the app's Python did not start: {line}")
        return next((line for line in console if PYTHON_STARTED in line), None)

    _wait(page, "the app's Python starting (Pyodide)", PYTHON_TIMEOUT, python_started)
    print(f"ok   Python started in the browser ({time.monotonic() - start:.0f} s): Pyodide ran the app's main module")
    _wait(page, "Flutter's semantics tree", UI_TIMEOUT, lambda: str(page.evaluate(SEMANTICS_JS)))

    def ready() -> str | None:
        shown = any(READY in t for t in _texts(page)) and _button(page) is not None
        return READY if shown and page.title() == name else None

    try:
        _wait(page, f"the skeleton's first status {READY!r}, its {BUTTON!r} button and the title {name!r}", UI_TIMEOUT, ready)
    except Failed as e:
        raise Failed(f"{e}; the page shows {_texts(page)[-1:]!r}, title {page.title()!r}") from None
    print(f"ok   its controls and window title {name!r} ({time.monotonic() - start:.0f} s): the app's main(page) ran")

    box = _button(page)
    if box is None:
        raise Failed(f"the {BUTTON!r} button went away")
    page.mouse.click(box["x"], box["y"])  # a real click where the button is drawn
    clicked = time.monotonic()
    retried = False

    def drawn() -> str | None:
        nonlocal retried
        texts = _texts(page)
        if any("Draw failed" in t for t in texts):
            raise Failed("the Draw handler failed: the app shows 'Draw failed (see the console)'")
        status = next((m.group(0) for t in texts if (m := DONE.search(t))), None)
        if status is None and not retried and time.monotonic() - clicked > CLICK_AGAIN and not any("Computing" in t for t in texts):
            _button(page, click=True)  # the click did not reach it: the semantics node's own tap
            retried = True
        return status

    status = _wait(page, f"the drawing after a click on {BUTTON!r}", DRAW_TIMEOUT, drawn)
    print(f"ok   {BUTTON!r} clicked: {status!r} ({time.monotonic() - clicked:.0f} s): the handler ran in Pyodide's event loop")


def check_web(project: Path, out: Path) -> None:
    name, _pkg, _minor = project_info(project)
    site = output_dir(project, "web")
    if not (site / "index.html").is_file():
        raise Failed(f"{site}: no index.html (did the web build run?)")
    playwright: Any = importlib.import_module("playwright.sync_api")
    out.mkdir(parents=True, exist_ok=True)
    console: list[str] = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(_Site, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("console", lambda m: console.append(f"[console.{m.type}] {m.text}"))
                page.on("pageerror", lambda e: console.append(f"[pageerror] {e}"))
                page.on("requestfailed", lambda r: console.append(f"[requestfailed] {r.url} {r.failure}"))
                try:
                    _drive(page, f"http://127.0.0.1:{server.server_address[1]}/", name, console)
                finally:
                    try:
                        page.screenshot(path=str(out / "web.png"), full_page=True)
                    except Exception as e:  # a crashed page: the check's own reason stays the error
                        console.append(f"[check] no screenshot: {e}")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        (out / "console.log").write_text("\n".join(console + _Site.log) + "\n", encoding="utf-8")


# --- apk -----------------------------------------------------------------------------------


def _members(data: bytes) -> dict[str, bytes]:
    """The entries of a zip held in memory, with their bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


def _module(names: dict[str, bytes], path: str) -> bool:
    """A Python module as flet build packs it: compiled (`.pyc`, the source dropped) or not."""
    return f"{path}.pyc" in names or f"{path}.py" in names


def check_apk(project: Path) -> None:
    _name, pkg, minor = project_info(project)
    folder = output_dir(project, "apk")
    apks = sorted(folder.glob("*.apk"))
    if len(apks) != 1:
        raise Failed(f"{folder}: {len(apks)} .apk files, not one: {[p.name for p in apks]}")
    apk = apks[0]
    print(f"{apk.name}: {apk.stat().st_size / 1_048_576:.1f} MB")
    with zipfile.ZipFile(apk) as z:
        broken = z.testzip()
        if broken is not None:
            raise Failed(f"{apk.name}: {broken} fails its CRC check")
        names = set(z.namelist())
        zips = {n: _members(z.read(n)) for n in APP_ZIPS if n in names}
    libs: dict[str, set[str]] = {}
    for n in names:
        parts = n.split("/")
        if len(parts) == 3 and parts[0] == "lib":
            libs.setdefault(parts[1], set()).add(parts[2])
    for abi in sorted(libs):
        print(f"  lib/{abi}/: {len(libs[abi])} libraries")

    problems = [f"no {n}" for n in ("AndroidManifest.xml", "classes.dex", "resources.arsc") if n not in names]
    if not any(n.startswith("assets/flutter_assets/") for n in names):
        problems.append("no assets/flutter_assets/ (Flutter's assets)")
    python_lib = f"libpython{minor}.so"
    for abi in ABIS:
        missing = [lib for lib in (*ABI_LIBS, python_lib) if lib not in libs.get(abi, set())]
        if missing:
            problems.append(f"lib/{abi}/ lacks {', '.join(missing)}")
    problems += [f"no {n}" for n in APP_ZIPS if n not in zips]
    app = zips.get("assets/app.zip", {})
    if app:
        wanted = ("main", f"{pkg}/__init__", f"{pkg}/ui/app", f"{pkg}/core/fractal", f"{pkg}/resources")
        problems += [f"assets/app.zip lacks {w}.py[c] (the app)" for w in wanted if not _module(app, w)]
        problems += [f"assets/app.zip holds the native module {n} (mobile apps ship the .py)" for n in sorted(app) if n.endswith((".so", ".soref"))]
    site = zips.get("assets/sitepackages.zip", {})
    if site and not _module(site, "flet/__init__"):
        problems.append("assets/sitepackages.zip lacks flet")
    # serious_python moves every native module to lib/<abi>/lib<dotted-name>.so and leaves a
    # `.soref` marker holding that name where the module was
    for zip_name in APP_ZIPS[1:]:
        entries = zips.get(zip_name, {})
        refs = {n: data.decode("utf-8").strip() for n, data in entries.items() if n.endswith(".soref")}
        if entries:
            print(f"  {zip_name}: {len(entries)} entries, {len(refs)} native modules")
        if entries and not refs:
            problems.append(f"{zip_name}: no native module (.soref) at all")
        for marker, lib in sorted(refs.items()):
            lacking = [abi for abi in ABIS if lib not in libs.get(abi, set())]
            if lacking:
                problems.append(f"{zip_name}: {marker} names {lib}, which lib/{','.join(lacking)}/ lacks")
        if zip_name.endswith("sitepackages.zip"):
            print(f"    the dependencies' native modules: {', '.join(sorted(refs)) or 'none'}")
    if problems:
        raise Failed(f"{apk.name}:\n  " + "\n  ".join(problems))
    print(
        f"ok   {apk.name}: manifest, dex and Flutter's assets; {', '.join(ABIS)} each with Flutter,"
        f" the Dart code, {python_lib}, the Dart bridge and every native module; the app ({pkg}) and"
        " flet as Python code"
    )


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "target":
            set_target(Path(argv[1]), argv[2])
        elif len(argv) == 2 and argv[0] == "flet":
            print(f"version={flet_version(Path(argv[1]))}")
        elif len(argv) == 3 and argv[0] == "web":
            check_web(Path(argv[1]), Path(argv[2]))
        elif len(argv) == 2 and argv[0] == "apk":
            check_apk(Path(argv[1]))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except Failed as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
