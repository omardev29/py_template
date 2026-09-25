# myapp: multi-backend uv template (CPython · PyPy · mypyc)

A single template for scripts, **raylib** games and **Flet** apps, with three ways to run
the same code and six ways to distribute it. Everything is driven by `./deploy`, a Justfile-style
runner that only needs [uv](https://docs.astral.sh/uv/) (if it is missing, it offers to install it).

| Backend | What it is | Types | Best for |
|---|---|---|---|
| `cpython` | The usual interpreter | Optional | Short scripts, C-API libraries (numpy, pillow...) |
| `pypy` | JIT compiler | Optional | Long-running pure Python and **cffi** libraries (raylib) |
| `mypyc` | Compiles your core to C (AOT) | **Required, no `Any`** | Numeric/CPU logic; shipping a fast binary |

Measured on this machine (Windows 11, CPython 3.14.7, PyPy 7.3.23, mypyc 2.3.1):

| Benchmark | CPython | PyPy | mypyc |
|---|---|---|---|
| Sieve up to 5 million (script preset) | 0.42 s | 0.11 s | 0.08 s |
| Collatz < 300,000 (script preset) | 2.45 s | 0.07 s | 0.13 s |
| Bunnymark 30,000 bunnies (raylib preset) | 67 FPS | **217 FPS** | 152 FPS |
| Fractal 640x400 (flet preset) | 2.81 s | n/a | 0.08 s |

## Getting started

```bash
./deploy setup            # interpreters, environments, uv.lock and VS Code configs
./deploy run              # run with the active backend (pytemplate.toml)
./deploy run mypyc        # compile the core with mypyc and run
./deploy test all         # pytest on every backend (on mypyc, against the binaries)
./deploy build mypyc      # PyInstaller executable in dist/
```

New project from this template (no need for a separate repo per project type):

```bash
./deploy new ../my-game --preset raylib     # or: script, flet
./deploy init flet --name myapp             # or convert THIS project to another preset
```

Requirements: only **uv**. The mypyc backend also needs a C compiler:
MSVC on Windows (`./deploy doctor` gives you the exact `winget` command), gcc or clang on
Linux and `xcode-select --install` on macOS. Whoever receives your program needs nothing.

## `./deploy` in every shell

The logic lives in `.pytemplate/deploy.py` (standard library only, run by uv). The
`deploy` (sh), `deploy.cmd` and `deploy.ps1` launchers only find uv and forward the arguments,
so `./deploy build mypyc` is typed the same way everywhere:

| Shell | How | Notes |
|---|---|---|
| xonsh (Windows) | `./deploy ...` | xonsh uses `deploy.cmd` (it only runs PATHEXT extensions) |
| xonsh (Linux/macOS), bash, zsh, Git Bash | `./deploy ...` | Uses `deploy` (`#!/bin/sh`: on Windows, `env bash` could open WSL) |
| PowerShell 7 / 5.1 | `./deploy ...` | If the execution policy blocks it: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `.\deploy.cmd` |
| cmd | `.\deploy ...` | |

Without `./` in any project made from the template: `./deploy shell-setup xonsh` (or `pwsh`,
`bash`) prints an alias with completion, ready to paste into `~/.xonshrc`.

## Commands

| Command | What it does |
|---|---|
| `setup` · `doctor` | Installs environments · checks uv, compiler, PyPy, JIT, shells and configs |
| `mode [BACKEND] [--supports +pypy] [--typing warn] [--jit on] [--editor basedpyright]` | Shows or changes the mode |
| `run [BACKEND] [args...]` | Runs the app (the arguments go to your app) |
| `check [BACKEND\|all]` · `lint [--fix]` · `fmt` | ruff + mypy with the backend's typing profile + mypyc rules |
| `test [BACKEND\|all] [args...]` | pytest; with mypyc it checks that the `.pyd`/`.so` files were loaded |
| `report [--open]` | mypyc HTML report: slow lines in red and how to fix them |
| `build [BACKEND] [--method M]` · `pyz-merge` | Distribution (see below) |
| `add PKG [--cpython-only]` · `remove` · `lock` · `sync` | Dependencies (uv) |
| `init PRESET` · `new DIR` · `render` · `clean` · `tasks` | Template and utilities |

Justfile-style **custom tasks** in `pytemplate.toml` (the same in every shell, no shell in between):

```toml
[tasks.gen]
help = "Generate the assets"
cmd = ["python", "scripts/gen.py", "{backend}"]   # runs with `uv run` in the backend's environment
deps = ["check"]                                   # other tasks or ./deploy commands
env = { SEED = "42" }
```

## Configuration: `pytemplate.toml` and `.pytemplate/`

`pytemplate.toml` is the single source of truth: active backend, supported backends,
typing profile, modules to compile and deploy options. Change it and run any
`./deploy` command: these files regenerate themselves from the variants in `.pytemplate/templates/`:

- `.mypy.ini`, `pyrightconfig.json` (Pylance), `.ruff.toml`
- `.vscode/settings.json`, `launch.json`, `tasks.json`, `extensions.json`
- `.python-version`, `.github/workflows/ci.yml`

**Do not edit them by hand**: `./deploy` detects the edit and does not overwrite it (`./deploy render --force`
to force it). Edit `pytemplate.toml`, or the variants in `.pytemplate/templates/typing/`
to change a profile. In `pyproject.toml`, `requires-python` and the block between the markers
in `[tool.uv]` are managed too; since they affect `uv.lock`, they only change with
`./deploy mode` or `./deploy lock`.

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
- `./deploy check` adds rules that mypy does not see: forbidden imports in compiled code,
  decorators that disable native classes, `__file__` at module level, t-strings...
- Compiled code has no pdb, cProfile or monkeypatch: debug it interpreted (F5 in VS Code).

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
`[deploy.pyz] targets = ["cp314-linux-x86_64", ...]`.

Other options in `pytemplate.toml`: `deploy.optimize` (`-O` bytecode; with mypyc it strips
the `assert`s), `deploy.exe.mode` (`onefile`/`onedir`), `app.gui` (no console),
`deploy.portable.runtime = "system"` (folder without an interpreter, for the target machine's Python).

## Presets

### script (default)
Console app: `src/myapp/core/bench.py` (compiled) and `src/myapp/app.py` (output with rich).

### raylib
PyPy by default (its JIT also speeds up the cffi calls), with a core that mypyc can compile.

- **Always `import raylib as rl`**, never pyray in loops: pyray wraps every call in
  Python (~700 ns vs ~100 ns). `compile.forbid_imports` prevents it in compiled code.
- **Create colors and structs once** (`gfx.color(...)`, cdata). Passing tuples such as `rl.RED`
  converts them again on every call: with tuples, PyPy loses all its advantage.
- **The official raylib stub lies**: 55 functions claim to return `bytes` but return a
  pointer, and `Color.r` claims to be `bytes` but is an `int`. Interpreted, nothing happens;
  compiled, mypyc checks the type and raises `TypeError`. The preset ships `typings/raylib`,
  the corrected stub (regenerate it after updating raylib: `./deploy stubs`).
- `./deploy bunnymark`: measures FPS with 30,000 bunnies (`./deploy run mypyc --frames 900 --bunnies 30000`
  for another backend).

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

`./deploy mode --jit on` sets `PYTHON_JIT=1` in `run`, `test` and in the `portable`/`pyz`
launchers. The CPython builds that uv downloads for Windows do not include the JIT, so it uses
a pinned python.org 3.14 (`py install 3.14`) in a separate environment (`.venv-jit`). It cannot
be enabled in a PyInstaller executable. Here it gave 1.6x on Collatz and 1.15x on the
sieve; mypyc and PyPy win by far.

## VS Code

Accept the recommended extensions (Python, Pylance, mypy, Ruff). F5 debugs
`src/main.py` interpreted (with PyPy there is an experimental configuration). The tasks
(`Ctrl+Shift+B`) call `./deploy`. mypy is the judge of types (it is what mypyc uses);
Pylance and mypy configure themselves according to the active backend's profile.

## Troubleshooting

- **mypyc "Unable to find a compatible Visual Studio installation"**: `./deploy` already adds
  the Visual Studio Installer to PATH (the `vcvarsall.bat` of VS 2026 needs it).
  If it fails, `./deploy doctor` tells you what is missing.
- **Paths longer than 260 characters** (Windows): shorten the project path or enable
  `LongPathsEnabled`. Flet and PyPy have files with long internal paths.
- **`bash` opens WSL**: on Windows use xonsh, PowerShell or cmd. If you use `./deploy` from WSL,
  the runner creates separate environments (`.venv-wsl`) so it does not break the Windows ones.
- **Flet downloads the client or installs packages on its own**: `flet` and `flet-desktop`
  must have the same version (`[preset.flet] version`).

## Layout

```
deploy, deploy.cmd, deploy.ps1   launchers (all the logic is in .pytemplate/)
pytemplate.toml                  configuration and mode
pyproject.toml, uv.lock          dependencies (a single lock for CPython and PyPy)
src/main.py                      launcher (never compiled)
src/myapp/core/                  core compiled by mypyc
src/myapp/*.py                   interpreted boundary
tests/                           pytest (conftest verifies the mypyc binaries)
.pytemplate/runner/              the runner (Python stdlib)
.pytemplate/templates/           configuration variants (typing profiles, VS Code, CI...)
.pytemplate/presets/             script, raylib and flet skeletons
.build/, dist/                   outputs (ignored by git)
```
