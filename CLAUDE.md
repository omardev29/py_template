# CLAUDE.md: py_template for coding agents

Technical map of this project: architecture, invariants, workarounds and fragile points. Read
the section you need before touching a file. Cite code by symbol (`render.apply`,
`envs.env_vars`), never by line number.

- This file is copied into every project made with `./deploy new`. Items tagged
  **[template repo]** only apply to the template repository itself, which is recognised by the
  marker file `.pytemplate/template-repo` (`./deploy new` does not copy it).
- `<pkg>` is the app package: `app.name` in snake_case (`myapp` in the template repo).

## 1. Ground rules

1. Drive everything through `./deploy` (`./deploy help`, `./deploy help COMMAND`). It sets the
   environment variables uv, mypy and mypyc need (sections 5.5 and 7). A bare `uv run`, `mypy` or
   `pytest` in the project can pick the wrong interpreter or silently recreate `.venv`.
2. Never hand-edit generated files (section 6.2). Change `pytemplate.toml`,
   `.pytemplate/templates/**` or the generator, run `./deploy render`, and commit the
   regenerated files together with the change.
3. The runner (`.pytemplate/runner/`) is stdlib only, Python 3.11 compatible and
   `mypy --strict` clean on linux, darwin and win32. Coding rules: section 14.
4. Verify with `./deploy selftest` (must pass), `./deploy render --check` and `./deploy doctor`.
   `selftest` needs `.venv`: run `./deploy setup` once.
5. Everything written into the repository is English: code, comments, messages, docs, preset
   UI strings, TOML comments, commit messages and PR text (**[template repo]** enforced by the
   language guard, section 13.1). Avoid non-ASCII: write `...` and `->`. Launchers and
   generated `.cmd` files must be pure ASCII. Talk to the owner in the language of their
   prompt (Spanish so far); only the repository content is English.
6. Git: never edit git config (the machine may have no global identity). Commit with
   `git -c user.name="..." -c user.email="..." commit`, copying the identity of the existing
   commits (`git log -1 --format='%an <%ae>'`). Keep `deploy` AND `deploy.ps1` at mode 100755
   (`git ls-files -s deploy deploy.ps1`; fix with `git update-index --chmod=+x <file>`;
   `./deploy setup` repairs both).
7. Keep scratch paths short (`C:\t\p1`, `%TEMP%\pt\...`): with `LongPathsEnabled=0` deep paths
   break MSVC (mypyc), PyPy runtime copies, `compileall` and the Flet client extraction.
8. Never touch user-global state from tests or tools: shell rc files, the user's Neovim
   config/data/state, PATH, the registry, installed programs.
9. Calling `./deploy` from an agent on Windows: a Git Bash tool runs `deploy` (sh); a
   PowerShell tool resolves `./deploy` to `deploy.ps1`; in cmd, Claude Code sessions set
   `NoDefaultCurrentDirectoryInExePath=1`, so type `.\deploy.cmd`, never a bare `deploy`.

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
.pytemplate/nvim/                 local Neovim plugin pytemplate.nvim (lua/, tests/smoke.lua,
                                  README.md: setup, keymaps, options)
.pytemplate/tests/                runner tests (pytest) + mypy-runner.ini
.pytemplate/editor.json           generated data file for the Neovim plugin
.pytemplate/state.json            hashes of the generated files (committed)
.pytemplate/template-repo         [template repo] marker, not copied by ./deploy new
.github/workflows/ci.yml          generated CI of the project
.github/workflows/template-*.yml  [template repo] launchers, nvim, e2e CI; not copied
ignored: .venv*/ .build/ dist/ build/ *.spec *.pyd *.so .flet/ tool caches,
         .claude/worktrees/ .claude/settings.local.json
```

## 4. Launchers and shells

### 4.1 Contract

The three launchers `deploy`, `deploy.cmd`, `deploy.ps1` and the Neovim plugin (section 12.2)
implement the same contract: change them together. The `nu` and `xonsh` snippets of
`shell-setup` (section 4.9) also call uv directly.

1. Find the project root: the directory that holds `.pytemplate/deploy.py`, first from the
   launcher's own location, else by walking up from the current directory.
2. Find uv (order below).
3. Export `PYTEMPLATE_CALLER_CWD` (the caller's cwd; `C:\...` form with an upper-case drive on
   Windows) and `PYTEMPLATE_LAUNCHER`.
4. Run `uv run --quiet --script <root>/.pytemplate/deploy.py ARGS...` with argv untouched and
   propagate its exit code. Launchers never `cd`; besides the uv search and the install
   hints they contain no logic.

Launcher exit codes: 2 = no project found, 127 = uv not found (after the install hints),
126 = `deploy.ps1` could not start uv; anything else comes from the runner.

uv search order (the same in all three launchers and the plugin, `init.uv_candidates`):
`$UV` (must be a file) -> PATH (`deploy`: `command -v uv` accepted only when it prints a
path, which rejects aliases and functions; `deploy.ps1`: `Get-Command -CommandType
Application`, only a real `uv.exe` on Windows) -> `UV_INSTALL_DIR[/bin]`, `XDG_BIN_HOME`,
`XDG_DATA_HOME/../bin`, `~/.local/bin`, `CARGO_HOME/bin`, `~/.cargo/bin` -> Windows: WinGet
`Links` and `Packages/astral-sh.uv_*` (`LOCALAPPDATA`, then `ProgramFiles`), scoop shims
(`SCOOP`, `~/scoop`, `SCOOP_GLOBAL`, `ProgramData/scoop`), chocolatey, and last the user and
machine `Path` stored in the registry (a console opened before uv was installed; not in the
plugin) -> POSIX: `/opt/homebrew/bin`, `/usr/local/bin`, linuxbrew, `~/.nix-profile/bin` (the
plugin also tries the nix default profile, `/run/current-system/sw/bin` and `/usr/bin`). On
Windows the sh launcher uses `USERPROFILE` as home.

Install hints: Windows prints the PowerShell installer, winget and scoop (never curl); POSIX
prints curl, brew and pipx. `deploy` (stdin and stderr are TTYs) and `deploy.ps1`
(`UserInteractive`, stdin not redirected) offer to run the official installer when `CI` is
unset; `deploy.cmd` never prompts.

uv exports `UV` (its own path) to everything it starts, so `proc.find_uv` checks `$UV` first,
then `shutil.which("uv")`, else `DeployError(..., 3)`.

`PYTEMPLATE_LAUNCHER` values: `sh`, `sh:bash`, `sh:zsh`, `sh:niubash`, each with `:msys` (when
`/usr/bin/msys-2.0.dll` exists) or `:cygwin` (`cygwin1.dll`) appended (`sh:bash:msys`,
`sh:msys` for dash under MSYS2); `cmd`; `ps1:<PSEdition>:<major>.<minor>` (`ps1:Core:7.6`,
`ps1:Desktop:5.1`); `nvim`; `nu` (shell-setup snippet). The xonsh snippet sets none.
`project.native_path` reads the `:msys`/`:cygwin` suffix, `shells.guess_shell` the prefix, and
`./deploy doctor` prints the value ("unknown" when unset).

### 4.2 Which launcher runs

| Caller | Launcher | Why |
|---|---|---|
| sh, bash, zsh, dash, ksh, busybox (Linux, macOS, WSL) | `deploy` | POSIX sh |
| Git Bash, MSYS2 (any MSYSTEM, login or not), Cygwin, busybox-w32, niubash | `deploy` | POSIX sh |
| cmd | `deploy.cmd` | PATHEXT; type `.\deploy` (see rule 1.9) |
| xonsh on Windows, nushell, Python `subprocess`, VS Code tasks on Windows | `deploy.cmd` | can only start PATHEXT files; `./deploy` resolves to `deploy.cmd` |
| PowerShell 7, Windows PowerShell 5.1 | `deploy.ps1` | `./deploy` resolves to `deploy.ps1` in both |
| VS Code tasks on Linux/macOS | `/bin/sh <root>/deploy` | no exec bit needed |
| Neovim plugin | none: runs uv directly | no shell, no cmd.exe; launcher only when uv is nowhere |
| `shell-setup` xonsh alias / nu function | none: runs uv directly | avoids cmd.exe's argument limits |

PowerShell on Windows: a full path WITHOUT extension (`C:\proj\deploy`) resolves to the
extensionless sh launcher, which Windows hands to its file association (no association: exit 0,
no output). Type `C:\proj\deploy.ps1`. On Linux/macOS the extensionless file is the sh launcher
itself, so pwsh users type `./deploy.ps1`.

### 4.3 `deploy` (POSIX sh)

Every rule below exists because of a verified failure; `test_launcher_sh.lint` enforces the
header rules (with detector tests proving each rule fires).

- First line `#!/bin/sh`, LF (`.gitattributes`: `deploy text eol=lf`), git mode 100755, ASCII.
  `#!/usr/bin/env bash` is wrong: on Windows xonsh maps it to `bash`, which can be the WSL
  stub. `shells.doctor` checks the shebang, LF, ASCII and the git mode.
- POSIX sh only (dash, bash 3.2, busybox ash, ksh; zsh through `emulate sh`): no arrays,
  `[[ ]]`, `${v//a/b}`, `local`, `$'...'`, `function`. No `set -e` / `set -u` (a caller's
  `set -eu` in niubash and `dash -eu deploy` still work).
- `printf '%s\n'`, never `echo`, for anything that may contain a backslash (dash's `echo`
  interprets `\`: `c:\Users` prints as `c: sers`).
- niubash hygiene (section 4.6): every variable and function name starts with `_pt_` and is
  `unset` before the hand-over; never `cd`; `exit` only at top level or inside `if`/`case`
  bodies (`return` is fine anywhere); no comment on a `name() {` line; never write
  `"...$(cmd "$x")..."`: assign the substitution to a variable first.
- niubash is detected by `__RUBASH_SHELL_NAME`. There uv runs WITHOUT `exec` (exec would only
  end the in-process script), then the launcher unsets `PYTEMPLATE_CALLER_CWD` and
  `PYTEMPLATE_LAUNCHER` and exits with uv's code, so nothing leaks into the calling session.
- Root discovery: `$BASH_SOURCE` -> zsh `${(%):-%x}` (read through `eval` so dash never parses
  it) -> `$0` -> walk up from `$PWD`. The walk stops at `/`, `C:` and `C:/`; relative
  candidates (`./`, `../`) are folded against `$PWD`.
- Windows detection: `OS=Windows_NT` and no `WSL_DISTRO_NAME`; `uname -s` only when `OS` is
  unset. Never `OSTYPE` (niubash fakes `msys`). Never trust the output format of `uname` or
  `cygpath`: in non-login MSYS2 shells on the maintainer's machine they resolve to WinuxCmd
  copies.
- On Windows the script path and `PYTEMPLATE_CALLER_CWD` are handed over as `C:\...`: `/c/x`
  and `/cygdrive/c/x` are converted in pure sh (drive letter upper-cased); `cygpath -m` runs
  only for paths inside the MSYS/Cygwin root such as `/home` or `/tmp`, and only an `X:/...`
  answer is accepted.
- `%NAME%` in registry values is expanded from the environment, retrying the upper-case name
  (MSYS2/Cygwin upper-case `SYSTEMROOT`, `PROGRAMFILES`...).
- Registry lookup: `reg.exe query KEY` WITHOUT `/v` (MSYS rewrites `/v` into `V:/`). It only
  runs when every other lookup failed.
- Overhead over a bare shell start (Windows, quiet machine, median of 15): Git dash +31 ms,
  MSYS2 dash +40 ms, MSYS2 `bash -lc` +44 ms, Git sh +52 ms, niubash script +78 ms, niubash
  `-c` +124 ms; the registry fallback adds ~80 ms; a project under an MSYS-root path adds two
  `cygpath` calls (60-100 ms). Under heavy machine load expect 5-10x.
- **[template repo]** CI runs `shellcheck -s sh deploy` (`template-launchers.yml`, Linux). It
  passes with shellcheck 0.8-0.11 and NO `# shellcheck disable=` directive: keep it so.
- Testing launcher code without writing files: pass it through the environment,
  `sh -c 'eval "$PT_CODE"'` (section 4.7: Cygwin mangles argv). Syntax checks: `dash -n`,
  `bash --posix -n`, `sh -n` (niubash has none, section 4.6).

### 4.4 `deploy.cmd`

- Pure ASCII (cmd reads it in the OEM code page); CRLF (`.gitattributes`: `*.cmd text
  eol=crlf`; labels and `goto` break with LF).
- No `( )` blocks: a PATH that contains `(x86)` closes them early. No delayed expansion. `%*`
  appears only on the uv line (`call` would expand it a second time). No `%` in comments:
  cmd expands them even on `rem` lines.
- Root: `%~dp0` has a trailing backslash and can be wrong when cmd found the file through PATH
  from a quoted name: check `%~dp0.pytemplate\deploy.py` first, else walk up from `%CD%` (the
  start is normalised so a drive root works).
- A `UV` variable that names a folder is rejected. The registry is read with
  `reg query KEY /v Path` (cmd has no MSYS rewriting); `call` expands `REG_EXPAND_SZ` values.
- The helper variables are cleared on the uv line itself
  (`set "PT_ROOT=" & set "PT_UV=" & "%PT_UV%" run ...`): cmd expands the whole line first, so
  uv still gets their values and the runner sees only the two `PYTEMPLATE_*` variables.
- cmd re-parses `%*`: `% ! " ^` and unquoted `& | < >` inside arguments do not survive. Every
  caller that starts a `.cmd` through CreateProcess/`list2cmdline` inherits this (xonsh on
  Windows, Python `subprocess`, VS Code tasks): the BatBadBut class. Never design CLI syntax
  that needs these characters; users pass such values through `./deploy` or `deploy.ps1`.
- Ctrl+C asks "Terminate batch job (Y/N)?" once uv has exited. UNC current directories are not
  supported (cmd.exe replaces them).

### 4.5 `deploy.ps1`

- ASCII, LF, no BOM (`.gitattributes`: `deploy.ps1 text eol=lf`): xonsh and Unix kernels read
  its shebang `#!/usr/bin/env pwsh`, and Windows PowerShell 5.1 reads BOM-less files as ANSI.
  Git mode 100755 so `./deploy.ps1` works from bash/zsh on Linux/macOS (`shells.doctor` only
  notes a wrong mode: PowerShell itself runs it without the x bit).
- No `param()` block: it would turn `-v`, `-h`, `-q` into PowerShell parameters. `a,b` arrays
  in `$args` are flattened.
- A `.ps1` runs inside the caller's session: never assign `$env:PATH`. The two `PYTEMPLATE_*`
  variables are restored in `finally`; a variable that did not exist before is removed with
  `Remove-Item Env:NAME`, never `[Environment]::SetEnvironmentVariable($n, $null)`: PowerShell
  passes `$null` to a .NET string parameter as `''`, and PowerShell 7 then keeps an empty
  variable.
- The caller's strict mode and preferences reach the script's scope, so it resets
  `Set-StrictMode -Off`, `$ErrorActionPreference = 'Continue'`,
  `$PSNativeCommandUseErrorActionPreference = $false` and `$ProgressPreference =
  'SilentlyContinue'` in its own scope: with `'Stop'`, 7.3+ turns a runner exit code into an
  error and 5.1 turned redirected runner stderr (`2>&1`) into a bogus exit 126.
- PowerShell removes a bare `--` before any script or function sees it (5.1 and 7 alike).
  Never design CLI syntax that needs `--`; users quote it (`'--'`) or use `deploy.cmd`.
- PowerShell < 7.3 (or `$PSNativeCommandArgumentPassing = 'Legacy'`) drops empty arguments and
  mangles embedded quotes when calling native programs, so the launcher pre-quotes every
  argument: a `"` is written `""` in 5.1 (Desktop: its quote counter ignores backslashes, so
  `x "y z` split into three arguments) and `\"` in 7.x; trailing backslashes are doubled.
- PowerShell 7 rewrites every native argument that is not a quoted literal, splatted `@argv`
  included (`NativeCommandParameterBinder.PossiblyGlobArg`): it globs wildcards on Linux/macOS
  (`'*'` reached uv as the file list) and expands `~`, `~/x` (and `~\x` on Windows, 7.6) to
  the home folder. On Core the launcher therefore rebuilds the uv call from single-quoted
  words (`CodeGeneration.EscapeSingleQuotedStringContent`, which keeps the file ASCII) and
  runs it with `Invoke-Expression`; Windows PowerShell 5.1 does neither and keeps the plain
  `& $uv ... @argv`. `selftest --shells` T1 passes `~`, `~/x`, `~\x` to PowerShell only
  (`shells.PS_ARGS`).
- uv: `Get-Command uv -CommandType Application`, and on Windows only a real `.exe` (a plain
  `Get-Command uv` can return an alias or function; a `uv.cmd`/`uv.ps1` wrapper would parse the
  arguments again). The registry `Path` is read with `[Environment]::GetEnvironmentVariable`
  (expands `%VARS%`). `Read-Host` is wrapped in `try`.
- Root: `$PSScriptRoot`, else walk up from `Get-Location` (its `ProviderPath` when the provider
  is FileSystem, else `[Environment]::CurrentDirectory`), which is also the caller cwd.
- Execution policy `Restricted`/`AllSigned`, or Mark-of-the-Web on a copy from a downloaded
  zip: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, `Unblock-File .\deploy.ps1`, or
  use `deploy.cmd`. `shells.doctor` reports the policy of 5.1 and 7 separately.
- 5.1 started with `-EncodedCommand` prints the calling session's module-loading progress as
  CLIXML on stderr; the launcher silences only its own.

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
- `bash -n` is WinuxCmd's `bash.exe` and returns 0 even on a broken script: niubash has no
  syntax check, only the behavioural tests cover it.
- A session keeps stale exports. `./deploy` no longer leaves `PYTEMPLATE_*` behind (section
  4.3), but other sources can (older launchers, a user export), which is why
  `project.caller_cwd` only trusts `PYTEMPLATE_CALLER_CWD` when it names the process cwd. The
  generated portable `.sh` launcher still leaks its variables into a niubash session.
- `niu -c`, niubash scripts and the xonsh `!` route read only the file named by `$NIU_ENV`, not
  `~/.niubashrc`: a shell function must be sourced from both.

### 4.7 MSYS2 and Cygwin

- MSYS2 login shells default to `MSYS2_PATH_TYPE=minimal` (`/etc/profile`): PATH is `/usr/bin`
  plus System32, so uv is not on PATH, and `$HOME` is `/home/<user>`, not the Windows profile.
  Windows variables (`USERPROFILE`, `LOCALAPPDATA`, `ProgramData`) keep their case;
  `SYSTEMROOT` and `PROGRAMFILES` are upper-cased. The launcher uses them and the registry.
- Non-login MSYS2 shells (the xonsh `!m` route) have the full Windows PATH but no `/usr/bin`;
  `MSYSTEM_PREFIX`, `EXEPATH` and `SHELL` do not reach the runner there (section 4.8).
- MSYS rewrites POSIX-looking arguments and environment values for native programs (`/c/x` ->
  `C:/x`, `--flag=/x` -> `--flag=C:/msys64/x`, `/v` -> `V:/`). Opt out with
  `MSYS_NO_PATHCONV=1` or `MSYS2_ARG_CONV_EXCL='*'`. Cygwin, niubash and busybox-w32 do not
  rewrite, so the runner can receive `/c/x` (handled by `project.native_path`).
- A Windows program that passes argv straight to an MSYS/Cygwin program (Python running
  `dash.exe deploy ARGS`) goes through Cygwin's own command-line parser, which mangles `\"`,
  expands `*` and drops `'`. Pass arguments through an environment variable or a script file.
- `uname -s` is `MSYS_NT-...` or `MINGW64_NT-...` (also for UCRT64 and CLANG64);
  `OSTYPE=cygwin`. An inherited `PWD` can make MSYS2 bash start with `PWD=C:/...`.
- Cygwin with `CYGWIN_NOWINPATH=1` also has a minimal PATH.

### 4.8 Paths on the runner side (`project.py`)

- `project.ROOT = Path(__file__).resolve().parents[2]`: the runner never uses the cwd to find
  the project. Every project path comes from `project.*` constants.
- `project.native_path(raw)` (identity outside Windows): `C:/x` and `c:\x` are normalised with
  an upper-case drive; `/c/x`, `/C/x` and `/cygdrive/c/x` -> `C:\x`; `//server/share` and
  `\\server\share` -> `\\server\share`; relative paths, `~` and `C:x` are returned unchanged
  (for `user_path`). Paths inside the MSYS root (`/home`, `/tmp`) go through `cygpath -w` only
  when `PYTEMPLATE_LAUNCHER` ends in `:msys` or `:cygwin`; otherwise they are returned as-is.
- `project.find_cygpath(dll)` accepts only a `cygpath.exe` next to `msys-2.0.dll` or
  `cygwin1.dll` (WinuxCmd's copy from niubash/xonsh-shell-kit turns `/tmp/x` into `\tmp\x`).
  Order: `MSYSTEM_PREFIX`, `EXEPATH` (Git Bash), `SHELL`, then PATH.
- `project.caller_cwd()`: `PYTEMPLATE_CALLER_CWD` only while it is absolute and the same
  directory as the process cwd (`os.path.samefile`); then it keeps the shell's logical path
  (junction, symlink). Otherwise `Path.cwd()`; a deleted cwd raises `DeployError`.
  `uv run --script` never changes the cwd.
- `project.user_path(raw)`: use it for EVERY path argument the user types for the runner
  (`new DEST`, `pyz-merge` inputs and `--out`, `selftest --shells --project`, `--nvim --dir`,
  `--e2e --base`). It expands `~`, rejects an empty path, resolves relative paths against
  `caller_cwd()`, normalises on Windows (POSIX keeps `..` for the OS: symlinks), and warns when
  an `:msys`/`:cygwin` launcher passed an MSYS-root path and no real cygpath was found (then it
  is read as a path on the current drive). Never use it for arguments forwarded to the app or
  to pytest. Never read `PYTEMPLATE_CALLER_CWD` directly.

### 4.9 Shell tooling (`shells.py`)

- `./deploy __probe EXIT STDIN(0|1) ARGS...` (`shells.probe`; `cli.main` routes it before
  `_parse_globals`: no config load, no render, not in help). Prints ONE line `PTPROBE{json}`
  to stdout (ASCII JSON with keys `argv`, `cwd`, `caller_cwd_raw`, `caller_cwd`, `launcher`,
  `stdin_tty`, `stdin`, `root`) and exits with EXIT. Launcher tests use it: keep the keys.
- `./deploy selftest --shells [NAME,...] [--list] [--json] [--keep] [--project DIR]
  [--tests T1,...] [--jobs N] [--timeout S]` (`shells.selftest`; default jobs min(8, CPUs),
  60 s per probe). Discovers the installed shells (Windows: cmd, powershell, pwsh, xonsh,
  niubash, niubash-shx, git-bash/sh/dash, msys2-<msystem> login shells, msys2-shx, msys2-dash,
  Cygwin, busybox-w32, nu, fish, WSL distros; POSIX: sh, bash, dash, zsh, ksh, mksh, yash,
  busybox, fish, nu, pwsh, xonsh; `MSYS2_ROOT`/`CYGWIN_ROOT` point at non-standard installs).
  A NAME prefix selects a family (`msys2` = every `msys2-*`). Tests: T1 argv round-trip, T2
  exit code, T3 cwd (from `src/` and from outside), T4 the xonsh-shell-kit route (niubash /
  MSYS2 non-login script mode; the shell's cwd must not change), T5 minimal PATH, T6 stdin, T7
  install hints with uv hidden. Table with ms per test and the launcher each shell reported;
  `--json` to stdout; exit 1 on any FAIL. T7 is SKIP where uv cannot be hidden (PowerShell
  reads the registry PATH itself; the MSYS2 login profile puts `reg.exe` back on PATH).
- How the probes reach each shell: POSIX shells get the command in `$PTCMD`
  (`sh -c 'eval "$PTCMD"'`, never in argv: section 4.7), fish `eval $PTCMD`, WSL through
  `WSLENV`, script mode a script file; cmd a hand-built line whose arguments are free of
  `% ! " ^`; PowerShell `-EncodedCommand`. `shells.child_env` drops `UV`, `VIRTUAL_ENV`,
  `UV_*` selection variables, `PYTHONHOME/PATH`, `PWD`, `OLDPWD` and `PYTEMPLATE_*`. Outputs go
  to files and every probe has a timeout. `--project DIR` probes another copy (to test launcher
  candidates before they land).
- `./deploy shell-setup [xonsh|pwsh|powershell|bash|zsh|niubash|msys2|fish|nu]`
  (`shells.cmd_shell_setup`; no argument guesses the shell from `PYTEMPLATE_LAUNCHER`,
  `XONSH_VERSION`, `SHELL`; unknown shell exits 2): prints a `deploy` function/alias that works
  from any subfolder, plus where to paste it. Output is ASCII with LF even on Windows (written
  to `stdout.buffer`: it is appended to rc files). bash, zsh, niubash and msys2 share one POSIX
  function whose walk-up stops at `/`, `C:`, `C:/` and at a backslash `PWD` (the old `dirname`
  loop never ended on niubash's `C:/...` paths); niubash: paste it into `~/.niubashrc` AND the
  `$NIU_ENV` file; msys2: above the interactive guard of `.bashrc` (`!m` lines also need
  `BASH_ENV`). pwsh/powershell: a function that walks up and calls `deploy.ps1`. nu: a
  `def --wrapped` that runs uv directly with `PYTEMPLATE_LAUNCHER=nu`. xonsh: an alias that
  runs uv directly (falls back to the launcher without uv; `@aliases.return_command` when the
  xonsh has it, else an unthreadable function alias) plus a registered completer whose words
  come from `cli.COMMANDS` and the project's `[tasks]` at print time.
- `shells.doctor(check)` (from `./deploy doctor`): step "launchers": the launcher that started
  the run, then `deploy` (`#!/bin/sh`, LF, ASCII, git mode 100755, exec bit on POSIX),
  `deploy.cmd` (CRLF, ASCII) and `deploy.ps1` (LF, ASCII, no BOM; a mode other than 100755 is
  only a note), each problem with the command that fixes it. Step "shell" (Windows): `bash` =
  WSL stub note, the execution policy of 5.1 and 7; WSL info.

## 5. Runner architecture

### 5.1 Modules (`.pytemplate/runner/`)

| Module | Responsibility |
|---|---|
| `deploy.py` (one level up) | Reconfigures stdout/stderr to UTF-8, puts its own dir on `sys.path`, calls `runner.cli.main`. |
| `cli.py` | `COMMANDS` table of `Command(module, func, summary, usage, render, group)`, modules imported lazily. `_parse_globals`, `dispatch`, `main` (exception -> exit code), `cmd_help`, `cmd_tasks`, `cmd_selftest` (plain, `--shells`, `--nvim`, `--e2e`), the `__probe` route, `EXAMPLES`. |
| `config.py` | Dataclass schema, strict loader (`_build`: unknown key or wrong type -> error with the full key path), `validate`, derived values (`pkg`, `min_python`, `profile_for`, `pypy_enabled`), `compiled_paths`, comment-preserving editor `set_value` / `update_file`, `toml_value`. |
| `project.py` | Paths (`ROOT`, `SRC`, `BUILD`, `DIST`, `TEMPLATES`, `PRESETS`...), `IS_WINDOWS/IS_MACOS/IS_WSL`, `ENV_SUFFIX`, `venv_python`, `host_os/host_arch` (uv names), `rel`, `code_dirs`, `native_path`, `find_cygpath`, `caller_cwd`, `user_path`. |
| `ui.py` | All runner output to stderr; `DeployError(msg, code)`; `VERBOSE/QUIET`; colours (`color_enabled`, `enable_vt_mode`); `check_line` (doctor lines `[ok]`, `[XX]`, `[--]`). |
| `proc.py` | `find_uv`, `base_env`, `run` (echo, `DRY_RUN`, cwd defaults to `ROOT`, UTF-8 capture), `output`, `show` (display quoting only), `vs_installer_dir`, `CommandFailed`. |
| `envs.py` | `PyEnv(key, dir, request, preference)`; `cpython_env`, `pypy_env`, `jit_env`, `tool_env` (always CPython), `runtime_env(backend)`, `env_vars`, `uv`, `uv_run` (= `uv run --locked`, plus `--project <ROOT>` when `cwd` is not the root: section 7), `sync`, `interpreter_info`, `find_jit_interpreter`. |
| `render.py` | Every generated file (`outputs`), hand-edit detection (`apply`, `auto`), typing profiles (`load_profile`), `mypy_ini`, `mypy_cli_args`, `pyright_config`, `ruff_config`, `to_toml`, `jsonc`, `ci_workflow`, managed pyproject parts (`managed_block`, `write_pyproject`, `pyproject_outdated`, `check_pyproject`). |
| `editors/vscode.py` | `.vscode/settings.json`, `extensions.json`, `launch.json`, `tasks.json` (`catalog`, `scan`, `problem_matchers`; section 12.1). |
| `editors/nvim.py` | `.lazy.lua` (verbatim template copy) and `.pytemplate/editor.json` (`editor_data`; section 12.2). |
| `presets.py` | Preset discovery/loading, option merge, `uv_extras`, `dependencies`, `skeleton`, `pristine`, `check_name_free`, `init`, `copy_template`, `new`. |
| `mypyc.py` | Incremental stage (`sync_tree`), `spec.json`, spawning `tools/mypyc_build.py`, `ANNOTATE_HTML`, `hidden_imports`, `exe_stage`, `runtime_env_vars`, `has_compiler_hint`. |
| `imports.py` | AST import extraction that skips `if TYPE_CHECKING:` blocks; parses bytes (tolerates a BOM). |
| `lintc.py` | Extra AST rules for compiled modules (section 9): `lint_file(cfg, path)`, `lint`, `Finding`. |
| `tasks.py` | `[tasks]`: `Placeholders` (lazy `{python}`), `deps`, cycle detection, `run_task`, `list_tasks`. |
| `cmd_env.py` | `setup`, `doctor`, `sync`, `lock`, `add`, `remove`, `clean`; `ensure_lock`; `_fix_exec_bit`; `_msvc`, `_long_paths`. |
| `cmd_mode.py` | `mode` (+ the Python 3.11 precheck before enabling PyPy), `render`, `init`, `new`, and their `--dry-run` planners (`_plan_mode`, `_plan_init`). |
| `cmd_dev.py` | `run`, `compile`, `check` (`run_checks`), `lint`, `fmt`, `test` (`test_backend`), `report`; `split_backend`; `only_flags`; `_profile_file`; `BASEDPYRIGHT`. |
| `cmd_build.py` | `build`: backend + method resolution, `COMPAT`, `payload`, `BuildRequest`, `dist_path`; `pyz-merge`. |
| `methods/*.py` | One `build(req: BuildRequest) -> Path` per method; `common.py` has target keys, `UV_PLATFORMS`, `export_requirements`, `install_deps`, `copy_app`, `uses_tkinter`; `nuitka.NUITKA`. |
| `shells.py` | `__probe`, launcher/shell doctor checks, `shell-setup` snippets, `selftest --shells` (section 4.9). |
| `cmd_nvim.py` | `./deploy nvim ...` and `doctor(check)` (section 12.2). |
| `nvimtest.py` | `selftest --nvim` (section 13.1). |
| `e2e.py` | `selftest --e2e` (section 13.1). |
| `hooks.py` | `./deploy hooks [install [--force]\|uninstall\|run\|status]`, `ensure_installed` (setup), `doctor`: the native git pre-commit hook (section 5.6). |
| `rename.py` | `./deploy rename NEW_NAME [--force]`: pure `plan` / `apply_plan` / `rewrite` (tokenizer + context rules), `check_new_name`, `git_changes`, `cmd_rename` (section 5.7). |
| `upx.py` | Optional UPX packing: pinned download (`VERSION`, `ASSETS` with SHA-256), `find`, `active`, `level_flags`, `env_value`, `excludes`, `candidates`, `pack_file`, `pack_tree`, `MAX_INPUT` (section 10). |

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
   `shell-setup`, `selftest`, `help`, `hooks` (the hook must not rewrite generated files in
   the middle of a commit).
5. Commands reject unknown arguments with exit 2 (a typo is never silently ignored):
   argparse commands, `render`/`mode`/`init`/`new` (`cmd_mode._parse`), `lint`, `fmt`,
   `clean`, `setup`, `doctor` (`cmd_dev.only_flags`), `check` and `sync` (extra positionals),
   `shell-setup`. By design: `run`/`test`/tasks forward the rest, `build` forwards unknown
   flags to the packager, `lock` to `uv lock`, plain `selftest` to pytest; `help` and `tasks`
   ignore extras.

### 5.3 Exit codes and output

- 0 ok; 1 = check/test failures, doctor problems, any FAIL in a selftest suite, or an internal
  runner error (traceback printed); 2 = usage/config (`DeployError` default, argparse); 3 =
  missing requirement (uv, compiler, interpreter, Neovim/git with `--require`, a missing
  `python.jit_interpreter`); 130 = Ctrl+C. `run`, `test BACKEND` and tasks return the child's
  exit code (`test all`: 0 or 1); `proc.CommandFailed` carries the failed child's code.
- Runner output goes to stderr through `ui` so the app keeps stdout. Exceptions, printed to
  stdout on purpose: `help`, `__probe`, `shell-setup` snippets, and the `--json` reports of
  `selftest --shells` (also with `--list`) and `selftest --e2e`.
- `ui.error` prints `error: ` and `ui.warn` prints `warning: ` (only the prefix is coloured on a
  TTY). The VS Code problem matcher `RULES_RE` and the Neovim parser `tasks.parse_line` depend
  on these exact prefixes and on `str(lintc.Finding)` (`src/...:N: msg`, relative to ROOT): do
  not reword them (`test_rules_matcher_reads_the_runner_output` fails if either changes).
- Colours: off when stderr is not a TTY, with `NO_COLOR` set to a NON-EMPTY value, or with
  `TERM=dumb`. On Windows `ui.enable_vt_mode` turns on `ENABLE_VIRTUAL_TERMINAL_PROCESSING`
  with `SetConsoleMode` on the stderr/stdout handles that are consoles (never `os.system("")`,
  which started a cmd.exe on every run; a test checks with an audit hook that no process
  starts).

### 5.4 `--dry-run`

- `proc.run` skips only ECHOED commands (`echo=True`) and reports them as exit 0;
  `echo=False` queries still run. So `run`, `test`, `check`, `add`, `remove`, `sync`, tasks and
  the plain `selftest` only print their commands; in-process work (the `lintc` rules) runs.
- `render.apply` behaves like `--check` (writes nothing); `render.auto` prints "would update";
  `render` prints `would update: ...`.
- `clean` prints what it would remove. `build` prints the checks (unless `--no-check`) and
  `(--dry-run) build B -> M: would output dist/<name>-<b>-<m>*`, then stops. `report` builds
  nothing and never opens the browser.
- `mode` validates the new `pytemplate.toml` in memory and prints the keys that would change
  (new and current value), whether `pyproject.toml` would be rewritten, `uv.lock` ("would
  re-lock", or a read-only `uv lock --check`), the generated files that would update, the
  environments it would sync and the leftover-environment note. `--jit on` still looks for the
  JIT interpreter. The PyPy 3.11 precheck runs read-only (`uv run --locked --no-sync`) and is
  skipped when `.venv` does not exist (`uv run --no-sync` would create it).
- `init` runs the real checks (name, `check_name_free`, pristine) and lists each file as `-`
  deleted, `+` new or `~` replaced, the dependencies removed and added, and what happens to
  `pyproject.toml` and `uv.lock`. `new` checks the destination and the name and prints
  destination, preset and name. `pyz-merge` validates its inputs and prints inputs and output.
- `nvim trust`, `extras`, `bootstrap` and `sync` print what they would do.
- `rename` runs the real checks (a dirty git tree is only a warning) and prints the move, each
  file with its reference count and sample lines, the pytemplate/pyproject lines, the `uv.lock`
  re-lock and the generated files it would re-render. `hooks install`/`uninstall` only print.
- `build` with `[deploy.upx]` enabled: `upx.pack_tree` lists what it would pack and stops.
- `setup` and `lock` report whether the managed parts of `pyproject.toml` would change
  (`render.write_pyproject` writes nothing under `DRY_RUN`; `render.pyproject_message` says
  "would update"); their `uv lock`/`uv sync` are echoed commands, so they are skipped.
- It is not a sandbox: scratch writes under `.build/` (`.build/cfg/*`, the mypyc stage,
  `mypy.ini`, `spec.json`) still happen.

### 5.5 Environment-variable contract

| Variable | Set by | Meaning |
|---|---|---|
| `PYTEMPLATE_CALLER_CWD` | launchers, Neovim plugin (`init.caller_cwd`: Neovim's cwd when inside the project, else the root), nu snippet | Caller's cwd; read only through `project.caller_cwd` |
| `PYTEMPLATE_LAUNCHER` | launchers, Neovim plugin (`nvim`), nu snippet (`nu`) | Which launcher/shell ran (section 4.1) |
| `UV` | uv | uv's own path; `proc.find_uv` and the launchers use it |
| `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `UV_PYTHON_PREFERENCE` | `envs.env_vars` | Environment selection (section 7) |
| `PYTHON_JIT` | `envs.env_vars`, portable launchers, pyz `.cmd` wrapper, JIT and mypyc launch configs | Always exactly `0` or `1` |
| `PYTHONUTF8=1` | `proc.base_env`, portable launchers, pyz `.cmd` wrapper, the Neovim mypy linter, every VS Code launch config (`vscode.DEBUG_ENV`) | mypy/mypyc otherwise read files as cp1252; F5 behaves like `./deploy run` |
| `PYTEMPLATE_BACKEND` | `cmd_dev.test_backend`, `mypyc.runtime_env_vars`, mypyc launch config | Backend under test (conftest) |
| `PYTEMPLATE_COMPILED` | `mypyc.runtime_env_vars` | Modules that must load from `.pyd/.so` (conftest) |
| `PYTEMPLATE_ASSETS` | `portable/boot.py`, `pyz/__main__.py` (setdefault) | Assets dir for `resources.assets_dir()` (raylib, flet) |
| `VSLANG=1033` | `mypyc.build`, wheel builds | English MSVC messages |
| `RUFF_OUTPUT_FORMAT=concise` | VS Code tasks with the ruff matcher, Neovim tasks that parse output | One-line ruff output for the parsers |
| `NO_COLOR` (non-empty), `TERM=dumb` | user | Disable runner colours |
| `CI` | CI | Disables the install prompt of `deploy` and `deploy.ps1`; `selftest --e2e` skips GUI runs on Windows/macOS CI |
| `__RUBASH_SHELL_NAME` | niubash | Detected by `deploy` (section 4.3) |
| `PT_TRUST_FILE`, `PT_ROOT`, `PTCMD` | `cmd_nvim.trust_file`, `nvimtest`, `shells` | File to trust headless; project the smoke test expects; probe command text |

`proc.base_env` removes `VIRTUAL_ENV`, `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `PYTHONHOME` and
`PYTHONPATH`; removes the runner's own ephemeral `Scripts/` or `bin/` from PATH when
`sys.prefix != sys.base_prefix` (uv exports it for `--script` runs); sets `PYTHONUTF8=1`; on
Windows appends `%ProgramFiles(x86)%\Microsoft Visual Studio\Installer` to PATH (VS 2026
`vcvarsall.bat` calls `vswhere.exe` by bare name; without it setuptools fails with "Unable to
find a compatible Visual Studio installation"). Everything else (e.g. `FLET_*`) passes through.

### 5.6 Git pre-commit hook (`hooks.py`)

- `./deploy setup` calls `hooks.ensure_installed(cfg)` when `[hooks] pre_commit` is true (the
  default): it installs or updates the hook, never fails setup, and is silent outside git.
  `doctor` shows one "git hook" line (missing = info, not a problem).
- The hook: `pre-commit` in the folder `git rev-parse --git-path hooks` reports (worktree
  aware), pure ASCII + LF, a marker comment, and `exec sh <launcher> hooks run` with the POSIX
  launcher path relative to the repository top (the project may be a subfolder of a bigger
  repo; non-ASCII folder names are written with `printf` escapes). `sh` explicitly (no
  dependence on the exec bit); git for Windows runs hooks with its own sh.exe, where the POSIX
  launcher works. A missing launcher makes the hook exit 0 (other checkouts).
- Foreign hooks are never overwritten: `install --force` renames it to `pre-commit.local` and
  ours runs it first; `uninstall` restores it. With `core.hooksPath` set nothing is written:
  install/status/doctor print the line to add (`sh ./deploy hooks run || exit $?`).
- `hooks run` checks the STAGED files (`git diff --cached --name-only --diff-filter=ACMR -z`)
  in ~0.3 s (0.74 s for the whole hook, measured): ruff (active typing profile, `exit_zero`
  honoured) and `ruff format --check` on staged `.py/.pyi` under the code dirs; generated
  files up to date (`render.apply(check=True)`) and none unstaged/untracked; managed pyproject
  parts and `uv lock --check`, and `uv.lock` staged with `pyproject.toml`; `lintc` on staged
  compiled modules (blocking only under the `mypyc` profile); `shells.launcher_problems` on
  staged launchers; the language guard in the template repo. Never mypy (the user's choice:
  `./deploy check` does it). It reads the working-tree version of the staged files.
- Git hands hooks a relative `GIT_INDEX_FILE` and, in linked worktrees, `GIT_DIR` without
  `GIT_WORK_TREE`: `hooks` makes them absolute for its own git calls and removes them before
  starting uv/ruff (they would point git at the wrong repository for a sub-folder project).

### 5.7 Renaming (`rename.py`)

- `./deploy rename NEW_NAME [--force]`: validates the name (format, keyword, standard-library
  module, dependency clash: the same rules as `presets.check_name_free`, used by `new` and
  `init` too), refuses a dirty git tree without `--force`, moves `src/<old_pkg>/` first (the
  step that can fail on a locked file; case-only renames use two moves), rewrites UTF-8 text
  files in `src/` and `tests/` (line endings and BOM kept, binaries/caches skipped), updates
  `pytemplate.toml` (`app.name` via `config.set_value`, package references in strings and
  comments) and `pyproject.toml` (`[project] name` and the preset block only), then
  `cmd_env.ensure_lock` and `render.apply`.
- Python code goes through `tokenize`: only real package references change (the first name of
  `import pkg...`/`from pkg... import`, and `pkg.x` in files that `import pkg` without `as`).
  When the old name equals the old package but the new name differs from the new package
  (`alpha` -> `My-Game` / `my_game`), strings/comments/text choose by context: paths, dotted
  names, `pkg:main`, "package"/"module", `import`/`from`, `-m` and `import_module`-like calls
  get the package; titles and other prose get the name; `x.pkg` never changes.
- Invariant (tested for the 3 presets, LF and CRLF, 7 name pairs): renaming the skeleton of
  name A to B is byte-identical to the skeleton of B, so `presets.pristine` stays true.
- Common words as names (`app`, `game`, `core`) also rewrite prose: that is why the tree must
  be clean and `--dry-run` shows sample lines. Top-level docs (README.md) are only listed.

## 6. Configuration and generated files

### 6.1 `pytemplate.toml` (`config.py`)

- `schema = 1` is only type-checked; there is no migration logic.
- `[app]`: `name` (`[A-Za-z][A-Za-z0-9_-]*`; `pkg = name.replace("-", "_").lower()`), `preset`
  (`config.validate`: `[a-z][a-z0-9_-]*` and `PRESETS/<name>/preset.toml` must exist, checked
  without importing `presets.py`), `gui`, `assets` (dir inside `src/`, `""` = none).
- `[backend]`: `active`, `supported` (non-empty subset of `cpython, pypy, mypyc`, contains
  `active`).
- `[python]`: `cpython` (`^\d+\.\d+$`), `pypy` (`^pypy@\d+\.\d+\.\d+$`, exact: a loose request
  can resolve to PyPy 8.0 / pp80, which has no wheels), `jit`, `jit_interpreter` (must exist,
  relative paths resolve against the project root, else exit 3).
- `[typing]`: `profile = auto|mypyc|strict|warn|off`, `relaxed = off|warn|strict` (what `auto`
  means on cpython/pypy), `editor = pylance|basedpyright`, `[[typing.mypy_overrides]]`
  (`module` required, `strict` forbidden: mypy would apply it to ALL modules; `{pkg}` token in
  module names). `backend.active = "mypyc"` rejects `warn`/`off`.
- `[compile]`: `modules`, `exclude`, `forbid_imports` (dotted names checked), `annotate` (every
  mypyc build writes the annotate report, section 9), `opt_level "0".."3"`, `multi_file`,
  `separate`, `strict_dunder_typing`.
- `[deploy]`: `optimize 0|1|2`, `default {backend: method}`, `exclude_modules` (dotted names:
  PyInstaller `--exclude-module`, Nuitka `--nofollow-import-to`; the flet preset sets
  `["PIL"]`), `[deploy.exe] mode console icon hidden_imports strip extra_args` (`strip`:
  PyInstaller `--strip`, Linux/macOS only), `[deploy.portable] runtime prune archive env`
  (`env` names must be identifiers), `[deploy.pyz] targets`, `[deploy.wheel] entry`,
  `[deploy.nuitka] mode extra_args`, `[deploy.flet] target cleanup exclude extra_args`
  (`target` is not validated; `cleanup` = `--cleanup-app --cleanup-packages`),
  `[deploy.upx] enabled level lzma exclude path` (`level` in `1..9|best|brute|ultra-brute`).
- `[hooks]`: `pre_commit` (setup installs the git hook; section 5.6).
- Every `*.env` table (`tasks.X.env`, `deploy.portable.env`) takes string values only.
- `[tasks.<name>]`: `cmd` (argv), `deps`, `env`, `backend`, `uv = true`, `cwd`, `help`,
  `background` (long-running dev server; section 12). Name regex `[a-z][a-z0-9_-]*`; `cmd` or
  `deps` required.
- `[preset.<name>]`: free-form option overrides, NOT validated.
- `[vscode]`: `settings` (merged into `.vscode/settings.json`; keys NOT validated, values must be
  JSON values: a TOML date/time or nan/inf is a DeployError naming the key, from
  `vscode.settings`), `buttons` (each first word must be a builtin command or a `[tasks]` name:
  `config.validate`).
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

`editor.json` lists `cli.COMMANDS` (name, usage, summary, group): adding or rewording a command
changes a generated file, so run `./deploy render` and commit `editor.json` + `state.json`
with it (`render --check` fails otherwise). Merge conflicts in those two files are resolved by
re-rendering.

`render.apply(cfg, force, check, show_diff)`:
- Hash = sha256 of the content with the BOM stripped and CRLF -> LF (`render._norm`,
  `_digest`), because `* text=auto eol=native` + `core.autocrlf=true` gives CRLF checkouts on
  Windows and editors/PS 5.1 add BOMs. Every runner write uses `newline="\n"`.
- Current hash == new hash -> skip. Recorded hash in `state.json` != current file -> hand-edited:
  not written (warning) unless `--force`. Otherwise write (LF) and record the hash.
- A missing or corrupt `state.json` counts as empty (`render._read_state`/`_load_state`: missing,
  not UTF-8 (PS 5.1 `>` writes UTF-16), not JSON, not an object, or `files` not an object; an
  entry whose value is not a sha256 counts as unrecorded): every generated file is overwritten
  without warning, and the next write is valid UTF-8 JSON. It is read as `utf-8-sig`, so a BOM
  does not disable hand-edit detection. `_save_state` keeps every other top-level key (`./deploy
  apply` records its own) and the key order. Hashes of files no longer generated stay recorded
  (a file that comes back keeps its hand-edit protection).
- `--check` and `--dry-run` write nothing. A folder in the way, or a read/write error, is a
  DeployError naming the file.
- `render.auto` runs before most commands and prints one line when something changed; it also
  warns when `pyproject_outdated`.
- Changing `render.HEADER` rewrites every generated file (fine: only files whose current hash
  differs from the recorded one are protected).
- Templates are read as `utf-8-sig` (a BOM is fine); errors are DeployErrors naming the file:
  `templates/vscode/settings.json` must be a plain JSON object (no comments, no trailing
  commas), a typing profile valid TOML with the known keys of the right type
  (`render._check_profile`), and `ci.yml` must leave no placeholder behind
  (`render.CI_PLACEHOLDERS`). `render.jsonc` refuses values JSON cannot hold (nan/inf, dates).

Formats:
- Generated JSON is JSONC: the first line is `// GENERATED ...` (`render.jsonc`). VS Code,
  pyright and overseer accept it; strict JSON parsers must drop that line first.
- `.pytemplate/editor.json` is plain ASCII JSON without comments (it has a `"generated"` key
  instead): Lua decodes it. Keys in section 12.2.
- `.lazy.lua` is a copy of `.pytemplate/templates/nvim/lazy.lua` (BOM stripped, CRLF -> LF)
  and must not depend on the config (section 12.2).
- Path styles differ per tool: `.build/cfg/mypy-*.ini` uses relative `mypy_path = src` /
  `files = src, tests`, so mypy must run with cwd = ROOT (the `proc.run` default); the ruff and
  pyright copies under `.build/cfg` use absolute paths (`absolute=True`) because those tools
  resolve paths relative to the config file; the compile-time `mypy.ini` only carries an
  absolute `typings` path because mypyc runs with cwd = stage.

### 6.3 `pyproject.toml` managed parts

- `requires-python` of the `[project]` table, in any TOML string form (literal, multi-line,
  quoted key, any indent), is rewritten to `">=<min_python>"` and inserted under `[project]`
  when missing (`render._set_requires_python`); a `requires-python` of another table is never
  touched.
- The `[tool.uv]` block between `# >>> pytemplate` and `# <<< pytemplate`
  (`render.managed_block`). The END MARKER IS AN INLINE COMMENT on the last key line
  (`python-preference = "only-managed"  # <<< pytemplate`) so uv/toml_edit insertions cannot
  detach it; `test_managed_block_bounds_cpython_minor` asserts it. The opening marker is a whole
  line `# >>> pytemplate[: ...]` and the closing one ends its line (`render._BEGIN_RE`,
  `_END_RE`): the preset markers `# >>> pytemplate-preset` never count (they once made `lock`
  replace `[tool.flet]` with the block). `render._managed_bounds`: both markers missing = the
  block is inserted after the `[tool.uv]` header (any spelling: `[ tool.uv ]`, a trailing
  comment) or in a new `[tool.uv]` table; one missing, duplicated, out of order, outside
  `[tool.uv]` or with a table header between them = DeployError (restore them, or delete the
  whole block and run `./deploy lock`).
- Compared by MEANING: `pyproject_outdated` and `write_pyproject` parse both texts
  (`render._same_meaning`), so a TOML formatter (taplo: Even Better TOML, LazyVim's toml
  extra) re-indenting arrays or re-spacing comments is no change: `write_pyproject` then writes
  nothing (layout, BOM and CRLF stay; a real rewrite is LF without a BOM). A template that only
  rewords the block's comments does not rewrite old projects.
- `render._verify` refuses (DeployError, nothing written) a rewrite that would give invalid TOML
  (e.g. a managed key repeated outside the markers), change anything but requires-python and the
  block's keys (a user key or table between the markers), or leave a managed value out of
  `[tool.uv]`. `pyproject_outdated` never raises (an unusable file counts as outdated, and
  `./deploy lock` then explains); `render.check_pyproject(cfg)` runs the same checks without
  writing, as a preflight for commands that change other files first.
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
- uv finds the project by walking up from the CWD: `envs.uv_run` adds `--project <ROOT>`
  whenever it runs with another `cwd` (a work dir with its own `pyproject.toml`, like the
  `flet build` stage, would otherwise become the project). Plain `envs.uv` calls with a `cwd`
  (nuitka, the stages without a pyproject) rely on the walk reaching `ROOT`.
- `git clean -fdx` is safe: only envs, `.build/`, `dist/`, caches and `.claude/` go; the next
  `uv run --locked` recreates `.venv` by itself (verified on a clone: `run`, `test all`, `check
  all`, `render --check`, `doctor` all pass without `setup`).
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
  A `.pyz` started directly (`python app.pyz`, shebang) cannot set it: only its `.cmd` does.
- JIT interpreter: uv's Windows CPython builds lack the JIT, so `envs.find_jit_interpreter`
  tries `uv python find` with `only-system`, then `py -X.Y` (both quietly: `py` prints "No
  suitable Python runtime found" when nothing is registered); `python.jit_interpreter`
  overrides. `mode --jit on` finds it before writing anything. `doctor` warns when it is
  scoop's `current` junction (`scoop update` moves it).
- WSL on `/mnt/*` (`project.IS_WSL`): separate `.venv*-wsl` envs and `.build/wsl`, so the
  Windows `.venv` is not turned into a Linux one.
- Tools outside `uv.lock` run through `uv run --locked --with <pin>` and are pinned in module
  constants: `cmd_dev.BASEDPYRIGHT = "basedpyright==1.40.1"` (`check` with
  `typing.editor = "basedpyright"`) and `methods.nuitka.NUITKA = "nuitka==4.2.2"`. Bump them
  deliberately.
- `mode --jit off` / `--supports -pypy` never delete the old env: they print a note that
  `./deploy clean --envs` removes the `.venv*` environments (all of them; `setup` recreates
  the ones in use).

## 8. Typing profiles and `check`

- Profiles live in `.pytemplate/templates/typing/<p>.toml` (`mypyc`, `strict`, `warn`, `off`).
  Keys: `description`, `blocking`, `skip_mypy`, `[mypy]`, `[mypy_compiled]` (sections for the
  compiled modules), `[pyright]`, `[pyright_compiled].strict`, `[basedpyright_compiled]`,
  `[ruff] select/ignore/exit_zero`, `[vscode]` (merged into settings; its
  `"mypy-type-checker.severity"` also feeds `editor.json` `typing.mypy_severity`).
  `render.load_profile` reads them as `utf-8-sig` and checks the type of these keys (DeployError
  naming the file). `[pyright]` rule names are pyright's, which Pylance and basedpyright share
  (`reportPossiblyUnboundVariable`; `reportPossiblyUnbound` never existed and was silently
  ignored): `test_profile_rule_names_are_known_to_the_pinned_basedpyright` runs the
  `cmd_dev.BASEDPYRIGHT` pin on every profile's keys when it is in the uv cache.
- `Config.profile_for(backend)`: `mypyc` backend -> `mypyc`; otherwise `typing.relaxed` when
  `profile = "auto"`, else `profile`.
- `cmd_dev.run_checks(cfg, backend, rules=True)`:
  1. `_profile_file` writes `.build/cfg/ruff-<profile>.toml` and `.build/cfg/mypy-<profile>.ini`
     (the profile of THAT backend, which may differ from the editor's active one).
  2. `ruff check --config ...` (`--exit-zero` when the profile says so).
  3. mypy unless `skip_mypy`; with PyPy supported `render.mypy_cli_args` adds
     `--python-version <min_python> --python-executable <tool python>`. Exit 1 is only a
     warning when the profile is not `blocking`.
  4. `lintc` rules on the compiled sources (when mypyc is supported and `rules`): errors only
     under the `mypyc` profile, warnings otherwise.
  5. With `typing.editor = "basedpyright"`: basedpyright (`BASEDPYRIGHT` pin) on
     `.build/cfg/pyright-<profile>.json`.
- `check all` runs each distinct profile once (cpython and pypy usually share one) and the
  mypyc rules only once, with the strictest profile (`mypyc` when present), so each finding
  appears once in the Problems panel.
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
  Change detection is size + `st_mtime_ns` (`copy2` preserves the exact mtime), so a same-size
  edit within one second is detected.
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
  (`has_compiler_hint`) is added only when the output has no `error: `.
- After the build every compiled module must have an extension, else `DeployError`.
- `compile.annotate = true`: every mypyc build (`run`, `test`, `compile`, `build`) also writes
  `mypyc.ANNOTATE_HTML` (`.build/reports/mypyc-annotate.html`); cost within timing noise.
- `./deploy compile [--release]` builds the stage without running it: the hidden VS Code task
  `deploy: compile` is the `preLaunchTask` of the mypyc debug config.
- `report` writes the same `ANNOTATE_HTML` with `compile_c=False` (no C compiler needed) plus
  mypy `--any-exprs-report` and `--lineprecision-report` into `.build/reports/`.
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
  `lintc` does not import `presets`.
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
- Both bootstraps (`portable/boot.py`, `pyz/__main__.py`) put `lib/` BEFORE site-packages
  (`_prepend_sitedir`: `site.addsitedir` for the `.pth` files, then moved to the front), so
  the locked dependencies win over packages installed in the running Python.

Per method:
- **exe** (PyInstaller): `--python-option "X utf8"` (dev parity with `PYTHONUTF8`),
  `--optimize`, `methods.exe.size_args` (`--noupx`, or UPX below; `--exclude-module` per
  `deploy.exclude_modules`; `--strip`), `--clean`, `--log-level=WARN` unless `-v`, `--hidden-import` for
  mypyc, `--add-data "<src>:<dest>"` (`:` is PyInstaller's documented separator). The flet
  preset uses `flet pack` instead (`methods/exe._flet_pack`): it runs from its own cwd
  `.build/flet-pack/<b>` because `flet pack -y` wipes `<cwd>/build` and the distpath; onedir
  uses `--contents-directory=.`, except on macOS, where `flet pack` rejects `--onedir` and always
  builds a `.app` bundle (PyInstaller only logs a deprecation for onefile + `.app`); it bundles
  the Flutter client (plain PyInstaller would
  download ~40 MB on first start). `flet` and `flet-desktop` must share a version, else Flet
  pip-installs `flet-desktop` at runtime, bypassing `uv.lock`.
- **portable**: `dist/<n>-<b>-portable-<key>/` (no `-<key>` with `runtime = "system"`, which
  bundles no interpreter) with `app/`, `lib/` (`uv pip install --target`), `runtime/` (pruned
  copy of the interpreter's `base_prefix` through `\\?\` extended paths; the `ignore` callback
  strips that prefix before comparing), `boot.py`, `<n>.cmd` / `<n>.sh`. Prunes `include libs
  Tools share`, `tcl*`, stdlib `test idlelib turtledemo ensurepip site-packages`, tkinter/turtle
  (unless the AST finds them imported), PyPy `hpy/devel`; deletes `EXTERNALLY-MANAGED`; copies
  `vcruntime140*.dll` from the CPython base into PyPy runtimes on Windows (PyPy's zip lacks
  them). `compileall` runs with the console `python.exe` and also compiles PyPy's stdlib (PyPy
  ships no `.pyc`).
  - Launchers: `.cmd` = ASCII + CRLF, `start ""` + `pythonw.exe` for GUI apps, env values with
    `%` written `%%` and values it cannot hold (non-ASCII, `"`, line breaks) rejected; `.sh` =
    0755, values through `shlex.quote`, its folder from `${BASH_SOURCE:-$0}` (niubash keeps the
    caller's `$0`). Both use `-s` plus `-O`/`-OO`, never `-I`/`-E`, and set `PYTHONUTF8=1` and
    `PYTHON_JIT`.
  - `runtime = "system"`: each launcher RUNS every candidate interpreter with a minimum-version
    probe (`.cmd`: `py -X.Y`, `python3`, `python`, exit 9009 when none fits: `py` can exist with
    no Python registered; `.sh`: `pythonX.Y`, `python3`, `python`, exit 127; PyPy: `pypy3`,
    `pypy`). With native dependencies it warns that the folder only works on the host key.
  - mypyc builds are smoke-tested (`_smoke_compiled`, code from `smoke_code`): the same
    `sys.path` as `boot.py` (`app/` first, then `lib/`), result read from a `PTSMOKE:` marker
    line because imported packages may print (raylib's banner); a failed import shows the
    traceback and raises `DeployError`.
- **pyz**: Python cannot import `.pyd/.so` from a zip, so `__main__.py` extracts to a per-build
  cache (`%LOCALAPPDATA%` / `~/Library/Caches` / `$XDG_CACHE_HOME`, then
  `<name>/pyz/<build_id>/<key|pure>/`), guarded by a `.complete` marker and an atomic
  `os.replace`; the 3 newest builds are kept. `common/` never holds extensions (runtime `bug:`
  check). The mypyc overlay `targets/<host>/app` is the FULL package (`.py` + `.pyd` +
  `__init__.py`): a partial overlay would be a namespace-package trap where `common`'s `.py`
  wins. `zipapp compressed=True` = deflate, never zstd: it must open on 3.11 and PyPy.
  Cross-target deps use `uv pip install --target --python-platform --python-version
  --only-binary :all:` (an sdist built for another OS would produce host binaries); PyPy
  targets are host-only; `_virtualenv*` junk is removed. The `<n>.cmd` wrapper runs each
  candidate interpreter with a minimum-version probe, sets `PYTHONUTF8=1` and
  `PYTHON_JIT=0|1`. `pyz-merge` (>= 2 zip parts, same app name) takes `common/` and
  `__main__.py` from the first part and `targets/` from all parts, recomputes the `build_id`
  and prints "no platform (pure Python)" when no part has binaries.
- **wheel**: synthetic build project in `.build/wheel/<b>` (`setuptools>=84`; for mypyc
  `mypy==<version locked in uv.lock>` in `build-system.requires`, a `setup.py` using mypycify
  with the same `compile.multi_file`, `separate` and `strict_dunder_typing` as the stage, and a
  compile `mypy.ini`). Assets go into `<pkg>/assets` (package data). Needs network (isolated
  build env). mypyc -> platform wheel; cpython/pypy -> `py3-none-any`.
- **nuitka**: `.build/nuitka-stage/<b>`, `uv run --locked --with nuitka==<NUITKA> python -m
  nuitka` with cwd = stage; `--include-package=<pkg>`, `--include-module` for mypyc hidden
  imports, `--python-flag=no_asserts/no_docstrings` from `optimize`, `--nofollow-import-to`
  per `deploy.exclude_modules`, the upx plugin when enabled. Output found by file-name
  prefix (onefile) or `.dist` suffix; none found -> `DeployError`. Builds take minutes (~4-7
  min measured). Flet (verified: runs and starts the client): `flet/__init__.py` loads its
  controls lazily (module `__getattr__` + `importlib`), which Nuitka cannot follow, so the
  method adds `--include-package=flet --include-package=flet_desktop`; the flet-desktop wheel
  has NO client, so `nuitka._flet_client_archive` downloads the release archive
  (`flet_desktop.get_artifact_filename()`, the same URL flet uses) once into
  `.build/flet-client/<version>/` and bundles it at `flet_desktop/app/<archive>`, where
  flet_desktop looks for a bundled client. 61 MB with UPX; ~25 min build.
- **flet** (`flet build`): requires `app.preset == "flet"`. Windows needs Developer Mode
  (Flutter symlinks; checked in the registry by `methods.flet._developer_mode`) and Visual
  Studio C++. The stage `.build/flet-build/<b>` is persistent (Flutter cache); stale
  extensions are deleted from it before this payload's are copied (a desktop `.pyd` must not
  reach a mobile/web build). `flet build` ignores `uv.lock`, so `build_pyproject` pins the
  `uv export --frozen --no-dev` versions and serialises the PARSED `[tool.flet]` of the
  project `pyproject.toml` (no other table leaks in; `[tool.flet.app]` alone is kept; default
  `app.path = "src"`). Mobile/web targets (`apk aab ipa ios-simulator web`) cannot load
  extensions: a mypyc backend ships the `.py`. Desktop embeds CPython 3.14, so cp314 `.pyd`
  files work. `cleanup`/`exclude` map to `--cleanup-app --cleanup-packages` / `--exclude`;
  with UPX the finished folder goes through `upx.pack_tree` (desktop targets only). Verified on
  Windows (Developer Mode on): Flet 1.0.1 downloads ITS pinned Flutter (3.44.8, ~3 GB in
  `~/flutter`, ignoring a scoop Flutter) and a Python build (`~/.flet`); first build ~7 min,
  next ~3 min; 97 MB folder, 78 MB with cleanup + UPX, 38 MB zipped, no unpacking at start.
  `uv run` MUST get `--project <ROOT>` here (`envs.uv_run` does it when `cwd != ROOT`): the
  stage has its own `pyproject.toml`, which uv otherwise takes for the project ("Unable to
  find lockfile at uv.lock").
- **Size and UPX** (`upx.py`, README "Binary size" has the measurements): exe = PyInstaller's
  own UPX step (`--upx-dir`, `--upx-exclude` per glob; the level travels in the `UPX`
  environment variable, which upx reads as default options; PyInstaller always adds `--lzma`,
  skips Control Flow Guard DLLs and Qt plugins, and `--clean` keeps its binary cache from
  reusing another level); nuitka = its upx plugin (hard-codes `--best --lzma`, ignores our
  excludes: it packed `python314.dll` and the app still ran); portable (before the smoke test,
  so the packed `.pyd` files are what it loads) and flet = `upx.pack_tree` (PE `.exe/.dll/.pyd`
  on Windows, ELF executables but no `.so` on Linux, in parallel). Never packed: files over
  `MAX_INPUT` (600 MiB; UPX refuses 768 MiB), `BUILTIN_EXCLUDE` (C runtime, API sets,
  `python3*.dll`, `libpython3*`, and `flutter_windows.dll`: a packed Flutter engine hangs the
  app at startup with a 4 MB working set and no window, measured), binaries UPX rejects
  (`GUARD_CF`: never pass `--force`). UPX 5.2.1 is downloaded once (SHA-256 checked) to
  `%LOCALAPPDATA%\pytemplate\tools\upx-5.2.1` / `$XDG_CACHE_HOME/pytemplate/tools`; macOS
  is unsupported (`upx.unsupported_reason`). `flet pack` ships Flet's prebuilt FULL client
  zipped (40.5 MB, libmpv 28 MB inside) and unpacks it on first start into
  `~/.flet/client/flet-desktop-full-<version>-<fingerprint>` (97 MB); the "light" flavor
  exists only for Linux. PyInstaller follows imports inside functions: flet's lazy
  `from PIL import ...` (RawImage) drags Pillow (13 MB) in, hence the preset's
  `exclude_modules = ["PIL"]`.
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
- A skeleton file is text (token replacement, LF) only if its suffix is in
  `presets.TEXT_SUFFIXES` or it has none, it holds no NUL byte and it decodes as UTF-8;
  anything else is copied byte for byte, and `pristine` never rewrites CRLF inside binaries.
- `pristine(cfg)`: `src/`, `tests/`, `typings/` equal the current preset skeleton rendered
  with the current name (CRLF-normalised). `init` refuses otherwise (use `--force`). `init`
  deletes `src/ tests/ typings/`, writes every skeleton file (including root
  `pytemplate.toml`, not covered by the pristine check), chmods `deploy` and `deploy.ps1` on
  POSIX, sets `project.name`, replaces the preset tables, `write_pyproject`, `uv remove/add
  --no-sync` for the dependency diff, `uv lock`, `render.apply(force=True)`.
- `presets.check_name_free` (in `new`, `init` and their dry runs): the app name may not equal
  (normalised) a dependency of the resulting project: uv refuses self-dependencies and
  `src/<pkg>/` would shadow the library. `new` names the app after the folder, so
  `./deploy new ../flet --preset flet` fails with a hint to use `--name`. `new` also checks the
  name format before copying (no half-made project).
- **[template repo]** Root `src/`, `tests/` and `pytemplate.toml` must equal
  `presets/script/files` rendered with `name = "myapp"` (verified: `presets.pristine` is
  true). Edit the preset, then regenerate the root with `./deploy init script --name myapp
  --force`, or mirror the edit byte for byte.
- `copy_template(dest)` (used by `new`) skips `.git`, `.build`, `dist`, caches, `.flet`,
  `.venv*`, `template-repo` at any depth; `build/` and `.claude/` at the root; and
  `.github/workflows/template-*` (template CI files MUST use that prefix). `new` then runs
  `init <preset> --name <n> --force` inside the copy, `git init` and
  `git add --chmod=+x deploy deploy.ps1`.
- Hard-coded preset names in the runner: `render.ci_workflow` (raylib: apt GL/X11 libs, no
  PyPy on macOS), `methods/exe.build` (flet -> `flet pack`), `methods/flet.build` (flet only),
  `e2e.SMOKE` / `e2e.COMPILED_MARK` (expected app output per preset). A new preset that needs
  special packaging or smoke checks must touch these.
- raylib: the upstream stub lies (returns/fields/params declared `bytes`/`list` that are cdata
  or int at runtime); mypyc checks simple types at runtime, so they raise `TypeError` only when
  compiled. `tools/raylib_stubs.py` regenerates `typings/raylib/__init__.pyi` (task `stubs`).
  `typings/` is picked up by `render.typings_dir` (mypy `mypy_path`, pyright `stubPath`, ruff
  `extend-exclude`, `presets.OWNED_DIRS`). Also: `[[typing.mypy_overrides]] raylib
  ignore_errors`, `no-build-package = ["raylib"]` via `[uv]`, `forbid_imports = ["pyray"]`
  (~7x slower), exe `extra_args` exclude setuptools/pycparser/_distutils_hack, PyPy is the
  default active backend. raylib 6.0.1.0 publishes PyPy wheels only for `macosx_10_15_x86_64`,
  `manylinux x86_64` and `win_amd64` (section 15).
- flet: measured with Flet 1.0.1 + mypyc 2.3.1 in compiled code: `async` handlers get no event,
  generator handlers never run, `@ft.component` fails at import, `@ft.control` loses its event
  types; hence `forbid_imports = flet, flet_desktop, flet_cli`. Heavy work runs in a
  `ProcessPoolExecutor` (compiled code does not release the GIL). mypy overrides relax
  `{pkg}.ui.*`. Wheel entry `{pkg}.ui.app:run`. Task `dev` (`flet run -d -r`) has
  `background = true`.
- Editor buttons per preset come from each preset's `pytemplate.toml` `[vscode] buttons`
  (script `run test check build`, raylib `run bunnymark test build`, flet `dev run test
  build`). Every preset also has a `ci` task.

## 12. Editors

Editor generators are `editors/<name>.py` with `outputs(cfg, profile) -> dict[str, str]`
(path relative to ROOT -> content), merged by `render.outputs`. Their content must be
deterministic, LF, free of absolute or machine-specific paths (it is committed), and hashed
like every generated file.

`[tasks]` with `background = true` (flet `dev`): neither editor waits for them. VS Code gets
NO `isBackground`: that only works with a background problem matcher whose begins/ends
patterns tell VS Code the server is ready, `flet run -r` prints no stable ready line, and VS
Code would wait forever when such a task is a dependency; it is a restartable RUN task
instead. Neovim opens its output on start and replaces a running instance (`unique`).

### 12.1 VS Code (`editors/vscode.py`)

- `settings.json` = `.pytemplate/templates/vscode/settings.json` + the profile's `[vscode]` +
  (with `typing.editor = "basedpyright"`) `vscode.BASEDPYRIGHT_SETTINGS` + `[vscode] settings`
  (later wins). The template sets the automation terminal profiles,
  `tasks.statusbar.default.hide: true` and `files.watcherExclude` (`.venv*`, `.build`,
  `dist`). `extensions.json`: Python, Pylance or basedpyright (then Pylance is unwanted),
  debugpy, mypy type checker, Ruff, Even Better TOML, `actboy168.tasks`.
- Anything VS Code or an extension writes into the Workspace settings lands in this generated
  file, which then counts as hand-edited (`render --check` and the hook fail): such settings
  belong in `[vscode] settings`. basedpyright's extension checks `python.languageServer` (set
  by the Python extension: "Default") and, with Pylance installed,
  `python.analysis.typeCheckingMode` at every start, and writes its modal's answer there, so
  `BASEDPYRIGHT_SETTINGS` ships the answers (`"None"`, `"off"`; basedpyright reads its own
  `basedpyright.analysis` section and `pyrightconfig.json`, not these).
- `tasks.json`: every task is `"type": "process"`, never `"shell"`: shell tasks go through the
  user's terminal profile (xonsh, niubash, MSYS2) and break; process tasks run the launcher
  directly. `"command": "/bin/sh"`, `"args": ["${workspaceFolder}/deploy", ...]` (no exec bit
  needed) and `"windows": {"command": "${workspaceFolder}\\deploy.cmd", "args": [...]}`. In
  `version 2.0.0` per-OS `args` REPLACE the default ones, so both blocks carry the full args.
  VS Code applies the block of the OS where the task runs (the remote OS under WSL/SSH). Every
  task has `options.cwd = ${workspaceFolder}`.
- Catalog (`vscode.catalog`): `run`, `run <b>` per other supported backend, `test` (default
  test task), `test <b>`, `test all` and `check all` (only with more than one backend),
  `check`, `build` (default build task), `report --open` (label `deploy: report`; mypyc only),
  `compile` (mypyc only, hidden), `lint --fix`, `fmt`, `doctor`, `setup`, and one task per
  `[tasks]` entry. Labels are `deploy: <args>` (only the catalog's `report --open` hides its
  `--open`: `run --open` is not `deploy: run`; buttons that name the same task, `report` and
  `report --open`, get one task); each task has `detail` (`./deploy <args>  |
  <summary>`), `icon` and a presentation preset (RUN, CHECK, OTHER); run-like tasks use
  `runOptions {instanceLimit 1, instancePolicy terminateOldest}` (a re-run restarts instead of
  hitting cmd's Ctrl+C prompt). A `[tasks]` entry gets its matchers and presentation from
  `vscode.scan`, which follows its `deps` recursively (cycle-safe; unbalanced quotes fall back
  to `str.split`).
- Problem matchers are inlined in each task (only extensions can define named matchers):
  RUFF (concise format; any code `[A-Z]+[0-9]+` or a hyphenated name like `invalid-syntax`),
  MYPY error and note (`path:line[:col]: error|note: msg  [code]`, backslash paths on Windows,
  "See https://..." notes skipped), MYPYC (stage-relative paths because `mypyc_build.py`
  chdirs into the stage, mapped to `${workspaceFolder}/src`), RULES (`RULES_RE`, section 5.3),
  PYTEST (crash lines). Under mypyc (a task whose scan has both `pytest` and `mypyc`) pytest
  imports the stage, so two more PYTEST matchers relative to `${workspaceFolder}/src` map
  `.build[/wsl]/mypyc-{dev,release}/stage/X` (relative or absolute) and the stage-relative path
  mypyc records for a compiled module (`<pkg>/core/x.py`) back to `src/`; the main one skips
  both with a lookahead, so every line has exactly one matcher whatever VS Code's order. Before,
  such a problem opened the throwaway stage copy or an unopenable `/<pkg>/...` (autoDetect falls
  back to absolute). No compiled module (empty `compile.modules`): no mypyc/stage matchers.
  PYRIGHT (only with basedpyright; captures `info` out of `information` because VS Code maps
  `information` to Ignore and falls back to Error). Severity follows the task's typing
  profiles (`blocking`, ruff `exit_zero`); with several profiles the strictest
  wins. `RUFF_OUTPUT_FORMAT=concise` is set only in tasks that carry the ruff matcher (ruff's
  default `full` format is multi-line). Every regex must work in JavaScript AND Python `re`:
  `test_vscode.py` checks them against real ruff, mypy, mypyc, pytest and basedpyright output,
  runs real ruff, mypy, pytest (and a real mypyc build when a C compiler exists) on a project
  with one known defect per tool through each generated task's matchers, resolving every file
  like VS Code's `getResource` (it must open under `src/` or `tests/`), and compares every
  regex with Node's `RegExp` when `node` is installed. Changing `ui.error`/`ui.warn`,
  `lintc.Finding` or the tools' formats breaks them.
- Buttons: tasks named in `[vscode] buttons` get `options.statusbar = {"label": "<Name>",
  "hide": false, "running": {"icon": {"id": "sync~spin"}}}` (read by `actboy168.tasks`). The
  label is the bare capitalised name without an icon because the extension (0.16.1) prefixes
  the task's own `icon` itself. The extension creates buttons in `tasks.json` order, so button
  tasks come FIRST, in the configured order; a button that names no catalog task (e.g.
  `build --method pyz`) gets its own task. Without the extension the extra keys are ignored.
- `launch.json`: "src/main.py (CPython, interpreted)" first, with no `python` key (VS Code's
  selected interpreter; works under WSL); "src/main.py (CPython JIT)" (`.venv-jit`,
  `PYTHON_JIT=1`) when `python.jit`; PyPy (only when supported; per-OS `python`; the debugger
  is unreliable on PyPy); "Run mypyc stage (compiled modules cannot be stepped into)" when mypyc
  is supported (program `.build/mypyc-dev/stage/main.py`, `.venv` or `.venv-jit` python per
  OS through a `windows` block, `preLaunchTask: "deploy: compile"`, `pathMappings` src <->
  stage, `PYTEMPLATE_BACKEND=mypyc`; breakpoints bind in `main.py` and the interpreted modules,
  never in compiled ones: verified with a headless DAP client); "Tests (pytest)". Every config
  sets `PYTHONUTF8=1` (`vscode.DEBUG_ENV`, merged under each config's own `env`): without it
  `open()` without an encoding reads cp1252 on Windows (Python < 3.15) only under F5. nvim-dap
  reads these configs: keep names stable.
- `terminal.integrated.automationProfile.windows` = `${env:windir}\System32\cmd.exe`,
  `.linux`/`.osx` = `/bin/sh`: the debugger's `runInTerminal` picks its quoting from the
  shell's name, and anything that is not powershell/pwsh/cmd/bash (xonsh, `niu.exe`) gets cmd
  syntax, so F5 breaks. It applies only in trusted workspaces; `[vscode] settings` can
  override it.
- Limits: Ctrl+C in a Windows task triggers cmd's "Terminate batch job (Y/N)?" (use Terminate
  Task); under Remote-WSL on a Windows checkout the PyPy, JIT and mypyc launch configs point at
  the Windows-side paths (`.venv-pypy`, `.build/mypyc-dev`: no `-wsl` suffix, no `.build/wsl`);
  `\\wsl$` UNC paths do not work with `deploy.cmd`.

### 12.2 LazyVim / Neovim (`editors/nvim.py`, `.pytemplate/nvim/`, `cmd_nvim.py`)

Files:
- `.lazy.lua` (generated from `.pytemplate/templates/nvim/lazy.lua`): lazy.nvim's `local_spec`
  loads the first `.lazy.lua` found upward from Neovim's cwd, through `vim.secure.read` +
  `loadstring`. It MUST stay static (identical bytes in every mode and preset;
  `test_nvim_render.py` checks 7 configs): Neovim trusts it by the sha256 of its raw bytes,
  keyed by its real path, in `stdpath('state')/trust`. Any byte change (CRLF, a BOM, an edit)
  or moving the folder = untrusted again. Hence `.gitattributes` `.lazy.lua text eol=lf` and
  all logic in the plugin. Editing the template forces every user to re-trust: avoid it.
- `loadstring` gives the chunk no path: the root is found with
  `vim.fs.root(vim.uv.cwd(), "pytemplate.toml")`.
- Trusting `.lazy.lua` also trusts `.pytemplate/nvim/**`, loaded as a local plugin
  (`{ dir = root .. "/.pytemplate/nvim", name = "pytemplate.nvim", lazy = false, priority =
  900, main = "pytemplate", opts = { root = root } }`) and never re-hashed. `.lazy.lua` also
  declares `optional = true` specs whose `opts`/`config` delegate to the plugin: which-key,
  overseer, nvim-lspconfig, nvim-lint, neotest, nvim-dap, nvim-dap-python, venv-selector.
- `.pytemplate/editor.json` (`editors/nvim.editor_data`, schema 1): ASCII data only, relative
  paths only, no comments. Keys: `schema`, `generated`, `name`, `pkg`, `preset`, `gui`,
  `min_python`, `pypy_enabled`, `backend{active, supported}`, `typing{profile, editor, mypy,
  mypy_severity, python_version}`, `envs{tools, cpython, mypyc, pypy}` (without the `-wsl`
  suffix, which the plugin adds itself; `.venv-jit` for cpython/mypyc when `python.jit`),
  `mypyc_stage`, `tasks[{name, help, background}]`, `commands[{name, usage, summary, group}]`
  (from `cli.COMMANDS`), `build{methods, default}`. The Lua side (`init.sanitize`) validates
  every value (whitelists, patterns) and never runs a program named in it. `init.info` re-reads
  it when its mtime/size changes; it is regenerated only when `./deploy` runs (render-on-save
  of `pytemplate.toml` covers edits made in Neovim).
- Plugin (`.pytemplate/nvim/lua/`, documented in `.pytemplate/nvim/README.md`):
  `pytemplate/init.lua` (root, `info`, environments, uv lookup, `deploy_cmd`, `deploy_env`,
  `refresh`), `pytemplate/tasks.lua` (`META` per command, output parser, pickers, keymaps,
  `:Deploy`, render on save), `pytemplate/integrations.lua` (lspconfig, nvim-lint, neotest,
  overseer, which-key, venv-selector), `pytemplate/dap.lua`, `pytemplate/health.lua`
  (`:checkhealth pytemplate`), `overseer/template/pytemplate.lua` (provider) and
  `overseer/component/pytemplate/refresh.lua`.

Runner contract from Lua (the fourth caller of section 4.1): argv `{<absolute uv>, "run",
"--quiet", "--script", <root>/.pytemplate/deploy.py, ...}` (`init.deploy_cmd`) through overseer
/ `jobstart` with a LIST, env `PYTEMPLATE_CALLER_CWD=<cwd>` and `PYTEMPLATE_LAUNCHER=nvim`
(`init.deploy_env`). Never a string command (it would go through `'shell'`, which may be xonsh
or niubash) and never `deploy.cmd`/`deploy` unless uv is nowhere (the launcher prints the
install hints). The uv lookup mirrors the launchers because a GUI, launchd or MSYS2-login
Neovim may have a minimal PATH.

LazyVim wiring:
- Extras imported by `.lazy.lua` (only when the config is LazyVim): `lazyvim.plugins.extras`
  `.lang.python`, `.lang.toml`, `.dap.core`, `.test.core`, `.editor.overseer`. The local spec
  is appended AFTER the user's spec, so an extra not already enabled trips LazyVim's
  import-order check: `.lazy.lua` sets `vim.g.lazyvim_check_order = false` only then; the
  permanent fix is `./deploy nvim extras`. Imports are de-duplicated by module name.
- User options (in `lua/config/options.lua`): `vim.g.pytemplate_python_lsp = "pyright"`
  (default basedpyright), `vim.g.pytemplate_prefix` (default `<leader>j`),
  `vim.g.pytemplate_render_on_save = false` (default true).
- `vim.g.lazyvim_python_lsp` set from `.lazy.lua` is too late when `lang.python` is already
  enabled (read at first import): servers are switched by setting `opts.servers.<x>.enabled`
  in an lspconfig `opts` function (runs last). basedpyright by default (no Node.js; `.venv`'s
  `basedpyright-langserver`, else `uv tool run --from basedpyright basedpyright-langserver
  --stdio`, else Mason); pyright comes from Mason and needs Node.js. Pylance exists only in VS
  Code. pyright/basedpyright find `.venv` through `pyrightconfig.json` `venvPath`/`venv`, so
  venv-selector's automatic activation is turned off.
- ruff server from `.venv` with `mason = false` (same version as `./deploy check`). Mason
  prepends its bin dir to PATH after `.lazy.lua` runs, so always use absolute `.venv` paths.
- mypy (nvim-lint, core in LazyVim): `cwd = root` (finds `.mypy.ini`; mypy then prints paths
  relative to it, the only form the parser matches), `--python-version <min_python>
  --python-executable <.venv python>` when PyPy is supported (like `render.mypy_cli_args`),
  severity from `typing.mypy_severity`, disabled with the `off` profile or without `.venv`.
  On Windows nvim-lint wraps every linter in `cmd.exe /C`, where a quoted absolute path breaks
  with spaces or `& ^ %`: the linter runs the bare name `mypy` with `.venv\Scripts` first on
  PATH. nvim-lint REPLACES the environment when a linter has `env`, so it passes the full
  environment plus `PYTHONUTF8=1`, minus `VIRTUAL_ENV`.
- dap: nvim-dap spawns adapters with raw `uv.spawn` (no PATHEXT), so Mason's `.cmd` shims fail
  on Windows. Adapter order (`dap.adapter`): `.venv` python with debugpy (dev group), the tools
  python, Mason's debugpy venv python, an ephemeral `uv run --no-project --with debugpy`
  adapter; `initialize_timeout_sec = 30` (a cold adapter can take more than the default 4 s).
  The program runs on the cpython runtime env. nvim-dap reads `<cwd>/.vscode/launch.json`
  (per-OS blocks lifted, JSONC accepted) and expands `${workspaceFolder}` to the cwd: a
  provider covers a cwd below the root.
- neotest-python: always set `python` explicitly (auto-detection globs `*/pyvenv.cfg`, gets
  two lines with `.venv` + `.venv-pypy` and builds a broken path; its `uv run` fallback also
  syncs); `discovery.filter_dir` skips dot-dirs (`.venv*`, `.build`), `dist`, `build`,
  `typings`. neotest cannot pass `-o pythonpath=<stage>` or `PYTEMPLATE_*`, so mypyc/all runs
  go through the `deploy: test` task.
- overseer: the pytemplate provider (one template per command in `editor.json`, `report` and
  `compile` only with mypyc, plus every `[tasks]` entry) replaces the `.vscode/tasks.json` one
  (`disable_template_modules = {"overseer.template.vscode"}`), otherwise labels would be
  duplicated and tasks would go through `deploy.cmd`. overseer runs `"type": "shell"` tasks
  through `'shell'`: another reason to keep VS Code tasks `process`. Commands that can change
  the mode (`mode`, `setup`, `sync`, `lock`, `add`, `remove`, `render`, `init`) get the
  `pytemplate.refresh` component (re-read `editor.json`, LSP `didChangeConfiguration`, rebuild
  the mypy linter). Without overseer, tasks run in a terminal split.
- Output parser `tasks.parse_line` (overseer `on_output_parse` -> diagnostics + quickfix):
  strips ANSI, honours the `error: `/`warning: ` prefixes, reads basedpyright
  `  path:l:c - sev: msg` and `path:l[:c]: [sev: ]msg` (mypy, ruff concise, pytest crash
  lines); skips notes, `site-packages` and `in <func>` frames.
- Keymaps under `<leader>j` (which-key group "deploy"; `tasks.KEYS`): `j` pick, `r`/`R` run /
  run on a backend with args, `t`/`T` test / all, `c`/`C` check / all, `b`/`B` build / on a
  backend, `l` lint --fix, `f` fmt, `m` switch backend, `k` `[tasks]` picker, `d` `dev` task,
  `p` mypyc report, `s` sync all, `S` setup, `D` doctor, `w` task list, `x` stop deploy tasks.
  `:Deploy ARGS` (completion; no args = help). `<leader>j` was chosen because no LazyVim core
  or extra mapping uses it.

`./deploy nvim [doctor|trust|extras|bootstrap|sync]` (`cmd_nvim.cmd_nvim`):
- Neovim's directories come from one headless query, never hard-coded (`cmd_nvim.headless`:
  `nvim --headless --clean -n -i NONE -c "lua ..." -c qa!`, which prints one `PTNVIM{json}`
  line; the `-c` snippets are one line without double quotes because they cross the Windows
  command line; respects `NVIM_APPNAME` and `XDG_*`). On Windows `XDG_CONFIG_HOME=X` gives
  `X\nvim` and `XDG_DATA_HOME`/`XDG_STATE_HOME=X` give `X\nvim-data`.
- `doctor` (default; exit 1 on real problems): Neovim >= 0.11.2 (`MIN_LAZYVIM`), LazyVim
  installed, no `local_spec = false`, the trust of `.lazy.lua`, missing extras in
  `lazyvim.json`, tools (git, curl, tar required; rg, fd, tree-sitter, python, node, uvx, a C
  compiler optional), and ruff, mypy, debugpy in `.venv` (basedpyright optional).
- Trust DB `<state>/trust`: lines `<sha256|!> <path>` (CRLF on Windows), path = real path
  (backslashes on Windows; compared case-insensitively), hash over raw bytes. `trust` calls
  `vim.secure.trust({action = "allow", path = ...})` on >= 0.12 (`bufnr` form on 0.11), with
  the file in `$PT_TRUST_FILE`, after creating the state dir (the DB is opened with
  `io.open(..., "w")`), then re-reads the DB to confirm; already trusted -> nothing. Neovim
  0.12 has no "allow" button: the user picks (v)iew, runs `:trust` and restarts (lazy.nvim
  already skipped the file for that session).
- `extras` adds exactly the five extras above to `<config>/lazyvim.json` after a timestamped
  `.bak`, in LazyVim's own format; it refuses (exit 3) when there is no config or no
  `lazyvim.json` yet (start Neovim once). `bootstrap` clones the LazyVim starter and deletes
  its `.git`, only when the config dir does not exist. `sync` = `nvim --headless "+Lazy! sync"
  +qa` with cwd = ROOT; when `.lazy.lua` is untrusted it warns and runs from a temp dir (the
  trust prompt would hang a headless run).
- `cmd_nvim.doctor(check)` (from `./deploy doctor`): one line, silent without `nvim`, at most
  one headless call.
- Only started inside the project: `nvim path/x.py` from elsewhere, or a later `:cd`, does not
  load `.lazy.lua`. `.lazy.lua` edits need a restart (the watcher ignores it).

Headless and test gotchas: `VeryLazy` never fires in `--headless` (run `-c "doautocmd
UIEnter"`); an untrusted `.lazy.lua` blocks a headless run on `confirm()` (pre-trust through
the API); when `NVIM_LOG_FILE` cannot be written Neovim drops `nvim.log` into the cwd (always
set it); headless `jobstart` with a pty on Windows loses the output (use
`strategy = {"jobstart", use_terminal = false}`), and neotest's pty leaks pytest output into
stdout; Treesitter/Mason installs are asynchronous and may log errors that do not matter;
leftover grandchildren (Git's `tar.exe`) can hold pipes open after nvim exits, so harnesses
write to log files, not pipes. NEVER touch the user's real Neovim dirs in tests: set
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `NVIM_LOG_FILE` to a
short temp tree and unset `NVIM_APPNAME`.

## 13. Tests and verification

### 13.1 Suites

- `./deploy selftest [pytest args]` (`cli.cmd_selftest`): `python -m pytest -q -p
  no:cacheprovider .pytemplate/tests [args...]` in `.venv`, then `mypy --strict
  --no-incremental --python-version 3.11 --config-file .pytemplate/tests/mypy-runner.ini
  .pytemplate/runner .pytemplate/deploy.py`. Both must pass. Needs `.venv` (`./deploy setup`).
  mypy checks the host platform only: add `--platform linux` / `--platform darwin` by hand to
  check the other branches.
- `.pytemplate/tests/`: `test_runner.py` (config, render, lintc, imports, target keys),
  `test_no_spanish.py`, `test_launcher_sh.py` (static lint of the `deploy` header rules, `-n`
  syntax checks, `__probe` round-trips per shell found), `test_launcher_win.py` (static rules
  on every OS, a PowerShell parser check, Windows behaviour through `__probe`),
  `test_paths.py` (path spellings, colours in a hidden console, dry runs in a throwaway copy),
  `test_render_core.py` (`render.apply`/`auto` and `state.json` in a sandbox, the render
  command's exit codes, the managed pyproject parts for every preset and backend set, typing
  profiles, the generated CI for 36 preset/backend combinations through a strict YAML reader and
  `actionlint` when installed, every output clean and hash-seed independent; real taplo and the
  pinned basedpyright when the uv cache has them), `test_shells.py`, `test_vscode.py` (also real
  tool output through each task's matchers and a Node `RegExp` cross-check),
  `test_nvim_render.py` (also loads the Lua modules in
  `nvim --headless --clean`), `test_cmd_nvim.py`, `test_fixes.py` (regression tests of the
  runner fixes: portable smoke with `lib/`, lazy `{python}`, pyz `PYTHON_JIT`, binary preset
  files, `compile.annotate`, `sync_tree` ns mtimes, portable launcher quoting and version
  probes, unknown arguments, `app.preset`, pinned tools, flet pyproject, wheel options, JIT
  path), `test_e2e_plan.py` (the pure planning of `e2e.py`).
- **[template repo]** Language guard `test_no_spanish.py`: skipped unless
  `.pytemplate/template-repo` exists. Scans `git ls-files --cached --others --exclude-standard`
  (so new untracked files count) for accented Spanish letters and a list of Spanish words
  (including the template's former default app name). Add `lang: allow` to a line to allow it
  on purpose.
- Test rules: tests that need a missing tool or shell must skip cleanly (they also run on
  Linux/macOS CI). Tests that spawn `./deploy` must scrub `UV`, `VIRTUAL_ENV`,
  `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON` and `PYTEMPLATE_*` from the child env (pytest itself
  runs under `uv run`). Keep them fast: a runner start costs ~0.3 s, and the Windows launcher
  and path tests already take 10-40 s under load.
- `test_runner.py` matches message substrings (`unknown key 'backend.mode'`, `boolean`,
  `is not in backend.supported`, `exact version`, `clashes with`, and the lintc texts `flet`,
  `@cache`, `nested class`, `__file__`, `__main__`): rewording those messages means updating
  the tests in the same commit.
- `./deploy selftest --shells`: section 4.9.
- `./deploy selftest --nvim [PRESET,...] [--keep] [--fresh] [--require] [--timeout S]
  [--dir DIR]` (`nvimtest.selftest`): isolated LazyVim under `--dir` (default `%TEMP%\pt\nvim`,
  `$TMPDIR/pt-nvim` elsewhere): `<dir>/x/{config,data,state,cache}` = the `XDG_*` homes,
  `<dir>/base.json` = the base is complete, `<dir>/p/<preset>` = scratch projects,
  `<dir>/logs/` = one log per step. It stops unless Neovim reports every stdpath inside
  `<dir>/x`, refuses a `--dir` inside the template, and only wipes a dir carrying its marker
  `.pytemplate-nvim-test`. The starter is cloned and `Lazy! sync`ed once and reused (unless
  `--fresh`); then per preset: `./deploy new` (name `pt-<preset>`), `./deploy sync cpython`
  (clean env), trust through the API, `Lazy! install` from the project, and
  `nvim --headless -c "doautocmd UIEnter" -c "luafile .pytemplate/nvim/tests/smoke.lua"` with
  `PT_ROOT=<project>` (timeout 600 s). Per-preset table with timings; exit 1 on any FAIL,
  non-zero exit, timeout or no result lines. Without `nvim`/`git`: SKIP with exit 0, or exit 3
  with `--require`; unknown preset: exit 2. First run ~1-2 min for the base, then ~7-35 s per
  preset.
- `smoke.lua` output contract (parsed by `nvimtest.parse_smoke`): one stdout line per check,
  `ok   NAME`, `FAIL NAME` followed by the error indented 5 spaces, or `SKIP NAME (reason)`
  (only for things that need a network install: a language server via uvx/Mason, a treesitter
  parser; basedpyright is SKIPped after 150 s); exit 0 via `qa!`, 1 via `cq!`; a 20-minute
  watchdog; `PT_ROOT` optional. Parse only lines that start with those markers. It runs
  `./deploy help`, `render` and `lint`, and briefly creates `src/<pkg>/_pt_smoke_lint.py` and
  `_pt_smoke_mypy.py`. 19 checks, including a debugger stopping at a breakpoint.
- `./deploy selftest --e2e [PRESET ...] [--backends B,..] [--methods M,..] [--quick|--full]
  [--gui auto|on|off] [--keep] [--reuse] [--json] [--base DIR]` (`e2e.selftest`): per preset
  (default script, raylib, flet) `./deploy new <base>/<preset>` from THIS template with app
  name `e2e-<preset>`, a verify step (`deploy` 100755; no `template-repo`, `template-*.yml`
  or `.claude` copied), `render --check`, `mode` only where the host cannot install a backend
  (`e2e.HOST_GAPS`: raylib + PyPy on macOS arm64 -> `mode cpython --supports cpython,mypyc`,
  the pypy rows SKIP, as the generated `ci.yml` drops it), `setup`, `doctor`, `check all`,
  `test all`, `run`
  per backend, and `build <b> --method <m> --no-check` for every pair `cmd_build.COMPAT`
  allows (non-empty `dist/` output), then smoke runs of the headless artifacts of console
  presets (exe, portable launcher, `python -S <pyz>` with its cache redirected, the wheel in a
  scratch venv, nuitka; output must contain the preset's text and, for mypyc, the compiled
  marker). Default: every method but nuitka; `--quick`: each backend's default method;
  `--full`: + nuitka and a `mode --supports +/-pypy` round trip. `flet build` is SKIP unless
  Flutter and (Windows) Developer Mode are available; GUI runs are SKIP without a display
  (Linux uses `xvfb-run`) or on Windows/macOS CI. Layout: `<base>/<preset>`,
  `<base>/logs/<preset>/NN-step.log`, `<base>/work/<preset>` (smoke scratch); default base
  `%TEMP%\pt\e2e` / `$TMPDIR/pt-e2e`; only a base carrying `.pytemplate-e2e` is wiped. Steps
  run with stdin closed, a clean env and per-step timeouts that kill the whole process tree; a
  failed `new`/`setup` skips the rest of its preset; a smoke needs its build. The base is kept
  on failure or `--keep`; `--reuse` reuses kept projects. `--json` report to stdout.
  Measured: script default ~3.5 min, raylib+flet `--quick` ~3 min, `--full` with nuitka ~12
  min.
- `./deploy render --check` (exit 1 when something is outdated or hand-edited) and
  `./deploy doctor`.
- Manual end-to-end: `./deploy new C:\t\p1 --preset <p> --name <n>`, then in the copy
  `./deploy setup`, `test all`, `check all`, `build <b> --method <m>`. Keep the path short.

### 13.2 CI

- `.github/workflows/ci.yml` is generated for every project (`render.ci_workflow` from
  `templates/ci.yml`: placeholders `__HEADER__`, `__MATRIX__`, `__LINUX_DEPS__` (its line must
  exist exactly), `__NAME__`, `__BUILD_BACKEND__`; action majors pinned: bump deliberately;
  `astral-sh/setup-uv` publishes no floating major tags since v8 (`@v10` does not resolve), so
  it is pinned to an exact release, `v10.2.0`, in every workflow; deleting the template
  disables CI generation; a placeholder left behind is a DeployError). It triggers on pushes to
  `main` AND `master` (a plain `git init` gives either), pull requests and manual dispatch. Its
  first step is `./deploy render --check` (no environment needed; the generators read no
  platform state, CRLF/BOM checkouts count as up to date): a `pytemplate.toml` edit committed
  without rendering or re-locking (web editor, no hook) fails there instead of being rendered
  silently inside the runner. It never runs `setup`: it `sync`s only the matrix backends of
  each OS (so raylib drops PyPy on macOS; an OS left with no backend gets no matrix row), then
  `check all`, `test` per backend, a pyz per OS, and `pyz-merge` into one cross-platform
  `.pyz`.
- **[template repo]** `template-launchers.yml` (Linux/macOS shells + shellcheck, Windows with
  MSYS2, Cygwin and busybox-w32, optional WSL job; `selftest --shells` plus user-style
  invocations; a gate job checks the marker file), `template-nvim.yml` (Ubuntu + Windows,
  Neovim stable, `fd` (venv-selector from LazyVim's `lang.python` errors on the first Python
  buffer without it), `selftest --nvim --require --dir $RUNNER_TEMP/pt-nvim` (the `runner`
  context is not allowed in a job-level `env`, hence the step env), logs on failure),
  `template-e2e.yml` (3 OS x 3 presets, dispatch quick/default/full, weekly, pushes that touch
  `.pytemplate/**` or the launchers; Linux gets the raylib libs, `libgl1-mesa-dri` and
  `xvfb`; JSON report always uploaded, logs on failure). First run on GitHub in September 2026
  (images ubuntu-24.04, macos-26-arm64, windows-2025-vs2026; uv 0.12, Neovim 0.12.5).

### 13.3 Coverage limits

Developed on Windows 11: the Linux/macOS code paths (launcher branches, `.sh` launchers, xvfb,
pyz cache in `HOME`, the nvim harness) are exercised by the CI workflows. Not installed locally, CI only: zsh,
ksh, mksh, yash, fish, nu, Cygwin, busybox-w32, WSL, macOS bash 3.2. Untested anywhere so far:
PowerShell 6.x-7.2, a UNC current folder, uv found only in `ProgramFiles` or chocolatey, a
PATH entry with quotes in `deploy.cmd`, the interactive install prompt, Neovim 0.11 (only
0.12.5), pyright via Mason, VS Code itself (buttons, Problems panel: only simulated), `flet
build` (Developer Mode is off), bundled PyPy portable builds on CI, Ctrl+C handling of the
harnesses.

## 14. Conventions and recipes

Runner code:
- stdlib only; Python 3.11 syntax and APIs (`tomllib` is why the floor is 3.11); `mypy
  --strict` clean on linux, darwin and win32 with `warn_unreachable`. Put Windows-only code
  (`winreg`, `ctypes.WinDLL`) INSIDE `if sys.platform == "win32":` (see `cmd_env._long_paths`,
  `methods.flet._developer_mode`, `shells.registry_path_dirs`, `ui.enable_vt_mode`), never
  behind an early `if sys.platform != "win32": return`: mypy would flag the rest as
  unreachable. Match the neighbouring modules' style (`from __future__ import annotations`,
  module docstring, typed signatures).
- Output through `ui` (stderr), except output meant for pipes (5.3).
- Expected failures raise `DeployError(msg, 2)` (usage/config) or `DeployError(msg, 3)`
  (missing requirement), with an actionable hint in the message.
- Processes only through `proc.run/output` or `envs.uv/uv_run`, as argv lists, never
  `shell=True`. `proc.run` defaults to cwd = ROOT and `proc.base_env()`. Long-running
  harnesses (`shells`, `nvimtest`, `e2e`) use `subprocess` directly with stdin closed, output
  to log files and timeouts that kill the process tree.
- Text files: `encoding="utf-8", newline="\n"`; write `"\ufeff"`, never a literal BOM; read
  `pytemplate.toml` as `utf-8-sig`; parse Python sources as bytes (`imports.parse`).
  Generated `.cmd` files: ASCII, explicit `\r\n`, written with `newline=""`.
- Project paths from `project.*` (never the cwd); user-typed paths through
  `project.user_path`.
- Honour `proc.DRY_RUN` for side effects (section 5.4).
- Reject unknown arguments (section 5.2, item 5).
- Never design CLI syntax that needs `--`, empty-string arguments or cmd metacharacters
  (PowerShell and cmd mangle them).

Adding a command:
1. `Command(module, func, summary, usage, render, group)` in `cli.COMMANDS`; `render=False` if
   it must work without (or before) rendering.
2. `def cmd_x(cfg: Config, args: list[str]) -> int` in a `cmd_*.py`, argparse with
   `prog="./deploy x"`; reject unknown arguments (`cmd_dev.only_flags` for flag-only commands).
3. `./deploy render` and commit `.pytemplate/editor.json` + `state.json` (section 6.2).
4. Tests in `.pytemplate/tests/`; README commands table; VS Code task catalog
   (`editors/vscode.catalog`, and `vscode.scan` if its output should reach the Problems panel)
   and the Neovim metadata (`tasks.META`: tag, backend picker, parse, refresh) if editors
   should offer it. The xonsh completion reads `cli.COMMANDS` when the snippet is printed.

Adding a build method:
1. `methods/<m>.py` with `build(req: BuildRequest) -> Path`, output via
   `dist_path(req, suffix)`, work dirs under `BUILD/<m>/<backend>`.
2. Add it to `config.METHODS` and `cmd_build.COMPAT` (a reason string per unsupported
   backend); a `[deploy.<m>]` dataclass in `DeployConfig` plus `validate` rules.
3. For mypyc use `mypyc.exe_stage` and `mypyc.hidden_imports`.
4. README matrix; `templates/ci.yml` if the output naming changes; `e2e.find_artifact` /
   `e2e.smoke_target` and `SMOKE_METHODS` if it should be smoke-run.

Adding a preset:
1. `presets/<p>/preset.toml` and `files/` (section 11), including `[vscode] buttons` and
   `[tasks]` (`background = true` for dev servers) in its `pytemplate.toml`.
2. Copy `tests/conftest.py` verbatim. Check the hard-coded preset branches (section 11).
3. It must pass `./deploy selftest --e2e <p>` and `./deploy selftest --nvim <p>`.

Adding a typing profile:
1. `templates/typing/<p>.toml` with the keys of section 8.
2. Extend `config.PROFILES`, the allowed `typing.relaxed` values, the `cmd_mode` `--typing`
   choices, the auto/relaxed mapping, and the Lua whitelist `PROFILES` in
   `.pytemplate/nvim/lua/pytemplate/init.lua`.

Adding an editor: `editors/<name>.py` with `outputs(cfg, profile)`, merged in `render.outputs`
(section 12); a doctor hook called from `cmd_env.cmd_doctor` if it needs checks; tests that
render it for several configs; README section.

Files and git:
- `.gitattributes`: `* text=auto eol=native`; `*.bat`, `*.cmd`, `*.ps1` CRLF; `*.sh` LF; then
  `deploy`, `deploy.ps1` and `.lazy.lua` LF (the last matching line wins); `*.png *.ico *.pyz`
  binary. With `core.autocrlf=true` most working-tree files are CRLF on Windows: that is fine,
  the runner normalises.
- `deploy` and `deploy.ps1` are 100755: `cmd_env._fix_exec_bit` repairs both on `setup`
  (`core.filemode=false` on Windows loses the bit), `presets.new` marks both, `init` chmods
  both on POSIX. `deploy.cmd` stays 100644.
- Default app content lives in `presets/script/files/` (section 11).

## 15. Known issues and fragile points (still open)

Behaviour:
- `cmd_dev.split_backend` treats a first argument equal to `cpython`, `pypy` or `mypyc` (and
  `all` for `test`/`check`) as the backend: an app argument with that value must be preceded
  by an explicit backend (`./deploy run cpython mypyc`). By design: the usual fix, `--`, is
  eaten by PowerShell.
- Tasks: a task with `backend = "mypyc"` runs interpreted in `.venv` unless it goes through a
  `deps` entry such as `compile` and runs the stage.
- `config.set_value` only edits single-line entries.
- `clean --envs` removes every `.venv*`, including the environments in use; there is no
  "unused only" option (`mode` only prints a note about leftovers).
- raylib + PyPy on macOS arm64: no PyPy wheel for that platform (raylib 6.0.1.0 still has
  none) and `no-build-package`, so `./deploy setup` of a raylib project fails to sync
  `.venv-pypy` on Apple Silicon (confirmed on `macos-latest`: "marked as `--no-build` but has
  no binary distribution"). The generated `ci.yml` syncs only the matrix backends and
  `selftest --e2e` switches the project off PyPy first (`e2e.HOST_GAPS`); users there run
  `./deploy mode cpython --supports cpython,mypyc`. Possible fix: skip PyPy for raylib on
  macOS aarch64 in `setup`/`new`.
- WSL on `/mnt`: `pyrightconfig.json` (`venv: ".venv"`) and the PyPy/JIT/mypyc launch
  configs ignore `ENV_SUFFIX`, so VS Code and pyright inside WSL point at the Windows-side
  environments (the Neovim plugin adds `-wsl` itself). Untested.
- MSYS2 without a login shell (the xonsh `!m` route): `MSYSTEM_PREFIX`, `EXEPATH` and `SHELL`
  do not reach the runner, so `/home/...`-style paths typed for `new`/`pyz-merge` fall back to
  the current drive with a warning. Fix idea: the launcher exports the Windows path of
  `/usr/bin/cygpath` and `find_cygpath` checks it first.
- niubash: the generated portable `.sh` launcher leaks its variables into the calling
  session (niubash runs sh scripts in-process).
- Argument limits by design: `deploy.cmd` (and every CreateProcess caller of it) cannot pass
  `% ! " ^ & | < >`; PowerShell drops a bare `--`; xonsh `-c` exits 1 on any failing command
  (the child's real code is in its `CalledProcessError`).
- `uv build` drops a `.gitignore` into `dist/<n>-<b>-wheel/`.

Editors:
- VS Code problem matchers and the Neovim parser depend on tool output formats (ruff, mypy,
  pytest, basedpyright) that can change between tool versions. The pytest matcher is best
  effort (the message is only the exception name, because the `E` lines come before the
  location; exception names not ending in Error, Exception, Failed, Warning, Exit or Interrupt
  are missed); for multi-line basedpyright messages only the first line is used (the rule code
  is lost); MSVC/gcc errors are not matched.
- `actboy168.tasks` is a small third-party extension (disabled in Restricted Mode).
- Stopping the flet `dev` task (hot reload) on Windows, from VS Code or Neovim, relies on
  ConPTY closing and uv's job object killing `flet.exe`: not verified.
- Extras imported from `.lazy.lua` show as "not managed" in `:LazyExtras` and change
  `lazy-lock.json` until `./deploy nvim extras` is run; `nvim extras` does not detect a
  `vim.g.lazyvim_json` override.
- `selftest --nvim` does not pre-install the treesitter python parser or warm basedpyright:
  on a cold cache those smoke checks SKIP and the first run is slow.
- On Windows the debugger prints harmless noise on disconnect (debugpy's "NoMoreMessages"
  traceback, "adapter exited with 1").

Code coupling (rename together):
- `hooks` imports the private `cmd_dev._profile_file` and `shells.launcher_problems`, and loads
  `.pytemplate/tests/test_no_spanish.py` by path (it needs `offending_lines`, `ALLOWED_PATHS`,
  `BINARY_SUFFIXES`, and only `pytest.mark` at module level); `rename` calls the private
  `config._build`, `presets._set_project_name` and `presets._norm_name`.
- `upx.BUILTIN_EXCLUDE` must keep `flutter_windows.dll`; `nuitka._flet_client_archive` mirrors
  flet_desktop's download URL and its `flet_desktop/app/` lookup.
- `cmd_mode._config_from_text` and `e2e.preset_info` call the private `config._build`;
  `e2e.flet_build_reason` imports `methods.flet._developer_mode`; `cmd_nvim.c_compiler`
  imports `cmd_env._msvc` lazily (`cmd_env` imports `cmd_nvim`).
- `RULES_RE` / `tasks.parse_line` <-> `ui.error`, `ui.warn`, `str(lintc.Finding)` (5.3).
- `editor.json` <-> `cli.COMMANDS` (6.2); `cmd_nvim.EXTRAS` <-> the extras list in
  `templates/nvim/lazy.lua`; `vscode.MYPYC_STAGE` / `editor.json` `mypyc_stage` /
  `vscode._STAGE` (the pytest stage matcher) <-> `mypyc.profile(cfg, ...).stage`; the CI pyz path
  <-> `BuildRequest.out_name` (10; `test_ci_workflow_for_every_preset_and_backend_set`).
