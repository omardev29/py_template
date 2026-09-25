# CLAUDE.md: py_template for coding agents

Technical map of this project: architecture, invariants, workarounds and fragile points. Read
the section you need before touching a file. Cite code by symbol (`render.apply`,
`envs.env_vars`), never by line number.

- This file is copied into every project made with `./deploy new`. Items tagged
  **[template repo]** only apply to the template repository itself, which is recognised by the
  marker file `.pytemplate/template-repo` (`./deploy new` does not copy it).
- `<pkg>` is the app package: `app.name` in snake_case (`myapp` in the template repo).
- "(planned)" marks behaviour whose code was being written in parallel when this file was
  written. Check the code before relying on it.

## 1. Ground rules

1. Drive everything through `./deploy` (`./deploy help`, `./deploy help COMMAND`). It sets the
   environment variables uv, mypy and mypyc need (sections 5.5 and 7). A bare `uv run`, `mypy` or
   `pytest` in the project can pick the wrong interpreter or silently recreate `.venv`.
2. Never hand-edit generated files (section 6). Change `pytemplate.toml`,
   `.pytemplate/templates/**` or the generator, run `./deploy render`, and commit the
   regenerated files together with the change.
3. The runner (`.pytemplate/runner/`) is stdlib only, Python 3.11 compatible and
   `mypy --strict` clean. Coding rules: section 14.
4. Verify with `./deploy selftest` (must pass), `./deploy render --check` and `./deploy doctor`.
   `selftest` needs `.venv`: run `./deploy setup` once.
5. English only in code, comments, messages, docs, preset UI strings and TOML comments
   (**[template repo]** enforced by the language guard, section 13). Avoid non-ASCII: write
   `...` and `->`. Launchers and generated `.cmd` files must be pure ASCII.
6. Git: never edit git config (the machine may have no global identity). Commit with
   `git -c user.name="..." -c user.email="..." commit`, copying the identity of the existing
   commits (`git log -1 --format='%an <%ae>'`). Keep `deploy` at mode 100755
   (`git ls-files -s deploy`; fix with `git update-index --chmod=+x deploy`).
7. Keep scratch paths short (`C:\t\p1`, `%TEMP%\pt\...`): with `LongPathsEnabled=0` deep paths
   break MSVC (mypyc), PyPy runtime copies, `compileall` and the Flet client extraction.
8. Never touch user-global state from tests or tools: shell rc files, the user's Neovim
   config/data/state, PATH, the registry, installed programs.

## 2. What this is

- A uv-based Python project template. One codebase, three backends:
  - `cpython`: `.venv`, uv-managed CPython (`python.cpython`, a minor version).
  - `pypy`: `.venv-pypy`, PyPy pinned exactly (`python.pypy = "pypy@3.11.15"`); code must be
    3.11 syntax and API while PyPy is supported.
  - `mypyc`: AOT compilation of `compile.modules` (default `<pkg>.core`); runs on the `.venv`
    CPython. Needs a C compiler (MSVC Build Tools on Windows).
  - Optional CPython JIT (`python.jit`): `.venv-jit` on a system python.org build.
- Presets `script`, `raylib`, `flet` (`.pytemplate/presets/*`): skeleton + deps + config.
- Six build methods: `exe` (PyInstaller / `flet pack`), `portable`, `pyz`, `wheel`, `nuitka`,
  `flet` (`flet build`).
- `./deploy` is a Justfile-like runner. The only hard requirement is uv.
- `pytemplate.toml` is the single source of truth; every derived config is generated.
- Editor integrations are generated too: VS Code (`.vscode/*.json`) and LazyVim (`.lazy.lua`,
  `.pytemplate/editor.json`, local plugin `.pytemplate/nvim/`).

## 3. Layout (tracked files)

```
deploy, deploy.cmd, deploy.ps1    launchers (section 4); all logic is in .pytemplate/
pytemplate.toml                   single source of truth (section 6.1)
pyproject.toml, uv.lock           deps; pyproject has managed parts (section 6.3)
src/main.py                       app entry point, never compiled
src/<pkg>/core/                   compiled by mypyc (compile.modules)
src/<pkg>/*.py                    interpreted boundary (UI, I/O, poorly typed libraries)
tests/                            pytest; conftest.py proves the .pyd/.so were loaded
typings/                          project stubs (raylib preset: the corrected raylib stub)
.lazy.lua                         generated, static LazyVim local spec (section 12.2)
.pytemplate/deploy.py             PEP 723 entry (requires-python >=3.11, dependencies = [])
.pytemplate/runner/               the runner package (section 5)
.pytemplate/runner/editors/       vscode.py, nvim.py: editor file generators
.pytemplate/runner/methods/       exe, portable, pyz, wheel, nuitka, flet (+ common.py)
.pytemplate/tools/mypyc_build.py  runs INSIDE .venv (needs mypy + setuptools), calls mypycify
.pytemplate/templates/            typing/*.toml, vscode/settings.json, nvim/lazy.lua, ci.yml,
                                  portable/boot.py, pyz/__main__.py
.pytemplate/presets/<p>/          preset.toml + files/ skeleton (+ raylib/tools/raylib_stubs.py)
.pytemplate/nvim/                 local Neovim plugin pytemplate.nvim (lua/, tests/smoke.lua)
.pytemplate/tests/                runner tests (pytest) + mypy-runner.ini
.pytemplate/editor.json           generated data file for the Neovim plugin
.pytemplate/state.json            hashes of the generated files (committed)
.pytemplate/template-repo         [template repo] marker, not copied by ./deploy new
.github/workflows/ci.yml          generated CI of the project
.github/workflows/template-*.yml  [template repo] CI of the template itself, not copied (planned)
ignored: .venv*/ .build/ dist/ build/ *.spec *.pyd *.so .flet/
```

## 4. Launchers and shells

### 4.1 Contract

Four callers implement the same contract: `deploy`, `deploy.cmd`, `deploy.ps1` and the Neovim
plugin (section 12.2). Change them together.

1. Find the project root: the directory that holds `.pytemplate/deploy.py`, first from the
   launcher's own location, else by walking up from the current directory (planned).
2. Find uv (order below).
3. Export `PYTEMPLATE_CALLER_CWD` (the caller's cwd; `C:\...` form on Windows (planned)) and
   `PYTEMPLATE_LAUNCHER` (planned).
4. Run `uv run --quiet --script <root>/.pytemplate/deploy.py ARGS...` with argv untouched and
   propagate its exit code. Launchers never `cd` and contain no other logic.

Launcher exit codes: 2 = no project found (planned), 127 = uv not found (after the install
hints), 126 = `deploy.ps1` could not start uv (planned); anything else comes from the runner.

uv search order (planned): `$UV` -> `command -v uv`, accepted only when it is a path (rejects
aliases) -> `UV_INSTALL_DIR[/bin]`, `XDG_BIN_HOME`, `XDG_DATA_HOME/../bin`, `~/.local/bin`,
`CARGO_HOME/bin`, `~/.cargo/bin` -> Windows: WinGet `Links` and `Packages/astral-sh.uv_*` (user
and machine), scoop shims (user, global), chocolatey, and last the user and machine `Path`
stored in the registry (a console opened before uv was installed) -> POSIX: Homebrew,
linuxbrew, nix. Install hints: Windows prints the PowerShell installer, winget and scoop (never
curl); POSIX prints curl, brew and pipx. Only an interactive run (TTY, no `CI`) offers to run
the official installer.

uv exports `UV` (its own path) to everything it starts, so `proc.find_uv` checks `$UV` first.

`PYTEMPLATE_LAUNCHER` values (planned): `sh`, `sh:bash`, `sh:zsh`, `sh:niubash`, each with
`:msys` or `:cygwin` appended under those runtimes (`sh:bash:msys`); `cmd`;
`ps1:<PSEdition>:<major>.<minor>` (`ps1:Core:7.6`, `ps1:Desktop:5.1`); `nvim`.
`project.native_path` reads the `:msys`/`:cygwin` suffix; `./deploy doctor` prints the value.

### 4.2 Which launcher runs

| Caller | Launcher | Why |
|---|---|---|
| sh, bash, zsh, dash, ksh, busybox (Linux, macOS, WSL) | `deploy` | POSIX sh |
| Git Bash, MSYS2 (any MSYSTEM, login or not), Cygwin, busybox-w32, niubash | `deploy` | POSIX sh |
| cmd | `deploy.cmd` | PATHEXT |
| xonsh on Windows, nushell, Python `subprocess`, VS Code tasks on Windows | `deploy.cmd` | can only start PATHEXT files; `./deploy` resolves to `deploy.cmd` |
| PowerShell 7, Windows PowerShell 5.1 | `deploy.ps1` | |
| VS Code tasks on Linux/macOS | `/bin/sh deploy` (planned) | no exec bit needed |
| Neovim plugin | none: runs uv directly (planned) | no shell, no cmd.exe |

### 4.3 `deploy` (POSIX sh)

Every rule below exists because of a verified failure.

- First line `#!/bin/sh`, LF (`.gitattributes`: `deploy text eol=lf`), git mode 100755, ASCII.
  `#!/usr/bin/env bash` is wrong: on Windows xonsh maps it to `bash`, which can be the WSL
  stub. `shells.doctor` checks the shebang and LF.
- POSIX sh only (dash, bash 3.2, busybox ash, ksh; zsh through `emulate sh`): no arrays,
  `[[ ]]`, `${v//a/b}`, `local`, `$'...'`, `function`. No `set -e` / `set -u`.
- `printf '%s\n'`, never `echo`, for anything that may contain a backslash (dash's `echo`
  interprets `\`: `c:\Users` prints as `c: sers`).
- niubash hygiene (section 4.6): every variable and function name starts with `_pt_`; the
  helpers are `unset` before `exec`; never `cd`; `exit` only at top level or inside `if`/`case`
  bodies (`return` is fine anywhere); no comment on a `name() {` line; never write
  `"...$(cmd "$x")..."`: assign the substitution to a variable first.
- Root discovery (planned): `$BASH_SOURCE` -> zsh `${(%):-%x}` (read through `eval` so dash
  never parses it) -> `$0` -> walk up from `$PWD`. Handles `/`, `C:/`, `C:`, `/c` and `//unc`
  roots; relative candidates (`./`, `../`) are folded against `$PWD`.
- Windows detection: `OS=Windows_NT` and no `WSL_DISTRO_NAME`; `uname -s` only when `OS` is
  unset. Never `OSTYPE` (niubash fakes `msys`). Never trust the output format of `uname` or
  `cygpath`: in non-login MSYS2 shells on the maintainer's machine they resolve to WinuxCmd
  copies.
- On Windows the script path and `PYTEMPLATE_CALLER_CWD` are handed over as `C:\...`: `/c/x`
  and `/cygdrive/c/x` are converted in pure sh; `cygpath -m` only for paths inside the
  MSYS/Cygwin root such as `/home` or `/tmp` (planned).
- Registry lookup: `reg.exe query KEY` WITHOUT `/v` (MSYS rewrites `/v` into `V:/`). It costs
  ~110 ms and only runs when every other lookup failed (planned).
- Launcher overhead measured on Windows for the design version: dash ~26 ms, niubash ~40 ms
  over a bare shell start (planned: re-measure the final file).
- CI runs `shellcheck -s sh deploy` (planned); keep its `# shellcheck disable=` directives.
- Testing launcher code without writing files: pass it through the environment,
  `sh -c 'eval "$PT_CODE"'` (Cygwin mangles backslashes in argv). Syntax checks: `dash -n`,
  `bash --posix -n`, `sh -n`.

### 4.4 `deploy.cmd`

- Pure ASCII (cmd reads it in the OEM code page); CRLF (`.gitattributes`: `*.cmd text
  eol=crlf`; labels and `goto` break with LF).
- No `( )` blocks: a PATH that contains `(x86)` closes them early. No delayed expansion. `%*`
  appears only on the uv line.
- `%~dp0` has a trailing backslash and can be wrong when cmd found the file through PATH from
  a quoted name: check `%~dp0.pytemplate\deploy.py` before trusting it, else walk up from
  `%CD%` (planned).
- cmd re-parses `%*`: `& | < > ^ %` (and `!`) inside arguments get mangled. Every caller that
  starts a `.cmd` through CreateProcess/`list2cmdline` inherits this (xonsh on Windows, Python
  `subprocess`, VS Code tasks): the BatBadBut class. Never design CLI syntax that needs these
  characters.
- `call` re-expands `%`. Ctrl+C asks "Terminate batch job (Y/N)?". UNC current directories are
  not supported.

### 4.5 `deploy.ps1`

- ASCII, LF, no BOM (`.gitattributes`: `deploy.ps1 text eol=lf`): xonsh and Unix kernels read
  its shebang `#!/usr/bin/env pwsh`, and Windows PowerShell 5.1 reads BOM-less files as ANSI.
  Git mode 100755 so `./deploy.ps1` works under pwsh on Linux/macOS (planned).
- No `param()` block: it would turn `-v`, `-h`, `-q` into PowerShell parameters.
- A `.ps1` runs inside the caller's session: never assign `$env:PATH`, and restore every
  variable it sets in a `try`/`finally` (planned).
- PowerShell removes a bare `--` before any script or function sees it (5.1 and 7 alike).
  Never design CLI syntax that needs `--`; users quote it (`'--'`) or use `deploy.cmd`.
- PowerShell < 7.3 (or `$PSNativeCommandArgumentPassing = 'Legacy'`) drops empty arguments and
  mangles embedded quotes when calling native programs: the launcher pre-quotes every argument
  (planned).
- `Get-Command uv -CommandType Application`: a plain `Get-Command uv` can return an alias or
  a function (planned).
- Execution policy `Restricted`/`AllSigned`, or Mark-of-the-Web on a copy from a downloaded
  zip: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, `Unblock-File .\deploy.ps1`, or
  use `deploy.cmd`. `shells.doctor` reports the policy.

### 4.6 niubash (1.1.4) quirks, all verified

- Runs `#!/bin/sh` files and nested `sh -c` IN-PROCESS and ignores the shebang. `$0` is the
  caller's (`niu` under `-c`; the temp script `%TEMP%\xonsh-shell-kit\bang-<pid>-<n>.sh` on
  the xonsh `!` route). `$BASH_SOURCE` is correct. This was the original bug:
  `$(dirname "$0")/.pytemplate/deploy.py` pointed into `%TEMP%`.
- Variables, functions and `cd` leak into the calling shell; `exit`/`exec` end only the script.
  Aliases expand inside scripts (oh-my-niu defines `g`, `ga`, `gd`, `gl`, `gp`...).
- `exit` is silently ignored inside `a || exit`, `a && exit` and `{ ...; }`.
- A comment on a `f() {` line is a syntax error.
- `"...$(cmd "$x" 2>&1)..."` keeps the inner quotes in the result.
- `$PWD` looks like `C:/Users/...`, PATH is `;`-separated, `OSTYPE=msys` is fake, `uname -s`
  prints `MSWindows_NT`.
- A session keeps stale exports: an old `PYTEMPLATE_CALLER_CWD` can survive, which is why
  `project.caller_cwd` only trusts it when it names the process cwd.
- `niu -c`, niubash scripts and the xonsh `!` route read only the file named by `$NIU_ENV`, not
  `~/.niubashrc`: a shell function must be sourced from both.

### 4.7 MSYS2 and Cygwin

- MSYS2 login shells default to `MSYS2_PATH_TYPE=minimal` (`/etc/profile`): PATH is `/usr/bin`
  plus System32, so uv is not on PATH, and `$HOME` is `/home/<user>`, not the Windows profile.
  Windows variables (`USERPROFILE`, `LOCALAPPDATA`, `ProgramData`) keep their case;
  `SYSTEMROOT` and `PROGRAMFILES` are upper-cased. The launcher uses them and the registry
  (planned).
- Non-login MSYS2 shells (the xonsh `!m` route) have the full Windows PATH but no `/usr/bin`.
- MSYS rewrites POSIX-looking arguments and environment values for native programs (`/c/x` ->
  `C:/x`, `--flag=/x` -> `--flag=C:/msys64/x`, `/v` -> `V:/`). Opt out with
  `MSYS_NO_PATHCONV=1` or `MSYS2_ARG_CONV_EXCL='*'`. Cygwin, niubash and busybox-w32 do not
  rewrite, so the runner can receive `/c/x` (handled by `project.native_path`).
- `uname -s` is `MSYS_NT-...` or `MINGW64_NT-...` (also for UCRT64 and CLANG64);
  `OSTYPE=cygwin`. An inherited `PWD` can make MSYS2 bash start with `PWD=C:/...`.
- Cygwin with `CYGWIN_NOWINPATH=1` also has a minimal PATH.

### 4.8 Paths on the runner side (`project.py`)

- `project.ROOT = Path(__file__).resolve().parents[2]`: the runner never uses the cwd to find
  the project. Every project path comes from `project.*` constants.
- `project.native_path(raw)` (identity outside Windows): `C:/x` and `c:\x` are normalised;
  `/c/x` and `/cygdrive/c/x` -> `C:\x`; `//unc` is left alone; paths inside the MSYS root go
  through `cygpath -w` only when `PYTEMPLATE_LAUNCHER` ends in `:msys` or `:cygwin`.
- `project.caller_cwd()`: `PYTEMPLATE_CALLER_CWD` only while it is the same directory as the
  process cwd (`os.path.samefile`), else `Path.cwd()`. `uv run --script` never changes the cwd.
- `project.user_path(raw)`: use it for EVERY path argument the user types for the runner
  (`new DEST`; `pyz-merge` inputs and `--out` (planned)). Never for arguments forwarded to the
  app or to pytest. Never read `PYTEMPLATE_CALLER_CWD` directly.

### 4.9 Shell tooling

- `./deploy __probe EXIT STDIN(0|1) ARGS...` (`shells.probe`; `cli.main` routes it before
  `_parse_globals`: no config load, no render, not in help). Prints ONE line `PTPROBE{json}`
  to stdout (ASCII JSON with keys `argv`, `cwd`, `caller_cwd_raw`, `caller_cwd`, `launcher`,
  `stdin_tty`, `stdin`, `root`) and exits with EXIT. Launcher tests use it: keep the keys.
- `./deploy selftest --shells [NAME,...] [--list] [--json] [--keep] [--project DIR]`
  (`shells.selftest`, planned): discovers the installed shells and runs, through each one,
  T1 argv round-trip, T2 exit codes, T3 cwd independence (from `src/` and from outside), T4
  the xonsh-shell-kit route (niubash / MSYS2 non-login script mode), T5 minimal PATH, T6
  stdin, T7 install hints. Prints a PASS/FAIL/SKIP table (`--json` to stdout) and exits 1 on
  any FAIL. POSIX shells get the command through the environment (`sh -c 'eval "$PTCMD"'`),
  cmd a hand-built line with arguments free of `% ! " ^`, PowerShell `-EncodedCommand`.
  `--project DIR` probes another copy (to test launcher candidates before they land).
- `./deploy shell-setup [xonsh|pwsh|powershell|bash|zsh|niubash|msys2|fish|nu]`
  (`shells.cmd_shell_setup`; the last four shells are planned): prints a `deploy`
  function/alias that works from any subfolder, plus where to paste it. The POSIX walk-up must
  stop at `/`, `C:` and `C:/` (the old `dirname` loop never ended on niubash's `C:/...`
  paths). Snippets are ASCII (5.1 reads profiles as ANSI). The xonsh completion words come
  from `cli.COMMANDS` at print time (planned).
- `shells.doctor(check)` (from `./deploy doctor`): the `deploy` shebang/LF check, the `bash` =
  WSL stub note, the PowerShell execution policy, WSL info. Planned: the launcher that started
  the run, `deploy` ASCII + 100755, `deploy.cmd` ASCII + CRLF, `deploy.ps1` ASCII + LF + no
  BOM + 100755.

## 5. Runner architecture

### 5.1 Modules (`.pytemplate/runner/`)

| Module | Responsibility |
|---|---|
| `deploy.py` (one level up) | Reconfigures stdout/stderr to UTF-8, puts its own dir on `sys.path`, calls `runner.cli.main`. |
| `cli.py` | `COMMANDS` table of `Command(module, func, summary, usage, render, group)`, modules imported lazily. `_parse_globals`, `dispatch`, `main` (exception -> exit code), `cmd_help`, `cmd_tasks`, `cmd_selftest`, the `__probe` route. |
| `config.py` | Dataclass schema, strict loader (`_build`: unknown key or wrong type -> error with the full key path), `validate`, derived values (`pkg`, `min_python`, `profile_for`, `pypy_enabled`), `compiled_paths`, comment-preserving editor `set_value` / `update_file`. |
| `project.py` | Paths (`ROOT`, `SRC`, `BUILD`, `DIST`, `TEMPLATES`, `PRESETS`...), `IS_WINDOWS/IS_MACOS/IS_WSL`, `ENV_SUFFIX`, `venv_python`, `host_os/host_arch` (uv names), `rel`, `code_dirs`, `native_path`, `caller_cwd`, `user_path`. |
| `ui.py` | All runner output to stderr; `DeployError(msg, code)`; `VERBOSE/QUIET`; colours (off with `NO_COLOR` or a non-TTY stderr); `check_line` (doctor lines `[ok]`, `[XX]`, `[--]`). |
| `proc.py` | `find_uv`, `base_env`, `run` (echo, `DRY_RUN`, cwd defaults to `ROOT`, UTF-8 capture), `output`, `show` (display quoting only), `vs_installer_dir`. |
| `envs.py` | `PyEnv(key, dir, request, preference)`; `cpython_env`, `pypy_env`, `jit_env`, `tool_env` (always CPython), `runtime_env(backend)`, `env_vars`, `uv`, `uv_run` (= `uv run --locked`), `sync`, `interpreter_info`, `find_jit_interpreter`. |
| `render.py` | Every generated file (`outputs`), hand-edit detection (`apply`, `auto`), typing profiles (`load_profile`), `mypy_ini`, `mypy_cli_args`, `pyright_config`, `ruff_config`, `to_toml`, `jsonc`, `ci_workflow`, managed pyproject parts. |
| `editors/vscode.py` | `.vscode/settings.json`, `extensions.json`, `launch.json`, `tasks.json` (section 12.1). |
| `editors/nvim.py` | `.lazy.lua` (verbatim template copy) and `.pytemplate/editor.json` (section 12.2). |
| `presets.py` | Preset discovery/loading, option merge, `uv_extras`, `dependencies`, `skeleton`, `pristine`, `init`, `copy_template`, `new`. |
| `mypyc.py` | Incremental stage, `spec.json`, spawning `tools/mypyc_build.py`, `hidden_imports`, `exe_stage`, `runtime_env_vars`, `has_compiler_hint`. |
| `imports.py` | AST import extraction that skips `if TYPE_CHECKING:` blocks; parses bytes (tolerates a BOM). |
| `lintc.py` | Extra AST rules for compiled modules (section 9). |
| `tasks.py` | `[tasks]`: placeholders, `deps`, cycle detection, `run_task`, `list_tasks`. |
| `cmd_env.py` | `setup`, `doctor`, `sync`, `lock`, `add`, `remove`, `clean`; `ensure_lock`; `_fix_exec_bit`. |
| `cmd_mode.py` | `mode` (+ the Python 3.11 precheck before enabling PyPy), `render`, `init`, `new`. |
| `cmd_dev.py` | `run`, `compile`, `check` (`run_checks`), `lint`, `fmt`, `test` (`test_backend`), `report`; `split_backend`; `_profile_file`. |
| `cmd_build.py` | `build`: backend + method resolution, `COMPAT`, `payload`, `BuildRequest`, `dist_path`, `fresh_dir`; `pyz-merge`. |
| `methods/*.py` | One `build(req: BuildRequest) -> Path` per method; `common.py` has target keys, `UV_PLATFORMS`, `export_requirements`, `install_deps`, `copy_app`, `uses_tkinter`. |
| `shells.py` | `__probe`, shell doctor checks, `shell-setup` snippets, `selftest --shells` (section 4.9). |
| `cmd_nvim.py` | `./deploy nvim ...` and `doctor(check)` (section 12.2). |
| `nvimtest.py` | `selftest --nvim` (section 13). |
| `e2e.py` | `selftest --e2e` (section 13). |

### 5.2 Call flow

1. Launcher -> `uv run --quiet --script .pytemplate/deploy.py ARGS`. uv picks any Python >= 3.11
   for the PEP 723 script, often in an ephemeral env, and exports `UV`.
2. `cli.main`: `__probe` short-circuit, then `_parse_globals` (global flags must come BEFORE the
   command: `-v/--verbose`, `-q/--quiet`, `--dry-run`, `--no-render`, `-h/--help`), then
   `dispatch`.
3. `dispatch`: `help` needs no config. Otherwise `config.load(set(COMMANDS))` (validates;
   task names may not shadow builtins). Builtins run `render.auto(cfg)` first when
   `Command.render` is true and `--no-render` is not set, then `module.func(cfg, args)`. Names
   in `[tasks]` run `render.auto` and `tasks.run_task(cfg, name, args, dispatch)`.
4. Commands with `render=False`: `clean`, `render`, `new`, `pyz-merge`, `tasks`,
   `shell-setup`, `selftest`, `help`.

### 5.3 Exit codes and output

- 0 ok; 1 = check/test failures, doctor problems, or an internal runner error (traceback
  printed); 2 = usage/config (`DeployError` default, argparse); 3 = missing requirement (uv,
  compiler, interpreter); 130 = Ctrl+C; `run`/`test`/tasks return the child's exit code;
  `proc.CommandFailed` carries the failed child's code.
- Runner output goes to stderr through `ui` so the app keeps stdout. Exceptions, printed to
  stdout on purpose: `help`, `__probe`, `shell-setup` snippets, `--json` reports (planned).
- `ui.error` prints `error: ` and `ui.warn` prints `warning: ` (wrapped in ANSI colours on a
  TTY). VS Code problem matchers and the Neovim output parser depend on these exact prefixes
  and on `lintc.Finding.__str__` (`src/...:N: msg`): do not reword them.
- On Windows `ui` enables ANSI with `os.system("")`, which spawns cmd.exe once per run
  (planned: `SetConsoleMode` through ctypes instead).

### 5.4 `--dry-run`

- `proc.run` skips only ECHOED commands (`echo=True`); `echo=False` queries still run.
- `render.apply` writes nothing and `render.auto` reports "would update".
- `clean`, `build` (it stops after the checks), `report --open` honour it.
- `mode`, `init`, `new`, `render`, `pyz-merge` print their plan and write nothing (planned).
- It is not a sandbox: scratch writes under `.build/` (`.build/cfg/*`, the mypyc stage,
  `spec.json`) still happen.

### 5.5 Environment-variable contract

| Variable | Set by | Meaning |
|---|---|---|
| `PYTEMPLATE_CALLER_CWD` | launchers, Neovim plugin (planned) | Caller's cwd; read only through `project.caller_cwd` |
| `PYTEMPLATE_LAUNCHER` | launchers, Neovim plugin (planned) | Which launcher/shell ran (section 4.1) |
| `UV` | uv | uv's own path; `proc.find_uv` uses it |
| `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `UV_PYTHON_PREFERENCE` | `envs.env_vars` | Environment selection (section 7) |
| `PYTHON_JIT` | `envs.env_vars`, portable launchers, pyz `.cmd` wrapper (planned) | Always exactly `0` or `1` |
| `PYTHONUTF8=1` | `proc.base_env`, portable launchers, pyz `.cmd` wrapper | mypy/mypyc otherwise read files as cp1252 |
| `PYTEMPLATE_BACKEND` | `cmd_dev.test_backend`, `mypyc.runtime_env_vars` | Backend under test (conftest) |
| `PYTEMPLATE_COMPILED` | `mypyc.runtime_env_vars` | Modules that must load from `.pyd/.so` (conftest) |
| `PYTEMPLATE_ASSETS` | `portable/boot.py`, `pyz/__main__.py` (setdefault) | Assets dir for `resources.assets_dir()` (raylib, flet) |
| `VSLANG=1033` | `mypyc.build`, wheel builds | English MSVC messages |
| `RUFF_OUTPUT_FORMAT=concise` | VS Code and Neovim check tasks (planned) | One-line ruff output for the parsers |
| `NO_COLOR` | user | Disables runner colours |
| `CI` | CI | Disables the launchers' install prompt (planned) |

`proc.base_env` removes `VIRTUAL_ENV`, `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `PYTHONHOME` and
`PYTHONPATH`; removes the runner's own ephemeral `Scripts/` or `bin/` from PATH when
`sys.prefix != sys.base_prefix` (uv exports it for `--script` runs); sets `PYTHONUTF8=1`; on
Windows appends `%ProgramFiles(x86)%\Microsoft Visual Studio\Installer` to PATH (VS 2026
`vcvarsall.bat` calls `vswhere.exe` by bare name; without it setuptools fails with "Unable to
find a compatible Visual Studio installation").

## 6. Configuration and generated files

### 6.1 `pytemplate.toml` (`config.py`)

- `schema = 1` is only type-checked; there is no migration logic.
- `[app]`: `name` (`[A-Za-z][A-Za-z0-9_-]*`; `pkg = name.replace("-", "_").lower()`), `preset`
  (validated against existing presets (planned)), `gui`, `assets` (dir inside `src/`, `""` =
  none).
- `[backend]`: `active`, `supported` (non-empty subset of `cpython, pypy, mypyc`, contains
  `active`).
- `[python]`: `cpython` (`^\d+\.\d+$`), `pypy` (`^pypy@\d+\.\d+\.\d+$`, exact: a loose request
  can resolve to PyPy 8.0 / pp80, which has no wheels), `jit`, `jit_interpreter`.
- `[typing]`: `profile = auto|mypyc|strict|warn|off`, `relaxed = off|warn|strict` (what `auto`
  means on cpython/pypy), `editor = pylance|basedpyright`, `[[typing.mypy_overrides]]`
  (`module` required, `strict` forbidden: mypy would apply it to ALL modules; `{pkg}` token in
  module names). `backend.active = "mypyc"` rejects `warn`/`off`.
- `[compile]`: `modules`, `exclude`, `forbid_imports` (dotted names checked), `annotate`
  (writes the annotate report on every mypyc build (planned)), `opt_level "0".."3"`,
  `multi_file`, `separate`, `strict_dunder_typing`.
- `[deploy]`: `optimize 0|1|2`, `default {backend: method}`, `[deploy.exe] mode console icon
  hidden_imports extra_args`, `[deploy.portable] runtime prune archive env`, `[deploy.pyz]
  targets`, `[deploy.wheel] entry`, `[deploy.nuitka] mode extra_args`, `[deploy.flet] target
  extra_args`.
- `[tasks.<name>]`: `cmd` (argv), `deps`, `env`, `backend`, `uv = true`, `cwd`, `help`,
  `background` (long-running dev server: editors start it without waiting). Name regex
  `[a-z][a-z0-9_-]*`; `cmd` or `deps` required.
- `[preset.<name>]`: free-form option overrides, NOT validated.
- `[vscode]`: `settings` (merged into `.vscode/settings.json`, NOT validated), `buttons`
  (each first word must be a builtin command or a `[tasks]` name: `config.validate`).
- `config.set_value` is line-based: it only rewrites single-line `key = value` entries (a
  multi-line array would break it). `update_file` re-parses with `tomllib` and refuses to write
  broken TOML. The config is read as `utf-8-sig` (tolerates a BOM).

### 6.2 Generated files and `state.json`

Generated (committed, never hand-edited): `.python-version`, `.mypy.ini`, `.ruff.toml`,
`pyrightconfig.json`, `.vscode/{settings,extensions,launch,tasks}.json`, `.lazy.lua`,
`.pytemplate/editor.json`, `.github/workflows/ci.yml` (only while `templates/ci.yml` exists),
`.pytemplate/state.json`. Also managed: parts of `pyproject.toml` (6.3), `uv.lock` (only via
`./deploy lock|add|remove|setup|mode|init`), `typings/raylib/__init__.pyi` (raylib task
`stubs`).

`render.apply(cfg, force, check, show_diff)`:
- Hash = sha256 of the content with the BOM stripped and CRLF -> LF (`render._norm`,
  `_digest`), because `* text=auto eol=native` + `core.autocrlf=true` gives CRLF checkouts on
  Windows and editors/PS 5.1 add BOMs. Every runner write uses `newline="\n"`.
- Current hash == new hash -> skip. Recorded hash in `state.json` != current file -> hand-edited:
  not written (warning) unless `--force`. Otherwise write (LF) and record the hash.
- A missing or corrupt `state.json` counts as empty: every generated file is overwritten
  without warning.
- `--check` and `--dry-run` write nothing.
- `render.auto` runs before most commands and prints one line when something changed; it also
  warns when `pyproject_outdated`.
- Changing `render.HEADER` rewrites every generated file (fine: only files whose current hash
  differs from the recorded one are protected).

Formats:
- Generated JSON is JSONC: the first line is `// GENERATED ...` (`render.jsonc`). VS Code,
  pyright and overseer accept it; strict JSON parsers must drop that line first.
- `.pytemplate/editor.json` is plain JSON without comments (it has a `"generated"` key
  instead): Lua decodes it.
- `.lazy.lua` is a byte copy of `.pytemplate/templates/nvim/lazy.lua` (CRLF -> LF) and must not
  depend on the config (section 12.2).
- Path styles differ per tool: `.build/cfg/mypy-*.ini` uses relative `mypy_path = src` /
  `files = src, tests`, so mypy must run with cwd = ROOT (the `proc.run` default); the ruff and
  pyright copies under `.build/cfg` use absolute paths (`absolute=True`) because those tools
  resolve paths relative to the config file; the compile-time `mypy.ini` only carries an
  absolute `typings` path because mypyc runs with cwd = stage.

### 6.3 `pyproject.toml` managed parts

- `requires-python`: the first `^requires-python` line is rewritten to `">=<min_python>"`.
- The `[tool.uv]` block between `# >>> pytemplate` and `# <<< pytemplate`
  (`render.managed_block`). The END MARKER IS AN INLINE COMMENT on the last key line
  (`python-preference = "only-managed"  # <<< pytemplate`) so uv/toml_edit insertions cannot
  detach it; `test_managed_block_bounds_cpython_minor` asserts it. Without markers the block is
  inserted right after `[tool.uv]`.
- `render.auto` only warns (`pyproject_outdated`); `write_pyproject` runs in `lock`, `mode`,
  `setup` (via `cmd_env.ensure_lock`) and `init`, because the change needs re-locking.
- Preset tables go between `# >>> pytemplate-preset` and `# <<< pytemplate-preset`
  (`presets.EXTRA_BEGIN/EXTRA_END`).
- Never add `[build-system]` to the project's `pyproject.toml`: the project is an app (uv would
  treat it as a package); the wheel method synthesises its own build project.

## 7. Environments and uv invariants

- `UV_PROJECT_ENVIRONMENT` and `UV_PYTHON` must ALWAYS be set together (`envs.env_vars`): with
  only one of them uv silently recreates the env with the wrong interpreter.
- `UV_PYTHON_PREFERENCE=only-managed` for `.venv` and `.venv-pypy` (also
  `python-preference = "only-managed"` in the managed block); `.venv-jit` uses `only-system`
  with `UV_PYTHON=<absolute path>`.
- `runtime_env(backend)`: `pypy` -> `.venv-pypy`; `cpython`/`mypyc` -> `.venv-jit` when
  `python.jit`, else `.venv`. `tool_env` is always `.venv`: every tool (mypy, ruff, mypyc,
  PyInstaller, pytest for selftest) runs there.
- Every tool call is `uv run --locked` (syncs when needed, fails on a stale lock).
  `cmd_env.ensure_lock` runs `uv lock --check` and then `uv lock` if needed.
- `environments` in the managed block is bounded to the CPython minor (e.g.
  `cpython and >=3.14,<3.15`), plus `pypy and >=3.11,<3.12` when PyPy is supported. Without the
  bound uv resolves for 3.15+, where raylib has no wheels. Changing `python.cpython` needs
  `./deploy lock`.
- With PyPy supported the block adds
  `override-dependencies = ["cffi>=1.15.1; implementation_name == 'cpython'"]`: PyPy ships
  cffi built in and uv would otherwise try to build cffi from PyPI.
- Dev group (`pyproject.toml [dependency-groups] dev`): `debugpy`, `mypy` (needs the Rust
  `ast-serialize`: no PyPy wheels), `pyinstaller`, `ruff` and `setuptools` carry
  `implementation_name == 'cpython'`; `.venv-pypy` only gets the app deps plus pytest.
- `PYTHON_JIT` is always exactly `"0"` or `"1"`: CPython only reads the first character
  (`enabled = *env != '0'`), so `false` would ENABLE it. `-E`/`-I` make Python ignore
  `PYTHON*` variables, so launchers that must honour `PYTHON_JIT` use `-s` only.
  `envs.interpreter_info` runs with `-I`: it reports JIT availability, not whether it is on.
- JIT interpreter: uv's Windows CPython builds lack the JIT, so `envs.find_jit_interpreter`
  tries `uv python find` with `only-system`, then `py -X.Y`; `python.jit_interpreter`
  overrides. `doctor` warns when it is scoop's `current` junction (`scoop update` moves it).
- WSL on `/mnt/*` (`project.IS_WSL`): separate `.venv*-wsl` envs and `.build/wsl`, so the
  Windows `.venv` is not turned into a Linux one.
- Unpinned tools: `basedpyright` (`cmd_dev.run_checks`) and `nuitka` (`methods/nuitka.py`)
  run through `uv run --with` and are not in `uv.lock` (planned: pinned through module
  constants).

## 8. Typing profiles and `check`

- Profiles live in `.pytemplate/templates/typing/<p>.toml` (`mypyc`, `strict`, `warn`, `off`).
  Keys: `description`, `blocking`, `skip_mypy`, `[mypy]`, `[mypy_compiled]` (sections for the
  compiled modules), `[pyright]`, `[pyright_compiled].strict`, `[basedpyright_compiled]`,
  `[ruff] select/ignore/exit_zero`, `[vscode]` (merged into settings; its
  `"mypy-type-checker.severity"` also feeds `editor.json` `typing.mypy_severity`).
- `Config.profile_for(backend)`: `mypyc` backend -> `mypyc`; otherwise `typing.relaxed` when
  `profile = "auto"`, else `profile`.
- `cmd_dev.run_checks(cfg, backend)`:
  1. `_profile_file` writes `.build/cfg/ruff-<profile>.toml` and `.build/cfg/mypy-<profile>.ini`
     (the profile of THAT backend, which may differ from the editor's active one).
  2. `ruff check --config ...` (`--exit-zero` when the profile says so).
  3. mypy unless `skip_mypy`; with PyPy supported `render.mypy_cli_args` adds
     `--python-version <min_python> --python-executable <tool python>`. Exit 1 is only a
     warning when the profile is not `blocking`.
  4. `lintc` rules on the compiled sources (when mypyc is supported): errors only under the
     `mypyc` profile, warnings otherwise.
  5. With `typing.editor = "basedpyright"`: basedpyright on `.build/cfg/pyright-<profile>.json`.
- `check all` dedupes by profile (cpython and pypy usually share one).
- `check` covers the app only: `src/` and `tests/` (`project.code_dirs`); `.ruff.toml` excludes
  `.build`, `dist`, `.pytemplate` and `typings`. The runner is checked by `./deploy selftest`
  (mypy --strict), not by `check`.
- Profiles that select `RUF` ignore `RUF001-003` (ambiguous unicode) so app text may be
  non-ASCII; template code stays ASCII anyway.

## 9. mypyc pipeline (`mypyc.py`, `tools/mypyc_build.py`)

- Compile in a COPY of `src/` (`.build/mypyc-{dev,release}/stage`), never in `src/`: a `.pyd`
  next to your `.py` would shadow your edits. The `.py` stays next to the `.pyd` in the stage
  (the extension loader wins) so pyz/portable can fall back to the `.py` on another interpreter.
- Profiles: `dev` (run, test, compile, report) keeps asserts, `debug_level "1"`; `release`
  (build, `compile --release`) strips asserts when `deploy.optimize >= 1`, `debug_level "0"`.
- `sync_tree(src, dst)` copies changed files only and deletes removed ones, never extensions.
  Change detection is size + integer mtime (planned: `st_mtime_ns`): a same-size edit within
  the same second as the previous copy is missed.
- `remove_stale_extensions` keeps only the compiled modules and `<group>__mypyc`.
- `group_name = pkg` gives a stable shared lib `<pkg>__mypyc.<tag>.pyd`; `hidden_imports` and
  `remove_stale_extensions` depend on that name. `compile.separate = true` -> `group_name=None`.
- `spec.json` uses stage-relative `c_dir=../c`, `build_temp=../obj`, `build_lib=../lib`: short
  paths under MSVC's MAX_PATH.
- `tools/mypyc_build.py` runs in `.venv` with `VSLANG=1033`: `chdir(stage)`,
  `mypycify(..., group_name, target_dir=../c)`, then `setup(build_ext --inplace ...
  --parallel N)`. It uses the mypycify API because `python -m mypyc` cannot set
  `strip_asserts`, `group_name` or `multi_file` and always writes to `./build`.
- Output is captured unless `-v`; on failure it is printed, and the compiler-install hint
  (`has_compiler_hint`) is added only when mypy printed no `error: `.
- After the build every compiled module must have an extension, else `DeployError`.
- `./deploy compile [--release]` builds the stage without running it (the VS Code mypyc debug
  config uses it as `preLaunchTask` (planned)).
- `report` builds the annotate HTML with `compile_c=False` (no C compiler needed) plus mypy
  `--any-exprs-report` and `--lineprecision-report` into `.build/reports/`.
- Test-time proof: `./deploy test mypyc` runs pytest with `-o pythonpath=<stage>` (overrides
  pyproject's `pythonpath = ["src"]`) and `PYTEMPLATE_COMPILED`; every preset's
  `tests/conftest.py` (identical copies) raises `UsageError` when a listed module did not load
  from `.pyd/.so`, and skips tests marked `interpreted_only` under mypyc.
- `exe_stage` deletes the compiled `.py` files so PyInstaller/Nuitka can only bundle the binary.
- `hidden_imports`: `imports_of` over the compiled sources (skips `if TYPE_CHECKING:`, resolves
  relative imports and `from pkg import submodule`) plus every extension module, including
  `<pkg>__mypyc`: PyInstaller and Nuitka cannot see imports inside a `.pyd`.
- `lintc` rules (compiled code only): `compile.forbid_imports`; `librt` while PyPy is
  supported; class decorators outside `NATIVE_CLASS_DECORATORS` make the class non-native
  (allowed with `@mypyc_attr(native_class=False)`); nested classes and classes inside
  functions; t-strings; `if __name__` at module level; module-level `__file__` (mypyc#700).
- With PyPy supported user code must be 3.11 syntax and API (no PEP 695;
  `typing_extensions.override`, not `typing.override`). `mode --supports +pypy` prechecks it
  (`cmd_mode._precheck_py311`: ruff `--target-version py311` syntax rules, then the mypy errors
  that appear only at 3.11).

## 10. Build methods (`cmd_build.py`, `methods/*`)

- `cmd_build`: backend (`split_backend`), `--method`, `--onefile/--onedir`, repeated
  `--target`, `--no-check`; unknown flags go to `req.extra` and are appended to the packager
  argv. Default method from `deploy.default`; `COMPAT` rejects exe/nuitka/flet with pypy. Runs
  `run_checks` unless `--no-check`. `payload`: the mypyc release stage, or `sync_tree(SRC,
  .build/payload/<backend>)`.
- Output: `dist_path(req, suffix)` = `dist/<app.name>-<backend>-<method><suffix>`; portable
  with a bundled runtime adds `-<target key>`, flet adds `-<target>`. The CI template hard-codes
  `dist/<NAME>-<BUILD_BACKEND>-pyz/<NAME>.pyz`: it is coupled to `BuildRequest.out_name`.
- Work dirs live under `.build/<name>/<backend>` (`exe-stage`, `pyinstaller`, `flet-pack`,
  `pyz`, `wheel`, `nuitka-stage`, `nuitka`, `flet-build`); portable builds straight into `dist/`.
- Target keys: `^(cp|pp)(\d)(\d+)-(windows|linux|macos)-(x86_64|aarch64)$`
  (`methods.common.KEY_RE`), e.g. `cp314-windows-x86_64`.

Per method:
- **exe** (PyInstaller): `--python-option "X utf8"` (dev parity with `PYTHONUTF8`),
  `--optimize`, `--noupx`, `--clean`, `--log-level=WARN` unless `-v`, `--hidden-import` for
  mypyc, `--add-data "<src>:<dest>"` (`:` is PyInstaller's documented separator). The flet
  preset uses `flet pack` instead (`methods/exe._flet_pack`): it runs from its own cwd
  `.build/flet-pack/<b>` because `flet pack -y` wipes `<cwd>/build` and the distpath; onedir
  uses `--contents-directory=.`; it bundles the Flutter client (plain PyInstaller would
  download ~40 MB on first start). `flet` and `flet-desktop` must share a version, else Flet
  pip-installs `flet-desktop` at runtime, bypassing `uv.lock`.
- **portable**: `dist/<n>-<b>-portable-<key>/` (no `-<key>` with `runtime = "system"`, which
  bundles no interpreter) with `app/`, `lib/` (`uv pip install
  --target`), `runtime/` (pruned copy of the interpreter's `base_prefix` through `\\?\`
  extended paths; the `ignore` callback strips that prefix before comparing), `boot.py`,
  `<n>.cmd` / `<n>.sh`. Prunes `include libs Tools share`, `tcl*`, stdlib `test idlelib
  turtledemo ensurepip site-packages`, tkinter/turtle (unless the AST finds them imported), PyPy
  `hpy/devel`; deletes `EXTERNALLY-MANAGED`; copies `vcruntime140*.dll` from the CPython base
  into PyPy runtimes on Windows (PyPy's zip lacks them). `.cmd` = ASCII + CRLF (`start ""` +
  `pythonw.exe` for GUI apps), `.sh` = 0755; both use `-s` plus `-O`/`-OO`, never `-I`/`-E`.
  `compileall` runs with the console `python.exe` and also compiles PyPy's stdlib (PyPy ships
  no `.pyc`). mypyc builds are smoke-tested (`_smoke_compiled`, with `lib/` on `sys.path`
  (planned)). `runtime = "system"` launchers probe the minimum Python version (planned) and the
  POSIX launcher quotes `[deploy.portable] env` values with `shlex.quote` (planned).
- **pyz**: Python cannot import `.pyd/.so` from a zip, so `__main__.py` extracts to a per-build
  cache (`%LOCALAPPDATA%` / `~/Library/Caches` / `$XDG_CACHE_HOME`, then
  `<name>/pyz/<build_id>/<key|pure>/`), guarded by a `.complete` marker and an atomic
  `os.replace`; the 3 newest builds are kept. `common/` never holds extensions (runtime `bug:`
  check). The mypyc overlay `targets/<host>/app` is the FULL package (`.py` + `.pyd` +
  `__init__.py`): a partial overlay would be a namespace-package trap where `common`'s `.py`
  wins. `zipapp compressed=True` = deflate, never zstd: it must open on 3.11 and PyPy.
  Cross-target deps use `uv pip install --target --python-platform --python-version
  --only-binary :all:` (an sdist built for another OS would produce host binaries); PyPy
  targets are host-only; `_virtualenv*` junk is removed. The `<n>.cmd` wrapper actually runs
  each candidate interpreter to check the minimum version (`py` may exist with no Python
  registered), sets `PYTHONUTF8=1` and `PYTHON_JIT` (planned). `pyz-merge` takes `common/` and
  `__main__.py` from the first part and `targets/` from all parts; app names must match; the
  `build_id` is recomputed.
- **wheel**: synthetic build project in `.build/wheel/<b>` (`setuptools>=84`; for mypyc
  `mypy==<version locked in uv.lock>` in `build-system.requires`, a `setup.py` using mypycify
  and a compile `mypy.ini`). Assets go into `<pkg>/assets` (package data). Needs network
  (isolated build env). mypyc -> platform wheel; cpython/pypy -> `py3-none-any`.
- **nuitka**: `.build/nuitka-stage/<b>`, `uv run --locked --with nuitka python -m nuitka` with
  cwd = stage; `--include-package=<pkg>`, `--include-module` for mypyc hidden imports,
  `--python-flag=no_asserts/no_docstrings` from `optimize`. Output found by file-name prefix or
  `.dist` suffix.
- **flet** (`flet build`): requires `app.preset == "flet"`. Windows needs Developer Mode
  (Flutter symlinks; checked in the registry) and Visual Studio C++. The stage
  `.build/flet-build/<b>` is persistent (Flutter cache). `flet build` ignores `uv.lock`, so the
  generated pyproject pins `uv export --no-hashes` versions; it slices the project
  `pyproject.toml` from `[tool.flet]` up to the preset end marker, so a user table placed after
  `[tool.flet]` leaks into it. Mobile/web targets (`apk aab ipa ios-simulator web`) cannot load
  extensions: a mypyc backend ships the `.py`. Desktop embeds CPython 3.14, so cp314 `.pyd`
  files work.
- Assets at runtime: `resources.assets_dir()` (raylib and flet presets) tries
  `$PYTEMPLATE_ASSETS`, then `sys._MEIPASS/assets`, then `<pkg>/assets` (wheel), then
  `src/assets`. Call it inside functions (module-level `__file__` is broken when compiled).

## 11. Presets (`presets.py`, `.pytemplate/presets/<p>/`)

- `preset.toml`: `description`, `dependencies` / `dev_dependencies` (with `{option}`),
  `[options]` (defaults of `[preset.<p>]`), optional `[uv]` (extra managed `[tool.uv]` keys),
  optional `pyproject` string (extra tables, with `{{name}}`/`{{pkg}}`).
- `files/`: complete skeleton, including a full `pytemplate.toml` (`init` overwrites the root
  one), `src/main.py`, `src/__pkg__/core/` (compiled) + a boundary, `tests/conftest.py`
  (identical in every preset), optional `typings/` and tools.
- Four templating syntaxes coexist: `__pkg__` in paths and `{{name}}`/`{{pkg}}` in text (plain
  `.replace`; `f"{{name}}: ..."` in `script/app.py` renders to the app name on purpose);
  `{option}` in `preset.toml` deps and `[uv]` (`str.format_map`); `__HEADER__`-style in
  `ci.yml`; `{root}`-style in `[tasks]` (`format_map`, double literal braces).
- `presets.TEXT_SUFFIXES` decides which files get token replacement; extension-less or binary
  files must be copied byte for byte (planned: decode-or-copy).
- `pristine(cfg)`: `src/`, `tests/`, `typings/` equal the current preset skeleton rendered
  with the current name (CRLF-normalised). `init` refuses otherwise (use `--force`). `init`
  deletes `src/ tests/ typings/`, writes every skeleton file (including root
  `pytemplate.toml`, not covered by the pristine check), sets `project.name`, replaces the
  preset tables, `write_pyproject`, `uv remove/add --no-sync` for the dependency diff, `uv
  lock`, `render.apply(force=True)`.
- **[template repo]** Root `src/`, `tests/` and `pytemplate.toml` must equal
  `presets/script/files` rendered with `name = "myapp"` (verified: `presets.pristine` is
  true). Edit the preset, then regenerate the root with `./deploy init script --name myapp
  --force`, or mirror the edit byte for byte.
- `copy_template(dest)` (used by `new`) skips `.git`, `.build`, `dist`, caches, `.flet`,
  `.venv*`, `template-repo` at any depth; `build/` and `.claude/` at the root; and
  `.github/workflows/template-*` (template CI files MUST use that prefix). `new` then runs
  `init <preset> --name <n> --force` inside the copy, `git init` and
  `git add --chmod=+x deploy` (only `deploy`; `deploy.ps1` keeps the filesystem mode).
- Hard-coded preset names in the runner: `render.ci_workflow` (raylib: apt GL/X11 libs, no
  PyPy on macOS), `methods/exe.build` (flet -> `flet pack`), `methods/flet.build` (flet only).
  A new preset that needs special packaging must touch these.
- raylib: the upstream stub lies (returns/fields/params declared `bytes`/`list` that are cdata
  or int at runtime); mypyc checks simple types at runtime, so they raise `TypeError` only when
  compiled. `tools/raylib_stubs.py` regenerates `typings/raylib/__init__.pyi` (task `stubs`).
  `typings/` is picked up by `render.typings_dir` (mypy `mypy_path`, pyright `stubPath`, ruff
  `extend-exclude`, `presets.OWNED_DIRS`). Also: `[[typing.mypy_overrides]] raylib
  ignore_errors`, `no-build-package = ["raylib"]` via `[uv]`, `forbid_imports = ["pyray"]`
  (~7x slower), exe `extra_args` exclude setuptools/pycparser/_distutils_hack, PyPy is the
  default active backend.
- flet: measured with Flet 1.0.1 + mypyc 2.3.1 in compiled code: `async` handlers get no event,
  generator handlers never run, `@ft.component` fails at import, `@ft.control` loses its event
  types; hence `forbid_imports = flet, flet_desktop, flet_cli`. Heavy work runs in a
  `ProcessPoolExecutor` (compiled code does not release the GIL). mypy overrides relax
  `{pkg}.ui.*`. Wheel entry `{pkg}.ui.app:run`. Task `dev` (`flet run -d -r`) has
  `background = true`.
- Editor buttons per preset come from each preset's `pytemplate.toml` `[vscode] buttons`
  (script `run test check build`, raylib `run bunnymark test build`, flet `dev run test
  build`).

## 12. Editors

Editor generators are `editors/<name>.py` with `outputs(cfg, profile) -> dict[str, str]`
(path relative to ROOT -> content), merged by `render.outputs`. Their content must be
deterministic, LF, free of absolute or machine-specific paths (it is committed), and hashed
like every generated file.

### 12.1 VS Code (`editors/vscode.py`)

- `settings.json` = `.pytemplate/templates/vscode/settings.json` + the profile's `[vscode]` +
  `[vscode] settings` (later wins). `extensions.json`: Python, Pylance or basedpyright (then
  Pylance is unwanted), debugpy, mypy type checker, Ruff, Even Better TOML, and
  `actboy168.tasks` (planned).
- `tasks.json`: every task is `"type": "process"`, never `"shell"`: shell tasks go through the
  user's terminal profile (xonsh, niubash, MSYS2) and break; process tasks run the launcher
  directly. Default `"command": "/bin/sh"`, `"args": ["${workspaceFolder}/deploy", ...]` (no
  exec bit needed) and `"windows": {"command": "${workspaceFolder}\\deploy.cmd", "args":
  [...]}` (planned; today the default command is `${workspaceFolder}/deploy`). In `version
  2.0.0` per-OS `args` REPLACE the default ones, so both blocks carry the full args. VS Code
  applies the block of the OS where the task runs (the remote OS under WSL/SSH).
- Catalog (planned): `run`, `run <b>` per other backend, `test` (default test task),
  `test <b>`, `test all`, `check`, `check all`, `build` (default build task), `report --open`
  (mypyc), `compile` (mypyc, hidden), `lint --fix`, `fmt`, `doctor`, `setup`, and one task per
  `[tasks]` entry. Labels are `deploy: <args>`; each task has `detail`, `icon`, a presentation
  preset; run-like tasks use `instanceLimit 1` + `terminateOldest`.
- Problem matchers (planned) are inlined in each task (VS Code only lets extensions define
  named matchers). They depend on exact tool output: ruff `concise` (the task sets
  `RUFF_OUTPUT_FORMAT=concise`; ruff's default `full` format is multi-line); mypy
  `path:line[:col]: error|note: msg  [code]` (backslash paths on Windows; "See https://..."
  notes skipped); mypyc errors are stage-relative (`mypyc_build.py` chdirs into the stage) and
  mapped to `${workspaceFolder}/src`; runner rules `error: src/...:N: msg` /
  `warning: src/...:N: msg`; pytest crash lines; basedpyright. Severity follows the task
  backend's profile (`blocking`, ruff `exit_zero`). Every regex must work in JavaScript AND
  Python `re` (tests compile them). Changing `ui.error`/`ui.warn` prefixes, `lintc.Finding`
  or the tools' formats breaks them.
- Buttons: tasks whose args are listed in `[vscode] buttons` get `options.statusbar`
  (read by `actboy168.tasks`); `tasks.statusbar.default.hide: true` hides the rest (planned).
  Without the extension the extra keys are ignored.
- `launch.json`: CPython `src/main.py` on the selected interpreter (works under WSL), PyPy
  (only when supported; per-OS `python`; the debugger is unreliable on PyPy), pytest. Planned:
  "mypyc stage" (program `.build/mypyc-dev/stage/main.py`, `.venv` or `.venv-jit` python,
  `preLaunchTask: "deploy: compile"`, `pathMappings` src <-> stage, `PYTEMPLATE_BACKEND=mypyc`;
  compiled modules never stop at breakpoints) and "CPython JIT" (`.venv-jit`,
  `PYTHON_JIT=1`) when `python.jit`.
- `terminal.integrated.automationProfile.windows` = cmd.exe, `.linux`/`.osx` = `/bin/sh`
  (planned): the debugger's `runInTerminal` picks its quoting from the shell's name, and
  anything that is not powershell/pwsh/cmd/bash (xonsh, `niu.exe`) gets cmd syntax, so F5
  breaks. It applies only in trusted workspaces; `[vscode] settings` can override it.
- Limits: Ctrl+C in a Windows task triggers cmd's "Terminate batch job (Y/N)?" (use Terminate
  Task); under Remote-WSL on a Windows checkout the fixed paths `.venv-pypy`,
  `.build/mypyc-dev` ignore the `-wsl` suffix and `.build/wsl`; `\\wsl$` UNC paths do not work
  with `deploy.cmd`.

### 12.2 LazyVim / Neovim (`editors/nvim.py`, `.pytemplate/nvim/`, `cmd_nvim.py`)

Files:
- `.lazy.lua` (generated from `.pytemplate/templates/nvim/lazy.lua`): lazy.nvim's `local_spec`
  loads the first `.lazy.lua` found upward from Neovim's cwd, through `vim.secure.read` +
  `loadstring`. It MUST stay static (identical bytes in every mode and preset): Neovim trusts
  it by the sha256 of its raw bytes, keyed by its real path, in `stdpath('state')/trust`. Any
  byte change (CRLF, a BOM, an edit) or moving the folder = untrusted again. Hence
  `.gitattributes` `.lazy.lua text eol=lf` and all logic in the plugin. Editing the template
  forces every user to re-trust: avoid it.
- `loadstring` gives the chunk no path: the root is found with
  `vim.fs.root(vim.uv.cwd(), "pytemplate.toml")`.
- Trusting `.lazy.lua` also trusts `.pytemplate/nvim/**`, loaded as a local plugin
  (`{ dir = root .. "/.pytemplate/nvim", name = "pytemplate.nvim" }`) and never re-hashed.
- `.pytemplate/editor.json` (`editors/nvim.editor_data`, schema 1): data only, relative paths
  only, no comments. Keys today: `schema`, `generated`, `name`, `pkg`, `preset`, `gui`,
  `min_python`, `backend{active, supported}`, `typing{profile, editor, mypy, mypy_severity}`,
  `envs{tools, cpython, mypyc, pypy}` (without the `-wsl` suffix; `.venv-jit` for
  cpython/mypyc when `python.jit`), `mypyc_stage`, `tasks[{name, help, background}]`; more
  keys planned (e.g. `commands`). The Lua side must validate every value (whitelists,
  patterns) and never run a program named in it. It is refreshed only when `./deploy` runs
  (render-on-save of `pytemplate.toml` covers edits made in Neovim (planned)).
- Plugin (`.pytemplate/nvim/lua/pytemplate/`): today `init.lua` (`setup`, `root`); planned:
  `info` (editor.json, re-read on mtime change), uv lookup, `deploy_cmd`, `tasks.lua`
  (runner, output parser, pickers, keymaps, `:Deploy`), `dap.lua`, `integrations.lua`
  (neotest, nvim-lint, lspconfig), `health.lua` (`:checkhealth pytemplate`),
  `lua/overseer/template/pytemplate.lua` and `lua/overseer/component/pytemplate/refresh.lua`.

Runner contract from Lua (planned): argv `{<absolute uv>, "run", "--quiet", "--script",
<root>/.pytemplate/deploy.py, ...}` through `vim.system`/`jobstart` with a LIST, env
`PYTEMPLATE_CALLER_CWD=<cwd>` and `PYTEMPLATE_LAUNCHER=nvim`. Never a string command (it would
go through `'shell'`, which may be xonsh or niubash) and never `deploy.cmd` unless uv is
nowhere (the launcher prints the install hints). uv lookup mirrors the launchers because a GUI
or MSYS2-login Neovim may have a minimal PATH.

LazyVim wiring (planned unless noted):
- Extras imported by `.lazy.lua`: `lazyvim.plugins.extras.lang.python`, `.lang.toml`,
  `.dap.core`, `.test.core`, `.editor.overseer`. The local spec is appended AFTER the user's
  spec, so extras not already enabled trip LazyVim's import-order check: `.lazy.lua` sets
  `vim.g.lazyvim_check_order = false` only then; the permanent fix is `./deploy nvim extras`.
  Imports are de-duplicated by module name.
- `vim.g.lazyvim_python_lsp` set from `.lazy.lua` is too late when `lang.python` is already
  enabled (read at first import): switch servers by setting `opts.servers.<x>.enabled` in an
  lspconfig `opts` function (runs last). basedpyright by default (no Node.js; from `.venv`,
  else `uvx --from basedpyright basedpyright-langserver --stdio`, else Mason);
  `vim.g.pytemplate_python_lsp = "pyright"` switches (Mason's pyright needs Node.js). Pylance
  exists only in VS Code. pyright/basedpyright find `.venv` through `pyrightconfig.json`
  `venvPath`/`venv`.
- ruff server from `.venv` with `mason = false` (same version as `./deploy check`). Mason
  prepends its bin dir to PATH after `.lazy.lua` runs, so always use absolute `.venv` paths.
- mypy (nvim-lint, core in LazyVim): `cwd = root` (finds `.mypy.ini`; mypy then prints paths
  relative to it, the only form the parser matches), `--python-version <min_python>
  --python-executable <.venv python>` when PyPy is supported (like `render.mypy_cli_args`),
  severity from `typing.mypy_severity`, disabled with the `off` profile. On Windows nvim-lint
  wraps every linter in `cmd.exe /C`: a quoted absolute path breaks when the root contains
  spaces or `& ^ % ( ) !`.
- dap: nvim-dap spawns adapters with raw `uv.spawn` (no PATHEXT), so Mason's `.cmd` shims fail
  on Windows: give nvim-dap-python the absolute `.venv` python (debugpy is in the dev group),
  else Mason's debugpy venv python, else an ephemeral `uv run --with debugpy` adapter. nvim-dap
  reads `<cwd>/.vscode/launch.json` (per-OS blocks lifted, JSONC accepted) and expands
  `${workspaceFolder}` to the cwd: a provider covers a cwd below the root.
- neotest-python: always set `python` explicitly (auto-detection globs `*/pyvenv.cfg`, gets
  two lines with `.venv` + `.venv-pypy` and builds a broken path; its `uv run` fallback also
  syncs); `discovery.filter_dir` skips `.venv*`, `.build`, `dist`, `typings`, dot-dirs.
  neotest cannot pass `-o pythonpath=<stage>` or `PYTEMPLATE_*`, so mypyc/pypy/all runs go
  through the `deploy: test` task.
- overseer: the pytemplate provider replaces the `.vscode/tasks.json` one
  (`disable_template_modules = {"overseer.template.vscode"}`), otherwise labels would be
  duplicated and tasks would go through `deploy.cmd`. overseer runs `"type": "shell"` tasks
  through `'shell'`: another reason to keep VS Code tasks `process`.
- Keymaps under `<leader>j` (which-key group "deploy", `vim.g.pytemplate_prefix` overrides),
  `:Deploy ARGS` with completion, render-on-save of `pytemplate.toml`. `<leader>j` was chosen
  because no LazyVim core or extra mapping uses it.

`./deploy nvim [doctor|trust|extras|bootstrap|sync]` (`cmd_nvim.cmd_nvim`, planned):
- Neovim's directories come from one headless query, never hard-coded:
  `nvim --headless --clean -n -i NONE -c "lua io.stdout:write(vim.json.encode(...stdpath...))"
  -c "qa!"` (respects `NVIM_APPNAME` and `XDG_*`). On Windows `XDG_CONFIG_HOME=X` gives
  `X\nvim` and `XDG_DATA_HOME`/`XDG_STATE_HOME=X` give `X\nvim-data`.
- Trust DB `<state>/trust`: lines `<sha256> <path>`, path = real path (backslashes on Windows;
  compare case-insensitively), hash over raw bytes. `vim.secure.trust({action = "allow", path =
  ...})` needs Neovim >= 0.12 (0.11 only accepts `bufnr`) and the state dir must exist first
  (it opens the DB with `io.open(..., "w")`). Neovim 0.12 has no "allow" button: the user picks
  (v)iew, runs `:trust` and restarts (lazy.nvim already skipped the file for that session).
- `extras` edits the user's `lazyvim.json` only when asked, with a backup, keeping its shape;
  if it does not exist the user starts Neovim once instead. `bootstrap` clones the LazyVim
  starter only when the config dir does not exist. `sync` = `nvim --headless "+Lazy! sync" +qa`
  with cwd = ROOT, after `trust`.
- `cmd_nvim.doctor(check)` (from `./deploy doctor`): silent without `nvim`; at most one
  headless call.
- Only started inside the project: `nvim path/x.py` from elsewhere, or a later `:cd`, does not
  load `.lazy.lua`. `.lazy.lua` edits need a restart (the watcher ignores it).
- The user can disable `local_spec`; `nvim doctor` detects it.

Headless and test gotchas: `VeryLazy` never fires in `--headless` (run `-c "doautocmd
UIEnter"`); an untrusted `.lazy.lua` blocks a headless run on `confirm()` (pre-trust through
the API); when `NVIM_LOG_FILE` cannot be written Neovim drops `nvim.log` into the cwd (always
set it); headless `jobstart` with a pty on Windows loses the output (use
`strategy = {"jobstart", use_terminal = false}`); Treesitter/Mason installs are asynchronous
and may log errors that do not matter. NEVER touch the user's real Neovim dirs in tests: set
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `NVIM_LOG_FILE` to a
short temp tree and unset `NVIM_APPNAME`.

## 13. Tests and verification

- `./deploy selftest` (`cli.cmd_selftest`): `python -m pytest -q -p no:cacheprovider
  .pytemplate/tests [args...]` in `.venv`, then `mypy --strict --no-incremental
  --python-version 3.11 --config-file .pytemplate/tests/mypy-runner.ini .pytemplate/runner
  .pytemplate/deploy.py`. Both must pass. Needs `.venv` (`./deploy setup`).
- `.pytemplate/tests/`: `test_runner.py` (config, render, lintc, imports, target keys),
  `test_no_spanish.py`, plus (planned) `test_launcher_sh.py`, `test_launcher_win.py`,
  `test_paths.py`, `test_shells.py`, `test_vscode.py`, `test_nvim_render.py`,
  `test_cmd_nvim.py`, `test_fixes.py`, `test_e2e_plan.py`.
- **[template repo]** Language guard `test_no_spanish.py`: skipped unless
  `.pytemplate/template-repo` exists. Scans `git ls-files --cached --others --exclude-standard`
  (so new untracked files count) for accented Spanish letters and a list of Spanish words
  (including the template's former default app name). Add `lang: allow` to a line to allow it
  on purpose.
- Test rules: tests that need a missing tool or shell must skip cleanly (they also run on
  Linux/macOS CI). Tests that spawn `./deploy` must scrub `UV`, `VIRTUAL_ENV`,
  `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON` and `PYTEMPLATE_*` from the child env (pytest itself
  runs under `uv run`). Keep them fast: a runner start costs ~0.3 s.
- `test_runner.py` matches message substrings (`unknown key 'backend.mode'`, `boolean`,
  `is not in backend.supported`, `exact version`, `clashes with`, and the lintc texts `flet`,
  `@cache`, `nested class`, `__file__`, `__main__`): rewording those messages means updating
  the tests in the same commit.
- `./deploy selftest --shells`: section 4.9 (planned).
- `./deploy selftest --nvim [PRESET,...] [--keep] [--fresh] [--require]` (`nvimtest.selftest`,
  planned): isolated LazyVim (starter cloned and synced once under a short temp dir, reused
  unless `--fresh`), then per preset: `./deploy new`, `./deploy sync cpython`, trust through the
  API, `Lazy! sync` from the project, `nvim --headless -c "doautocmd UIEnter" -c "luafile
  .pytemplate/nvim/tests/smoke.lua"` with `PT_ROOT=<project>`. `smoke.lua` prints one
  `ok   <name>` / `FAIL <name>` / `SKIP <name>` line per check and exits with `qa!`/`cq!`.
  Skips (exit 0) without `nvim`/`git` unless `--require`.
- `./deploy selftest --e2e [PRESET,...] [--backends ..] [--methods ..] [--quick] [--full]
  [--keep] [--json] [--base DIR]` (`e2e.selftest`, planned): `./deploy new` per preset, then
  setup, doctor, check all, test all, run, and `build` for every compatible backend/method
  pair; PASS/FAIL/SKIP table with timings; logs kept on failure. `flet build` is SKIP unless
  Flutter + Developer Mode are available.
- `./deploy render --check` (exit 1 when something is outdated or hand-edited) and
  `./deploy doctor`.
- Manual end-to-end: `./deploy new C:\t\p1 --preset <p>`, then in the copy `./deploy setup`,
  `test all`, `check all`, `build <b> --method <m>`. Keep the path short.
- CI: `.github/workflows/ci.yml` is generated for every project (`render.ci_workflow` from
  `templates/ci.yml`: placeholders `__HEADER__`, `__MATRIX__`, `__LINUX_DEPS__` (its line must
  exist exactly), `__NAME__`, `__BUILD_BACKEND__`; action majors pinned: bump deliberately;
  deleting the template disables CI generation). **[template repo]**
  `template-launchers.yml`, `template-nvim.yml`, `template-e2e.yml` (planned) test the template
  itself and are not copied by `./deploy new`.
- Coverage limits: Linux/macOS code paths run only in CI; `flet build` is untested locally
  (Developer Mode off, no Flutter); WSL is untested (no distro installed); Cygwin, busybox-w32,
  zsh and macOS bash 3.2 are covered only by the launcher CI (planned).

## 14. Conventions and recipes

Runner code:
- stdlib only; Python 3.11 syntax and APIs (`tomllib` is why the floor is 3.11); `mypy
  --strict` clean on every OS (guard Windows-only APIs such as `ctypes.windll` with
  `sys.platform == "win32"`, import `winreg` lazily inside `try`). Match the neighbouring
  modules' style (`from __future__ import annotations`, module docstring, typed signatures).
- Output through `ui` (stderr), except output meant for pipes (5.3).
- Expected failures raise `DeployError(msg, 2)` (usage/config) or `DeployError(msg, 3)`
  (missing requirement), with an actionable hint in the message.
- Processes only through `proc.run/output` or `envs.uv/uv_run`, as argv lists, never
  `shell=True`. `proc.run` defaults to cwd = ROOT and `proc.base_env()`.
- Text files: `encoding="utf-8", newline="\n"`; write `"\ufeff"`, never a literal BOM; read
  `pytemplate.toml` as `utf-8-sig`; parse Python sources as bytes (`imports.parse`).
  Generated `.cmd` files: ASCII, explicit `\r\n`, written with `newline=""`.
- Project paths from `project.*` (never the cwd); user-typed paths through
  `project.user_path`.
- Honour `proc.DRY_RUN` for side effects.
- Never design CLI syntax that needs `--`, empty-string arguments or cmd metacharacters
  (PowerShell and cmd mangle them).

Adding a command:
1. `Command(module, func, summary, usage, render, group)` in `cli.COMMANDS`; `render=False` if
   it must work without (or before) rendering.
2. `def cmd_x(cfg: Config, args: list[str]) -> int` in a `cmd_*.py`, argparse with
   `prog="./deploy x"`; reject unknown arguments.
3. Tests in `.pytemplate/tests/`; README commands table; VS Code task catalog
   (`editors/vscode.py`) and the Neovim command list (plugin `tasks.lua` / `editor.json`
   (planned)) if editors should offer it. The xonsh completion reads `cli.COMMANDS` (planned).

Adding a build method:
1. `methods/<m>.py` with `build(req: BuildRequest) -> Path`, output via
   `dist_path(req, suffix)`, work dirs under `BUILD/<m>/<backend>`.
2. Add it to `config.METHODS` and `cmd_build.COMPAT` (a reason string per unsupported
   backend); a `[deploy.<m>]` dataclass in `DeployConfig` plus `validate` rules.
3. For mypyc use `mypyc.exe_stage` and `mypyc.hidden_imports`.
4. README matrix; `templates/ci.yml` if the output naming changes.

Adding a preset:
1. `presets/<p>/preset.toml` and `files/` (section 11), including `[vscode] buttons` and
   `[tasks]` (`background = true` for dev servers) in its `pytemplate.toml`.
2. Copy `tests/conftest.py` verbatim. Check the hard-coded preset branches (section 11).
3. It must pass `./deploy selftest --e2e <p>` (planned).

Adding a typing profile:
1. `templates/typing/<p>.toml` with the keys of section 8.
2. Extend `config.PROFILES`, the allowed `typing.relaxed` values, the `cmd_mode` `--typing`
   choices and the auto/relaxed mapping.

Adding an editor: `editors/<name>.py` with `outputs(cfg, profile)`, merged in `render.outputs`
(section 12); a doctor hook called from `cmd_env.cmd_doctor` if it needs checks; tests that
render it for several configs; README section.

Files and git:
- `.gitattributes`: `* text=auto eol=native`; `*.bat`, `*.cmd`, `*.ps1` CRLF; `*.sh` LF; then
  `deploy`, `deploy.ps1` and `.lazy.lua` LF (the last matching line wins); `*.png *.ico *.pyz`
  binary. With `core.autocrlf=true` most working-tree files are CRLF on Windows: that is fine,
  the runner normalises.
- `deploy` is 100755 (`cmd_env._fix_exec_bit` repairs it on `setup`; `core.filemode=false`
  on Windows loses it); `deploy.ps1` is 100755 too (planned).
- Default app content lives in `presets/script/files/` (section 11).

## 15. Known issues and fragile points (still open)

- `cmd_dev.split_backend` treats a first argument equal to `cpython`, `pypy` or `mypyc` (and
  `all` for `test`/`check`) as the backend: an app argument with that value must be preceded
  by an explicit backend (`./deploy run cpython mypyc`).
- Tasks: a task with `backend = "mypyc"` runs interpreted in `.venv` unless it goes through a
  `deps` entry such as `run`. `tasks._placeholders` resolves `{python}` (and so the JIT
  lookup) eagerly (planned: lazily).
- `config.set_value` only edits single-line entries.
- `mode --jit off` and `mode --supports -pypy` leave `.venv-jit` / `.venv-pypy` behind;
  `clean --envs` removes every `.venv*` (planned: a hint).
- WSL on `/mnt`: `pyrightconfig.json` (`venv: ".venv"`) and `launch.json` ignore
  `ENV_SUFFIX`, so editors inside WSL point at the Windows venv. Untested.
- `methods/flet`: user tables after `[tool.flet]` leak into the `flet build` pyproject; the
  `FLET_*` env passthrough is redundant (`base_env` already copies `os.environ`).
- `uv build` drops a `.gitignore` into `dist/<n>-<b>-wheel/`.
- `presets.new` marks only `deploy` executable in the new repository.
- VS Code problem matchers and the Neovim parser depend on tool output formats (ruff, mypy,
  pytest, basedpyright) that can change between tool versions.
- actboy168.tasks is a small third-party extension (disabled in Restricted Mode).
- Stopping `deploy dev` (flet hot reload) from Neovim on Windows relies on ConPTY closing and
  uv's job object killing `flet.exe`: not verified.
