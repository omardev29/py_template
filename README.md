# myapp: multi-backend uv template (CPython · PyPy · mypyc)

A single template for scripts, **raylib** games and **Flet** apps, with three ways to run
the same code and six ways to distribute it. Everything is driven by `./deploy`, a Justfile-style
runner that only needs [uv](https://docs.astral.sh/uv/) (if it is missing, the launcher offers to
install it or prints how).

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
./deploy setup            # interpreters, environments, uv.lock and editor configs
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

The app name (the executable, and the package in `src/` with `-` written as `_`) is the folder
name unless you pass `--name`: letters, digits, `-` and `_`, starting with a letter. `new` and
`init` also reject a name that is one of the project's dependencies:
`./deploy new ../flet --preset flet` fails, because uv refuses a project that depends on itself
and `src/flet/` would shadow the library. Pick another one with `--name`.

Requirements: only **uv**. The mypyc backend also needs a C compiler:
MSVC on Windows (`./deploy doctor` gives you the exact `winget` command), gcc or clang on
Linux and `xcode-select --install` on macOS. Whoever receives your program needs nothing.

Editors: [VS Code](#vs-code) and [Neovim with LazyVim](#neovim-lazyvim) are configured from the
same `pytemplate.toml`, with tasks, problem reporting and debugging for every backend.

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

## Commands

| Command | What it does |
|---|---|
| `setup` · `doctor` | Installs environments · checks uv, compiler, PyPy, JIT, launchers, shells, Neovim and configs |
| `mode [BACKEND] [--supports +pypy] [--typing warn] [--jit on] [--editor basedpyright]` | Shows or changes the mode |
| `run [BACKEND] [args...]` | Runs the app (the arguments go to your app) |
| `compile [--release]` | Compiles the mypyc stage without running it (for debuggers and editors) |
| `check [BACKEND\|all]` · `lint [--fix]` · `fmt [--check]` | ruff + mypy with the backend's typing profile + mypyc rules |
| `test [BACKEND\|all] [args...]` | pytest; with mypyc it checks that the `.pyd`/`.so` files were loaded |
| `report [--open] [--no-mypy]` | mypyc HTML report: slow lines in red and how to fix them |
| `build [BACKEND] [--method M] [--no-check]` · `pyz-merge A.pyz B.pyz... --out C.pyz` | Distribution (see below) |
| `add PKG [--cpython-only]` · `remove` · `lock` · `sync` | Dependencies (uv) |
| `init PRESET` · `new DIR` · `rename NEW_NAME [--force]` · `render` · `clean [--envs]` · `tasks` | Template and utilities (see [Renaming](#renaming-the-app)) |
| `hooks [install [--force]\|uninstall\|run\|status]` | The git pre-commit hook (see [below](#git-pre-commit-hook)) |
| `nvim [doctor\|trust\|extras\|bootstrap\|sync]` | Neovim/LazyVim integration (see [Neovim](#neovim-lazyvim)) |
| `shell-setup [SHELL]` | Prints a `deploy` function for your shell (see [Shells](#shells)) |
| `selftest [--shells\|--nvim\|--e2e]` | Tests of the template itself (see below) |
| `help [COMMAND]` | Every command, or the options of one |

Global options go BEFORE the command: `-v` (verbose), `-q` (quiet), `--dry-run` (show what
would change without changing it), `--no-render`. Example: `./deploy --dry-run mode mypyc`.

Unknown arguments are an error (exit 2), never silently ignored. Only a few commands pass extra
arguments on: `run` to your app, `test` to pytest, `lock` to `uv lock`, `build` to the packager
(PyInstaller, Nuitka or flet) and custom tasks to their command.

**Tests of the template itself** (for changes to `.pytemplate/` or the launchers):

```bash
./deploy selftest            # the runner's tests (pytest) + mypy --strict; extra args go to pytest
./deploy selftest --shells   # every launcher through every shell installed here
./deploy selftest --nvim     # the LazyVim integration, per preset, in an isolated LazyVim
./deploy selftest --e2e      # each preset end to end
```

- `--shells [NAME,...] [--list] [--json] [--keep] [--project DIR] [--tests T1,...] [--jobs N]
  [--timeout S]`: seven probes per shell (arguments, exit code, folders, a temporary script like
  xonsh-shell-kit's `!` lines, a minimal PATH, stdin, uv install hints). `--list` shows the
  shells it found; `msys2` selects every `msys2-*` shell.
- `--nvim [PRESET,...] [--keep] [--fresh] [--require] [--timeout S] [--dir DIR]`: creates each
  preset with `./deploy new` and runs a headless smoke test in its own Neovim folders, never
  yours (minutes the first time). `--fresh` reinstalls that LazyVim; `--require` fails instead of
  skipping when nvim or git is missing (CI).
- `--e2e [PRESET ...] [--backends B,..] [--methods M,..] [--quick|--full] [--gui auto|on|off]
  [--keep] [--reuse] [--json] [--base DIR]`: creates a project from each preset with
  `./deploy new` in a short temp dir, then runs setup, doctor, check, test, run and every
  compatible build in it. It prints a PASS/FAIL/SKIP table (`--json` for CI). `--quick` builds
  only each backend's default method; `--full` adds Nuitka and a PyPy round trip. The
  `template-e2e` workflow runs it weekly.

Justfile-style **custom tasks** in `pytemplate.toml` (the same in every shell, no shell in between):

```toml
[tasks.gen]
help = "Generate the assets"
cmd = ["python", "scripts/gen.py", "{backend}"]   # runs with `uv run` in the backend's environment
deps = ["check"]                                   # other tasks or ./deploy commands
env = { SEED = "42" }
```

Run it with `./deploy gen [extra args]`. Placeholders: `{root}` `{src}` `{build}` `{dist}`
`{backend}` `{name}` `{pkg}` `{python}`. `backend = "pypy"` picks the environment, `uv = false`
runs the program as-is, and `background = true` marks a long-running server (editors start it
without waiting). Tasks also show up in `./deploy help`, VS Code and Neovim.

### Renaming the app

Changing `app.name` by hand moves nothing: use `./deploy rename NEW_NAME` (try `--dry-run`
first). It moves `src/<pkg>/`, rewrites the imports and the other references in `src/` and
`tests/` (package paths get the package, window and table titles get the name), updates
`pytemplate.toml` (`app.name`, `compile.modules`, the mypy overrides, the wheel entry, task
commands) and `pyproject.toml` (the project name and the preset tables), re-locks `uv.lock` and
regenerates the configs. The package is the name in lower case with `_` for `-`
(`My-Game` -> `src/my_game/`). It refuses a dirty git tree unless `--force` (review the result
with `git diff`), and names whose package would be a dependency (`flet`, `rich`), a Python
keyword or a standard-library module (`json`); `./deploy new` and `init` apply the same rules.
`dist/` keeps the artifacts built with the old name. To choose the name at creation:
`./deploy new DIR --name NAME`.

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

## Configuration: `pytemplate.toml` and `.pytemplate/`

`pytemplate.toml` is the single source of truth: active backend, supported backends,
typing profile, modules to compile and deploy options. Change it and run any
`./deploy` command: these files regenerate themselves from the variants in `.pytemplate/templates/`:

- `.mypy.ini`, `pyrightconfig.json` (Pylance, pyright, basedpyright), `.ruff.toml`
- `.vscode/settings.json`, `launch.json`, `tasks.json`, `extensions.json`
- `.lazy.lua`, `.pytemplate/editor.json` (Neovim)
- `.python-version`, `.github/workflows/ci.yml`

**Do not edit them by hand**: `./deploy` detects the edit and does not overwrite it
(`./deploy render --force` to force it). Edit `pytemplate.toml`, or the variants in
`.pytemplate/templates/typing/` to change a profile. In `pyproject.toml`, `requires-python`
and the block between the markers in `[tool.uv]` are managed too; since they affect `uv.lock`,
they only change with `./deploy mode`, `./deploy lock` or `./deploy setup`.

**Pinned tools outside `uv.lock`.** basedpyright (used by `check` when
`typing.editor = "basedpyright"`) and Nuitka are not in `uv.lock`: they run with
`uv run --with`, pinned in `cmd_dev.BASEDPYRIGHT` (1.40.1) and `methods.nuitka.NUITKA` (4.2.2)
under `.pytemplate/runner/`. Bump them there.

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
