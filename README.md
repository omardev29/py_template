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

Requirements: only **uv**. The mypyc backend also needs a C compiler:
MSVC on Windows (`./deploy doctor` gives you the exact `winget` command), gcc or clang on
Linux and `xcode-select --install` on macOS. Whoever receives your program needs nothing.

Editors: [VS Code](#vs-code) and [Neovim with LazyVim](#neovim-lazyvim) are configured from the
same `pytemplate.toml`, with tasks, problem reporting and debugging for every backend.

## Shells

The logic lives in `.pytemplate/deploy.py` (standard library only, run by uv). Three thin
launchers only find the project and uv and hand every argument over unchanged, so
`./deploy build mypyc` is typed the same way everywhere:

| Shell | Launcher | Notes |
|---|---|---|
| bash, zsh, sh, dash, ksh, busybox (Linux, macOS, WSL) | `deploy` | `#!/bin/sh`, plain POSIX sh |
| Git Bash, MSYS2 (any MSYSTEM), Cygwin, busybox-w32, niubash | `deploy` | Finds uv even when a login shell's PATH lacks it (MSYS2 starts with a minimal PATH) |
| xonsh on Linux/macOS | `deploy` | |
| cmd | `deploy.cmd` | `deploy ...` or `.\deploy ...` |
| xonsh on Windows, nushell, VS Code tasks | `deploy.cmd` | They can only start PATHEXT files: `./deploy` resolves to `deploy.cmd` |
| PowerShell 7 / Windows PowerShell 5.1 | `deploy.ps1` | If the execution policy blocks it: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `.\deploy.cmd` |

**From subfolders.** The launchers find the project from their own location and, when a shell
hides it (niubash runs scripts inside the calling shell, so `$0` is not the launcher), they walk
up from the current folder: `../deploy test` from `src/`, or the full path of `deploy` from
anywhere, works. Paths you give the runner (`./deploy new ../game`,
`./deploy pyz-merge a.pyz b.pyz --out all.pyz`) are relative to the folder where you typed the
command; on Windows `/c/Users/...`, `/cygdrive/c/...` and `C:/...` are accepted too.

**Without `./`.** `./deploy shell-setup SHELL` prints a `deploy` function (or alias) that finds
the enclosing project from any subfolder, with a comment saying where to paste it. Shells:
`bash`, `zsh`, `niubash`, `msys2`, `fish`, `nu`, `xonsh` (with completion), `pwsh`,
`powershell`.

```bash
./deploy shell-setup niubash    # niubash reads ~/.niubashrc, but `niu -c` and scripts read $NIU_ENV
```

**Known limits.**
- cmd re-parses the arguments of every `.cmd` file: `& | < > ^ %` inside an argument break
  when the call goes through `deploy.cmd` (cmd itself, xonsh on Windows, nushell, VS Code
  tasks). For such arguments use PowerShell, Git Bash, or the xonsh/pwsh `deploy` function from
  `shell-setup` (it calls uv directly).
- PowerShell removes a bare `--` before any script sees it (5.1 and 7 alike): quote it (`'--'`)
  or use `.\deploy.cmd`. `./deploy` itself never needs `--`: everything after `run` or `test`
  already goes to your app or to pytest.
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
| `check [BACKEND\|all]` · `lint [--fix]` · `fmt` | ruff + mypy with the backend's typing profile + mypyc rules |
| `test [BACKEND\|all] [args...]` | pytest; with mypyc it checks that the `.pyd`/`.so` files were loaded |
| `report [--open]` | mypyc HTML report: slow lines in red and how to fix them |
| `build [BACKEND] [--method M]` · `pyz-merge A.pyz B.pyz... --out C.pyz` | Distribution (see below) |
| `add PKG [--cpython-only]` · `remove` · `lock` · `sync` | Dependencies (uv) |
| `init PRESET` · `new DIR` · `render` · `clean` · `tasks` | Template and utilities |
| `nvim [doctor\|trust\|extras\|bootstrap\|sync]` | Neovim/LazyVim integration (see [Neovim](#neovim-lazyvim)) |
| `shell-setup [SHELL]` | Prints a `deploy` function for your shell (see [Shells](#shells)) |
| `selftest [--shells\|--nvim\|--e2e]` | Tests of the template itself: the runner; every launcher through every installed shell; LazyVim in isolation; each preset end to end |
| `help [COMMAND]` | Every command, or the options of one |

Global options go BEFORE the command: `-v` (verbose), `-q` (quiet), `--dry-run` (show what
would change without changing it), `--no-render`. Example: `./deploy --dry-run mode mypyc`.

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

## Configuration: `pytemplate.toml` and `.pytemplate/`

`pytemplate.toml` is the single source of truth: active backend, supported backends,
typing profile, modules to compile and deploy options. Change it and run any
`./deploy` command: these files regenerate themselves from the variants in `.pytemplate/templates/`:

- `.mypy.ini`, `pyrightconfig.json` (Pylance, pyright, basedpyright), `.ruff.toml`
- `.vscode/settings.json`, `launch.json`, `tasks.json`, `extensions.json`
- `.lazy.lua`, `.pytemplate/editor.json` (Neovim)
- `.python-version`, `.github/workflows/ci.yml`

**Do not edit them by hand**: `./deploy` detects the edit and does not overwrite it (`./deploy render --force`
to force it). Edit `pytemplate.toml`, or the variants in `.pytemplate/templates/typing/`
to change a profile. In `pyproject.toml`, `requires-python` and the block between the markers
in `[tool.uv]` are managed too; since they affect `uv.lock`, they only change with
`./deploy mode`, `./deploy lock` or `./deploy setup`.

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
  With `compile.annotate = true` every mypyc build also writes that report.
- `./deploy check` adds rules that mypy does not see: forbidden imports in compiled code,
  decorators that disable native classes, `__file__` at module level, t-strings...
- Compiled code has no pdb, cProfile or monkeypatch: debug it interpreted (F5 in VS Code, the
  dap keys in Neovim). The "mypyc stage" debug configuration runs the compiled build, but
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

`./deploy mode --jit on` sets `PYTHON_JIT=1` in `run`, `test`, the `portable` launchers and the
`.cmd` wrapper next to a `.pyz` (running `python app.pyz` yourself uses whatever `PYTHON_JIT`
your shell has). The CPython builds that uv downloads for Windows do not include the JIT, so it
uses a pinned python.org 3.14 (`py install 3.14`) in a separate environment (`.venv-jit`). It
cannot be enabled in a PyInstaller executable. Here it gave 1.6x on Collatz and 1.15x on the
sieve; mypyc and PyPy win by far.

## VS Code

Accept the recommended extensions (Python, Pylance or basedpyright, mypy, Ruff, Python
Debugger, Even Better TOML and Tasks). mypy is the judge of types (it is what mypyc uses);
Pylance and mypy configure themselves according to the active backend's profile.

**Do the task buttons work on Linux and Windows at the same time?** Yes. Every task in the
generated `.vscode/tasks.json` is a `"process"` task: by default it runs
`/bin/sh <project>/deploy ...` (so the executable bit does not matter), and its `"windows"`
block runs `deploy.cmd` instead. VS Code uses the block of the OS where the task runs (also the
remote OS under WSL or SSH). Process tasks never go through your terminal's shell, so xonsh,
niubash or MSYS2 cannot break them, and the same committed file works on every machine.

Tasks (Terminal > Run Task, all named `deploy: ...`; the list follows `pytemplate.toml` and is
regenerated when you change the mode):

| Task | Notes |
|---|---|
| `run`, `run <backend>` | Re-running restarts the app |
| `test` (default test task), `test <backend>`, `test all` | |
| `check`, `check all` | Errors in the Problems panel |
| `build` (default build task: Ctrl+Shift+B) | Runs the checks first |
| `report` | mypyc: opens the HTML report |
| `compile` | Hidden; runs before the mypyc debug configuration |
| `lint --fix`, `fmt`, `doctor`, `setup` | |
| one per `[tasks]` entry | Every preset: `ci`; raylib: `bunnymark`, `stubs`; flet: `dev` |

**Problems panel.** The check, test and build tasks report ruff, mypy (with the severity of the
typing profile: `warn` shows warnings), the mypyc rules, mypyc compile errors (mapped back to
`src/`), pytest failures and basedpyright. The editor extensions report the files you have open;
the tasks report the whole project.

**Buttons.** VS Code has no task buttons of its own: the recommended **Tasks** extension
(`actboy168.tasks`) shows the tasks listed in `[vscode] buttons` in the status bar:

```toml
[vscode]
buttons = ["run", "test", "check", "build"]   # ./deploy commands or [tasks] names
```

Preset defaults: script `run test check build`, raylib `run bunnymark test build`, flet
`dev run test build`. Without the extension nothing breaks: the extra keys are ignored.

**Debugging (F5).**

| Configuration | When |
|---|---|
| `src/main.py` on CPython, interpreted | Always (uses the selected interpreter) |
| `src/main.py` on PyPy | PyPy supported (experimental: the debugger is unreliable on PyPy) |
| mypyc stage | mypyc supported: `./deploy compile` first, then runs the compiled build; breakpoints only stop in interpreted modules |
| CPython JIT | `python.jit` on: `.venv-jit` with `PYTHON_JIT=1` |
| Tests (pytest) | Always |

The generated settings pin the debug terminal to cmd on Windows and `/bin/sh` elsewhere
(`terminal.integrated.automationProfile.*`), so F5 keeps working when your default terminal is
xonsh or niubash. Anything in `[vscode] settings` of `pytemplate.toml` overrides the generated
settings.

## Neovim (LazyVim)

Open the project in LazyVim and you get, without installing Python tools into Neovim:

- **LSP**: basedpyright by default (no Node.js needed), reading the generated
  `pyrightconfig.json`; set `vim.g.pytemplate_python_lsp = "pyright"` in your `options.lua` for
  pyright (needs Node.js). The ruff server comes from the project's `.venv`, the same version
  `./deploy check` uses.
- **mypy** diagnostics with the project's `.mypy.ini` and the typing profile's severity (none
  with the `off` profile).
- **Debugging** (nvim-dap) with the configurations of `.vscode/launch.json` and the `.venv`
  debugpy.
- **Tests** (neotest) with the project's interpreter; mypyc, PyPy and "all" runs go through
  the `deploy: test` task.
- **Tasks** (overseer): every `./deploy` command and every `[tasks]` entry, with backend,
  method and extra arguments as parameters; errors land in the diagnostics and the quickfix list.
- `:Deploy ARGS` with completion, `<leader>j` keymaps and `:checkhealth pytemplate`. Saving
  `pytemplate.toml` regenerates the configs.

Everything runs `uv run --script .pytemplate/deploy.py` as a list of arguments: Neovim's
`shell` option (xonsh, niubash...) is never used.

| Keys | Action | Keys | Action |
|---|---|---|---|
| `<leader>jj` | Pick any deploy task | `<leader>jm` | Switch the active backend |
| `<leader>jr` / `jR` | Run / run on a backend, with arguments | `<leader>jk` | `[tasks]` picker |
| `<leader>jt` / `jT` | Test / test all backends | `<leader>jd` | `dev` (flet hot reload) |
| `<leader>jc` / `jC` | Check / check all backends | `<leader>jp` | mypyc report |
| `<leader>jb` / `jB` | Build / build on a backend | `<leader>js` / `jS` | Sync all / setup |
| `<leader>jl` | Lint --fix | `<leader>jD` | Doctor |
| `<leader>jf` | Format | `<leader>jw` / `jx` | Task list / stop deploy tasks |

The LazyVim keys stay as they are: `<leader>o` (overseer), `<leader>d` (debug), `<leader>t`
(tests). Change the prefix with `vim.g.pytemplate_prefix`.

**First time** (Neovim 0.11.2 or newer; `trust` needs 0.12), after `./deploy setup`:

```bash
./deploy nvim doctor      # Neovim, LazyVim, trust state, extras and tools, with hints
./deploy nvim bootstrap   # only without LazyVim: installs the LazyVim starter config
./deploy nvim trust       # trust this project's .lazy.lua (once per clone or folder)
./deploy nvim extras      # optional: enable the LazyVim extras permanently (lazyvim.json)
./deploy nvim sync        # install the plugins the project needs (Lazy! sync)
```

Then start Neovim inside the project: `nvim` from the project folder or any subfolder.

**How it works.** lazy.nvim loads `.lazy.lua` from the folder where Neovim starts, once you
trust it. That file never changes (it is identical in every mode and preset), so trusting it
once is enough: it enables the LazyVim extras the project needs (`lang.python`, `lang.toml`,
`dap.core`, `test.core`, `editor.overseer`) and loads the plugin in `.pytemplate/nvim/`. Whatever
depends on the mode comes from `.pytemplate/editor.json`, a data file that every `./deploy`
command regenerates.

**Per preset.** script: `run` shows its output as it starts. raylib: the `typings/` stubs reach
the LSP through `pyrightconfig.json`; `bunnymark` and `stubs` are tasks; the game's output only
opens on failure. flet: `<leader>jd` starts `dev` (hot reload) as a background task; debugging
(F5 configuration) runs without hot reload.

**Troubleshooting.**
- Nothing loads: Neovim must start inside the project. `nvim path/to/file.py` from another
  folder, or a later `:cd`, does not load `.lazy.lua`.
- Trust prompt: Neovim 0.12 has no "allow" button. Choose (v)iew, run `:trust`, then restart
  Neovim; or run `./deploy nvim trust` once. Moving the folder asks again.
- A warning about the order of the LazyVim extras: `./deploy nvim extras` fixes it for good.
- ".venv not found": run `./deploy setup` and restart Neovim.
- The language server is chosen when Neovim starts: restart it after changing `typing.editor`
  (`./deploy mode --editor ...`) or `vim.g.pytemplate_python_lsp`.
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
CLAUDE.md                        technical notes for coding agents
.pytemplate/runner/              the runner (Python stdlib)
.pytemplate/templates/           configuration variants (typing profiles, VS Code, Neovim, CI...)
.pytemplate/presets/             script, raylib and flet skeletons
.pytemplate/nvim/                the Neovim plugin that .lazy.lua loads
.pytemplate/tests/               tests of the runner (./deploy selftest)
.build/, dist/                   outputs (ignored by git)
```
