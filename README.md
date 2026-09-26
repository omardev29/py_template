[![Tests](https://github.com/omardev29/py_template/actions/workflows/template-selftest.yml/badge.svg)](https://github.com/omardev29/py_template/actions/workflows/template-selftest.yml)
[![E2E](https://github.com/omardev29/py_template/actions/workflows/template-e2e.yml/badge.svg)](https://github.com/omardev29/py_template/actions/workflows/template-e2e.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](#requirements)
[![Backends](https://img.shields.io/badge/backends-CPython%20%7C%20PyPy%20%7C%20mypyc-informational)](#backends)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![GitHub stars](https://img.shields.io/github/stars/omardev29/py_template?style=social)](https://github.com/omardev29/py_template/stargazers)

# py_template

A uv-based template for Python applications. One codebase runs on three backends (CPython,
PyPy and mypyc) and is packaged six ways; the configurations of mypy, ruff, pyright, VS Code
and Neovim are generated from one file, `pytemplate.toml`. Everything goes through `./deploy`,
a command runner that only needs [uv](https://docs.astral.sh/uv/). A preset sets up the kind of
project: `script` (a console program), `raylib` (a 2D game) or `flet` (a desktop app).

This page is the manual of `./deploy` and `pytemplate.toml`. A project made with `./deploy new`
keeps a copy of it, from the template version it was made with, as `.pytemplate/README.md`.
Parts marked **(template repository)** only concern the template itself.

| Backend | What runs the code | Typing | Typical use |
|---|---|---|---|
| `cpython` | uv's CPython (`python.cpython`, 3.14 by default) | optional | scripts, libraries with C extensions (numpy, pillow...) |
| `pypy` | PyPy, a JIT compiler (`python.pypy`, pinned exactly); Python 3.11 syntax | optional | long-running pure Python, cffi libraries (raylib) |
| `mypyc` | the modules of `compile.modules` compiled to C extensions ahead of time, on the CPython of `.venv` | required, no `Any` in compiled code | numeric and CPU-bound logic |

Measured on the maintainer's machine (Windows 11, CPython 3.14.7, PyPy 7.3.23, mypyc 2.3.1):

| Benchmark | CPython | PyPy | mypyc |
|---|---|---|---|
| Sieve up to 5 million (script preset) | 0.42 s | 0.11 s | 0.08 s |
| Collatz < 300,000 (script preset) | 2.45 s | 0.07 s | 0.13 s |
| Bunnymark 30,000 bunnies (raylib preset) | 67 FPS | **217 FPS** | 152 FPS |
| Fractal 640x400 (flet preset) | 2.81 s | n/a | 0.08 s |

## Requirements

- [uv](https://docs.astral.sh/uv/) 0.10.12 or newer: the only program `./deploy` needs. Older
  versions cannot download `pypy@3.11.15`, and uv 0.8 installs a CPython 3.14 release candidate
  instead of 3.14. uv refuses them inside a project ("Required uv version `>=0.10.12` does not
  match", from the `required-version` that `./deploy` writes into `pyproject.toml`) and
  `./deploy doctor` flags them: update with `uv self update` (or `brew upgrade uv`, `pipx
  upgrade uv`, `winget upgrade astral-sh.uv`, `scoop update uv`). When uv is missing, the
  launchers print how to install it; `deploy` and `deploy.ps1` also offer to run the official
  installer in an interactive terminal (never when `CI` is set).
- No Python installation: uv downloads the interpreters (`python.cpython`, `python.pypy`). The
  runner itself runs on the project's CPython, which must be 3.11 or newer.
- A C compiler for the `mypyc` backend (every preset supports it): MSVC Build Tools on Windows
  (`./deploy doctor` prints the `winget` command), gcc or clang on Linux, the Xcode Command Line
  Tools on macOS (`xcode-select --install`).
- The `nuitka` build method needs a C compiler for every backend (on Windows, Nuitka downloads
  one when it finds none) and, on Linux, `patchelf`.
- The `flet` build method (`flet build`) needs, on Windows, Developer Mode and the Visual Studio
  C++ tools; Flet downloads the Flutter SDK it pins (about 3 GB) on the first build.
- The Neovim integration needs Neovim 0.11.2 or newer with LazyVim.
- Network access for the first `./deploy setup` (interpreters and packages) and whenever
  `uv.lock` is re-locked.

What a user of the finished program needs depends on the build method. An `exe`, `nuitka`,
`flet` or bundled `portable` build carries its own Python and dependencies, and runs only on the
OS and CPU it was built on (Linux `exe` and `nuitka` builds also need a glibc at least as new as
the build machine's). A `.pyz`, a wheel or a `runtime = "system"` portable folder needs an
installed CPython (or PyPy, when the project supports it) at or above the project's minimum
Python: `python.cpython` (3.14 by default), or 3.11 when PyPy is supported.

## Getting started

```sh
git clone https://github.com/omardev29/py_template
cd py_template
./deploy new ../my-game --preset raylib   # a new project: script (default), raylib or flet
cd ../my-game
./deploy setup       # interpreters, environments, uv.lock, git hook, generated files
./deploy run         # run the app on the active backend (backend.active)
./deploy test all    # pytest on every supported backend (mypyc: on the compiled modules)
./deploy build       # package the app into dist/ with the backend's default method
```

On Windows, type the same commands in PowerShell or Git Bash; in cmd, `.\deploy` (see
[Shells](#shells)). `./deploy help` lists every command and task, `./deploy help COMMAND`
shows one. On a Mac with Apple Silicon or on Linux ARM64, a raylib project first needs
`./deploy mode cpython --supports cpython,mypyc`: raylib publishes no PyPy wheel there.

The template repository is itself a project of the script preset: `./deploy setup` also works
in a plain clone.

### New projects

`./deploy new DIR [--preset P] [--name NAME]` creates a project in `DIR`, a new or empty folder
outside the template:

1. It copies the files git tracks (and lists the untracked ones it leaves out; without git, or
   when nothing was ever committed, it copies every file). It never copies the git history, the
   environments, builds and caches, `.claude/`, the template's own CI (`template-*.yml`), or the
   template's `README.md` and `LICENSE`, which go to `.pytemplate/README.md` (this manual) and
   `.pytemplate/LICENSE`.
2. It writes the project's own `README.md` (its name, the preset's description and the first
   commands) and `[project] description` in `pyproject.toml`.
3. It runs the preset step in the copy: `src/`, `tests/`, `typings/` and `pytemplate.toml` from
   the preset's skeleton, the preset's dependencies in `pyproject.toml` and `uv lock`, which
   needs the network. raylib and flet projects get the versions the template was tested with
   (`.pytemplate/presets/<preset>/constraints.txt`), once: `./deploy lock --upgrade` moves on.
4. It runs `git init -b main` (unless `DIR` is inside a git work tree) with `deploy` and
   `deploy.ps1` executable. It makes no commit.

When a step fails (a name uv refuses, no network, Ctrl+C), `new` removes what it created.
Dependencies added with `./deploy add` and tracked files of your own (docs, scripts) come along
into the copy: for a clean project, run `new` from an untouched clone of the template.

The app name is the folder name unless `--name` is given (accents are dropped, other characters
become `-`). It is the executable's name; the Python package is the name in lower case with `_`
for `-` (`My-Game` -> `src/my_game/`). A name has letters, digits, `-` and `_`, starts with a
letter and ends with a letter or digit, and must not be a Python keyword, a standard-library
module, a backend (`cpython`, `pypy`, `mypyc`), a package the project locks (directly or not:
`flet`, `rich`, `pygments`...), one of the project's own names (`tests`, `typings`, `build`,
`dist`, `assets`, `main`) or a Windows device name (`con`, `aux`, `nul`, `com1`...). So
`./deploy new ../flet --preset flet` fails: add `--name`.

The preset is fixed when the project is created: to use another one, create a new project with
it and move the code over.

## Commands

`./deploy [-v|-q] [--dry-run] [--no-render] COMMAND [args...]`

| Command (as `./deploy help COMMAND` shows it) | What it does |
|---|---|
| `setup [--force]` | The first run on a fresh clone: the same operation as `apply` |
| `apply [--force]` | Applies every `pytemplate.toml` change: rename, dependencies, `uv.lock`, environments, git hook, generated files ([details](#after-editing-pytemplatetoml)) |
| `doctor` | Checks uv, the environments, the C compiler, the generated files, `pyproject.toml`, `uv.lock`, the changes `apply` has not applied yet, the launchers, the shell, the git hook and Neovim; exit 1 when a line is `[XX]` |
| `sync [cpython\|pypy\|mypyc\|all]` | `uv sync --locked --all-groups` of one environment or of all (default); it never re-locks |
| `lock [--upgrade] [--upgrade-package PKG]` | Rewrites the managed parts of `pyproject.toml` and re-locks `uv.lock`; its other arguments go to `uv lock`. It does not apply `[preset.*]`: `apply` does |
| `add PKG... [--dev\|--group G] [--cpython-only]` | `uv add`; `--cpython-only` adds the marker `implementation_name == 'cpython'` (C-API libraries that are slow or missing on PyPy) |
| `remove PKG... [--dev\|--group G]` | `uv remove` |
| `clean [--envs]` | Deletes `.build/` and `dist/`; `--envs` also this side's `.venv*` environments (`setup` recreates the ones in use) |
| `hooks [install [--force]\|uninstall\|run\|status]` | The git pre-commit hook ([details](#git-pre-commit-hook)); without an argument, `status` |
| `mode [BACKEND] [--supports +B\|-B\|B,B...] [--typing auto\|off\|warn\|strict\|mypyc] [--editor pylance\|basedpyright]` | Shows the mode without arguments; otherwise edits `pytemplate.toml` and applies it (re-lock, generated files, a new PyPy environment) |
| `render [--check] [--diff] [--force]` | Regenerates the generated files; `--check` exits 1 when one is outdated or hand-edited (or `pyproject.toml` does not match), `--diff` shows hand edits, `--force` overwrites them |
| `rename NEW_NAME [--force]` | Renames the app ([details](#renaming-the-app)) |
| `new DIR [--preset P] [--name NAME]` | Creates a project from this template ([details](#new-projects)) |
| `run [BACKEND] [app args...]` | Runs `src/main.py` (mypyc: compiles first); the arguments go to the app |
| `check [BACKEND\|all]` | ruff and mypy with the backend's typing profile, the mypyc rules, and basedpyright with `typing.editor = "basedpyright"` |
| `lint [--fix]` | `ruff check` of `src/` and `tests/` with the active typing profile |
| `fmt [--check]` | `ruff format` of `src/` and `tests/` |
| `test [BACKEND\|all] [pytest args...]` | pytest; with mypyc on the compiled modules (it fails when they were not loaded) |
| `report [--open] [--no-mypy]` | mypyc's HTML report of slow lines and mypy's `Any` reports, in `.build/reports/` (no C compiler needed) |
| `compile [--release]` | Compiles the mypyc stage without running it (for debuggers and editors) |
| `build [BACKEND] [--method exe\|portable\|pyz\|wheel\|nuitka\|flet] [--onefile\|--onedir] [--target KEY]... [--no-check]` | Runs `check`, then packages the app into `dist/` ([details](#distribution)) |
| `pyz-merge A.pyz B.pyz... --out C.pyz` | Merges the `.pyz` files built on several OSes into one |
| `tasks` | Lists the `[tasks]` entries of `pytemplate.toml` |
| `shell-setup [xonsh\|pwsh\|powershell\|bash\|zsh\|niubash\|msys2\|fish\|nu]` | Prints a `deploy` function for a shell ([details](#shells)) |
| `nvim [doctor\|trust\|extras\|bootstrap\|sync]` | The Neovim/LazyVim integration ([details](#neovim-lazyvim)) |
| `selftest [--shells\|--nvim\|--e2e] [args...]` | The template's own tests ([details](#testing-the-template)) |
| `help [COMMAND]` | Every command and task, or one of them |

`BACKEND` is `cpython`, `pypy` or `mypyc` (default: `backend.active`). `run`, `test`, `check`
and `build` read their first argument as the backend when it is one of these words (or `all`,
for `test` and `check`): to pass such a word to the app, name the backend first
(`./deploy run cpython mypyc`).

Global options go before the command: `-v` (more detail, such as the full compiler and
PyInstaller output), `-q` (no progress lines; results, warnings and errors still print),
`--dry-run` (shows what would change and changes nothing; it ignores `-q`), `--no-render` (does
not regenerate the generated files first). For example `./deploy --dry-run apply`; after the
command, `--dry-run` is an error.

Unknown arguments are an error (exit 2), never ignored. Only these commands pass extra
arguments on: `run` to the app, `test` to pytest, `lock` to `uv lock`, `selftest` to pytest,
`build --method exe|nuitka|flet` to the packager (PyInstaller or `flet pack`, Nuitka, `flet
build`; `pyz`, `portable` and `wheel` take none), and a task with a `cmd` to its program.
`-h` or `--help` after a command shows `./deploy help COMMAND`, except after `run`, `test`,
`lock` and `selftest`, where it goes to the app, pytest, uv or the suite; `./deploy -h COMMAND`
works too.

`--dry-run` is not a sandbox: it skips every command it would run and every change to the
project's files, but still writes scratch files under `.build/` (tool configurations, the mypyc
stage). `selftest --shells`, `--nvim` and `--e2e` refuse it.

### Output, exit codes and environment

The runner writes its own messages to stderr, so stdout belongs to the app
(`./deploy run > out.txt` captures only the app). `help`, `shell-setup` and the `--json` reports
of `selftest --shells` and `selftest --e2e` write to stdout, for pipes. Colours are used only on a
terminal, and never with `NO_COLOR` (any non-empty value) or `TERM=dumb`.

Exit codes:

- 0: success.
- 1: check, test or doctor failures, or an internal runner error (a traceback is printed).
- 2: a usage or configuration error (also a program without its executable bit or `#!` line, a
  working folder that does not exist, a bad `[tasks]` entry).
- 3: a missing requirement: uv, a uv older than 0.10.12, a compiler, an interpreter, or the
  runner started on a Python older than 3.11.
- 130: Ctrl+C. The runner waits for the app to finish its own cleanup, then stops without
  running the next step; it exits with the app's code, or 130 when the app exited with 0.
- 141: the reader of stdout went away (`./deploy help | head -1`; Linux and macOS).
- 128 + N: a program killed by signal N.
- `run`, `test BACKEND` and tasks return their program's exit code (pytest: 5 when no test was
  collected, 4 for a usage error). `test all` tests every backend, even after a failure, and
  returns 0 or 1.

The launchers have their own codes: 2 (no project found), 127 (uv not found), 126 (`deploy.ps1`
could not start uv, or PowerShell runs it in ConstrainedLanguage mode).

`run`, `test` and tasks start in the project root, whatever folder the command was typed in, and
the arguments they pass on are not rewritten: a relative path given to the app or to pytest is
relative to the root (`../deploy test tests/test_core.py` from `src/`). Paths that the runner
itself takes (`new DIR`, the `pyz-merge` files, `--project`, `--dir` and `--base` of `selftest`)
are relative to the current folder.

Environment variables the runner and the launchers read: `UV` (the uv binary, looked at first),
`UV_INSTALL_DIR` (searched for uv), `CI` (no install prompt; `selftest --e2e` skips GUI runs on
Windows and macOS CI), `NO_COLOR` and `TERM`, the compiler variables of mypyc (`CC`, `CFLAGS`,
`CPPFLAGS`, `LDSHARED`, `LDFLAGS`, `ARCHFLAGS`, `CL`, `_CL_`), `MACOSX_DEPLOYMENT_TARGET` (the
oldest macOS the pyz and portable wheels support, 13.0 by default), `LOCALAPPDATA` and
`XDG_CACHE_HOME` (the pyz and UPX caches). At runtime, the app's `resources.assets_dir()` (raylib
and flet presets) reads `PYTEMPLATE_ASSETS`, which the portable and pyz launchers set.

The runner ignores an activated virtual environment (`VIRTUAL_ENV`, `PYTHONHOME`, `PYTHONPATH`)
and uv's environment selection (`UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `UV_PROJECT`,
`UV_NO_PROJECT`, `UV_WORKING_DIR`, `UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON`, `UV_ISOLATED`,
`UV_NO_DEV`, `UV_NO_DEFAULT_GROUPS`, `UV_NO_SYNC`): its tools always run in the project's
environments. uv's resolution settings (indexes, `UV_EXCLUDE_NEWER`, `UV_RESOLUTION`,
`UV_PRERELEASE`) and its cache pass through.

### Custom tasks

Justfile-style recipes in `pytemplate.toml`, run as `./deploy NAME [args...]` the same way in
every shell (no shell in between):

```toml
[tasks.gen]
help = "Generate the assets"
cmd = ["python", "scripts/gen.py", "{backend}"]   # the program and its arguments
deps = ["check"]                                   # run first: tasks or ./deploy commands
env = { SEED = "42" }
```

- `cmd`: the program and its arguments, as a list. Extra arguments (`./deploy gen a b`) are
  appended unchanged; the task's exit code is the program's.
- `deps`: tasks or `./deploy` commands with their arguments (`"check all"`), run first and in
  order, each once per invocation; the first one that fails stops the task. A task with only
  `deps` (every preset's `ci`) takes no arguments, and `./deploy ci -h` shows its help.
- `env`: environment variables (string values; the names are identifiers).
- `cwd`: the working folder, relative to the project root (default: the root).
- `backend`: the environment the task runs in (default: `backend.active`). `pypy` needs PyPy in
  `backend.supported`. `mypyc` runs interpreted in `.venv`: to run the compiled modules, add
  `deps = ["compile"]` and run `{build}/mypyc-dev/stage/main.py`.
- `uv`: `true` (default) runs `cmd` with `uv run` in that environment; `false` runs the program
  as it is (a `{python}` whose environment does not exist yet is created first).
- `help`: the line that `./deploy tasks` and `./deploy help` show.
- `background`: a long-running server (flet's `dev`): the editors start it without waiting.

Placeholders in `cmd`, `env` values and `cwd`: `{root}`, `{src}`, `{build}`, `{dist}`,
`{backend}`, `{name}`, `{pkg}` and `{python}` (the backend's interpreter). They are bare names;
a literal brace is written twice (`"d = {{}}"`). Task names are lower-case letters, digits, `-`
and `_`, start with a letter, and are never a `./deploy` command. Tasks also show up in
`./deploy help`, in VS Code and in Neovim.

## Configuration

`pytemplate.toml` is the single source of truth: backends, Python versions, typing, compiled
modules, build options, tasks, editor buttons and the git hook. Every other configuration file
is generated from it or managed by `./deploy`.

### After editing `pytemplate.toml`

Run `./deploy apply` (preview it with `./deploy --dry-run apply`). It brings the whole project in
line with `pytemplate.toml`, and only does what is needed: a second run changes no file.
`./deploy setup` is the same operation under its first-time name, for a fresh clone.
`./deploy mode` edits the common keys for you (it keeps comments, CRLF line endings and a BOM)
and applies them.

| You edited | What `./deploy apply` does |
|---|---|
| `app.name` | Renames the app the way `./deploy rename` does ([Renaming the app](#renaming-the-app)): moves `src/<pkg>/`, rewrites the references, updates `pyproject.toml`, re-locks and regenerates. It refuses when git has uncommitted changes (`--force` skips that check) and when the name is not valid (the rules of [New projects](#new-projects)). |
| `app.preset` | Refuses (exit 2) and writes nothing: a project cannot switch presets in place. Put the old value back; for another preset, create a project with `./deploy new DIR --preset P` and move the code. |
| `[preset.flet] version` | Pins `flet`, `flet-desktop` and `flet-cli` to that version, re-locks `uv.lock` and syncs the environments. |
| `[preset.raylib] package`, `version` | Removes the old raylib package, adds `{package}=={version}`, moves `no-build-package` to it, re-locks and syncs. |
| `backend.supported` | Rewrites the managed parts of `pyproject.toml`, re-locks and syncs every supported environment. When PyPy is new it first checks that the code is valid Python 3.11. Environments no longer used are only listed (`./deploy clean --envs` removes them). |
| `python.cpython`, `python.pypy` | Rewrites the managed parts of `pyproject.toml`, re-locks when needed and syncs the environments on the new interpreters. |
| `hooks.pre_commit` | `true`: installs or updates the git hook. `false`: removes pytemplate's own hook (another tool's hook is never touched). |
| `backend.active`, `[typing]`, `[compile]`, `[vscode]`, `[tasks]`, `app.gui`, `app.assets` | Regenerates the generated files (most commands do it too). It warns when `compile.modules` or `app.assets` names something that does not exist. |
| `[deploy]` and its tables | Nothing: `build` reads them. It warns when `deploy.exe.icon` or `deploy.upx.path` names a missing file. |

Every check and refusal happens before the first write. When a dependency edit or the re-lock
fails, `pyproject.toml` gets its old content back and nothing is recorded: fix the problem and run
`apply` again. `--force` only skips the uncommitted-changes check of a rename. The summary ends
with `ok pytemplate.toml applied` (`setup`: `ok done. Try: ./deploy run ...`).

Until `apply` runs, such an edit is only partly in effect. The commands that regenerate files
warn `pyproject.toml does not match pytemplate.toml ... ./deploy apply` after an edit of
`backend.supported` or `[python]`, and `./deploy doctor` lists every pending change:

```
  [XX] app.name = 'NEW' is not applied: the package is still src/OLD/
  [XX] [preset.flet] is not applied to pyproject.toml (add flet==1.0.0, ...)
  [XX] hooks.pre_commit = false, but pytemplate's git pre-commit hook is installed
  [ok] pytemplate.toml applied (app.name, app.preset, [preset.*], hooks.pre_commit)
```

`./deploy lock` alone re-locks with the managed parts of `pyproject.toml` but does not apply
`[preset.*]`: after a `[preset.*]` edit, use `apply`. `./deploy add flet==X` does not change the
Flet version either: it fails against the pinned `flet-cli`, and `apply` would put
`[preset.flet] version` back.

### Renaming the app

`./deploy rename NEW_NAME [--force]` renames the app; preview it with `./deploy --dry-run rename
NEW_NAME`, which lists the folder move and every file with sample lines. An `app.name` edited by
hand is finished by `./deploy apply` (or by `./deploy rename` with that name). What changes:

- `src/<pkg>/` moves to the new package (the name in lower case, `_` for `-`).
- The Python files of `src/` and `tests/`: the imports of the package and the names bound to
  them (a local variable of the same name is left alone and reported). Strings, comments and
  other text files there: package paths, dotted names, `-m` arguments and `pkg:function`
  references get the package; titles and other prose get the name.
- `pytemplate.toml`: `app.name` and every package reference (`compile.modules`, `exclude`,
  `forbid_imports`, the mypy overrides, `hidden_imports`, `exclude_modules`, the wheel entry);
  other mentions are reported. `pyproject.toml`: `[project] name` and the preset tables.
- `uv.lock` is re-locked, the generated files are regenerated, and the files that ruff accepted
  before get their import order and formatting fixed.

Other files (README.md, `docs/`, scripts, your own workflows) are only listed when they mention
the old name; `dist/` and `.build/` keep the old name until the next build (`./deploy clean`). It refuses uncommitted changes
without `--force` (a project fresh from `./deploy new` has no commit yet: commit first), and
the names `new` refuses. A write that fails puts every file back. When the old name is a
common word (`app`, `game`, `core`), matching words in comments and strings change too: review
`git diff`.

### Generated files

These files are generated from `pytemplate.toml` and `.pytemplate/templates/`, and committed:
`.python-version`, `.mypy.ini`, `.ruff.toml`, `pyrightconfig.json`, `.vscode/settings.json`,
`.vscode/extensions.json`, `.vscode/launch.json`, `.vscode/tasks.json`, `.lazy.lua`,
`.pytemplate/editor.json`, `.github/workflows/ci.yml` and `.pytemplate/state.json`. Most
commands regenerate them first and say which changed; `./deploy render` does only that.

- Never edit them by hand. `./deploy` notices an edit (by the hashes in `.pytemplate/state.json`),
  leaves the file alone and warns; `./deploy render --diff` shows the difference and
  `./deploy render --force` overwrites it. Change `pytemplate.toml`, or the sources in
  `.pytemplate/templates/` (a typing profile is `.pytemplate/templates/typing/<profile>.toml`).
  CRLF line endings and a BOM (Windows checkouts, editors) do not count as edits.
- After a merge conflict in `state.json` or `editor.json`, run `./deploy render` and commit both.
- The project's CI, `.github/workflows/ci.yml`, is generated from `.pytemplate/templates/ci.yml`:
  edit that file. Deleting it stops the generation (deleting only `ci.yml` is undone by the next
  command).
- `pyproject.toml` has two managed parts: `requires-python` (`>=` the oldest Python in use: 3.11
  with PyPy, else `python.cpython`) and the `[tool.uv]` block between `# >>> pytemplate` and
  `# <<< pytemplate` (the Python versions `uv.lock` resolves for, the uv version floor,
  uv-managed interpreters only, and preset keys such as raylib's `no-build-package`). A TOML
  formatter may reformat them (only the meaning is compared), but the markers must stay; broken
  markers are an error that says how to fix them. The rest of `pyproject.toml` is yours. Never add
  `[build-system]`: the project is an application (the wheel method writes its own).

Three tools run outside `uv.lock`, at versions pinned in the runner: `check` with
`typing.editor = "basedpyright"` runs `basedpyright==1.40.1` with the Node.js runtime
`nodejs-wheel-binaries==24.19.0`, the nuitka method runs `nuitka==4.2.2`, and UPX 5.2.1 is
downloaded for `[deploy.upx]` ([Maintaining the template](#maintaining-the-template) says how to
move them).

### `pytemplate.toml` reference

Every key, with its value in the script preset (and where raylib or flet differ). An unknown key
or a value of the wrong type stops every command with an error that names the key (exit 2). The
file must be UTF-8 (a BOM is fine); `schema = 1` is the layout this runner reads.

| Key | Script preset | Meaning |
|---|---|---|
| `app.name` | the folder name | the executable's and `dist/` name; the package `src/<pkg>/` is its snake_case form |
| `app.preset` | `"script"` | `script`, `raylib` or `flet`: fixed when the project is created |
| `app.gui` | `false` (raylib, flet: `true`) | `true`: no console window (exe, nuitka, wheel); launchers use `pythonw`/`pypyw` |
| `app.assets` | `""` (raylib, flet: `"assets"`) | `"assets"` bundles `src/assets/` with the app, `""` bundles nothing (no other name) |
| `backend.active` | `"cpython"` (raylib: `"pypy"`) | the backend of `run`, `test`, `check` and `build` when none is given |
| `backend.supported` | `["cpython", "mypyc"]` (raylib: all three) | the environments, `uv.lock` and the CI matrix |
| `python.cpython` | `"3.14"` | the CPython minor version (uv picks the patch); the runner runs on it too |
| `python.pypy` | `"pypy@3.11.15"` | the exact PyPy version ([PyPy](#pypy)) |
| `typing.profile` | `"auto"` | `auto` (`mypyc` on the mypyc backend, else `typing.relaxed`), `mypyc`, `strict`, `warn` or `off` |
| `typing.relaxed` | `"off"` | what `auto` means on cpython and pypy: `off`, `warn` or `strict` |
| `typing.editor` | `"pylance"` | `pylance` or `basedpyright`: the VS Code extension, the rules of `pyrightconfig.json`, and `check` also runs basedpyright |
| `typing.mypy_overrides` | none (raylib, flet: one) | `[[typing.mypy_overrides]]` tables: `module` (a name, a pattern or a list; `{pkg}` works) and mypy options other than `strict`, one `.mypy.ini` section each |
| `compile.modules` | `["<pkg>.core"]` | what mypyc compiles: packages or modules of `src/`, none inside another |
| `compile.exclude` | `[]` | modules or subpackages inside them that stay interpreted |
| `compile.forbid_imports` | `[]` (raylib: `["pyray"]`; flet: `["flet", "flet_desktop", "flet_cli"]`) | imports `./deploy check` refuses in compiled code |
| `compile.annotate` | `false` | every mypyc build also writes the report of slow lines |
| `compile.opt_level` | `"3"` | mypyc's C optimisation, `"0"` to `"3"` ([C compiler options](#c-compiler-options-and-rebuilds)) |
| `compile.no_semantic_interposition` | `true` | Linux gcc/clang: calls between compiled functions may be inlined |
| `compile.multi_file` | `false` | mypyc's `multi_file`: one C file per module |
| `compile.separate` | `false` | one shared library per compiled module instead of one per package |
| `compile.strict_dunder_typing` | `false` | mypyc's `strict_dunder_typing` |
| `deploy.optimize` | `1` | Python's `-O` level of the exe, nuitka and portable builds: `0`, `1` (no asserts) or `2` (no docstrings either) |
| `deploy.default` | `{ cpython = "exe", mypyc = "exe", pypy = "portable" }` | the `build` method of each backend (one left out keeps its default) |
| `deploy.exclude_modules` | `[]` (flet: `["PIL"]`) | modules the exe and nuitka builds never bundle, even when something imports them |
| `deploy.exe.mode` | `"onefile"` (raylib, flet: `"onedir"`) | `onefile` or `onedir` |
| `deploy.exe.console` | `"auto"` | `auto` (the opposite of `app.gui`), `yes` or `no` |
| `deploy.exe.icon` | `""` | an icon, relative to the project: `.ico` on Windows (exe, nuitka), `.icns` for a macOS `.app` (`app.gui = true`); PyInstaller ignores it on Linux |
| `deploy.exe.hidden_imports` | `[]` | extra modules PyInstaller (and `flet pack`) must bundle |
| `deploy.exe.strip` | `false` | Linux, macOS: strip the symbol tables of the bundled binaries |
| `deploy.exe.extra_args` | `[]` (raylib: three `--exclude-module`) | appended to the PyInstaller or `flet pack` command |
| `deploy.portable.runtime` | `"bundled"` | `bundled` (the interpreter inside) or `system` (the target's own Python) |
| `deploy.portable.prune` | `true` | leave the parts of the interpreter the app does not use out |
| `deploy.portable.archive` | `true` | also a `.zip` (Windows) or `.tar.gz` of the folder |
| `deploy.portable.env` | `{}` | environment variables the launchers set (literal values) |
| `deploy.pyz.targets` | `["host"]` | extra platforms, such as `"cp314-linux-x86_64"` |
| `deploy.wheel.entry` | `""` (flet: `"<pkg>.ui.app:run"`) | the installed command, `"package.module:function"`; empty: `<pkg>.app:main` |
| `deploy.nuitka.mode` | `"standalone"` | `standalone` or `onefile` |
| `deploy.nuitka.lto` | `"auto"` | `auto`, `yes` or `no` |
| `deploy.nuitka.pgo` | `false` | profile-guided C optimisation (experimental) |
| `deploy.nuitka.pgo_args` | `[]` | the app's arguments for the profiling run |
| `deploy.nuitka.extra_args` | `[]` | appended to the Nuitka command |
| `deploy.flet.target` | `"host"` | `host`, `windows`, `macos`, `linux`, `apk`, `aab`, `ipa` or `web` |
| `deploy.flet.cleanup` | `true` | `--cleanup-app --cleanup-packages`: no tests or docs in the bundle |
| `deploy.flet.exclude` | `[]` | app files or folders `flet build` leaves out |
| `deploy.flet.extra_args` | `[]` | appended to the `flet build` command |
| `deploy.upx.enabled` | `false` | pack the binaries with UPX ([Binary size](#binary-size)) |
| `deploy.upx.level` | `"best"` | `1` to `9`, `best`, `brute` or `ultra-brute` |
| `deploy.upx.lzma` | `true` | LZMA: smaller, slower to unpack |
| `deploy.upx.exclude` | `[]` | extra file-name globs that are never packed |
| `deploy.upx.path` | `""` | a UPX binary: absolute, `~`, or relative to the project root |
| `tasks.<name>` | `ci` | custom tasks ([Custom tasks](#custom-tasks)) |
| `preset.raylib.package` | (raylib: `"raylib"`) | `raylib` (GLFW), `raylib_sdl` (SDL3) or `raylib_software` |
| `preset.raylib.version` | (raylib: `"6.0.1.0"`) | the raylib version |
| `preset.flet.version` | (flet: `"1.0.1"`) | the version of `flet`, `flet-desktop` and `flet-cli` |
| `vscode.settings` | `{}` | merged into `.vscode/settings.json` (JSON values only) |
| `vscode.buttons` | `["run", "test", "check", "build"]` | status bar buttons: commands or task names ([VS Code](#vs-code)) |
| `hooks.pre_commit` | `true` | the git pre-commit hook ([Git pre-commit hook](#git-pre-commit-hook)) |

## Shells

The logic lives in `.pytemplate/deploy.py` (standard library only, run by uv). Three thin
launchers only find the project root and uv and forward the arguments, unchanged, so
`./deploy build mypyc` is typed the same way everywhere:

| Shell | Launcher | Notes |
|---|---|---|
| bash, zsh, sh, dash, ksh, busybox (Linux, macOS, WSL) | `deploy` | `#!/bin/sh`, plain POSIX sh |
| Git Bash, MSYS2 (any MSYSTEM), Cygwin, busybox-w32, niubash | `deploy` | Finds uv even when a login shell's PATH lacks it (MSYS2 starts with a minimal PATH) |
| xonsh, fish, nushell, PowerShell 7 on Linux/macOS | `deploy` | Through its `#!/bin/sh` (in PowerShell, `./deploy.ps1` works too) |
| cmd | `deploy.cmd` | `.\deploy ...` (a bare `deploy ...` too, see the limits below) |
| xonsh on Windows | `deploy.cmd` | xonsh can only start PATHEXT files: `./deploy` resolves to `deploy.cmd` |
| nushell on Windows | `deploy.cmd` | Type `./deploy.cmd`, or use the `shell-setup nu` function |
| PowerShell 7 / Windows PowerShell 5.1 on Windows | `deploy.ps1` | `./deploy` resolves to `deploy.ps1`. If the execution policy blocks it: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `.\deploy.cmd` |

Editors do not depend on your shell: VS Code tasks run `/bin/sh deploy` (`deploy.cmd` on
Windows) and Neovim runs uv directly.

**From subfolders.** The launchers find the project from their own location and, when that
fails, walk up from the current folder: `../deploy test` from `src/`, or the full path of the
launcher from anywhere, works. Paths you give the runner (`./deploy new ../game`,
`./deploy pyz-merge a.pyz b.pyz --out all.pyz`) are relative to the folder where you typed the
command; `~` works everywhere, and on Windows `/c/Users/...`, `/cygdrive/c/...` and `C:/...` are
accepted too.

**Without `./`.** `./deploy shell-setup SHELL` prints a `deploy` function (or alias) that finds
the enclosing project from any subfolder, with a comment saying where to paste it. Shells:
`bash`, `zsh`, `niubash`, `msys2`, `fish`, `nu`, `xonsh` (with completion), `pwsh`,
`powershell`.

```bash
./deploy shell-setup niubash    # niubash reads ~/.niubashrc, but `niu -c` and scripts read $NIU_ENV
```

**Known limits.**
- cmd re-parses the arguments of every `.cmd` file: `% ! " ^ & | < >` inside an argument do
  not survive when the call goes through `deploy.cmd` (cmd itself, xonsh on Windows, nushell on
  Windows, VS Code tasks on Windows), even when the calling program quotes them. For such
  arguments use PowerShell, Git Bash, or the xonsh or nu `deploy` from `shell-setup` (they call
  uv directly).
- PowerShell removes a bare `--` before any script sees it (5.1 and 7 alike): quote it (`'--'`)
  or use `.\deploy.cmd`. `./deploy` itself never needs `--`: everything after `run` or `test`
  already goes to your app or to pytest.
- In PowerShell on Windows, `./deploy` and `../deploy` resolve to `deploy.ps1`, but a full path
  without the extension (`C:\proj\deploy`) opens the extensionless sh launcher through Windows'
  file association instead: type `C:\proj\deploy.ps1`.
- cmd only runs a bare `deploy` from the current folder while `NoDefaultCurrentDirectoryInExePath`
  is unset (some environments set it): `.\deploy` or `.\deploy.cmd` always works.
- In cmd (and in VS Code tasks on Windows), Ctrl+C asks "Terminate batch job (Y/N)?".
- On Windows, the `bash` on PATH may be WSL's launcher (see [Troubleshooting](#troubleshooting)).

`./deploy doctor` shows which launcher started it and checks that the launchers kept their line
endings and executable bit.

### Git pre-commit hook

`./deploy setup` installs a pre-commit hook (`[hooks] pre_commit = true`, the default): a small
sh script in the repository's hooks folder that runs `./deploy hooks run`. On every commit it
checks the staged files in about a second: ruff and `ruff format --check` on the staged Python
files (with the active typing profile), the generated files up to date and staged, `uv.lock` in
sync with `pyproject.toml`, the mypyc rules on staged compiled modules (blocking only with the
`mypyc` profile) and the launchers' line endings and modes. It does not run mypy: that is
`./deploy check`, the editors and CI. It checks the working-tree version of the staged files.

- Skip it once: `git commit --no-verify`. Remove it: `./deploy hooks uninstall` (and
  `pre_commit = false` so that `setup` does not install it again). State: `./deploy hooks`.
- An existing hook of yours is never overwritten: `./deploy hooks install --force` keeps it as
  `pre-commit.local` and runs it first. With `core.hooksPath` set, nothing is written there:
  add `sh ./deploy hooks run || exit $?` to your own hook.
- It works from any git client (Git Bash, cmd, PowerShell, xonsh, VS Code, lazygit): git runs
  hooks with its own `sh`, and the hook calls the POSIX launcher, which finds uv by itself.

### Typing by backend

| Profile | When | What it requires |
|---|---|---|
| `off` | cpython/pypy by default | Nothing: only real errors (syntax, undefined names) |
| `warn` | `./deploy mode --typing warn` | Everything as a warning, never blocking |
| `strict` | `./deploy mode --typing strict` | mypy `--strict`, `Any` allowed |
| `mypyc` | always with the mypyc backend | `--strict` across all of `src/` and **`Any` forbidden in compiled code** |

With mypyc, `Any` is not just ugly, it is **slow**: mypyc generates generic operations wherever
there is `Any` (a `list[Any]` can be slower than uncompiled CPython). That is why the
`mypyc` profile enables `disallow_any_explicit/expr/decorated/unimported` in the modules of
`compile.modules`, while the rest of `src/` (the boundary with poorly typed libraries) stays
strict but may use `Any`. Pylance in strict mode does not flag an `Any` written on
purpose; if you want to see it in the editor too: `./deploy mode --editor basedpyright`.

## mypyc: rules to make the binary fly

- **Layout**: `src/<package>/core/` is compiled; the boundary (`app.py`, `ui/`, `gfx/`) is not.
  `src/main.py` is the launcher and is never compiled (a compiled module cannot be `__main__`).
- **Constants with `Final`**: a global without `Final` is looked up in a dictionary on every access.
- **Native classes**: typed `float`/`int` attributes; only the `@dataclass`, `@final`,
  `@trait` and `@mypyc_attr` decorators (any other one turns the class into a slow Python class).
- **Concrete types**: `list[bool]` compiles to direct accesses, `bytearray` takes the generic
  path (sieve: 4.2x vs 1.9x).
- `./deploy report --open` marks every generic operation in red ("make it Final", "Generic `*`").
  With `compile.annotate = true`, every mypyc build (`run`, `test`, `compile`, `build`) also
  writes that annotated HTML report of slow lines to `.build/reports/mypyc-annotate.html` (the
  same report as `./deploy report`, without mypy's Any reports).
- `./deploy check` adds rules that mypy does not see: forbidden imports in compiled code,
  decorators that disable native classes, `__file__` at module level, t-strings...
- Compiled code has no pdb, cProfile or monkeypatch: debug it interpreted (F5 in VS Code, the
  dap keys in Neovim). The "Run mypyc stage" debug configuration runs the compiled build, but
  compiled modules cannot be stepped into.

### Fast integers: `i64`

A Python `int` has no size limit, so mypyc stores it as a *tagged* integer: small values live in
a machine word, and every operation checks the tag and for overflow, with a slow path that
creates a big Python int. That is already much faster than CPython, but the C compiler cannot
see through the slow path, so a loop stays a loop. `mypy_extensions.i64` (also `i32`, `i16`,
`u8`) is your promise that the value always fits in 64 bits: mypyc then emits plain C
`int64_t` arithmetic, and gcc/clang can simplify, vectorise or even delete the loop.
`mypy-extensions` is already a runtime dependency of every project (`pyproject.toml`): just
`from mypy_extensions import i64`. Interpreted code (the cpython and pypy backends) sees `i64`
as a plain `int`.

100 million iterations of `x += 1` (Linux x86_64, gcc 13, CPython 3.14, mypyc 2.3.1, Nuitka
4.2.2; MSVC on Windows not measured):

| How it runs | Time | The loop |
|---|---|---|
| CPython 3.14, interpreted | ~1.8 s | runs |
| Nuitka (default, `--lto=yes` or PGO) | ~1.3-1.9 s | runs (Python objects and libpython calls) |
| mypyc, `int`, `compile.opt_level` "1"-"3" | ~0.13-0.18 s | runs |
| mypyc, `int`, `compile.opt_level = "0"` | ~3.4 s | runs (slower than CPython: debug only) |
| mypyc, `i64`, `compile.opt_level` "2"/"3" | ~0 s | removed: the function became `return n` |

When to use it:
- Use `i64` for local variables in the hot loops of compiled modules (`src/<pkg>/core/`):
  counters, indices, accumulators, bit twiddling, hashes, grid/pixel coordinates, fixed-point
  math, when the values are known to fit in +-9.2e18. Profile first (`./deploy report --open`).
- Keep `int` everywhere else: public functions, ids, money, sizes that come from outside,
  values that can grow (powers, factorials), and anything stored in a `list`/`dict`/`set`
  (containers hold Python int objects either way: no speed-up there). `int` in mypyc is
  already ~10x faster than CPython on this loop.
- A `range` loop gets a native index only when its END is a fixed-width int: `for i in range(n)`
  with `n: i64`, or `range(i64(n))`. With `n: int` the index is a tagged int and the loop stays
  (158 ms for 1e8 in the same test, even with `x: i64`). Constant bounds (`range(1, 100_000_001)`)
  are also cheap.

What changes when compiled (checked with mypyc 2.3.1; interpreted code keeps Python's
unlimited ints, so the cpython/pypy backends never show these):

| `x: i64` | Compiled | Interpreted |
|---|---|---|
| `2**63 - 1 + 1`, `2**62 * 4`, `1 << 64` | wraps silently: `-2**63`, `0`, `1` | `2**63`, `2**64`, `2**64` |
| `-x` / `abs` for `x = -2**63` | `-2**63` | `2**63` |
| assigning an `int` that does not fit (`x = 2**63`) | `ValueError: int too large to convert to i64` | works |
| `-2**63 // -1` | `OverflowError` | `2**63` |
| `-7 // 2`, `-7 % 2`, `x // 0` | `-4`, `1`, `ZeroDivisionError` (Python semantics) | same |

mypy accepts `+ - * // % << >> & | ^`, comparisons, `min`/`max`, `int(x)`, `float(x)`, an `i64`
wherever an `int` is expected, and an `int` assigned to an `i64` (range-checked when compiled).
It rejects, as type errors, `x / y`, `x ** y`, `abs(x)`, `round(x)`, `divmod(x, y)` and mixing
with `float` (`x * 0.5`): convert explicitly, e.g. `float(x) / y`, `float(x) * 0.5`,
`int(x) ** 2`, `abs(int(x))`, `divmod(int(x), int(y))` (those take the generic, slower path).
Because overflow only happens compiled, test the edge values with `./deploy test mypyc`: the
cpython and pypy test runs cannot catch a wrap-around.

## PyPy

Enable it per project with `./deploy mode --supports +pypy` (the raylib preset comes with it):
it checks that your code is valid Python 3.11, lowers `requires-python` to `>=3.11` and
creates `.venv-pypy`. Details the template already handles for you:

- PyPy is pinned **exactly** (`pypy@3.11.15`): PyPy 8.0 changed the extension ABI and there
  are no wheels for it yet.
- The tools (mypy, ruff, PyInstaller) always run on CPython; the PyPy environment
  only has your dependencies and pytest.
- CPython C-API libraries (numpy, pillow, pydantic-core) are slow on PyPy:
  add them with `./deploy add numpy --cpython-only`. **cffi** libraries (raylib) run very well.
- The JIT needs to warm up (~1 s): on very short scripts PyPy does not win.

## Distribution: `./deploy build [BACKEND] --method ...`

| Method | cpython | mypyc | pypy | Result |
|---|---|---|---|---|
| `exe` | ✓ | ✓ | ✗ | PyInstaller executable (Flet: `flet pack`, with the Flutter client inside) |
| `portable` | ✓ | ✓ | ✓ | Folder with the interpreter inside + `.cmd`/`.sh` launcher (the only standalone option for PyPy) |
| `pyz` | ✓ | ✓ | ✓ | **A single file** that runs on any installed CPython or PyPy |
| `wheel` | ✓ | ✓ | ✓ | Package for `uv tool install` (with mypyc, a platform wheel) |
| `nuitka` | ✓ | ✓ | ✗ | Nuitka executable (it also compiles the dependencies; slow builds) |
| `flet` | ✓ | ✓ | ✗ | `flet build`: native installers, Android/iOS/web (flet preset) |

By default, `cpython` and `mypyc` use `exe` and `pypy` uses `portable` (`[deploy] default`).

**The portable `.pyz`** carries your code as `.py` plus, for each platform, the binaries
(the mypyc `.pyd` files from the OS you compiled on, and the native dependencies). The first
time it runs, it extracts itself to a cache and uses whatever matches the interpreter running it.
The same 1.7 MB file, tested here: CPython 3.14 uses the compiled core, PyPy runs the
`.py` with its JIT and CPython 3.13 runs the `.py`. mypyc does not compile for other OSes: the
CI (`.github/workflows/ci.yml`) compiles on Windows, Linux and macOS, and `./deploy pyz-merge`
merges the three into a single `.pyz`. For native dependencies of other OSes without CI:
`[deploy.pyz] targets = ["cp314-linux-x86_64", ...]`. On Windows, the `.cmd` next to the `.pyz`
finds a suitable Python or PyPy for you.

Other options in `pytemplate.toml` (for the size ones, see [Binary size](#binary-size)):
`deploy.optimize` (`-O` bytecode; with mypyc it strips
the `assert`s), `deploy.exe.mode` (`onefile`/`onedir`), `app.gui` (no console),
`deploy.portable.runtime = "system"` (folder without an interpreter, for the target machine's
Python), `deploy.portable.env` (variables the portable launchers set: the names must be valid
variable names and the values strings; the Windows `.cmd` launcher only takes ASCII values with
no `"` or line breaks).

### Binary size

Measured on Windows 11 with the flet preset (CPython backend, Flet 1.0.1):

| Build | Folder | Zip | First start |
|---|---|---|---|
| `exe` (`flet pack`, onedir) with Pillow | 85.5 MB | - | unpacks the Flet client: +97 MB in `~/.flet/client` |
| `exe` with `exclude_modules = ["PIL"]` (the preset default) | 72 MB | 58 MB | +97 MB |
| ... plus `[deploy.upx] enabled = true` | 63 MB | 57 MB | +97 MB |
| `flet` (`flet build`), no options | 97 MB | - | nothing to unpack |
| `flet` with `cleanup = true` + UPX | 78 MB | **38 MB** | nothing to unpack |

Where it goes: every Flet desktop app carries the Flutter engine (`flutter_windows.dll`, 20 MB)
and Flet's compiled Dart UI (`app.so`, 15-19 MB); Python adds its runtime (`python314.dll`,
6 MB, plus the standard library) and your dependencies. `flet pack` (and PyInstaller with
`flet-desktop`, which is the same thing) ships Flet's prebuilt **full** client, zipped (40 MB),
with libmpv for audio and video (28 MB) and Rive, and unpacks it on the first start. `flet build`
compiles a client with only the Flutter packages the app uses: the smallest download and the
smallest install. It needs Windows Developer Mode, the Visual Studio C++ tools, and it downloads
the Flutter SDK version Flet pins (3.44.8 for Flet 1.0.1: about 3 GB in `~/flutter`, once); the
first build takes about 7 minutes, the next ones about 3. For the smallest Flet app:
`./deploy build --method flet` (or `[deploy] default = { cpython = "flet", ... }`).

For the script preset with mypyc: the onefile `exe` is 13.2 MB (12.3 MB with UPX); the
`portable` folder is 62 MB, 49 MB with UPX, and its zip 23 MB.

Size settings (each method ignores what does not apply to it):

| Setting | Methods | Effect |
|---|---|---|
| `[deploy] exclude_modules = [...]` | exe, nuitka | Modules never bundled even if something imports them (PyInstaller also follows imports inside functions). flet preset: `["PIL"]` (-13 MB; Flet only uses Pillow for `RawImage`). `"ssl"` saves 2 MB more if the app never uses HTTPS (OpenSSL's `libcrypto` stays while `hashlib` needs it). |
| `[deploy.upx]` `enabled`, `level`, `lzma`, `exclude` | exe, nuitka, portable, flet | UPX packs executables and libraries (below) |
| `[deploy.exe] mode = "onefile"` | exe | One compressed file (zlib), unpacked to a temp folder on every start |
| `[deploy.exe] strip = true` | exe (Linux, macOS) | Strips the symbol tables of the bundled binaries |
| `[deploy.nuitka] mode = "onefile"` | nuitka | One zstd-compressed file |
| `[deploy.flet] cleanup = true`, `exclude = [...]` | flet | `--cleanup-app --cleanup-packages` (no tests or docs in the bundle); app files left out |
| `[deploy.portable] prune`, `archive` | portable | Unused parts of the interpreter removed; a zip or tar.gz next to the folder |
| `[deploy] optimize = 2` | all | `-OO` bytecode (no docstrings) |

**UPX** (`[deploy.upx]`, off by default): `level` is `1`..`9`, `best` (default), `brute` or
`ultra-brute` (much slower builds for a few % more); `lzma = true` packs smaller and unpacks
slower; `exclude` adds file-name globs. The exe method uses PyInstaller's own UPX step (every
binary is packed before bundling, also in onefile mode; PyInstaller always uses LZMA and skips
Control Flow Guard DLLs), Nuitka its upx plugin (always `--best --lzma`), and the portable and
flet builds are packed when they are done (the portable smoke test then loads the packed
modules). Never packed: files over 600 MiB (UPX refuses anything over 768 MiB; the margin is on
purpose), binaries UPX rejects (Control Flow Guard), the C runtime, `python3*.dll` and
`flutter_windows.dll` (a packed Flutter engine hangs the app at startup). UPX is downloaded
once (pinned version, SHA-256 checked) to `%LOCALAPPDATA%\pytemplate\tools`
(`~/.cache/pytemplate/tools` on Linux) unless `upx` is on PATH or `deploy.upx.path` names one;
macOS is not supported. The price: every start unpacks the files in memory (slower start, no
memory shared between processes), and some antivirus engines flag UPX-packed files.

**Compressed binaries**: mypyc builds ordinary C extensions (`.pyd`/`.so`, without debug
information in release builds); nothing compresses them by default, but UPX packs them to about
a third. What is always compressed: onefile executables (PyInstaller zlib, Nuitka zstd), the
`.pyz` (deflate) and the portable archive. **Nuitka with Flet** works: the method includes all
of `flet` (it loads its controls lazily, which Nuitka cannot follow) and bundles the Flet client
archive as `flet pack` does. It does not make the app smaller (61 MB standalone with UPX, about
the same as `flet pack`, because the Flutter client dominates) and the build takes about 25
minutes (Nuitka compiles all of Flet to C): use it for other reasons (startup, obfuscation).

## Presets

### script (default)
Console app: `src/myapp/core/bench.py` (compiled) and `src/myapp/app.py` (output with rich).

### raylib
PyPy by default (its JIT also speeds up the cffi calls), with a core that mypyc can compile.
raylib publishes no PyPy wheel for Apple Silicon (macOS arm64): there, switch the project to
CPython and mypyc once with `./deploy mode cpython --supports cpython,mypyc`.

- **Always `import raylib as rl`**, never pyray in loops: pyray wraps every call in
  Python (~700 ns vs ~100 ns). `compile.forbid_imports` prevents it in compiled code.
- **Create colors and structs once** (`gfx.color(...)`, cdata). Passing tuples such as `rl.RED`
  converts them again on every call: with tuples, PyPy loses all its advantage.
- **The official raylib stub lies**: 55 functions claim to return `bytes` but return a
  pointer, and `Color.r` claims to be `bytes` but is an `int`. Interpreted, nothing happens;
  compiled, mypyc checks the type and raises `TypeError`. The preset ships `typings/raylib`,
  the corrected stub (regenerate it after updating raylib: `./deploy stubs`).
- `./deploy bunnymark`: measures FPS with 30,000 bunnies
  (`./deploy run mypyc --frames 900 --bunnies 30000` for another backend).

### flet
**No need for a separate repo**: Flet is a preset (dependencies, skeleton and packaging)
of this same template. Flutter draws the UI and Flet's Python cannot be compiled,
so mypyc speeds up your core (`src/<pkg>/core/`) and the UI always runs interpreted.
Tested with Flet 1.0.1 and mypyc 2.3.1, with Flet in a compiled module:

- `async def` handlers are called **without the event** (TypeError);
- generator handlers never run, and raise no error;
- `@ft.component` fails on import;
- `@ft.control` loses the types of its events.

That is why compiled code has `forbid_imports = ["flet"]`. Pattern: the (interpreted) handler
converts Flet values to simple types and calls the core in another process
(`ProcessPoolExecutor`; compiled code does not release the GIL, so a thread would freeze the UI).
Hot reload: `./deploy dev`. `./deploy build` uses `flet pack`, which puts the Flutter client
inside the executable (with plain PyInstaller, 40 MB would be downloaded at startup).

## CPython JIT (experimental)

`./deploy mode --jit on` sets `PYTHON_JIT=1` in `run`, `test`, the `portable` launchers and the
pyz `.cmd` wrapper (a `.pyz` started directly with `python app.pyz` or through its shebang
cannot set it: export `PYTHON_JIT=1` yourself). The CPython builds that uv downloads for
Windows do not include the JIT, so it uses a system python.org 3.14 (`py install 3.14`, or the
path in `python.jit_interpreter`) in a separate environment (`.venv-jit`). It cannot be enabled
in a PyInstaller executable. Here it gave 1.6x on Collatz and 1.15x on the sieve; mypyc and
PyPy win by far.

## VS Code

Accept the recommended extensions (Python, Pylance or basedpyright, mypy, Ruff, Python
Debugger, Even Better TOML and Tasks). mypy is the judge of types (it is what mypyc uses);
Pylance and mypy configure themselves according to the active backend's profile.

**Does the same `tasks.json` work on Windows, Linux and macOS?** Yes. Every task in the
generated `.vscode/tasks.json` is a `"process"` task: by default (Linux and macOS) it runs
`/bin/sh <project>/deploy ...` (so the executable bit does not matter), and its `"windows"`
block runs `deploy.cmd` instead. VS Code uses the block of the OS where the task runs (also the
remote OS under WSL or SSH). Process tasks never go through your terminal's shell, so xonsh,
niubash or MSYS2 cannot break them, and the same committed file, buttons included, works on
every machine.

Tasks (Terminal > Run Task, all named `deploy: ...`; the list follows `pytemplate.toml` and is
regenerated when you change the mode):

| Task | Notes |
|---|---|
| `run`, `run <backend>` | Re-running restarts the app |
| `test` (default test task), `test <backend>`, `test all` | `test all` with more than one backend |
| `check`, `check all` | Errors in the Problems panel; `check all` with more than one backend |
| `build` (default build task: Ctrl+Shift+B) | Runs the checks first |
| `report` | mypyc supported: `report --open`, opens the HTML report |
| `compile` | mypyc supported; hidden, runs before the mypyc debug configuration |
| `lint --fix`, `fmt`, `doctor`, `setup` | |
| one per `[tasks]` entry | Every preset: `ci`; raylib: `bunnymark`, `stubs`; flet: `dev` |

**Problems panel.** The check, test and build tasks report ruff, mypy (with the severity of the
typing profile: `warn` shows warnings), the mypyc rules, mypyc compile errors (mapped back to
`src/`), pytest failures and, with `typing.editor = "basedpyright"`, basedpyright. The editor
extensions report the files you have open; the tasks report the whole project.

**Buttons.** VS Code has no task buttons of its own: the recommended **Tasks** extension
(`actboy168.tasks`) shows the tasks listed in `[vscode] buttons` in the status bar, in that
order:

```toml
[vscode]
buttons = ["run", "test", "check", "build"]   # ./deploy commands or [tasks] names
```

Preset defaults: script `run test check build`, raylib `run bunnymark test build`, flet
`dev run test build`. A button can also carry arguments (`"build --method pyz"`): it gets its
own task. Without the extension nothing breaks: the extra keys are ignored.

**Debugging (F5).**

| Configuration | When |
|---|---|
| `src/main.py (CPython, interpreted)` | Always, first in the list (uses the selected interpreter) |
| `src/main.py (CPython JIT)` | `python.jit` on: `.venv-jit` with `PYTHON_JIT=1` |
| `src/main.py (PyPy, experimental: the debugger is unreliable on PyPy)` | PyPy supported (`.venv-pypy`) |
| `Run mypyc stage (compiled modules cannot be stepped into)` | mypyc supported: runs the `deploy: compile` task first, then the compiled build; breakpoints stop in `src/main.py` and the interpreted modules only |
| `Tests (pytest)` | Always |

Under Remote-WSL on a `/mnt/...` checkout only the CPython and pytest configurations work (the
others point at the Windows-side environments).

The generated settings pin the debug terminal to cmd on Windows and `/bin/sh` elsewhere
(`terminal.integrated.automationProfile.*`), so F5 keeps working when your default terminal is
xonsh or niubash. Anything in `[vscode] settings` of `pytemplate.toml` overrides the generated
settings.

## Neovim (LazyVim)

Open the project in LazyVim and you get, without installing Python tools into Neovim:

- **LSP**: basedpyright by default (no Node.js needed), reading the generated
  `pyrightconfig.json`; set `vim.g.pytemplate_python_lsp = "pyright"` in your
  `lua/config/options.lua` for pyright (needs Node.js). `typing.editor` only picks the VS Code
  extension. The ruff server comes from the project's `.venv`, the same version `./deploy check`
  uses.
- **mypy** diagnostics with the project's `.mypy.ini` and the typing profile's severity (none
  with the `off` profile).
- **Debugging** (nvim-dap) with the configurations of `.vscode/launch.json` and the `.venv`
  debugpy.
- **Tests** (neotest) run pytest interpreted, on the active backend's environment; compiled
  mypyc runs and "all backends" go through the `deploy: test` task (`<leader>jt`, `<leader>jT`).
- **Tasks** (overseer): every `./deploy` command and every `[tasks]` entry, with backend,
  method and extra arguments as parameters; errors land in the diagnostics and the quickfix list.
- `:Deploy ARGS` with completion, `<leader>j` keymaps and `:checkhealth pytemplate`. Saving
  `pytemplate.toml` regenerates the configs.

Everything runs `uv run --quiet --script .pytemplate/deploy.py` as a list of arguments: Neovim's
`shell` option (xonsh, niubash...) is never used.

| Keys | Action | Keys | Action |
|---|---|---|---|
| `<leader>jj` | Pick any deploy task | `<leader>jm` | Switch the active backend |
| `<leader>jr` / `jR` | Run / run on a backend, with arguments | `<leader>jk` | `[tasks]` picker |
| `<leader>jt` / `jT` | Test / test all backends | `<leader>jd` | `dev` (flet hot reload) |
| `<leader>jc` / `jC` | Check / check all backends | `<leader>jp` | mypyc report |
| `<leader>jb` / `jB` | Build / build on a backend, with arguments | `<leader>js` / `jS` | Sync all / setup |
| `<leader>jl` | Lint --fix | `<leader>jD` | Doctor |
| `<leader>jf` | Format | `<leader>jw` / `jx` | Task list / stop deploy tasks |

The LazyVim keys stay as they are: `<leader>o` (overseer), `<leader>d` (debug), `<leader>t`
(tests). Options for `lua/config/options.lua`: `vim.g.pytemplate_prefix` (default
`"<leader>j"`), `vim.g.pytemplate_python_lsp` and `vim.g.pytemplate_render_on_save = false`.
The plugin's own [README](.pytemplate/nvim/README.md) has the details.

**First time** (Neovim 0.11.2 or newer), after `./deploy setup`:

```bash
./deploy nvim doctor      # Neovim, LazyVim, trust state, extras and tools, with hints
./deploy nvim bootstrap   # only without a Neovim config: installs the LazyVim starter
                          # (then start nvim once, so LazyVim installs itself)
./deploy nvim trust       # trust this project's .lazy.lua (once per clone or folder)
./deploy nvim extras      # optional: enable the LazyVim extras permanently (lazyvim.json)
./deploy nvim sync        # install the plugins the project needs (Lazy! sync)
```

`./deploy nvim` alone is `nvim doctor`. `bootstrap` never touches an existing config, and
`extras` backs up `lazyvim.json` before changing it (LazyVim creates that file the first time it
starts). Then start Neovim inside the project: `nvim` from the project folder or any subfolder.

**How it works.** lazy.nvim loads `.lazy.lua` from the folder where Neovim starts (or the
nearest parent that has one), once you trust it. That file never changes (it is identical in
every mode and preset), so trusting it once is enough: it enables the LazyVim extras the
project needs (`lang.python`, `lang.toml`, `dap.core`, `test.core`, `editor.overseer`) and
loads the plugin in `.pytemplate/nvim/`. Whatever depends on the mode comes from
`.pytemplate/editor.json`, a data file that every `./deploy` command regenerates.

**What gets downloaded, and where.** Nothing has to be added to your own LazyVim config: the
extras are imported by `.lazy.lua` only while Neovim runs inside the project. lazy.nvim still
installs their plugins (nvim-dap, neotest, overseer, nvim-lint...) in its usual global plugin
folder (`stdpath("data")/lazy`, e.g. `%LOCALAPPDATA%\nvim-data\lazy`) the first time, or when you
run `./deploy nvim sync`. The Python tools do not come from Mason: ruff, mypy and debugpy are
the project's `.venv` versions (pinned in `uv.lock`); basedpyright comes from `.venv` if you add
it, else `uv tool run` (cached by uv), else Mason. Mason and nvim-treesitter still install what
the extras declare (the TOML server taplo, the Python and TOML parsers). Outside pytemplate
projects those extras are not imported, so a `:Lazy clean` there would remove their plugins
until the next time: `./deploy nvim extras` (optional) adds them to your `lazyvim.json` for good.

**Per preset.** script: `run` shows its output as it starts. raylib: the `typings/` stubs reach
the LSP through `pyrightconfig.json`; `bunnymark` and `stubs` are tasks; the game's output only
opens on failure. flet: `<leader>jd` starts `dev` (hot reload) as a background task; debugging
(F5 configuration) runs without hot reload.

**Troubleshooting.**
- Nothing loads: Neovim must start inside the project. `nvim path/to/file.py` from another
  folder, or a later `:cd`, does not load `.lazy.lua`.
- Trust prompt: Neovim 0.12 has no "allow" button (0.11 still has (a)llow). Choose (v)iew, run
  `:trust`, then restart Neovim; or run `./deploy nvim trust` once, before starting Neovim.
  Moving the folder asks again.
- A warning about the order of the LazyVim extras: `./deploy nvim extras` fixes it for good.
- No `.venv` yet (`:checkhealth pytemplate` warns): run `./deploy setup` and restart Neovim.
- The language server is chosen when Neovim starts: restart it after changing
  `vim.g.pytemplate_python_lsp`.
- `:checkhealth pytemplate` and `./deploy nvim doctor` show what is missing.

## Troubleshooting

- **mypyc "Unable to find a compatible Visual Studio installation"**: `./deploy` already adds
  the Visual Studio Installer to PATH (the `vcvarsall.bat` of VS 2026 needs it).
  If it fails, `./deploy doctor` tells you what is missing.
- **Paths longer than 260 characters** (Windows): shorten the project path or enable
  `LongPathsEnabled`. Flet and PyPy have files with long internal paths.
- **`bash` opens WSL** (Windows): the `bash` on PATH may be WSL's launcher. Use Git Bash or
  MSYS2 (their own `bash.exe`), xonsh, PowerShell or cmd. If you use `./deploy` from WSL, the
  runner creates separate environments (`.venv-wsl`) so it does not break the Windows ones.
- **`deploy: uv not found`**: the launchers also look in uv's usual install folders and, on
  Windows, in the PATH saved in the registry (a terminal opened before installing uv). Otherwise
  install uv with one of the printed commands and open a new terminal.
- **Flet downloads the client or installs packages on its own**: `flet` and `flet-desktop`
  must have the same version (`[preset.flet] version`).
- **`flet build` on Windows**: it needs Developer Mode (Settings > System > For developers) and
  the Visual Studio C++ tools; `./deploy build --method flet` says so when Developer Mode is off.
- **`git clean -fdx` is safe**: it only deletes untracked and ignored files (`.venv*`, `.build/`,
  `dist/`, caches, `.claude/`); every generated file, `uv.lock` and `.pytemplate/state.json` are
  committed. Afterwards any command works (uv recreates `.venv` on its own; `./deploy setup`
  also recreates the PyPy and JIT environments). The git hook lives in `.git/`, so it stays.
  `git clean -fdx -e .claude` keeps Claude Code's local settings.

## Layout

```
deploy, deploy.cmd, deploy.ps1   launchers (all the logic is in .pytemplate/)
pytemplate.toml                  configuration and mode
pyproject.toml, uv.lock          dependencies (a single lock for CPython and PyPy)
src/main.py                      launcher (never compiled)
src/myapp/core/                  core compiled by mypyc
src/myapp/*.py                   interpreted boundary
tests/                           pytest (conftest verifies the mypyc binaries)
.lazy.lua                        LazyVim project spec (generated, never changes)
.github/workflows/               ci.yml (generated); template-*.yml test the template itself
                                 (e2e, launchers, nvim) and are not copied by ./deploy new
CLAUDE.md                        technical notes for coding agents
.pytemplate/runner/              the runner (Python stdlib)
.pytemplate/templates/           configuration variants (typing profiles, VS Code, Neovim, CI...)
.pytemplate/presets/             script, raylib and flet skeletons
.pytemplate/nvim/                the Neovim plugin that .lazy.lua loads
.pytemplate/tests/               tests of the runner (./deploy selftest)
.build/, dist/                   outputs (ignored by git)
```
