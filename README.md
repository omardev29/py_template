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
  `./deploy doctor` flags them: update with `uv self update` (or `brew upgrade uv`,
  `pipx upgrade uv`, `winget upgrade astral-sh.uv`, `scoop update uv`). When uv is missing, the
  launchers print how to install it; `deploy` and `deploy.ps1` also offer to run the official
  installer in an interactive terminal (never when `CI` is set).
- No Python installation is needed: uv downloads the interpreters (`python.cpython`,
  `python.pypy`). The runner itself runs on the project's CPython, which must be 3.11 or newer.
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
Python: `python.cpython` (3.14 by default), or 3.11 when PyPy is supported; a pyz with native
dependencies needs the exact Python minor it was built for ([pyz](#pyz)).

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

Unknown arguments are an error (exit 2), never ignored. Only these commands pass extra arguments on:
`run` to the app, `test` to pytest, `lock` to `uv lock`, `selftest` to pytest,
`build --method exe|nuitka|flet` to the packager (PyInstaller or `flet pack`, Nuitka, `flet build`;
`pyz`, `portable` and `wheel` take none), and a task with a `cmd` to its program. `-h` or `--help`
after a command shows `./deploy help COMMAND`, except after `run`, `test`, `lock` and `selftest`,
where it goes to the app, pytest, uv or the suite; `./deploy -h COMMAND` works too.

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
- 3: a missing requirement: uv, a uv older than 0.10.12, a program, a compiler, an interpreter,
  Neovim or git for `selftest --nvim --require`, or the runner started on a Python older than
  3.11.
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
`UV_INSTALL_DIR` (one of the folders searched for uv), `CI` (no install prompt; `selftest --e2e`
skips GUI runs on Windows and macOS CI), `NO_COLOR` and `TERM`, the compiler variables of mypyc
(`CC`, `CFLAGS`, `CPPFLAGS`, `LDSHARED`, `LDFLAGS`, `ARCHFLAGS`, `CL`, `_CL_`),
`MACOSX_DEPLOYMENT_TARGET` (the oldest macOS the pyz and portable wheels support, 13.0 by default),
`LOCALAPPDATA` and `XDG_CACHE_HOME` (the pyz and UPX caches). At runtime, the app's
`resources.assets_dir()` (raylib and flet presets) reads `PYTEMPLATE_ASSETS`, which the portable and
pyz launchers set.

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
deps = ["check"]                                   # run first: other tasks or commands
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

`./deploy rename NEW_NAME [--force]` renames the app; preview it with
`./deploy --dry-run rename NEW_NAME`, which lists the folder move and every file with sample lines.
An `app.name` edited by hand is finished by `./deploy apply` (or by `./deploy rename` with that
name). What changes:

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
the old name; `dist/` and `.build/` keep the old name until the next build (`./deploy clean`).
It refuses uncommitted changes without `--force` (a project fresh from `./deploy new` has no
commit yet: commit first), and the names `new` refuses. A write that fails puts every file back.
When the old name is a common word (`app`, `game`, `core`), matching words in comments and
strings change too: review `git diff`.

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
| `compile.separate` | `false` | one shared library per compiled module instead of one for all of them |
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
| `deploy.portable.prune` | `true` | leave out the parts of the interpreter that apps do not use |
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

## Backends

`run`, `test`, `check` and `build` take the backend as their first argument (default:
`backend.active`). `./deploy mode BACKEND` changes the active one; `./deploy mode --supports +pypy`,
`-pypy` or a full list (`cpython,mypyc`) changes the supported set (`mode pypy` alone also adds
PyPy). The cpython and mypyc backends share `.venv`; pypy has `.venv-pypy`. The tools (mypy, ruff,
mypyc, PyInstaller) always run in `.venv`.

### Typing profiles

| Profile | When | What it requires |
|---|---|---|
| `off` | cpython and pypy by default (`typing.relaxed = "off"`) | nothing: ruff reports only real errors (syntax, undefined names); mypy does not run |
| `warn` | `./deploy mode --typing warn` | mypy and ruff findings as warnings, never blocking |
| `strict` | `./deploy mode --typing strict` | mypy `--strict`, `Any` allowed; blocking |
| `mypyc` | always with the mypyc backend (`./deploy mode --typing mypyc`: on every backend) | `--strict` across `src/` and `tests/`, and `Any` forbidden in the compiled modules |

`check` uses the profile of the backend it checks, and `check all` runs each profile once. The
generated `.mypy.ini`, `.ruff.toml`, `pyrightconfig.json` and VS Code settings follow the active
backend's profile. With the `mypyc` backend the profile cannot be `warn` or `off` (mypyc stops at
any mypy error).

With mypyc, `Any` is slow: mypyc generates generic operations wherever there is `Any` (a
`list[Any]` can be slower than uncompiled CPython). That is why the `mypyc` profile enables
`disallow_any_explicit/expr/decorated/unimported` in the modules of `compile.modules`, while the
rest of `src/` (the boundary with poorly typed libraries, and the modules of `compile.exclude`)
stays strict but may use `Any`. Pylance in strict mode does not flag an `Any` written on
purpose; to see it in the editor too: `./deploy mode --editor basedpyright`.

### mypyc

mypyc compiles the modules of `compile.modules` (default `<pkg>.core`) to C extensions, never in
`src/`: `run`, `test`, `compile` and `report` use a copy in `.build/mypyc-dev/stage` (asserts
kept) and `build` one in `.build/mypyc-release/stage` (asserts stripped when `deploy.optimize` is
1 or 2). Each `.pyd`/`.so` sits next to its `.py`, and only what changed is copied and compiled
again.

- **Layout**: `src/<pkg>/core/` is compiled; the boundary (`app.py`, `ui/`, `gfx.py`,
  `resources.py`) is not. `src/main.py` starts the app and is never compiled (a compiled module
  cannot be `__main__`). `compile.exclude` keeps modules or subpackages of `compile.modules`
  interpreted; an entry that names nothing is an error.
- **Constants with `Final`**: a global without `Final` is looked up in a dictionary on every
  access.
- **Native classes**: typed attributes, and only these class decorators: `@dataclass`,
  `@attr.s` (`@attr.attrs`), `@final`, `@trait` and `@mypyc_attr`. Any other one, attrs'
  `@define`, `@frozen` and `@mutable` included, turns the class into a slower regular Python
  class (mark it `@mypyc_attr(native_class=False)` when that is intended).
- **Concrete types**: `list[bool]` compiles to direct accesses, `bytearray` takes the generic
  path (sieve: 4.2x vs 1.9x).
- `./deploy report --open` marks every generic operation in red ("make it Final", "Generic `*`").
  With `compile.annotate = true`, every mypyc build (`run`, `test`, `compile`, `build`) also writes
  that report to `.build/reports/mypyc-annotate.html` (the same report as `./deploy report`, without
  mypy's `Any` reports).
- `./deploy check` adds rules for the compiled modules that mypy does not check: imports listed in
  `compile.forbid_imports`, class decorators that make a class non-native, nested classes and
  classes defined inside functions, t-strings, `if __name__ == "__main__"`, `librt` while PyPy is
  supported, and a module-level `__file__` when `compile.modules` is a single top-level module
  (there it is a relative path). Compiled code that imports `librt` needs it as an app dependency:
  `./deploy add librt --cpython-only` (mypy installs it only in the dev group).
- **Executables**: PyInstaller and Nuitka cannot see the imports inside a compiled module:
  `./deploy` passes them on (`from X import submodule` included). If one is still missing at
  runtime, list it in `[deploy.exe] hidden_imports` (PyInstaller) or add `--include-module=NAME`
  to `[deploy.nuitka] extra_args`.
- **Debugging and tests**: compiled code has no pdb, cProfile or monkeypatch: debug it
  interpreted (F5 in VS Code, the dap keys in Neovim). The "Run mypyc stage" debug configuration
  runs the compiled build, but compiled modules cannot be stepped into. `./deploy test mypyc`
  runs pytest on the compiled stage and fails when a compiled module was loaded from its `.py`;
  mark the tests that mock compiled code with `@pytest.mark.interpreted_only` (skipped there).
- **Native libraries you vendor** (`.so`/`.pyd` files in `src/`, added with `git add -f` past
  `.gitignore`): the portable and wheel builds carry them; PyInstaller and Nuitka bundle only what
  they detect (a library loaded with ctypes needs `--add-binary` in `[deploy.exe] extra_args`).

#### Fast integers: `i64`

A Python `int` has no size limit, so mypyc stores it as a *tagged* integer: small values live in a
machine word, and every operation checks the tag and for overflow, with a slow path that creates a
big Python int. That is already much faster than CPython, but the C compiler cannot see through the
slow path, so a loop stays a loop. `mypy_extensions.i64` (also `i32`, `i16`, `u8`) promises that the
value always fits in 64 bits: mypyc then emits plain C `int64_t` arithmetic, and gcc/clang can
simplify, vectorise or even delete the loop. `mypy-extensions` is already a runtime dependency of
every project (`pyproject.toml`): `from mypy_extensions import i64`. Interpreted code (the cpython
and pypy backends) sees `i64` as a plain `int`.

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

#### C compiler options and rebuilds

- `compile.opt_level` is mypyc's C optimisation level, `"3"` by default. `"0"` is unoptimised C
  with the C asserts on, about 1.8x slower than the interpreter: for debugging only. MSVC has no
  levels: `"1"` to `"3"` are all `/O2`, `"0"` is `/Od`.
- `compile.no_semantic_interposition = true` (the default) adds `-fno-semantic-interposition` on
  Linux with gcc or clang, as CPython itself is built. Without it, every call between two
  compiled functions goes through the PLT and gcc never inlines it. Measured with gcc 13 (a
  function calling another one in an `i64` loop, 100 million iterations): 177 ms without the flag,
  0 ms with it (gcc inlined the call and folded the loop into `return max(n, 0)`). Nothing is
  added on macOS (not verified there) or with MSVC; `false` keeps mypyc's own flags.
- gcc and clang always get `-fno-strict-overflow`, so the wrap-around of `i64`/`i32` arithmetic
  stays defined C. A `CFLAGS` environment variable replaces Python's own compile flags (setuptools
  does that): your flags stay and mypyc's `-O<opt_level>` after them wins, but Python's `-DNDEBUG`
  is lost, which turns the C asserts of mypyc's runtime back on (slower).
- A change of `compile.opt_level`, `no_semantic_interposition`, `multi_file`, `separate`,
  `strict_dunder_typing` or `deploy.optimize`, or of a compiler variable (`CC`, `CFLAGS`,
  `CPPFLAGS`, `LDSHARED`, `LDFLAGS`, `ARCHFLAGS`, `CL`, `_CL_`), forces a full rebuild of the
  stage, and so does a build that failed or was interrupted. Extensions built for another
  `python.cpython` are removed from the stage.
- `deploy.optimize` is Python's `-O` level (0 keeps the asserts, 1 strips them, 2 also strips the
  docstrings): higher strips more, it is not faster. With the mypyc backend, 1 and 2 also strip
  the asserts of the compiled modules in every build method.
- The C compiler's install hint appears only when the C step failed (a type error that mypyc
  rejects is shown as such).

### PyPy

`./deploy mode --supports +pypy` enables PyPy (the raylib preset has it): it checks that the code
is valid Python 3.11 (ruff's syntax rules for 3.11, and the mypy errors that appear only on 3.11,
whatever the typing profile), lowers `requires-python` to `>=3.11`, re-locks `uv.lock` and
creates `.venv-pypy`. `./deploy apply` runs the same check when `backend.supported` gains PyPy.

- While PyPy is supported, the code must be Python 3.11 in syntax and API: no `class C[T]` generics
  (PEP 695), and `from typing_extensions import override`, not `from typing import override`.
- PyPy is pinned exactly: `python.pypy = "pypy@3.11.15"` is a PyPy 7.3. PyPy 8.0.0 (released on
  2026-09-19, with Python 3.11.16 and a beta of Python 3.12) changed the C-extension ABI to pp80:
  wheels built for PyPy 7.3 do not load on it, and as of September 2026 raylib, numpy and cffi
  publish none for it. A loose request (`pypy@3.11`) would pick PyPy 8. Its release notes say that,
  barring security issues, it is the last release to support Python 3.11
  (<https://pypy.org/posts/2026/09/pypy-v800-release.html>). The plan: move to `pypy@3.12.x` once
  raylib and the project's other native dependencies publish wheels for PyPy 8, then run
  `./deploy apply`.
- PyPy has no single-file executable (PyInstaller, Nuitka and `flet build` do not support it):
  `portable`, a folder with PyPy inside (optionally archived), is the only way to ship a PyPy
  build to someone who has no PyPy.
- uv resolves the pin from the downloads it knows: with uv 0.12, `pypy@3.11.15` installs PyPy
  7.3.23 (uv takes the newest PyPy build of that Python version). A later uv may stop offering
  it (uv 0.12 no longer offers 3.11.11 and 3.11.13): see [Troubleshooting](#troubleshooting).
- The tools (mypy, ruff, PyInstaller, debugpy) run on CPython: `.venv-pypy` holds the app's
  dependencies and pytest.
- CPython C-API libraries (numpy, pillow, pydantic-core) are slow on PyPy: add them with
  `./deploy add numpy --cpython-only`. cffi libraries (raylib) are fast: the JIT also compiles the
  cffi calls.
- The JIT needs about 1 s to warm up: very short scripts run slower than on CPython.
- While PyPy is supported, mypy checks the code as Python 3.11 (`--python-version`). mypy has
  dropped a target version 6 to 9 months after that Python's end of life (3.8, 3.9); 3.11
  reaches its end of life in October 2027, so expect it around mid-2028. `uv.lock` pins mypy, so
  nothing changes until mypy is upgraded. After an upgrade that refuses 3.11, `./deploy check`
  and the PyPy check of `mode`/`apply` fail with mypy's message. Then move to PyPy 3.12 (above),
  keep mypy at the last version that accepts 3.11, or drop PyPy for that project
  (`./deploy mode --supports -pypy`).
- raylib publishes PyPy wheels only for Linux x86_64, Windows x86_64 and macOS x86_64: on Apple
  Silicon and Linux ARM64 a raylib project needs `./deploy mode cpython --supports cpython,mypyc`.

## Distribution

`./deploy build [BACKEND] [--method M]` runs `./deploy check` for the backend (`--no-check` skips
it), then packages the app into `dist/`:

| Method | cpython | mypyc | pypy | Result |
|---|---|---|---|---|
| `exe` | yes | yes | no | a PyInstaller executable (flet preset: `flet pack`, with the Flutter client inside) |
| `portable` | yes | yes | yes | a folder with the interpreter and a launcher: the standalone build for PyPy |
| `pyz` | yes | yes | yes | one zip file run by an installed CPython or PyPy: every platform when pure |
| `wheel` | yes | yes | yes | an installable package (`uv tool install`); mypyc: a platform wheel |
| `nuitka` | yes | yes | no | a Nuitka executable (it compiles the dependencies to C too; slow builds) |
| `flet` | yes | yes | no | `flet build` (flet preset only): desktop apps, Android, iOS and web |

PyInstaller, Nuitka and `flet build` do not support PyPy. The default method is `exe` for
cpython and mypyc and `portable` for pypy (`[deploy] default`; a backend left out of that table
keeps its default). Each build replaces the previous output of the same backend and method:

| Method | Output | Start it with |
|---|---|---|
| `exe` | `dist/<name>-<backend>-exe/` | `<name>` (`<name>.exe` on Windows; onedir: inside the folder `<name>/`; macOS with flet: an `.app`) |
| `portable` | `dist/<name>-<backend>-portable-<key>/` (`<key>` such as `cp314-linux-x86_64`; no key with `runtime = "system"`), and a `.zip` (Windows) or `.tar.gz` of it | `<name>.cmd` (Windows) or `<name>.sh` |
| `pyz` | `dist/<name>-<backend>-pyz/<name>.pyz` and `<name>.cmd` | `python <name>.pyz`; on Windows also `<name>.cmd` |
| `wheel` | `dist/<name>-<backend>-wheel/<file>.whl` | `uv tool install <file>.whl`, then `<name>` |
| `nuitka` | `dist/<name>-<backend>-nuitka/` | `<name>` (`<name>.exe` on Windows; `<name>.bin` in a standalone build on Linux or macOS when the app name has no `-`) |
| `flet` | `dist/<name>-<backend>-flet-<target>/` | the platform's app |

Which one to ship: `exe`, `nuitka`, `flet` and a bundled `portable` folder need nothing installed on
the user's machine, but each build serves the OS and CPU it was built on (build on each OS). A `pyz`
needs an installed Python and can serve every platform with one file: a pure one runs wherever a
compatible Python does; one with native dependencies runs only where it has binaries, which for a
local build is the platform that built it until target keys are added or the per-OS builds of the
generated CI are merged ([pyz](#pyz)). A wheel suits users who install Python tools
(`uv tool install`). PyPy has no single-file executable: a PyPy build ships as `portable` (PyPy
inside), or as a pyz or a wheel for users who have PyPy.

Arguments: `--onefile` and `--onedir` override `deploy.exe.mode` and `deploy.nuitka.mode` (exe
and nuitka only); `--target KEY` (repeatable) adds platforms to a pyz; other flags go to the
packager of exe (PyInstaller or `flet pack`), nuitka and flet (`flet build`), while pyz,
portable and wheel refuse them (exit 2). Options are not abbreviated (`--meth` is not
`--method`), and a bare word is an error with a hint ("unknown backend 'mypy': did you mean
mypyc?", "did you mean --method pyz?"). `./deploy --dry-run build ...` checks the arguments and
the configuration as a real build does and prints the output name (for nuitka also its
options), without building.

### exe

PyInstaller, in UTF-8 mode (`-X utf8`, like `./deploy run`), with the bytecode of
`deploy.optimize`, no console for GUI apps (`deploy.exe.console`), the hidden imports of the
compiled modules (plus `deploy.exe.hidden_imports`), `src/assets/`, the icon and
`deploy.exe.extra_args`. `onefile` is one compressed file that unpacks itself to a temporary
folder on every start; `onedir` starts faster.

The flet preset uses `flet pack` instead: PyInstaller plus Flet's Flutter client inside the
executable (with plain PyInstaller the app would download about 40 MB at first start). Its
onedir build is a flat folder on Windows (`<name>.exe` next to its files) and keeps
PyInstaller's `_internal/` on Linux; on macOS `flet pack` always builds an `.app`.

### portable

A folder that runs the app with its own interpreter:

- `runtime/`: a copy of the backend's interpreter (uv's CPython or PyPy). With `prune = true`
  (default) it leaves out what an app does not need: headers and import libraries (`include/`,
  `libs/`), `share/`, `Tools/`, the base's `Scripts/` (Windows) and every `bin/` entry but the
  interpreter (so console scripts installed into uv's Python are not shipped), on Linux the
  shared `libpython3.X.so` when the interpreter does not use it, the standard library's tests,
  `idlelib`, `turtledemo`, `ensurepip` and `site-packages`, PyPy's debug symbols, and Tk unless
  `src/` or a dependency imports `tkinter` (`prune = false` keeps it for an app that loads it
  another way).
- `lib/`: the dependencies at the versions of `uv.lock`; they win over packages installed in a
  Python.
- `app/`: the app (with mypyc, the compiled modules next to their `.py`), and `boot.py`.
- `<name>.cmd` (a Windows build) or `<name>.sh` (Linux, macOS): run it from any folder (the `.sh`
  also through a symlink); the arguments reach the app and its exit code comes back. They run
  the interpreter with `-s` and the `-O` level of `deploy.optimize`, and set `PYTHONUTF8=1` and
  the `[deploy.portable] env` variables (literal values; the `.cmd` takes only ASCII values
  without `"` or line breaks). For a GUI app the `.cmd` starts `pythonw` and returns at once. If
  an unzip tool dropped the executable bit, run `sh <name>.sh`.

A bundled folder runs only on the OS and CPU it was built on (the key in its name): build it on
each OS, or use a pyz. The build precompiles the standard library, `lib/` and `app/`, so a
read-only install starts fast, and it starts the copied interpreter before it reports success
(for mypyc also the compiled modules). With `archive = true` (default) a `.zip` (Windows) or
`.tar.gz` (Linux, macOS) of the folder is written next to it.

`runtime = "system"` writes a folder without an interpreter (`dist/<name>-<backend>-portable/`, with
both launchers). The launcher runs each candidate (`py -X.Y`, `python3`, `python` on Windows;
`pythonX.Y`, `python3`, `python` elsewhere; `pypy3`, `pypy` for PyPy) and uses the first that is at
least the project's minimum Python. None: it prints `<name>: needs Python X.Y or newer in PATH` and
exits with 9009 (`.cmd`) or 127 (`.sh`). With native dependencies such a folder only works on the OS
it was built on (the build warns). On Windows with the Python install manager and no Python at all,
the first start silently downloads one (the install manager's default); set
`PYTHON_MANAGER_AUTOMATIC_INSTALL=false` or run `py install 3.X` to control that.

### pyz

exe, nuitka and a bundled portable folder carry an interpreter, so each build serves one OS, one
CPU and, on Linux, one C library floor. A pyz carries none: one file can serve every platform
where a compatible Python is installed. It holds the app as `.py`, the dependencies, and, per
platform key (such as `cp314-linux-x86_64`), the binaries: the mypyc extensions of the machine
that built it, and the native dependencies. Python cannot import `.pyd`/`.so` files from a zip,
so the first start extracts it to a cache: `%LOCALAPPDATA%\<name>\pyz` (Windows),
`~/Library/Caches/<name>/pyz` (macOS) or `$XDG_CACHE_HOME/<name>/pyz` (`~/.cache/<name>/pyz`). It
keeps the three most recently started builds and any started in the last day; deleting the
folder is always safe. Without a usable cache (a read-only home) it extracts into a private
temporary folder for that run.

How far a pyz reaches depends on its dependencies; the build prints which case it is:

- **Pure** (`pure: ...`): every locked dependency is pure Python and none is limited to some
  platforms or Python versions by a marker. Such a pyz has no platform-specific file but the mypyc
  extensions, and runs on any CPython or PyPy at or above the project's minimum Python
  (`python.cpython`, 3.14 by default; 3.11 when PyPy is supported); an older one gets
  `<name>: needs Python X.Y or newer`. It is tested on Windows, macOS and Linux with glibc; Linux
  with musl (Alpine), Android through Termux (`python <name>.pyz`, or `./<name>.pyz` through
  termux-exec), the BSDs and other CPUs are expected to work but untested (the bootstrap needs only
  the standard library and a writable cache). On the maintainer's machine, a 1.7 MB pure pyz of a
  project with PyPy supported used the compiled core on CPython 3.14 and ran the `.py` on PyPy and
  on CPython 3.13.
- **Not pure** (`runs on: <keys>`): native dependencies (raylib, flet, any platform wheel), or a pin
  that a marker leaves out on some target. It carries the dependencies of each target key and runs
  only there: the exact CPython minor of the lock (`cp314` wheels load only in 3.14, so a Python
  3.13 or 3.15 gets `this .pyz has no build for this interpreter and platform`), on Windows, Linux
  or macOS, x86_64 or aarch64 (Linux: glibc 2.28 on x86_64 or 2.35 on aarch64, or newer; macOS 13 or
  newer). There are no musl or Android targets.
- `[deploy.pyz] targets` is `["host"]` by default, so a local build that is not pure runs only on
  the platform that built it. For one file that serves several platforms, add target keys
  (`targets = ["host", "cp314-windows-x86_64"]`, or `--target KEY`: the locked CPython minor on
  any of those systems and CPUs; PyPy keys come only from a PyPy build on that platform), or use
  the generated CI, which builds a pyz on Windows, Linux and macOS and merges them on every run.
- mypyc compiles only for the machine it runs on: its extensions are used for the key that built
  them, and everywhere else the same code runs as `.py` (slower, same result).
  `./deploy pyz-merge A.pyz B.pyz... --out all.pyz` joins pyz files built on several machines from
  one commit (the same app, minimum Python, locked dependencies and code) into one file with every
  platform's binaries, and also writes `all.cmd` next to it.
- On Linux the dependencies of pyz and portable builds target glibc 2.28 (x86_64) or 2.35
  (aarch64) when the build machine can use those wheels (else its own, with a warning); on macOS,
  macOS 13 or newer (`MACOSX_DEPLOYMENT_TARGET` changes it).
- On Windows, `<name>.cmd` looks for a Python or PyPy that meets the minimum (`py -X.Y`,
  `python3`, `python`, `pypy3`) and runs the pyz with it (`pythonw` for a GUI app).

### wheel

A package for `uv tool install` or pip that installs the command `<name>` (a GUI script when
`app.gui = true`: no console window on Windows), from `deploy.wheel.entry` (default
`<pkg>.app:main`; flet: `<pkg>.ui.app:run`). cpython and pypy build a `py3-none-any` wheel;
mypyc builds a platform wheel with the compiled modules and their `.py`. It holds every file of
the package and `src/assets/` (as `<pkg>/assets`). It is built offline in the locked `.venv`
(`uv build --no-build-isolation`). Its dependencies are the version ranges of
`[project] dependencies`, not the exact versions of `uv.lock`.

### nuitka

Nuitka `4.2.2` (run with `uv run --with nuitka==4.2.2`, outside `uv.lock`) compiles the app and
the dependencies it follows to C. It needs a C compiler on every backend and, on Linux,
`patchelf`; a build takes minutes (4 to 13 measured for the script preset, about 25 for the flet
preset). This Nuitka supports CPython up to 3.14: a newer `python.cpython` stops with exit 3
unless Nuitka's own `--experimental=python3.X` is passed.

- `[deploy.nuitka] mode`: `standalone` (a folder) or `onefile` (one file, zstd-compressed with
  CPython 3.14's `compression.zstd`).
- `lto`: `auto` (default), `yes` or `no`, always passed as `--lto=...`. `auto` means yes with
  uv's CPython on Linux, Windows and macOS until more than 250 modules are compiled (the
  standard library does not count): the script and raylib presets stay far below (a raylib app
  compiles about 18), the flet preset compiles about 794 and gets no LTO. Measured with gcc 13
  on a small script: `--lto=yes` built in 9-10 s instead of 22 s, 7.21 MB instead of 7.78 MB,
  and ran 0-5% faster.
- `pgo = true` adds `--pgo-c`: Nuitka runs the app once while building to profile it
  (`pgo_args` are the app's arguments for that run). Only for console apps (`app.gui = false`)
  without assets, not with the mypyc backend and not on macOS; each refusal exits 2 with the
  reason. Nuitka calls it experimental; measured: 10-15% faster on pure-Python loops only.
- `extra_args` are appended, after the `--lto` above: a later `--lto` wins. Asserts and
  docstrings follow `deploy.optimize`.
- Imports Nuitka cannot find (a platform-guarded `import winreg`) are skipped. Flet works: all of
  `flet` is included (it loads its controls lazily, which Nuitka cannot follow) and the Flet
  client archive is bundled, as `flet pack` does.

### flet build

`flet build` (flet preset only) builds desktop apps (`host`, `windows`, `macos`, `linux`),
Android (`apk`, `aab`), iOS (`ipa`) or `web` apps: `[deploy.flet] target`. On Windows it needs
Developer Mode (Settings > System > For developers) and the Visual Studio C++ tools. The first
build downloads the Flutter SDK that Flet pins (3.44.8 for Flet 1.0.1, about 3 GB in `~/flutter`)
and a Python build (`~/.flet`). Measured on Windows 11 in September 2026: the first build took
about 7 minutes, the next ones about 3.

- It embeds exactly the `python.cpython` minor, so the mypyc extensions work in desktop apps.
  Mobile and web apps cannot load extensions: with the mypyc backend they get the `.py`.
- `flet build` ignores `uv.lock`: the runner pins the locked versions in its build project. It
  reads `[tool.flet]` of `pyproject.toml`, where `org`, `company` and `copyright` are
  placeholders that end up in the app; `[tool.flet.app] path` is always `src`.
- `cleanup` (default `true`): `--cleanup-app --cleanup-packages`; `exclude`: app files left out;
  `extra_args` go to `flet build`.

### Binary size

Measured on Windows 11 in September 2026 with the flet preset (CPython backend, Flet 1.0.1):

| Build | Folder | Zip | First start |
|---|---|---|---|
| `exe` (`flet pack`, onedir) with Pillow | 85.5 MB | - | unpacks the Flet client: +97 MB in `~/.flet/client` |
| `exe` with `exclude_modules = ["PIL"]` (the preset default) | 72 MB | 58 MB | +97 MB |
| ... plus `[deploy.upx] enabled = true` | 63 MB | 57 MB | +97 MB |
| `flet` (`flet build`) with `cleanup = false`, no UPX | 97 MB | - | nothing to unpack |
| `flet` with the default `cleanup = true` + UPX | 78 MB | **38 MB** | nothing to unpack |

Where it goes: every Flet desktop app carries the Flutter engine (`flutter_windows.dll`, 20 MB) and
Flet's compiled Dart UI (`app.so`, 15-19 MB); Python adds its runtime (`python314.dll`, 6 MB, plus
the standard library) and the dependencies. `flet pack` (and PyInstaller with `flet-desktop`, which
is the same thing) ships Flet's prebuilt full client, zipped (40 MB), with libmpv for audio and
video (28 MB) and Rive, and unpacks it on the first start. `flet build` compiles a client with only
the Flutter packages the app uses: the smallest download and the smallest install. The smallest Flet
app: `./deploy build --method flet` (or `[deploy] default = { cpython = "flet", ... }`).

The script preset with mypyc, measured on Windows 11 in September 2026: the onefile `exe` is
13.2 MB (12.3 MB with UPX); the `portable` folder 62 MB (49 MB with UPX), its zip 23 MB. A
bundled portable build of the script preset on Linux x86_64 (CPython 3.14.7, September 2026,
with the `bin/` and `libpython` pruning above): an 80 MB folder and a 25 MB `.tar.gz`.

Size settings (each method ignores what does not apply to it):

| Setting | Methods | Effect |
|---|---|---|
| `[deploy] exclude_modules = [...]` | exe, nuitka | Modules never bundled even if something imports them (PyInstaller also follows imports inside functions). flet preset: `["PIL"]` (-13 MB; Flet only uses Pillow for `RawImage`). `"ssl"` saves 2 MB more if the app never uses HTTPS (OpenSSL's `libcrypto` stays while `hashlib` needs it). |
| `[deploy.upx]` `enabled`, `level`, `lzma`, `exclude` | exe (Windows), nuitka, portable, flet | UPX packs executables and libraries (below) |
| `[deploy.exe] mode = "onefile"` | exe | One compressed file (zlib), unpacked to a temporary folder on every start |
| `[deploy.exe] strip = true` | exe (Linux, macOS) | Strips the symbol tables of the bundled binaries |
| `[deploy.nuitka] mode = "onefile"` | nuitka | One zstd-compressed file |
| `[deploy.flet] cleanup` (default `true`), `exclude = [...]` | flet | `--cleanup-app --cleanup-packages` (no tests or docs in the bundle); app files left out |
| `[deploy.portable] prune`, `archive` | portable | Unused parts of the interpreter removed; a zip or tar.gz next to the folder |
| `[deploy] optimize = 2` | exe, nuitka, portable | `-OO` bytecode (no asserts, no docstrings); with mypyc, the compiled modules lose their asserts in every method |

**UPX** (`[deploy.upx]`, off by default): `level` is `1` to `9`, `best` (default), `brute` or
`ultra-brute` (much slower builds for a few % more); `lzma = true` packs smaller and unpacks
slower; `exclude` adds file-name globs. The exe method uses PyInstaller's own UPX step, on
Windows only (PyInstaller turns UPX off on other systems, where packed `.so` files crash: the
build warns that the exe is not packed); every binary is packed before bundling, also in
onefile mode, and PyInstaller always uses LZMA and skips Control Flow Guard DLLs. Nuitka uses its
upx plugin (always `--best --lzma`), and the portable and flet builds are packed when they are
done (the portable smoke test then loads the packed modules). Never packed: files over 600 MiB
(UPX refuses anything over 768 MiB), binaries UPX rejects (Control Flow Guard), the C runtime,
`python3*.dll`, `libpython3*` and `flutter_windows.dll` (a packed Flutter engine hangs the app at
startup). UPX 5.2.1 is downloaded once (SHA-256 checked) to `%LOCALAPPDATA%\pytemplate\tools`
(`$XDG_CACHE_HOME/pytemplate/tools` or `~/.cache/pytemplate/tools` elsewhere), unless `upx` is on
PATH or `deploy.upx.path` names one (absolute, `~`, or relative to the project root; the file is
named `upx` or `upx.exe`). macOS is not supported. The price: every start unpacks the files in
memory (a slower start, no memory shared between processes), and some antivirus engines flag
UPX-packed files.

**Compressed binaries**: mypyc builds ordinary C extensions (`.pyd`/`.so`, without debug
information in release builds); nothing compresses them by default, but UPX packs them to about a
third. Always compressed: onefile executables (PyInstaller zlib, Nuitka zstd), the `.pyz`
(deflate) and the portable archive. Nuitka with Flet does not make the app smaller (61 MB
standalone with UPX, about the same as `flet pack`, because the Flutter client dominates) and the
build takes about 25 minutes (Nuitka compiles all of Flet to C).

## Presets

The preset is chosen when a project is created (`./deploy new DIR --preset P`). Every preset has
a `ci` task (`./deploy ci`: `check all`, then `test all`).

### script (default)

A console program: `src/<pkg>/core/bench.py` (compiled: a sieve and a Collatz benchmark) and
`src/<pkg>/app.py` (the output, with rich).

### raylib

A 2D game with raylib's cffi binding (the `raylib` package). PyPy is the default active backend
(its JIT also speeds up the cffi calls), and the core (`src/<pkg>/core/`) compiles with mypyc.

- **Always `import raylib as rl`**, never pyray in loops: pyray wraps every call in Python (~700 ns
  vs ~100 ns). `compile.forbid_imports = ["pyray"]` keeps it out of compiled code.
- **Create colors and structs once** (`gfx.color(...)`, cdata). Passing tuples such as `rl.RED`
  converts them again on every call: with tuples, PyPy loses its advantage.
- **The upstream raylib stub declares wrong types**: 55 functions claim to return `bytes` but
  return a pointer, and `Color.r` claims to be `bytes` but is an `int`. Interpreted, nothing
  happens; compiled, mypyc checks the type and raises `TypeError`. The preset ships the corrected
  stub in `typings/raylib`; regenerate it after changing the raylib version with `./deploy stubs`.
- `./deploy bunnymark` measures FPS with 30,000 bunnies
  (`./deploy run mypyc --frames 900 --bunnies 30000` for another backend).
- `[preset.raylib] package` picks the binding (`raylib` with GLFW, `raylib_sdl` with SDL3, or
  `raylib_software`) and `version` its version; `./deploy apply` swaps the dependency.
- `src/assets/` is bundled with the game (`<pkg>.resources.asset("name")` finds it in every
  build).
- There is no PyPy wheel for Apple Silicon or Linux ARM64 ([PyPy](#pypy)), and on Linux the game
  needs the GL/X11 libraries ([Troubleshooting](#troubleshooting)).
- Wayland or X11 on Linux, per the release notes of raylib-python-cffi 6.0.1.0, the version the
  preset pins (<https://github.com/electronstudio/raylib-python-cffi/releases>): `raylib` (GLFW, the
  default package) opens a native Wayland window and falls back to X11 when there is no Wayland;
  `pyray.glfw_init_hint(pyray.GLFW_PLATFORM, pyray.GLFW_PLATFORM_X11)`, called before the window is
  created (in interpreted code), forces XWayland. `raylib_sdl` (SDL3) uses X11 (XWayland on a
  Wayland desktop). On Wayland, GLFW cannot place a window: `SetWindowPosition` does nothing there.
  The template's CI and e2e runs test X11 only (xvfb): the native Wayland path is untested.

### flet

A desktop app with Flet: Flutter draws the UI and Flet's Python side cannot be compiled, so mypyc
speeds up the core (`src/<pkg>/core/`) and the UI (`src/<pkg>/ui/`) always runs interpreted.
Tested with Flet 1.0.1 and mypyc 2.3.1, with Flet in a compiled module:

- `async def` handlers are called without the event (TypeError);
- generator handlers never run, and raise no error;
- `@ft.component` fails on import;
- `@ft.control` loses the types of its events.

So compiled code has `compile.forbid_imports = ["flet", "flet_desktop", "flet_cli"]`. The pattern:
the interpreted handler converts Flet values to simple types and calls the core in another
process (`ProcessPoolExecutor`: compiled code does not release the GIL, so a thread would freeze
the UI).

- `./deploy dev`: hot reload (`flet run -d -r`; the editors start it without waiting).
- `./deploy build` uses `flet pack`; `./deploy build --method flet` uses `flet build`
  ([Distribution](#distribution)).
- `[preset.flet] version` pins `flet`, `flet-desktop` and `flet-cli` together (`./deploy apply`):
  when `flet` and `flet-desktop` differ, Flet pip-installs `flet-desktop` at runtime, outside
  `uv.lock`.
- The mypy rules are relaxed for `<pkg>.ui.*`, since Flet's API exposes `Any`.
- `src/assets/` is served by Flet and packaged with the app.

## VS Code

Accept the recommended extensions (`.vscode/extensions.json`): Python, Pylance (or basedpyright
with `typing.editor = "basedpyright"`), Python Debugger, Mypy Type Checker, Ruff, Even Better TOML
and Tasks (actboy168.tasks). mypy is the judge of types (it is what mypyc uses); Pylance and mypy
follow the active backend's typing profile.

The generated `.vscode/tasks.json` works on Windows, Linux and macOS: every task is a
`"process"` task that runs `/bin/sh <project>/deploy ...` (the executable bit does not matter),
and its `"windows"` block runs `deploy.cmd`. VS Code uses the block of the OS where the task runs
(the remote OS under WSL or SSH). Process tasks never go through the terminal's shell, so xonsh,
niubash or MSYS2 as the default terminal do not affect them.

Tasks (Terminal > Run Task, all named `deploy: ...`):

| Task | Notes |
|---|---|
| `run`, `run <backend>` | running it again restarts the app |
| `test` (default test task), `test <backend>`, `test all` | `test all` with more than one backend |
| `check`, `check all` | `check all` with more than one backend |
| `build` (default build task) | runs the checks first |
| `report` | mypyc supported: `report --open` |
| `compile` | mypyc supported; hidden: it runs before the mypyc debug configuration |
| `lint --fix`, `fmt`, `doctor`, `apply`, `setup` | |
| one per `[tasks]` entry | every preset: `ci`; raylib: `bunnymark`, `stubs`; flet: `dev` |

The `.vscode/*.json` files change when a `./deploy` command runs (VS Code runs none when
`pytemplate.toml` is saved): after editing it, run `./deploy apply` (or any task), and VS Code
reloads the tasks and buttons.

**Problems panel.** The check, test and build tasks report ruff, mypy (with the severity of the
typing profile: `warn` shows warnings), the mypyc rules, mypyc compile errors, pytest failures
(with mypyc mapped back to `src/`) and, with basedpyright, its findings. The editor extensions
report the open files; the tasks report the whole project. Limits: a pytest failure shows only
when the exception's name ends in Error, Exception, Failed, Warning, Exit or Interrupt; a
basedpyright message of several lines keeps its first line; C compiler errors are not matched.

**Buttons.** VS Code has no task buttons of its own: the recommended Tasks extension
(actboy168.tasks) shows the tasks listed in `[vscode] buttons` in the status bar, in that order:

```toml
[vscode]
buttons = ["run", "test", "check", "build"]   # commands or [tasks] names
```

Preset defaults: script `run test check build`, raylib `run bunnymark test build`, flet
`dev run test build`. A button can carry arguments (`"build --method pyz"`): it gets a task of its
own. Without the extension there are no buttons and nothing breaks.

**Workspace trust.** In Restricted Mode VS Code runs no tasks and the Tasks extension is off:
trust the folder.

**Settings.** `.vscode/settings.json` is generated. A setting changed in the Workspace tab of the
Settings UI (or written by an extension) lands in that file, and `./deploy` then reports it as
hand-edited (`render --check` and the git hook fail). Put such settings in `[vscode] settings`
and run `./deploy render --force`: each top-level key there replaces the generated value as a
whole, and the values must be JSON values (no TOML dates, no nan or inf). With
`typing.editor = "basedpyright"` the generated settings already set
`python.languageServer = "None"` and `python.analysis.typeCheckingMode = "off"`, so the prompts of
the basedpyright extension do not appear.

**Debugging (F5).**

| Configuration | When |
|---|---|
| `src/main.py (CPython, interpreted)` | always, first in the list (VS Code's selected interpreter) |
| `src/main.py (PyPy, experimental: the debugger is unreliable on PyPy)` | PyPy supported (`.venv-pypy`) |
| `Run mypyc stage (compiled modules cannot be stepped into)` | mypyc supported: runs the `deploy: compile` task, then the compiled stage; breakpoints stop in `src/main.py` and the interpreted modules only |
| `Tests (pytest)` | always |

Every configuration runs in UTF-8 mode (`PYTHONUTF8=1`), like `./deploy run`. The generated
settings pin the debug terminal to cmd on Windows and `/bin/sh` elsewhere
(`terminal.integrated.automationProfile.*`), so F5 works when the default terminal is xonsh or
niubash. Under Remote-WSL on a `/mnt/...` checkout only the CPython and pytest configurations
work (the others point at the Windows environments). In a Windows task, Ctrl+C asks "Terminate
batch job (Y/N)?": use Terminate Task.

## Neovim (LazyVim)

Open a project in LazyVim (Neovim 0.11.2 or newer) and you get, without installing Python tools
into Neovim:

- **LSP**: basedpyright by default (no Node.js), reading the generated `pyrightconfig.json`:
  from `.venv` if the project adds it, else `uvx` at the version `./deploy check` pins
  (basedpyright 1.40.1), else Mason. `vim.g.pytemplate_python_lsp = "pyright"` switches to
  pyright (Mason, needs Node.js). `typing.editor` does not choose Neovim's server (it picks the
  VS Code extension, basedpyright's rules in `pyrightconfig.json`, and basedpyright in `check`).
  The ruff server comes from `.venv`, the same version `./deploy check` uses.
- **mypy** diagnostics (nvim-lint) with the project's `.mypy.ini` and the severity of the typing
  profile. The default profile on cpython and pypy is `off` (`typing.relaxed = "off"`), which
  shows none: `./deploy mode --typing warn` (or `strict`) turns them on.
- **Debugging** (nvim-dap) with the configurations of `.vscode/launch.json` and the debugpy of
  `.venv`.
- **Tests** (neotest) run pytest on the active backend's environment; compiled mypyc runs and
  "all backends" go through the `deploy: test` task (`<leader>jt`, `<leader>jT`).
- **Tasks** (overseer): every `./deploy` command and every `[tasks]` entry, with the backend,
  method and extra arguments as parameters; errors go to the diagnostics and the quickfix list
  (mypyc errors land on the files in `src/`).
- `:Deploy ARGS` with completion (quotes group words: `:Deploy run cpython "a b"`), the keys
  below, and `:checkhealth pytemplate`. Saving `pytemplate.toml` runs `./deploy render`; the
  commands that change the mode or the name (`mode`, `apply`, `setup`, `sync`, `lock`, `add`,
  `remove`, `render`, `rename`) refresh the editor.

The plugin runs `uv run --quiet --script .pytemplate/deploy.py ARGS` as a list of arguments:
Neovim's `'shell'` (xonsh, niubash...) is never used. Without uv it runs the launcher
(`/bin/sh deploy`, or `deploy.cmd` on Windows), which prints how to install uv.

| Keys | Action | Keys | Action |
|---|---|---|---|
| `<leader>jj` | pick any deploy task | `<leader>jm` | switch the active backend |
| `<leader>jr` / `<leader>jR` | run / run on a backend, with arguments | `<leader>jk` | `[tasks]` picker |
| `<leader>jt` / `<leader>jT` | test / test all backends | `<leader>jd` | `dev` (flet hot reload) |
| `<leader>jc` / `<leader>jC` | check / check all backends | `<leader>jp` | mypyc report |
| `<leader>jb` / `<leader>jB` | build / build on a backend, with arguments | `<leader>js` / `<leader>jS` | sync all / setup |
| `<leader>jl` | lint --fix | `<leader>jD` | doctor |
| `<leader>jf` | format | `<leader>jw` / `<leader>jx` | task list / stop the deploy tasks |

LazyVim's own keys stay: `<leader>o` (overseer), `<leader>d` (debug), `<leader>t` (tests).
Options for `lua/config/options.lua`: `vim.g.pytemplate_prefix` (default `"<leader>j"`),
`vim.g.pytemplate_python_lsp` and `vim.g.pytemplate_render_on_save = false`. The plugin's
[README](https://github.com/omardev29/py_template/blob/main/.pytemplate/nvim/README.md) has the
details.

**First time**, after `./deploy setup`:

```sh
./deploy nvim doctor      # Neovim, LazyVim, the trust of .lazy.lua, extras and tools, with hints
./deploy nvim bootstrap   # only without a Neovim config: installs the LazyVim starter
                          # (then start nvim once, so LazyVim installs itself)
./deploy nvim trust       # trust this project's .lazy.lua (once per clone or folder)
./deploy nvim extras      # optional: enable the LazyVim extras permanently (lazyvim.json)
./deploy nvim sync        # install the plugins the project adds (Lazy! install)
```

`./deploy nvim` alone is `nvim doctor`. `bootstrap` never touches an existing config. `extras`
backs up `lazyvim.json` before changing it, and refuses (exit 3) until LazyVim has created that
file (start Neovim once). `sync` installs only (it never updates or removes your plugins) and
needs the trust first (exit 3 otherwise). Then start Neovim inside the project: `nvim` from the
project folder or any subfolder.

**How it works.** lazy.nvim loads `.lazy.lua` from the folder where Neovim starts (or the
nearest parent that has one), once it is trusted. That file never changes (it is identical in
every mode and preset), so trusting it once is enough: it enables the LazyVim extras the project
needs (`lang.python`, `lang.toml`, `dap.core`, `test.core`, `editor.overseer`) and loads the
plugin in `.pytemplate/nvim/`. Whatever depends on the mode comes from `.pytemplate/editor.json`,
a data file that `./deploy` regenerates.

**What gets downloaded, and where.** Nothing has to be added to your own LazyVim config: the
extras are imported by `.lazy.lua` only while Neovim runs inside the project. lazy.nvim still
installs their plugins (nvim-dap, neotest, overseer, nvim-lint...) in its usual plugin folder
(`stdpath("data")/lazy`, e.g. `%LOCALAPPDATA%\nvim-data\lazy`) the first time, or with
`./deploy nvim sync`. The Python tools do not come from Mason: ruff, mypy and debugpy are the
versions of `.venv` (pinned in `uv.lock`); basedpyright comes from `.venv` if you add it, else
`uv tool run` (cached by uv), else Mason. Mason and nvim-treesitter still install what the extras
declare (the TOML server taplo, the Python and TOML parsers). Outside pytemplate projects those
extras are not imported, so a `:Lazy clean` there removes their plugins until the next time:
`./deploy nvim extras` adds them to your `lazyvim.json` for good.

**Per preset.** script: `run` shows its output as it starts. raylib: the `typings/` stubs reach
the LSP through `pyrightconfig.json`; `bunnymark` and `stubs` are tasks; the game's output opens
only on failure. flet: `<leader>jd` starts `dev` (hot reload) as a background task; debugging
(F5 configuration) runs without hot reload.

**When something does not work:**

- Nothing loads: Neovim must start inside the project. `nvim path/to/file.py` from another
  folder, or a later `:cd`, does not load `.lazy.lua`.
- The trust prompt: Neovim 0.12 has no "allow" button (0.11 still has (a)llow). Choose (v)iew,
  run `:trust`, then restart Neovim; or run `./deploy nvim trust` once, before starting Neovim.
  Moving the folder asks again.
- A warning about the order of the LazyVim extras: `./deploy nvim extras` fixes it for good.
- No `.venv` yet (`:checkhealth pytemplate` warns): run `./deploy setup` and restart Neovim.
- The language server is chosen when Neovim starts: restart it after changing
  `vim.g.pytemplate_python_lsp`.
- `:checkhealth pytemplate` and `./deploy nvim doctor` show what is missing.

## Shells

The logic lives in `.pytemplate/deploy.py` (standard library only, run by uv). Three launchers
only find the project root and uv and pass the arguments on unchanged, so `./deploy build mypyc`
is typed the same way everywhere:

| Shell | Launcher | Notes |
|---|---|---|
| sh, bash, zsh, dash, ksh, busybox (Linux, macOS, WSL) | `deploy` | `#!/bin/sh`, plain POSIX sh |
| Git Bash, MSYS2 (any MSYSTEM), Cygwin, busybox-w32, niubash | `deploy` | finds uv even when a login shell's PATH lacks it (MSYS2 starts with a minimal PATH) |
| xonsh, fish, nushell, PowerShell 7 on Linux/macOS | `deploy` | through its `#!/bin/sh` (in PowerShell, `./deploy.ps1` works too) |
| cmd | `deploy.cmd` | `.\deploy ...` (a bare `deploy ...` too while `NoDefaultCurrentDirectoryInExePath` is unset) |
| xonsh on Windows | `deploy.cmd` | xonsh starts only PATHEXT files: `./deploy` resolves to `deploy.cmd` |
| nushell on Windows | `deploy.cmd` | type `./deploy.cmd`, or use the `shell-setup nu` function |
| PowerShell 7 / Windows PowerShell 5.1 on Windows | `deploy.ps1` | `./deploy` resolves to `deploy.ps1`. If the execution policy blocks it: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (a copy from a downloaded zip also needs `Unblock-File .\deploy.ps1`), or use `.\deploy.cmd` |

Editors do not depend on the shell: VS Code tasks run `/bin/sh deploy` (`deploy.cmd` on
Windows) and Neovim runs uv directly. The launchers ignore a `UV_PYTHON`, `PYTHONHOME`,
`PYTHONPATH` or `UV_WORKING_DIR` you export: the runner always runs on the project's
`python.cpython`, in the folder where the command was typed (the xonsh alias of `shell-setup`
keeps them: it can only hand uv an argument list). `./deploy` works under a caller's `set -eu`.
`./deploy doctor` shows which launcher started it and checks that the launchers kept their line
endings and executable bit.

**From subfolders.** The launchers find the project from their own location and, when that
fails, walk up from the current folder: `../deploy test` from `src/`, or the full path of the
launcher from anywhere, works (from a symlinked folder too). Paths given to the runner itself
(`./deploy new ../game`, `./deploy pyz-merge a.pyz b.pyz --out all.pyz`) are relative to the
folder where the command was typed; `~` works everywhere, and on Windows `/c/Users/...`,
`/cygdrive/c/...` and `C:/...` are accepted too. Arguments passed on to the app or to pytest are
relative to the project root
([Output, exit codes and environment](#output-exit-codes-and-environment)).

**Without `./`.** `./deploy shell-setup SHELL` prints a `deploy` function (or alias) that finds
the enclosing project from any subfolder, with a comment saying where to paste it. Shells:
`bash`, `zsh`, `niubash`, `msys2`, `fish`, `nu`, `xonsh` (with completion of the commands and
tasks), `pwsh`, `powershell`; without a name it guesses the shell. The xonsh alias and the nu
function run uv directly (no `deploy.cmd` and its argument limits); the pwsh function passes
pipeline input on. Print the xonsh snippet again to complete commands added later.

```sh
./deploy shell-setup niubash    # niubash reads ~/.niubashrc, but `niu -c` and scripts read $NIU_ENV
```

**Known limits.**

- cmd re-parses the arguments of every `.cmd` file: `% ! " ^` and unquoted `& | < >` inside an
  argument do not survive when the call goes through `deploy.cmd` (cmd itself, xonsh and nushell
  on Windows, VS Code tasks on Windows, Python's `subprocess`), even when the calling program
  quotes them. For such arguments use PowerShell, Git Bash, or the xonsh or nu `deploy` of
  `shell-setup` (they call uv directly).
- In a `.bat` or `.cmd` script, write `call deploy ...`: without `call`, cmd does not come back to
  the script after `deploy.cmd`.
- PowerShell removes a bare `--` before any script sees it (5.1 and 7 alike): quote it (`'--'`)
  or use `.\deploy.cmd`. `./deploy` itself never needs `--`: everything after `run` or `test`
  already goes to the app or to pytest.
- Inside PowerShell, a typed list (`--supports cpython,mypyc`) and a typed `-X:utf8` stay one
  argument each, as for any native program (but `-X: v`, with a space, arrives as `-X:v`), `--%`
  passes through literally, and pipeline input reaches the app
  (`Get-Content data.txt | ./deploy run`). `pwsh -File deploy.ps1 ...`, and `./deploy.ps1` typed
  in bash or zsh, split every argument that starts with `-` at its first colon (PowerShell's own
  parsing): from POSIX shells use `./deploy`.
- In PowerShell on Windows, `./deploy` and `../deploy` resolve to `deploy.ps1`, but a full path
  without the extension (`C:\proj\deploy`) opens the extensionless sh launcher through Windows'
  file association: type `C:\proj\deploy.ps1`.
- PowerShell in ConstrainedLanguage mode (AppLocker or WDAC policies) cannot run `deploy.ps1`
  (exit 126): use `.\deploy.cmd`.
- In cmd (and in VS Code tasks on Windows), Ctrl+C asks "Terminate batch job (Y/N)?" after the
  app has stopped. A UNC current folder (`\\server\share`) does not work with `deploy.cmd`.
- On Windows, the `bash` on PATH may be WSL's launcher ([Troubleshooting](#troubleshooting)).

## Git pre-commit hook

`./deploy apply` (and `setup`) installs a pre-commit hook when `[hooks] pre_commit = true` (the
default): a small sh script in the repository's hooks folder that runs `./deploy hooks run`. On
each commit it checks (and prints how long that took):

- ruff and `ruff format --check` on the staged Python files of `src/` and `tests/`, with the
  active typing profile, in their staged version (a file with unstaged changes is checked as it
  is staged; a staged file deleted from the working tree fails with a `git restore` or
  `git rm --cached` hint);
- the generated files: up to date, and none left unstaged or untracked;
- `pyproject.toml`: its managed parts, the `pytemplate.toml` changes that `./deploy apply` has not
  applied yet, and `uv lock --check`;
- `pytemplate.toml`, `pyproject.toml`, `uv.lock` and the generated files committed together;
- the mypyc rules on the staged compiled modules (blocking only with the `mypyc` profile);
- the launchers' line endings and modes.

It never runs mypy: that is `./deploy check`, the editors and CI. The checks of the whole project
read the working tree, so they can stop a commit that touches none of their files (a commit that
only deletes files is checked too).

- Skip it once: `git commit --no-verify`. Remove it: `./deploy hooks uninstall`, and set
  `pre_commit = false` (then `./deploy apply` removes it too). Its state: `./deploy hooks`.
- An existing hook of yours (or a symlink) is never overwritten: `./deploy hooks install --force`
  keeps it as `pre-commit.local` and runs it first; `uninstall` puts it back. With
  `core.hooksPath` set, nothing is written: add `sh ./deploy hooks run || exit $?` to your own hook
  (husky 9: `.husky/pre-commit`).
- A project in a subfolder of a bigger repository: the hook goes into that repository's hooks
  folder and checks the project's staged files. Two projects in one repository: `apply` leaves
  the other project's hook alone, and `./deploy hooks install --force` runs both. A project that
  the enclosing repository ignores gets no hook. Linked worktrees share the hook.
- It works from any git client (Git Bash, cmd, PowerShell, xonsh, VS Code, lazygit): git runs
  hooks with its own `sh`, and the hook calls the POSIX launcher, which finds uv by itself. When
  the launcher cannot check the commit (uv not found from a GUI client, a broken
  `pytemplate.toml`, a branch whose older `./deploy` has no `hooks run`), the hook says so and
  names `git commit --no-verify` and `./deploy hooks uninstall`. A checkout without `./deploy`
  (another branch) skips the checks.
- After the runner changes, `./deploy hooks` shows the hook as outdated until `./deploy apply` or
  `./deploy hooks install` rewrites it.

## Troubleshooting

- **`./deploy doctor`** checks uv, the environments, the C compiler, the generated files,
  `pyproject.toml`, `uv.lock`, the changes `./deploy apply` has not applied yet, the launchers,
  the shell, the git hook and Neovim, each problem with the command that fixes it. It exits 1
  when a line is `[XX]` (a C compiler is required whenever mypyc is supported).
- **"pyproject.toml does not match pytemplate.toml"**: run `./deploy apply`.
- **A generated file is "hand-edited"** (`render --check` and the git hook fail): something wrote
  into it (VS Code's Settings UI writes `.vscode/settings.json`). Move the change to
  `pytemplate.toml` (`[vscode] settings`) or to `.pytemplate/templates/`, then run
  `./deploy render --force`; `./deploy render --diff` shows what differs.
- **`pytemplate.toml` is not UTF-8** (every command stops with exit 2 and says how to fix it):
  save it as UTF-8. Windows PowerShell 5.1 writes UTF-16 with `>` and `Out-File`, and ANSI with
  `Set-Content`: add `-Encoding utf8`.
- **uv is too old** ("Required uv version `>=0.10.12` does not match the running version"):
  `uv self update` (or the package manager that installed uv).
- **`deploy: uv not found`**: the launchers also look in uv's usual install folders and, on
  Windows, in the PATH saved in the registry (a terminal opened before uv was installed).
  Otherwise install uv with one of the printed commands and open a new terminal.
- **`error: No such file or directory (os error 2)` from every command**: the current folder was
  deleted (uv refuses to start there): `cd` to an existing folder.
- **uv settings of your own**: the runner overrides uv's environment selection for its calls
  ([Output, exit codes and environment](#output-exit-codes-and-environment)). A `UV_EXCLUDE_NEWER`,
  `UV_RESOLUTION` or `UV_PRERELEASE` that disagrees with `uv.lock` makes `uv run --locked` fail:
  unset it for the project. The launchers need the Python of `.python-version` (`python.cpython`):
  with `UV_NO_MANAGED_PYTHON`, `UV_PYTHON_PREFERENCE=only-system` or `UV_PYTHON_DOWNLOADS=never`
  set, unset them or run `uv python install <python.cpython>`.
- **PyPy: "No interpreter found for PyPy 3.11.15 in managed installations"** after a uv update: that
  uv no longer downloads the pinned PyPy. Pick a version from
  `uv python list --only-downloads --all-versions pypy`, set `python.pypy`, and run
  `./deploy apply`.
- **mypyc: "Unable to find a compatible Visual Studio installation"** (Windows): `./deploy` adds
  the Visual Studio Installer folder to PATH (the `vcvarsall.bat` of VS 2026 needs `vswhere.exe`
  from it). If it still fails, `./deploy doctor` says what is missing.
- **Paths longer than 260 characters** (Windows): shorten the project path or enable
  `LongPathsEnabled`: MSVC (mypyc), PyPy and the Flet client have long internal paths.
- **`bash` opens WSL** (Windows): the `bash` on PATH may be WSL's launcher. Use Git Bash or MSYS2
  (their own `bash.exe`), xonsh, PowerShell or cmd. From WSL on a `/mnt/...` checkout the runner
  keeps separate environments (`.venv-wsl`, `.build/wsl`), so the Windows ones stay intact.
- **raylib: `setup` fails with "marked as `--no-build` but has no binary distribution"** (Apple
  Silicon, Linux ARM64): raylib publishes no PyPy wheel there. Run
  `./deploy mode cpython --supports cpython,mypyc`, then `./deploy setup`. The generated CI already
  skips PyPy on macOS.
- **raylib on a minimal Linux** (containers, WSL, CI) needs the GL and X11 libraries:
  `libgl1 libx11-6 libxrandr2 libxinerama1 libxcursor1 libxi6` (Debian/Ubuntu names; desktops have
  them).
- **Flet downloads its client or pip-installs packages when it starts**: `flet` and
  `flet-desktop` must have the same version. Set `[preset.flet] version` and run `./deploy apply`.
- **`flet build` on Windows** needs Developer Mode (Settings > System > For developers) and the
  Visual Studio C++ tools; `./deploy build --method flet` says so when Developer Mode is off.
- **An app that uses `flet.auth`**: run `./deploy add "httpx<1"`. Flet 1.0.1 accepts any httpx
  from 0.28.1 on, and the httpx 1.0 previews drop what `flet.auth` needs.
- **`./deploy clean` exits 1 on Windows**: a file in `.venv` is in use (the editor's mypy and ruff
  servers run from it). Close VS Code or Neovim and run it again.
- **`git clean -fdx` is safe**: it only deletes untracked and ignored files (`.venv*`, `.build/`,
  `dist/`, caches, `.claude/`); the generated files, `uv.lock` and `.pytemplate/state.json` are
  committed. The next command works (uv recreates `.venv`, pyz and portable builds create a
  missing environment, and `./deploy setup` recreates them all). The git hook lives in `.git/`, so
  it stays. `git clean -fdx -e .claude` keeps Claude Code's local settings.

## Layout

```
deploy, deploy.cmd, deploy.ps1   launchers (all the logic is in .pytemplate/)
pytemplate.toml                  configuration and mode: the single source of truth
pyproject.toml, uv.lock          dependencies (one lock for CPython and PyPy); managed parts
README.md                        the project's page (in the template repository: this manual)
LICENSE                          (template repository) the MIT license
CLAUDE.md                        technical notes for coding agents
src/main.py                      entry point (never compiled)
src/<pkg>/core/                  what mypyc compiles (compile.modules)
src/<pkg>/*.py                   the interpreted boundary (UI, I/O, poorly typed libraries)
src/assets/                      data bundled with the app (app.assets = "assets")
tests/                           pytest (conftest.py checks that mypyc's binaries were loaded)
typings/                         the project's stubs (raylib: the corrected raylib stub)
.python-version, .mypy.ini, .ruff.toml, pyrightconfig.json, .vscode/, .lazy.lua   generated
.github/workflows/ci.yml         the project's CI (generated)
.github/workflows/template-*.yml (template repository) the template's own CI
.pytemplate/README.md, LICENSE   (projects) this manual and the license of the copied runner
.pytemplate/runner/              the runner (Python standard library only)
.pytemplate/templates/           sources of the generated files (typing, VS Code, Neovim, CI)
.pytemplate/presets/             the script, raylib and flet skeletons, deps and tested pins
.pytemplate/nvim/                the Neovim plugin that .lazy.lua loads
.pytemplate/tests/               tests of the runner (./deploy selftest)
.pytemplate/state.json           hashes of the generated files, and the record of the last apply
.pytemplate/editor.json          generated data for the Neovim plugin
.build/, dist/                   outputs (ignored by git)
```

## Maintaining the template

Each project keeps its own copy of the runner in `.pytemplate/` and of the launchers: nothing is
downloaded or updated behind its back, and it does not follow later versions of the template.
The rest of this section is about keeping the template itself working. `CLAUDE.md` (in the
repository root, and copied into every project) is the technical map for coding agents:
<https://github.com/omardev29/py_template/blob/main/CLAUDE.md>.

### Versions

Pinned, and moved on purpose:

| What | Where | How |
|---|---|---|
| dependencies | `uv.lock`; the presets' `constraints.txt` | `./deploy lock --upgrade` (or `--upgrade-package NAME`), then the tests below; regenerate the constraints of a preset whose versions moved (CLAUDE.md, section 11) |
| CPython, PyPy | `python.cpython`, `python.pypy` in the root and preset `pytemplate.toml` files | edit, `./deploy apply`; native dependencies must publish wheels for the new version |
| the uv floor | `envs.MIN_UV` (0.10.12), written as `required-version` | with the Python pins: the first uv that downloads them |
| basedpyright | `cmd_dev.BASEDPYRIGHT` and `cmd_dev.BASEDPYRIGHT_NODE` | together, then `./deploy render` (`editor.json` carries the version) |
| Nuitka | `methods.nuitka.NUITKA` and `methods.nuitka.NUITKA_PYTHON` | together (a `python.cpython` newer than `NUITKA_PYTHON` needs a newer Nuitka) |
| UPX | `upx.VERSION` and the SHA-256 values of `upx.ASSETS` | together |
| GitHub actions | `.pytemplate/templates/ci.yml` and `.github/workflows/template-*.yml` | edit the template, then `./deploy render`; never edit the generated `ci.yml` |
| the Neovim test | `cmd_nvim.STARTER_REV` and `.pytemplate/nvim/tests/lazy-lock.json` | from one green run without the lock (CLAUDE.md, section 13.1) |

Not pinned: uv itself (the generated CI takes the latest, never older than the floor) and the GitHub
runner images (`*-latest`), on purpose: CI runs what users run; nor the Neovim plugins of a user's
own config. Known good in September 2026: uv 0.12.19, CPython 3.14.7, PyPy 7.3.23 (`pypy@3.11.15`),
mypyc 2.3.1, Flet 1.0.1 and Neovim 0.12.5; the template's CI first ran on the GitHub images
ubuntu-24.04, macos-26-arm64 and windows-2025-vs2026. Known dates: Python 3.11 reaches its end of
life in October 2027, and mypy has dropped a target version 6 to 9 months after that
([PyPy](#pypy)); Node 24, the runtime of the GitHub actions used here, reaches its end of life on
2028-04-30, so move the action versions before then.

The template's root `src/`, `tests/` and `pytemplate.toml` are the script preset's skeleton
rendered with the name `myapp`: change the preset in `.pytemplate/presets/script/files/` and
regenerate the root (CLAUDE.md, section 11).

### Testing the template

```sh
./deploy selftest            # the runner's tests (pytest), then mypy --strict on the runner
./deploy selftest --shells   # every launcher through every shell installed here
./deploy selftest --nvim     # the LazyVim integration, per preset, in an isolated LazyVim
./deploy selftest --e2e      # each preset end to end: new, setup, check, test, run and builds
```

- `selftest` needs `.venv` (`./deploy setup` once). Its arguments are added to the whole suite
  (select tests with `-k EXPR`). It needs the network only for two tests that re-lock a copy;
  they are skipped offline.
- `--shells [NAME,...] [--list] [--json] [--keep]`
  `[--project DIR] [--tests T1,...] [--jobs N] [--timeout S]`: seven probes per shell (arguments,
  exit code, folders, a temporary script like xonsh-shell-kit's `!` lines, a minimal PATH, stdin, uv
  install hints). `--list` shows the shells it found; `msys2` selects every `msys2-*` shell.
- `--nvim [PRESET,...] [--keep] [--fresh] [--require] [--timeout S] [--dir DIR]`: creates each
  preset with `./deploy new` and runs a headless smoke test in Neovim folders of its own, never
  yours, with LazyVim and its plugins at pinned commits (minutes the first time). `--fresh`
  reinstalls that LazyVim; `--require` fails instead of skipping when nvim or git is missing.
- `--e2e [PRESET ...] [--backends B,..] [--methods M,..] [--quick|--full]`
  `[--gui auto|on|off] [--keep] [--reuse] [--json] [--base DIR]`: creates a project of each preset
  with `./deploy new` in a short temporary folder and checks what it got, then works in it like a
  user: setup, doctor, the first commit through the git hook, check, test, run and every build the
  backends allow but nuitka, and starts the headless builds (a portable folder from another path).
  The default depth also runs `./deploy selftest` in the project, a usage error and a rename there
  and back; `--quick` builds only each backend's default method and skips those three; `--full`
  adds Nuitka, a PyPy round trip and a `[preset.*]` edit applied with `./deploy apply`. It prints a
  PASS/FAIL/SKIP table (`--json` for CI) and exits 1 on any FAIL.
- `./deploy render --check` and `./deploy doctor` must pass too.

**(template repository)** The template's own CI, in `.github/workflows/template-*.yml` (not
copied into projects):

- `template-selftest.yml` runs `./deploy selftest` on Linux, macOS and Windows, the runner's
  tests on Python 3.11 (its floor), the suite with the oldest uv that `required-version`
  accepts, and `./deploy selftest` inside new raylib and flet projects; on every push and pull
  request, and weekly.
- `template-launchers.yml` runs `selftest --shells` and `shellcheck` (weekly with the newest
  xonsh too).
- `template-nvim.yml` runs `selftest --nvim` with Neovim 0.12.5 and 0.11.2 and pinned plugins,
  plus a weekly canary with the newest Neovim, LazyVim and plugins.
- `template-e2e.yml` runs `selftest --e2e` for the three presets on the three systems (`--quick`
  on pushes and pull requests, the default depth weekly, `--full` monthly).
- `template-keepalive.yml` re-enables the scheduled ones every week: GitHub disables a scheduled
  workflow after 60 days without activity in the repository.

The Tests and E2E badges at the top show the state of `template-selftest.yml` and
`template-e2e.yml`.

## Quality bar

The owner's bar for this template, set in `CLAUDE.md` (rule 1.10 and section 13.4):

- Bug density is the number of counted bugs per line of our own code. At most 1 bug per 1000
  lines is ACCEPTABLE: the only state in which the template counts as finished. Worse than 1 per
  1000 but better than 1 per 500 is TOLERABLE only with a written plan back under 1 per 1000
  (every known bug listed with its fix). 1 per 500 or worse is UNACCEPTABLE. Worse than 1 per 100
  is UNRELIABLE software.
- Counted: a reproduced defect of our code (the runner, the launchers, the templates, the presets,
  the Neovim plugin, the template's CI) that stops the user from doing something. It is critical
  (it loses or corrupts data, opens a security hole, or gives a silently wrong result), serious
  (a command, build method or documented feature fails in a supported setup, with no reasonable
  workaround) or notable (it fails but a workaround exists, or it leaves a half-made change the
  user must repair by hand). Stability defects, what breaks by itself with time (a moving
  version, a schedule GitHub disables), count like bugs, at the same severities.
- Not counted, but still fixed: minor and cosmetic defects that stop nothing (a character printed
  wrong, an unclear hint, layout), the user's own code, and a defect of a dependency (uv,
  PyInstaller, Nuitka, mypyc, Flet, PowerShell, a shell...) when our workaround is documented in
  `CLAUDE.md` section 15.1. An undocumented workaround counts as our bug.
- How it is measured: lines are the non-blank lines of the product code that are not only a
  comment or a docstring (the runner, `deploy.py`, the tools, the launchers, the Neovim plugin,
  the templates, the preset skeletons and tools, the template's workflows; tests excluded). A
  bug hunt on a fixed commit reports findings that an independent verifier reproduces; two
  independent hunts on the same commit estimate the total by capture-recapture (found by the
  first x found by the second / found by both).
- The measurements: on 2026-09-25, at commit fc131b9, 10,837 lines: between 1 bug per 91 lines
  (high and medium bugs and stability defects only) and 1 per 50 (all of them): UNRELIABLE
  either way. The September 2026 overhaul fixed or deliberately closed each of that hunt's 172 bug
  findings and took on its stability defects. On 2026-09-26, at commit 87e28f9 (after the
  overhaul, 15,495 lines), two independent hunts of 10 agents each, one verifier per area and a
  skeptical second check confirmed 66 counted defects (9 critical, 1 serious, 56 notable), and
  the first CI runs on macOS and Windows found 12 more: 78 confirmed, 1 per 199 lines;
  capture-recapture estimates about 99, 1 per 157 lines. UNACCEPTABLE: the template is not at
  the bar. Every one of those defects is being fixed, and a new measurement follows.

The template is MIT-licensed (`LICENSE`). A project made with `./deploy new` keeps that notice as
`.pytemplate/LICENSE`, next to the copied runner, and has no root `LICENSE` of its own.
