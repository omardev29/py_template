"""VS Code: .vscode/settings.json, extensions.json, launch.json and tasks.json.

Tasks are `"type": "process"`, so they never go through the user's shell (xonsh, niubash,
MSYS2...): `/bin/sh <root>/pyt ARGS` by default (no dependence on the exec bit) and
`<root>\\pyt.cmd ARGS` in the `windows` block. VS Code applies the block of the OS the task
runs on (the remote one under Remote-WSL/SSH), and in tasks 2.0.0 a per-OS `args` REPLACES the
default one, so the same tasks.json works on every OS.

Errors reach the Problems panel through inline problem matchers (a workspace cannot define
named ones): ruff (concise format, forced with RUFF_OUTPUT_FORMAT in the task env), mypy,
mypyc (stage-relative paths mapped back to src/), the runner's mypyc rules, basedpyright and
pytest crash lines. Every regexp must be valid in JavaScript AND Python `re`
(test_vscode.py compiles them and checks them against real tool output).

Status bar buttons come from the actboy168.tasks extension, which reads
`options.statusbar` from tasks.json: the tasks named in [vscode] buttons get
`{"hide": false}` and everything else is hidden by `tasks.statusbar.default.hide`.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .. import render, tasks as task_runner
from ..config import BACKENDS, Config, compiled_paths
from ..project import ROOT, TEMPLATES, rel
from ..ui import PytError

WS = "${workspaceFolder}"
# mypyc.profile(cfg, "dev").stage relative to the root. launch.json is committed and shared by
# every OS, so it cannot follow the runner's WSL layout (.build/wsl, .venv*-wsl): under
# Remote-WSL on a /mnt/* checkout only the CPython and pytest configs (which use the selected
# interpreter) work; the PyPy and mypyc ones point at the Windows-side paths.
MYPYC_STAGE = ".build/mypyc-dev/stage"
COMPILE_LABEL = "pyt: compile"

ICONS = {
    "run": "play",
    "test": "beaker",
    "check": "checklist",
    "build": "package",
    "report": "graph",
    "compile": "gear",
    "lint": "wand",
    "fmt": "edit",
    "doctor": "pulse",
    "setup": "tools",
    "apply": "sync",
}
CUSTOM_ICON = "run-all"
P_RUN = {"reveal": "always", "focus": True, "panel": "dedicated", "clear": True, "showReuseMessage": False}
P_OTHER = {"reveal": "always", "panel": "shared", "clear": True, "showReuseMessage": False}
P_CHECK = {**P_OTHER, "revealProblems": "onProblem"}
# Re-running a run-like task restarts it instead of asking (a Ctrl+C in a Windows task
# terminal would stop at cmd's "Terminate batch job (Y/N)?").
RESTART = {"instanceLimit": 1, "instancePolicy": "terminateOldest"}
CHECK_KINDS = frozenset({"ruff", "mypy", "rules", "pyright"})


# --- problem matchers ------------------------------------------------------------------------

# Paths: relative to the root as the tools print them (backslashes on Windows; VS Code turns
# them into /) or absolute (C:\... or /...). `[^:]` keeps a Windows drive letter out of it.
_ANY_FILE = r"((?:[A-Za-z]:)?[^:\s][^:]*\.pyi?)"
_MYPY_CODE = r"(?:  \[([a-z0-9-]+)\])?$"
_NO_SEE_ALSO = r"(?!See https?://)"  # mypy's "note: See https://..." lines carry no information

# ruff --output-format concise: `src\x.py:1:8: F401 [*] msg` / `src\x.py:2:1: invalid-syntax: msg`.
# The code alternative needs a hyphen, so mypy's `error`/`note` with columns never match.
RUFF_RE = "^" + _ANY_FILE + r":(\d+):(\d+): ([A-Z]+[0-9]+|[a-z]+(?:-[a-z]+)+):? (?:\[\*\] )?(.*)$"
# The runner's mypyc rules (cmd_dev.run_checks): ui.error/ui.warn + str(lintc.Finding).
RULES_RE = r"^(error|warning): ((?:src|tests)/[^:]*\.pyi?):(\d+): (.*)$"
# basedpyright CLI: `  C:\p\src\x.py:3:5 - error: msg (reportX)`; a multi-line message prints the
# rule on its last, unmatched line. VS Code maps "information" to Ignore (then Error), so the
# group captures only its "info" prefix.
PYRIGHT_RE = r"^\s+((?:[A-Za-z]:)?[^:]+?):(\d+):(\d+) - (error|warning|info)(?:rmation)?: (.*?)(?: \((report[A-Za-z]+)\))?$"
# pytest crash lines: `tests\test_x.py:15: AssertionError` (long tracebacks) or
# `C:\p\tests\test_x.py:15: assert 1 == 2` (--tb=line). Intermediate frames end in "" or "in f".
_PYTEST_TAIL = r":(\d+): ((?:(?:[A-Z]\w*)?(?:Error|Exception|Failed|Warning|Exit|Interrupt)|assert)\b.*)$"
PYTEST_RE = r"^((?:[A-Za-z]:)?[^:\s][^:]*\.py)" + _PYTEST_TAIL
# Under the mypyc backend pytest imports the stage (-o pythonpath=<stage>): an interpreted module
# prints as `.build/mypyc-dev/stage/<pkg>/x.py` (relative or absolute; `.build/wsl/...` under WSL)
# and a compiled one as the stage-relative path mypyc recorded, `<pkg>/core/x.py`. Both map back
# to src/: the stage copy is overwritten on the next run, and `<root>/<pkg>/...` does not exist.
_STAGE = r"(?:(?:[A-Za-z]:)?[^:\s][^:]*[\\/])?\.build[\\/](?:wsl[\\/])?mypyc-(?:dev|release)[\\/]stage[\\/]"


def _roots() -> str:
    """Return the regexp alternatives of the code folders mypy reports: src, and tests and typings
    when they hold code (a folder left holding only __pycache__ is not in a fresh clone:
    render._holds_python, as .mypy.ini `files` and pyright's `include` decide it)."""
    return "|".join(["src", *(r for r in ("tests", "typings") if render._holds_python(ROOT / r))])


def _packages(cfg: Config) -> str:
    """Return the regexp alternatives of the top-level names mypyc compiles (paths in its stage)."""
    names = {p.split("/")[0].removesuffix(".py") for p in compiled_paths(cfg)}
    return "|".join(re.escape(n) for n in sorted(names))


def _mypy_re(severity: str) -> str:
    head = r"^((?:" + _roots() + r")[\\/][^:]*\.pyi?):(\d+):(?:(\d+):)? "
    if severity == "error":
        return head + r"error: (.*?)" + _MYPY_CODE
    return head + r"note: " + _NO_SEE_ALSO + r"(.*)$"


def _mypyc_re(cfg: Config) -> str:
    return (
        r"^((?:" + _packages(cfg) + r")(?:[\\/][^:]*)?\.pyi?):(\d+):(?:(\d+):)? (error|note): "
        + _NO_SEE_ALSO + r"(.*?)" + _MYPY_CODE
    )


RELATIVE = ["relative", WS]
AUTODETECT = ["autoDetect", WS]  # relative to the root first, then absolute
SRC_RELATIVE = ["relative", f"{WS}/src"]  # paths in the mypyc stage, a copy of src/


def _matcher(owner: str, source: str, where: list[str], severity: str | None, pattern: dict[str, Any]) -> dict[str, Any]:
    """`severity`: the default one; a captured severity group overrides it."""
    m: dict[str, Any] = {"owner": f"pytemplate-{owner}", "source": source, "fileLocation": where}
    if severity:
        m["severity"] = severity
    m["pattern"] = pattern
    return m


def problem_matchers(cfg: Config, kinds: set[str], profiles: list[str]) -> list[dict[str, Any]]:
    """Return the matchers for the tools a task runs. Severities follow the typing profiles.

    `profiles`: the profiles whose checks the task runs (`check all` runs several: the
    strictest one wins).
    """
    data = [render.load_profile(p) for p in profiles] or [{"blocking": True}]
    out: list[dict[str, Any]] = []
    if "ruff" in kinds:
        lenient = all(d.get("ruff", {}).get("exit_zero") for d in data)
        pattern = {"regexp": RUFF_RE, "file": 1, "line": 2, "column": 3, "code": 4, "message": 5}
        out.append(_matcher("ruff", "ruff", AUTODETECT, "warning" if lenient else "error", pattern))
    if "mypy" in kinds and not all(d.get("skip_mypy") for d in data):
        blocking = any(d.get("blocking") for d in data)
        err = {"regexp": _mypy_re("error"), "file": 1, "line": 2, "column": 3, "message": 4, "code": 5}
        note = {"regexp": _mypy_re("note"), "file": 1, "line": 2, "column": 3, "message": 4}
        out.append(_matcher("mypy", "mypy", RELATIVE, "error" if blocking else "warning", err))
        out.append(_matcher("mypy", "mypy", RELATIVE, "info", note))
    if "rules" in kinds and cfg.supports("mypyc"):
        pattern = {"regexp": RULES_RE, "severity": 1, "file": 2, "line": 3, "message": 4}
        out.append(_matcher("rules", "mypyc rules", RELATIVE, None, pattern))
    if "pyright" in kinds and cfg.typing.editor == "basedpyright":
        pattern = {"regexp": PYRIGHT_RE, "file": 1, "line": 2, "column": 3, "severity": 4, "message": 5, "code": 6}
        out.append(_matcher("pyright", "basedpyright", AUTODETECT, None, pattern))
    compiled = _packages(cfg) if "mypyc" in kinds and cfg.supports("mypyc") else ""
    if compiled:
        pattern = {"regexp": _mypyc_re(cfg), "file": 1, "line": 2, "column": 3, "severity": 4, "message": 5, "code": 6}
        out.append(_matcher("mypyc", "mypyc", SRC_RELATIVE, None, pattern))
    if "pytest" in kinds:
        regexp = PYTEST_RE
        staged: list[str] = []
        if compiled:
            module = r"(?:" + compiled + r")(?:[\\/][^:]*)?\.py"
            staged = [r"^" + _STAGE + r"([^:\s][^:]*\.py)" + _PYTEST_TAIL, r"^(" + module + r")" + _PYTEST_TAIL]
            # exactly one matcher per line: VS Code's result must not depend on their order
            regexp = r"^(?!" + _STAGE + r"|" + module + r":)" + PYTEST_RE[1:]
        out.append(_matcher("pytest", "pytest", AUTODETECT, "error", {"regexp": regexp, "file": 1, "line": 2, "message": 3}))
        for rx in staged:
            out.append(_matcher("pytest", "pytest", SRC_RELATIVE, "error", {"regexp": rx, "file": 1, "line": 2, "message": 3}))
    return out


# --- tasks.json ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scan:
    """What a ./pyt command prints that the Problems panel can use, and whether it runs the app."""

    kinds: frozenset[str]
    profiles: tuple[str, ...]
    runs_app: bool


def _target(cfg: Config, rest: list[str], *, allow_all: bool) -> list[str]:
    """Mirror cmd_dev.split_backend: the backends a command acts on."""
    first = rest[0] if rest else ""
    if allow_all and first == "all":
        return list(cfg.backend.supported)
    return [first if first in BACKENDS else cfg.backend.active]


def _profiles(cfg: Config, backends: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(cfg.profile_for(b) for b in backends))


def split_words(text: str) -> list[str]:
    """Split a [tasks] dep or a [vscode] buttons entry like the runner splits deps
    (tasks.split_words: quotes group words, a backslash is a plain character); unbalanced quotes
    give plain words (the runner reports them when the task runs)."""
    try:
        return task_runner.split_words(text)
    except ValueError:
        return text.split()


_READS_BACK = re.compile(r"[^\s'\"]+")


def shown(args: Iterable[str]) -> str:
    """The words as a label or a detail shows them: an argument is quoted only when split_words
    would not read it back as one word (empty, a blank or a quote in it); a backslash or a
    non-ASCII letter stays as typed (shlex.join quoted both)."""
    return " ".join(a if _READS_BACK.fullmatch(a) else shlex.quote(a) for a in args)


def scan(cfg: Config, argv: list[str], stack: tuple[str, ...] = ()) -> Scan:
    """Return what `./pyt ARGV` runs (a [tasks] entry: the union of its deps)."""
    if not argv:
        return Scan(frozenset(), (), False)
    name, rest = argv[0], argv[1:]
    if name in cfg.tasks:
        task = cfg.tasks[name]
        kinds: set[str] = set()
        profiles: dict[str, None] = {}
        runs = bool(task.cmd)
        deps = [] if name in stack else task.deps  # a cycle is the runner's error to report
        for dep in deps:
            sub = scan(cfg, split_words(dep), (*stack, name))
            kinds |= sub.kinds
            profiles.update(dict.fromkeys(sub.profiles))
            runs = runs or sub.runs_app
        return Scan(frozenset(kinds), tuple(profiles), runs)
    if name == "run":
        backends = _target(cfg, rest, allow_all=False)
        return Scan(frozenset({"mypyc"} if "mypyc" in backends else set()), (), True)
    if name == "test":
        backends = _target(cfg, rest, allow_all=True)
        return Scan(frozenset({"pytest", *(["mypyc"] if "mypyc" in backends else [])}), (), False)
    if name == "check":
        backends = _target(cfg, rest, allow_all=True)
        return Scan(CHECK_KINDS, _profiles(cfg, backends), False)
    if name == "build":
        backends = _target(cfg, rest, allow_all=False)
        kinds = set() if "--no-check" in rest else set(CHECK_KINDS)
        if "mypyc" in backends:
            kinds.add("mypyc")  # the release stage
        return Scan(frozenset(kinds), _profiles(cfg, backends), False)
    if name == "report":
        return Scan(frozenset({"mypy", "mypyc"}), ("mypyc",), False)
    if name == "compile":
        return Scan(frozenset({"mypyc"}), (), False)
    if name == "lint":
        return Scan(frozenset({"ruff"}), (cfg.profile_for(),), False)
    return Scan(frozenset(), (), False)


@dataclass(frozen=True)
class Entry:
    args: tuple[str, ...]
    summary: str
    icon: str
    group: str | dict[str, Any] | None = None
    hide: bool = False

    @property
    def label(self) -> str:
        # The catalog's `report --open` reads "pyt: report"; any other argument stays visible
        # (`run --open` passes --open to the app: it is not the catalog's "pyt: run").
        args = self.args[:1] if self.args == ("report", "--open") else self.args
        return "pyt: " + shown(args)  # quoted: an argument with a space stays one


def _what_runs(backend: str) -> str:
    return {
        "cpython": "src/main.py on CPython",
        "pypy": "src/main.py on PyPy",
        "mypyc": "compile with mypyc (dev stage) and run the stage",
    }[backend]


def _check_summary(cfg: Config, profile: str) -> str:
    data = render.load_profile(profile)
    tools = "ruff" if data.get("skip_mypy") else "ruff + mypy"
    extra = (" + mypyc rules" if cfg.supports("mypyc") else "") + (
        " + basedpyright" if cfg.typing.editor == "basedpyright" else ""
    )
    return f"{tools} with the '{profile}' typing profile{extra}"


def _custom_summary(cfg: Config, name: str) -> str:
    task = cfg.tasks[name]
    if task.help:
        return task.help
    return "runs: " + " ".join(task.cmd) if task.cmd else "deps: " + ", ".join(task.deps)


def catalog(cfg: Config) -> list[Entry]:
    """Return every task in its default order (buttons are moved to the front by tasks())."""
    active = cfg.backend.active
    others = [b for b in cfg.backend.supported if b != active]
    many = len(cfg.backend.supported) > 1
    out = [Entry(("run",), f"{_what_runs(active)} (active backend: {active})", ICONS["run"])]
    out += [Entry(("run", b), _what_runs(b), ICONS["run"]) for b in others]
    tested = "the mypyc-compiled modules" if active == "mypyc" else active
    out.append(Entry(("test",), f"pytest on {tested} (active backend)", ICONS["test"], {"kind": "test", "isDefault": True}))
    for b in others:
        what = "the mypyc-compiled modules (compiles first)" if b == "mypyc" else b
        out.append(Entry(("test", b), f"pytest on {what}", ICONS["test"], "test"))
    if many:
        out.append(Entry(("test", "all"), f"pytest on every supported backend ({', '.join(cfg.backend.supported)})", ICONS["test"], "test"))
    out.append(Entry(("check",), _check_summary(cfg, cfg.profile_for()), ICONS["check"]))
    if many:
        profiles = ", ".join(_profiles(cfg, cfg.backend.supported))
        out.append(Entry(("check", "all"), f"check every supported backend (typing profiles: {profiles})", ICONS["check"]))
    method = cfg.deploy.default.get(active, "exe")
    build = f"check, then package {active} with the '{method}' method into dist/"
    out.append(Entry(("build",), build, ICONS["build"], {"kind": "build", "isDefault": True}))
    if cfg.supports("mypyc"):
        out.append(Entry(("report", "--open"), "mypyc HTML report of slow lines + mypy Any reports, opened in the browser", ICONS["report"]))
        out.append(Entry(("compile",), "mypyc dev stage only (preLaunchTask of the mypyc debug config)", ICONS["compile"], hide=True))
    out.append(Entry(("lint", "--fix"), f"ruff check --fix (rules of the '{cfg.profile_for()}' typing profile)", ICONS["lint"]))
    out.append(Entry(("fmt",), "ruff format src/ and tests/", ICONS["fmt"]))
    out.append(Entry(("doctor",), "check uv, the compiler, PyPy, shells and the generated files", ICONS["doctor"]))
    out.append(Entry(("apply",), "apply every pytemplate.toml change (rename, dependencies, uv.lock, environments, hook)", ICONS["apply"]))
    out.append(Entry(("setup",), "first time on a clone: the same as apply", ICONS["setup"]))
    out += [Entry((name,), _custom_summary(cfg, name), CUSTOM_ICON) for name in cfg.tasks]
    return out


def _button_entry(cfg: Config, args: tuple[str, ...]) -> Entry:
    """A task for a button that names no catalog task (e.g. "build --method pyz")."""
    from ..cli import COMMANDS

    name = args[0]
    if name in cfg.tasks:
        summary = _custom_summary(cfg, name)
    else:
        summary = COMMANDS[name].summary if name in COMMANDS else "from [vscode] buttons"
    return Entry(args, summary, ICONS.get(name, CUSTOM_ICON))


def _task(cfg: Config, entry: Entry, button: str | None) -> dict[str, Any]:
    found = scan(cfg, list(entry.args))
    matchers = problem_matchers(cfg, set(found.kinds), list(found.profiles))
    t: dict[str, Any] = {
        "label": entry.label,
        "detail": f"./pyt {shown(entry.args)}  |  {entry.summary}",
        "icon": {"id": entry.icon},
    }
    if entry.hide:
        t["hide"] = True
    options: dict[str, Any] = {"cwd": WS}
    if any(m["owner"] == "pytemplate-ruff" for m in matchers):
        options["env"] = {"RUFF_OUTPUT_FORMAT": "concise"}
    if button:
        # actboy168.tasks prefixes the task's own icon: the label is the bare name.
        options["statusbar"] = {"label": button, "hide": False, "running": {"icon": {"id": "sync~spin"}}}
    t.update(
        {
            "type": "process",
            "command": "/bin/sh",
            "args": [f"{WS}/pyt", *entry.args],
            "windows": {"command": WS + "\\pyt.cmd", "args": list(entry.args)},
            "options": options,
        }
    )
    if entry.group:
        t["group"] = entry.group
    first = entry.args[0]
    if found.runs_app:
        # [tasks] with `background = true` (flet `dev`) are run-like too: "isBackground" only
        # helps with a background problem matcher (begins/ends patterns) that tells VS Code when
        # the server is ready, and `flet run -r` prints no stable marker. Without one, VS Code
        # would wait forever for it when used as a dependency; as a restartable RUN task it is
        # started from the button and restarted on the next click.
        t["presentation"] = P_RUN
        t["runOptions"] = RESTART
    elif first == "check" or (first in cfg.tasks and found.kinds & CHECK_KINDS):
        t["presentation"] = P_CHECK
    else:
        t["presentation"] = P_OTHER
    t["problemMatcher"] = matchers
    return t


def _button_text(entry: Entry) -> str:
    text = entry.label.removeprefix("pyt: ")
    return text[:1].upper() + text[1:]


def tasks(cfg: Config) -> dict[str, Any]:
    entries = catalog(cfg)
    # actboy168.tasks creates the buttons in tasks.json order: the button tasks go first, in
    # the order of [vscode] buttons; a button that names no catalog task gets its own task.
    buttons: list[Entry] = []
    for args in dict.fromkeys(tuple(split_words(b)) for b in cfg.vscode.buttons):
        if not args:
            continue
        match = next((e for e in entries if e.args == args or e.label == f"pyt: {shown(args)}"), None)
        if match is None:
            match = _button_entry(cfg, args)
        else:
            entries.remove(match)
        if any(b.label == match.label for b in buttons):
            continue  # "report" and "report --open" name the same task: one task, one button
        buttons.append(match)
    out = [_task(cfg, e, _button_text(e)) for e in buttons]
    out += [_task(cfg, e, None) for e in entries]
    return {"version": "2.0.0", "tasks": out}


# --- launch.json -----------------------------------------------------------------------------

# UTF-8 mode in every debug session, as under ./pyt run/test (proc.base_env) and in the builds:
# without it open() without an encoding reads cp1252 on Windows (Python < 3.15), only under F5.
DEBUG_ENV = {"PYTHONUTF8": "1"}


def _debug(name: str, program: str, env_dir: str | None = None, **extra: Any) -> dict[str, Any]:
    """A debugpy launch config. `env_dir`: an explicit interpreter for both OS layouts
    (bin/python vs Scripts/python.exe); None keeps VS Code's selected interpreter. An `env` in
    `extra` is added to DEBUG_ENV."""
    conf: dict[str, Any] = {"name": name, "type": "debugpy", "request": "launch", "program": f"{WS}/{program}"}
    if env_dir:
        conf["python"] = f"{WS}/{env_dir}/bin/python"
        conf["windows"] = {"python": f"{WS}/{env_dir}/Scripts/python.exe"}
    conf.update({"cwd": WS, "console": "integratedTerminal", "justMyCode": True})
    conf.update(extra)
    conf["env"] = {**DEBUG_ENV, **extra.get("env", {})}
    return conf


def launch(cfg: Config) -> dict[str, Any]:
    # CPython first and on the selected interpreter (so it also works in WSL, where the
    # environment is .venv-wsl): F5 is for interpreted debugging; the backends run as tasks.
    configs = [_debug("src/main.py (CPython, interpreted)", "src/main.py")]
    if cfg.pypy_enabled:
        name = "src/main.py (PyPy, experimental: the debugger is unreliable on PyPy)"
        configs.append(_debug(name, "src/main.py", ".venv-pypy"))
    if cfg.supports("mypyc"):
        # Breakpoints bind in src/main.py and in the interpreted modules (pathMappings maps
        # their stage copies back to src/); the compiled modules are C and never stop.
        configs.append(
            _debug(
                "Run mypyc stage (compiled modules cannot be stepped into)",
                f"{MYPYC_STAGE}/main.py",
                ".venv",  # envs.runtime_env
                preLaunchTask=COMPILE_LABEL,
                pathMappings=[{"localRoot": f"{WS}/src", "remoteRoot": f"{WS}/{MYPYC_STAGE}"}],
                env={"PYTEMPLATE_BACKEND": "mypyc"},
            )
        )
    configs.append(
        {
            "name": "Tests (pytest)",
            "type": "debugpy",
            "request": "launch",
            "module": "pytest",
            "cwd": WS,
            "console": "integratedTerminal",
            "justMyCode": False,
            "env": dict(DEBUG_ENV),
        }
    )
    return {"version": "0.2.0", "configurations": configs}


# --- settings.json / extensions.json ---------------------------------------------------------

# With typing.editor = "basedpyright" the basedpyright extension checks these at every start. When
# they are not set it asks to change them and writes the answer into the workspace settings, i.e.
# this generated file, which then counts as hand-edited: the Python extension's own language server
# conflicts with basedpyright's, and Pylance's type checking would duplicate its diagnostics.
# `[vscode] settings` can still override them.
BASEDPYRIGHT_SETTINGS = {"python.languageServer": "None", "python.analysis.typeCheckingMode": "off"}


def _key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _json_problem(value: Any, where: str) -> str | None:
    """Why `value` cannot go into a JSON file (None when it can): TOML also has dates and times,
    and json.dumps would write nan/inf, which is not JSON."""
    if isinstance(value, dict):
        for k, v in value.items():
            problem = _json_problem(v, f"{where}.{_key(str(k))}")
            if problem:
                return problem
        return None
    if isinstance(value, list):
        for i, v in enumerate(value):
            problem = _json_problem(v, f"{where}[{i}]")
            if problem:
                return problem
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return f"{where} = {value}: JSON has no nan or inf"
    if value is None or isinstance(value, (str, int, float, bool)):
        return None
    kind = type(value).__name__.replace("datetime", "date-time")
    return f"{where} = {value} is a {kind}: VS Code settings take strings, numbers, booleans, arrays and tables"


def _settings_template() -> dict[str, Any]:
    path = TEMPLATES / "vscode" / "settings.json"
    try:
        data: object = json.loads(path.read_text(encoding="utf-8-sig"))  # an editor may add a BOM
    except OSError as e:
        raise PytError(f"cannot read {rel(path)}: {e.strerror or e}") from None
    except UnicodeDecodeError:
        raise PytError(f"{rel(path)} is not UTF-8 text: save it as UTF-8") from None
    except json.JSONDecodeError as e:
        raise PytError(
            f"{rel(path)}: invalid JSON at line {e.lineno}: {e.msg} (plain JSON: no comments, no trailing commas)"
        ) from None
    if not isinstance(data, dict):
        raise PytError(f"{rel(path)} must hold a JSON object ({{ ... }})")
    problem = _json_problem(data, "settings")
    if problem:
        raise PytError(f"{rel(path)}: {problem}")
    return data


def settings(cfg: Config, profile: str) -> dict[str, Any]:
    """The template, then the typing profile's [vscode], then the basedpyright answers, then
    pytemplate.toml [vscode] settings: later ones win."""
    base = _settings_template()
    from_profile = render.load_profile(profile).get("vscode", {})
    problem = _json_problem(from_profile, "[vscode]")
    if problem:
        raise PytError(f"typing profile '{profile}': {problem}")
    base.update(from_profile)
    if cfg.typing.editor == "basedpyright":
        base.update(BASEDPYRIGHT_SETTINGS)
    problem = _json_problem(cfg.vscode.settings, "vscode.settings")
    if problem:
        raise PytError(f"pytemplate.toml: {problem}")
    base.update(cfg.vscode.settings)
    return base


def extensions(cfg: Config) -> dict[str, Any]:
    checker = "detachhead.basedpyright" if cfg.typing.editor == "basedpyright" else "ms-python.vscode-pylance"
    recs = [
        "ms-python.python",
        checker,
        "ms-python.debugpy",
        "ms-python.mypy-type-checker",
        "charliermarsh.ruff",
        "tamasfe.even-better-toml",
        "actboy168.tasks",  # status bar buttons from tasks.json ([vscode] buttons)
    ]
    data: dict[str, Any] = {"recommendations": recs}
    if cfg.typing.editor == "basedpyright":
        data["unwantedRecommendations"] = ["ms-python.vscode-pylance"]
    return data


def outputs(cfg: Config, profile: str) -> dict[str, str]:
    """Return the generated VS Code files: {path relative to the root: content}."""
    return {
        ".vscode/settings.json": render.jsonc(settings(cfg, profile)),
        ".vscode/extensions.json": render.jsonc(extensions(cfg)),
        ".vscode/launch.json": render.jsonc(launch(cfg)),
        ".vscode/tasks.json": render.jsonc(tasks(cfg)),
    }
