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
- Presets `script`, `raylib`, `flet` (`.pytemplate/presets/*`): skeleton + deps + config. The
  preset is chosen when the project is created (`./deploy new DIR --preset P`); there is no
  public `init` (section 11).
- Six build methods: `exe` (PyInstaller / `flet pack`), `portable`, `pyz`, `wheel`, `nuitka`,
  `flet` (`flet build`).
- `./deploy` is a Justfile-like runner. The only hard requirement is uv.
- `pytemplate.toml` is the single source of truth; every derived config is generated. After
  editing it, `./deploy apply` brings the whole project in line (section 5.8); `./deploy setup`
  is the same operation under its first-time name.
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
.pytemplate/state.json            hashes of the generated files + the `applied` record (committed)
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
  (junction, symlink). Otherwise `Path.cwd()`; a deleted cwd raises `DeployError`, which only
  a runner started without uv sees (tests): uv itself refuses to start in a deleted folder
  (`error: No such file or directory (os error 2)`, exit 2, for every command, `help`
  included). `uv run --script` never changes the cwd.
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
| `cli.py` | `COMMANDS` table of `Command(module, func, summary, usage, render, group)`, modules imported lazily; `FORWARDS` / `HELP_PASSES_THROUGH` (section 5.2); `INTERNAL` (routes listed nowhere: `__init`). `_parse_globals`, `dispatch` (also the exit-2 hint of the removed `init`), `main`/`_main` (exception -> exit code, closed stdout), `cmd_help` (commands and `[tasks]` entries), `cmd_tasks`, `cmd_selftest` (plain, `--shells`, `--nvim`, `--e2e`), the `__probe` route, `EXAMPLES`. |
| `config.py` | Dataclass schema (`SCHEMA`, `DEFAULT_METHODS`), `read_text` (UTF-8 only, clear error otherwise), strict loader (`_build`: unknown key or wrong type -> error with the full key path), `validate`, derived values (`pkg`, `min_python`, `pypy_minor`, `profile_for`, `pypy_enabled`), `compiled_paths`, comment-preserving editor `set_value` / `update_file` (section 6.1), `toml_value`. |
| `project.py` | Paths (`ROOT`, `SRC`, `BUILD`, `DIST`, `TEMPLATES`, `PRESETS`...), `IS_WINDOWS/IS_MACOS/IS_WSL`, `ENV_SUFFIX`, `venv_python`, `host_os/host_arch` (uv names), `rel`, `code_dirs`, `native_path`, `find_cygpath`, `caller_cwd`, `user_path`. |
| `ui.py` | All runner output to stderr; `DeployError(msg, code)`; `VERBOSE/QUIET`; `report` (never hidden by `-q`); colours (`color_enabled`, `enable_vt_mode`); `check_line` (doctor lines `[ok]`, `[XX]`, `[--]`). |
| `proc.py` | `find_uv`, `base_env` (`UV_SELECTION`), `run` (echo, `DRY_RUN`, cwd defaults to `ROOT` and must be a folder, UTF-8 capture, waits through Ctrl+C), `output`, `show` (display quoting only), `exit_code` (signal N -> 128+N), `vs_installer_dir`, `CommandFailed`, `Interrupted`. |
| `envs.py` | `PyEnv(key, dir, request, preference)`; `cpython_env`, `pypy_env`, `tool_env` (always CPython), `runtime_env(backend)`, `env_vars`, `uv`, `uv_run` (= `uv run --locked`, plus `--project <ROOT>` when `cwd` is not the root: section 7), `sync` (all groups), `interpreter_info` (with `platform`); `MIN_UV`, `uv_version`, `uv_problem`, `require_min_uv`, `UV_UPDATE`, `uv_error` (uv's `error:` message). |
| `render.py` | Every generated file (`outputs`), hand-edit detection (`apply`, `auto`), typing profiles (`load_profile`), `mypy_ini`, `mypy_cli_args`, `pyright_config`, `ruff_config`, `to_toml`, `jsonc`, `ci_workflow`, managed pyproject parts (`managed_block`, `write_pyproject`, `pyproject_outdated`, `check_pyproject`). |
| `editors/vscode.py` | `.vscode/settings.json`, `extensions.json`, `launch.json`, `tasks.json` (`catalog`, `scan`, `problem_matchers`; section 12.1). |
| `editors/nvim.py` | `.lazy.lua` (verbatim template copy) and `.pytemplate/editor.json` (`editor_data`; section 12.2). |
| `presets.py` | Preset discovery/loading, option merge, `uv_extras`, `dependencies`, `skeleton`, `pristine`, `check_name_free`, `init` (run by `./deploy __init`), `copy_template`, `new`; for apply and rename: `default_options`, `option_dependencies` (the requirements with an `{option}`), `set_project_name` / `project_name` (the `[project]` table only), `shadows_stdlib` (`STDLIB_OTHER_VERSIONS`). |
| `mypyc.py` | Incremental stage (`sync_tree`), `spec.json`, spawning `tools/mypyc_build.py`, `ANNOTATE_HTML`, `hidden_imports`, `exe_stage`, `runtime_env_vars`, `has_compiler_hint`. |
| `imports.py` | AST import extraction that skips `if TYPE_CHECKING:` blocks; parses bytes (tolerates a BOM). |
| `lintc.py` | Extra AST rules for compiled modules (section 9): `lint_file(cfg, path)`, `lint`, `Finding`. |
| `tasks.py` | `[tasks]`: `Placeholders` (lazy `{python}`), `deps` (each once per invocation), cycle detection, `run_task`, `describe`, `list_tasks`. |
| `cmd_env.py` | `setup` (= `cmd_apply.apply(command="setup")`), `doctor` (calls `cmd_apply.doctor`), `sync`, `lock`, `add`, `remove`, `clean` (`_env_dirs`, `_remove`, `_is_link`); `ensure_lock`; `_fix_exec_bit`; `_c_compiler`, `_msvc(platform)`, `_xcode_problem`, `_long_paths`. |
| `cmd_apply.py` | `./deploy apply [--force]` / `setup [--force]` (section 5.8): `make_plan` (every refusal before the first write), `apply`, `_print_plan` (--dry-run), the `applied` record (`load_record`, `save_record`, `trusted_record`, `project_record`, `record_of`, `rename_record`), `applied_state` / `_applied_preset` / `applied_name`, `dependency_changes` (`DepChanges`, `req_key`), `read_project`, `pending` + `doctor` (changes not applied yet), `reference_problems`, `unused_envs`. |
| `cmd_mode.py` | `mode` (+ the Python 3.11 precheck before enabling PyPy), `render`, `new`, the internal `__init` (`cmd_init`), and their `--dry-run` planners (`_plan_mode`, `_plan_init`). |
| `cmd_dev.py` | `run`, `compile`, `check` (`run_checks`), `lint`, `fmt`, `test` (`test_backend`), `report`; `split_backend`; `only_flags`; `_profile_file`; `BASEDPYRIGHT`, `BASEDPYRIGHT_NODE`. |
| `cmd_build.py` | `build`: backend + method resolution, `COMPAT`, `payload`, `BuildRequest`, `dist_path`; `pyz-merge`. |
| `methods/*.py` | One `build(req: BuildRequest) -> Path` per method; `common.py` has target keys (`parse_key`, `check_key`, `targets_for`), `UV_PLATFORMS`/`host_floor`, `ensure_env`, `export_requirements`, `install_deps`, `drop_install_junk`, `has_native`, `skipped_requirements`, `copy_app`, `uses_tkinter`, `windowed`, `tree_bytes`; `nuitka.NUITKA`/`NUITKA_PYTHON`, `check_python`, `check_options`, `optimization_args` (`[deploy.nuitka]` lto/pgo); `pyz.check_parts`, `merge`. |
| `shells.py` | `__probe`, launcher/shell doctor checks, `shell-setup` snippets, `selftest --shells` (section 4.9). |
| `cmd_nvim.py` | `./deploy nvim ...` and `doctor(check)` (section 12.2). |
| `nvimtest.py` | `selftest --nvim` (section 13.1). |
| `e2e.py` | `selftest --e2e` (section 13.1). |
| `hooks.py` | `./deploy hooks [install [--force]\|uninstall\|run\|status]`, `ensure_installed` (apply/setup), `doctor`: the native git pre-commit hook (section 5.6); `find_repo` (`NotInGit`), `classify`, `hook_script`/`launcher_of`, `install`/`uninstall` (apply removes the hook when `hooks.pre_commit = false`), `checks`. |
| `rename.py` | `./deploy rename NEW_NAME [--force]` and the rename step of apply: pure `plan` / `apply_plan` (undoes itself when a write fails) / `rewrite` (tokenizer + `ast` scopes + context rules, `MODULE_KEYS`), `check_new_name` (`locked_names`), `git_changes`, `dirty_tree_message`, `validate_config`, `tidy_before`/`tidy_after` (ruff, `Tidy`), `report`, `cmd_rename` (section 5.7). |
| `upx.py` | Optional UPX packing: pinned download (`VERSION`, `ASSETS` with SHA-256), `find`, `active`, `level_flags`, `env_value`, `excludes`, `candidates`, `pack_file`, `pack_tree`, `MAX_INPUT` (section 10). |

### 5.2 Call flow

1. Launcher -> `uv run --quiet --script .pytemplate/deploy.py ARGS`. uv picks any Python >= 3.11
   for the PEP 723 script, often in an ephemeral env, and exports `UV`.
2. `cli.main`: `__probe` short-circuit, then `_parse_globals` (global flags must come BEFORE the
   command: `-v/--verbose`, `-q/--quiet`, `--dry-run`, `--no-render`, `-h/--help`; `-h` keeps
   the command: `./deploy -h run` = `help run`; `--dry-run` switches `-q` off), then
   `dispatch`.
3. `dispatch`: `help` needs no config. `-h`/`--help` anywhere after a builtin prints `help
   COMMAND` (no config load, nothing runs), except after `cli.HELP_PASSES_THROUGH` (`run`,
   `test`, `lock`, `selftest`), where it goes to the app, pytest, uv or the suite. Otherwise
   `config.load(set(COMMANDS))` (validates; task names may not shadow builtins). Builtins run
   `render.auto(cfg)` first when `Command.render` is true and `--no-render` is not set, then
   `module.func(cfg, args)`. Names in `[tasks]` run `render.auto` and
   `tasks.run_task(cfg, name, args, dispatch)`; `-h` after a deps-only task prints its help.
   `help NAME` also describes a `[tasks]` entry (cmd, deps, backend, cwd, env); an unknown
   NAME, or a second one, exits 2. `cli.INTERNAL` routes (`__init`) dispatch like builtins but are
   listed nowhere (help, `editor.json`, the editors' task lists and the shell completion read
   `COMMANDS` only). `init` is no longer a command: unless a `[tasks]` entry took the name, it
   exits 2 with the hint `./deploy new DIR --preset P`.
4. Commands with `render=False`: `clean`, `render`, `new`, `pyz-merge`, `tasks`,
   `shell-setup`, `selftest`, `help`, `hooks` (the hook must not rewrite generated files in
   the middle of a commit), `apply` and `setup` (they render at the end: a refused apply, e.g.
   a hand-edited `app.preset`, writes nothing), `rename` (renders after its checks, never with
   a hand-edited `app.name` before the dirty-tree check). `test_cli_core.NEVER_RENDER` pins
   this list.
5. Commands reject unknown arguments with exit 2 (a typo is never silently ignored):
   argparse commands, `render`/`mode`/`__init`/`new` (`cmd_mode._parse`), `lint`, `fmt`,
   `clean`, `apply`, `setup` (only `--force`), `doctor`, `tasks` (`cmd_dev.only_flags`), `check` and `sync` (extra
   positionals), `shell-setup`, `help`, a `[tasks]` entry without `cmd`. By design
   (`cli.FORWARDS`): `run`/`test` forward the rest, `build` forwards unknown flags to the
   packager of exe, nuitka and flet only (`cmd_build.PASSTHROUGH`; pyz, portable and wheel
   refuse them, `--onefile/--onedir` apply to exe/nuitka, `--target` to pyz, and a bare word or
   a global flag such as `--dry-run` after `build` is an error), `lock` to `uv lock`, plain
   `selftest` to pytest, tasks with a `cmd` to it.
   `test_cli_core.test_every_command_rejects_an_unknown_argument` runs every other command
   with a bogus flag and a bogus positional: a new command fails it until it is classified
   (`MINIMAL` there, or `cli.FORWARDS`).
   `mode` also rejects abbreviations (`allow_abbrev=False`), an option given twice,
   `--supports` mixing `+`/`-` changes with plain names or adding and removing the same backend,
   and a BACKEND that `--supports` removes or leaves out of a full list
   (`cmd_mode._supports_after`); stray commas and spaces in `--supports` are ignored.

### 5.3 Exit codes and output

- 0 ok; 1 = check/test failures, doctor problems, any FAIL in a selftest suite, or an internal
  runner error (traceback printed); 2 = usage/config (`DeployError` default, argparse; also a
  program that cannot be started: no exec bit, no `#!` line, a folder; a working folder that
  does not exist; bad `[tasks]` entries); 3 = missing requirement (uv, a uv older than
  `envs.MIN_UV`, a program, compiler, interpreter, Neovim/git with `--require`); 130 = Ctrl+C;
  141 = the reader of stdout went away (`./deploy help | head -1`: quiet, no traceback; POSIX
  only, Windows reports a closed pipe as `OSError` EINVAL, unhandled). `run`, `test BACKEND`
  (pytest's own code: 5 = no tests collected, 4 = usage error) and tasks return the child's
  exit code (`test all`: 0 or 1, after testing every backend even when one fails to build);
  `proc.CommandFailed` carries the failed child's code. A child killed by signal N gives
  128 + N (`proc.exit_code`, like sh and uv; `cli.main` also maps a negative code a command
  returns), never 256 - N.
- Ctrl+C (`proc._wait_through_ctrl_c`): the child got the same Ctrl+C (terminal process group,
  console), so `proc.run` waits for it instead of letting `subprocess.run` SIGKILL it 0.25 s
  later (an app's cleanup was cut short; under `uv run` the app kept running as an orphan). A
  no-op Python handler records the Ctrl+C, only in the main thread and only while SIGINT has
  its default handler (a runner started with SIGINT ignored keeps passing SIG_IGN on). Then
  `proc.Interrupted` (a KeyboardInterrupt) stops the command, so `check`, `test all` and task
  deps never go on to the next step; `cli.main` prints `error: interrupted` and exits with the
  child's code, or 130 when it exited 0 (an interrupted command never reports success) or died
  of the Ctrl+C (`STATUS_CONTROL_C_EXIT` on Windows). A child that ignores SIGINT is waited
  for, like `uv run` does. Ctrl+C in the runner's own Python code: KeyboardInterrupt, 130.
- Runner output goes to stderr through `ui` so the app keeps stdout. Exceptions, printed to
  stdout on purpose: `help`, `__probe`, `shell-setup` snippets, and the `--json` reports of
  `selftest --shells` (also with `--list`) and `selftest --e2e`.
- `-q` hides progress (`ui.step`, `ui.command`, `ui.ok`, `ui.info`), never what was asked for:
  `ui.report` (the `tasks` list, the stderr of a failed query), warnings, errors and
  `check_line` always print, and a dry run ignores `-q` (its output is the plan). Still
  `ui.info` (hidden by `-q`): the `mode` display (`cmd_mode._describe`) and `render
  --check/--diff` lists (`cmd_mode.cmd_render`, `render.apply`).
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

- `proc.run` skips only ECHOED commands (`echo=True`) and reports them as exit 0 (without
  checking their working folder: a skipped step would create it); `echo=False` queries still
  run. So `run`, `test`, `check`, `add`, `remove`, `sync`, tasks and the plain `selftest` only
  print their commands; in-process work (the `lintc` rules) runs. A task's deps are echoed,
  each once, and its `cwd` is not checked. `-q` has no effect: the output is the plan.
- `selftest --shells|--nvim|--e2e` refuse `--dry-run` (exit 2): they start shells, Neovim and
  `./deploy` runs themselves, in their own scratch folders.
- `render.apply` behaves like `--check` (writes nothing); `render.auto` prints "would update";
  `render` prints `would update: ...`.
- `clean` prints `would remove X` per target. `build` validates its arguments, the pyz target
  keys, the Nuitka pin and PGO rules (`nuitka.check_python`, `check_options`) as a real build
  does, prints the checks (unless `--no-check`) and `(--dry-run) build B -> M: would output
  dist/<name>-<b>-<m>*` (nuitka: also `Nuitka options: --lto=... [--pgo-c ...] <extras>`), then
  stops. `report` builds nothing and never opens the browser.
- `mode` validates the new `pytemplate.toml` in memory and prints the keys that would change
  (new and current value), whether `pyproject.toml` would be rewritten, `uv.lock` ("would
  re-lock", or a read-only `uv lock --check`), the generated files that would update, the
  environments it would sync and the leftover-environment note. The PyPy 3.11 precheck runs
  read-only (`uv run --locked --no-sync`) and is skipped when `.venv` does not exist (`uv run
  --no-sync` would create it).
- `__init` runs the real checks (name, `check_name_free`, pristine) and lists each file as `-`
  deleted, `+` new or `~` replaced, the dependencies removed and added, and what happens to
  `pyproject.toml` and `uv.lock`. `new` checks the destination and the name and prints
  destination, preset, name and the `__init` step it would run in the copy. `pyz-merge`
  validates its inputs (`pyz.check_parts`: valid `_pyz.json`, one app, one build) and prints
  inputs and outputs (the `.pyz` and its `.cmd`).
- `nvim trust`, `extras`, `bootstrap` and `sync` print what they would do.
- `rename` runs the real checks (a dirty git tree is only a warning) and prints the move, each
  file with its reference count and sample lines, the pytemplate/pyproject lines, the lines left
  unchanged, the other files that mention the old name, the `uv.lock` re-lock and the generated
  files it would re-render (`render.apply(new_cfg)` in check mode: exactly what the real run
  writes). `hooks install`/`uninstall` only print.
- `apply` and `setup` run every check and refusal of the real run (`cmd_apply.make_plan`: a
  hand-edited preset, `render.check_pyproject`, the new name, the renamed pytemplate.toml; the
  dirty tree is only a warning; the PyPy 3.11 precheck runs read-only) and print one row per
  step (`cmd_apply._print_plan`): app.name (then the rename plan, as `rename --dry-run` prints
  it), app.preset, `[preset.<name>]` (the `uv remove/add` it would run), pyproject.toml,
  uv.lock ("would re-lock", or a read-only `uv lock --check`), environments, git hook,
  generated files, then the unused-environment note and the reference warnings.
- `build` with `[deploy.upx]` enabled: `upx.pack_tree` lists what it would pack and stops.
- `lock` reports whether the managed parts of `pyproject.toml` would change
  (`render.write_pyproject` writes nothing under `DRY_RUN`; `render.pyproject_message` says
  "would update"); its `uv lock` is an echoed command, so it is skipped. `cmd_env.ensure_lock`
  (used by `mode` and `rename`) echoes `uv lock` without checking (the check would read the old
  file), and `_fix_exec_bit` echoes `chmod +x` / `git update-index --chmod=+x` without running
  them.
- It is not a sandbox: scratch writes under `.build/` (`.build/cfg/*`, the mypyc stage,
  `mypy.ini`, `spec.json`) still happen.

### 5.5 Environment-variable contract

| Variable | Set by | Meaning |
|---|---|---|
| `PYTEMPLATE_CALLER_CWD` | launchers, Neovim plugin (`init.caller_cwd`: Neovim's cwd when inside the project, else the root), nu snippet | Caller's cwd; read only through `project.caller_cwd` |
| `PYTEMPLATE_LAUNCHER` | launchers, Neovim plugin (`nvim`), nu snippet (`nu`) | Which launcher/shell ran (section 4.1) |
| `UV` | uv | uv's own path; `proc.find_uv` and the launchers use it |
| `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `UV_PYTHON_PREFERENCE` | `envs.env_vars` | Environment selection (section 7) |
| `PYTHONUTF8=1` | `proc.base_env`, portable launchers, pyz `.cmd` wrapper, the Neovim mypy linter, every VS Code launch config (`vscode.DEBUG_ENV`) | mypy/mypyc otherwise read files as cp1252; F5 behaves like `./deploy run` |
| `PYTEMPLATE_BACKEND` | `cmd_dev.test_backend`, `mypyc.runtime_env_vars`, mypyc launch config | Backend under test (conftest) |
| `PYTEMPLATE_COMPILED` | `mypyc.runtime_env_vars` | Modules that must load from `.pyd/.so` (conftest) |
| `PYTEMPLATE_ASSETS` | `portable/boot.py`, `pyz/__main__.py` (setdefault) | Assets dir for `resources.assets_dir()` (raylib, flet) |
| `VSLANG=1033` | `mypyc.build`, wheel builds | English MSVC messages |
| `MACOSX_DEPLOYMENT_TARGET` | `methods.common.install_deps` for macOS targets, unless the user set it (`MACOS_FLOOR`, 13.0) | The oldest macOS the pyz/portable wheels must support |
| `RUFF_OUTPUT_FORMAT=concise` | VS Code tasks with the ruff matcher, Neovim tasks that parse output | One-line ruff output for the parsers |
| `NO_COLOR` (non-empty), `TERM=dumb` | user | Disable runner colours |
| `CI` | CI | Disables the install prompt of `deploy` and `deploy.ps1`; `selftest --e2e` skips GUI runs on Windows/macOS CI |
| `__RUBASH_SHELL_NAME` | niubash | Detected by `deploy` (section 4.3) |
| `PT_TRUST_FILE`, `PT_ROOT`, `PTCMD` | `cmd_nvim.trust_file`, `nvimtest`, `shells` | File to trust headless; project the smoke test expects; probe command text |

`proc.base_env` removes `VIRTUAL_ENV`, `PYTHONHOME`, `PYTHONPATH` and the user's uv variables
that would move the runner's `uv run --locked` calls (`proc.UV_SELECTION`, each measured with
uv 0.12): `UV_PROJECT_ENVIRONMENT` and `UV_PYTHON` (`envs.env_vars` sets both), `UV_PROJECT`,
`UV_NO_PROJECT`, `UV_WORKING_DIR` (another project or none: `--locked` is ignored, relative
paths move), `UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON` (exit 2 next to
`UV_PYTHON_PREFERENCE`), `UV_ISOLATED` (a throwaway env instead of `.venv`), `UV_NO_DEV`,
`UV_NO_DEFAULT_GROUPS` (no mypy/ruff/pytest: a PATH-wide one of another version runs) and
`UV_NO_SYNC` (`.venv` stays empty after `git clean -fdx`). Resolution settings (indexes,
`UV_EXCLUDE_NEWER`, `UV_RESOLUTION`, `UV_PRERELEASE`), `UV_FROZEN`/`UV_LOCKED` (uv ignores
`UV_FROZEN` next to `--locked`) and the cache stay: they are the user's, and uv reports when
they disagree with `uv.lock`. It also removes the runner's own ephemeral `Scripts/` or `bin/`
from PATH when `sys.prefix != sys.base_prefix` (uv exports it for `--script` runs); sets
`PYTHONUTF8=1`; on Windows appends `%ProgramFiles(x86)%\Microsoft Visual Studio\Installer` to
PATH (VS 2026 `vcvarsall.bat` calls `vswhere.exe` by bare name; without it setuptools fails
with "Unable to find a compatible Visual Studio installation"). Everything else (e.g.
`FLET_*`) passes through.

### 5.6 Git pre-commit hook (`hooks.py`)

- `./deploy apply` (and `setup`) calls `hooks.ensure_installed(cfg)` when `[hooks] pre_commit`
  is true (the default): it installs the hook or updates this project's own, never fails the
  command, and is silent outside git (`hooks.NotInGit`: no git, not a work tree). When
  `pre_commit` is false, apply removes pytemplate's own hook (`hooks.uninstall`, which restores a
  `pre-commit.local`) and never touches another one (section 5.8). Any other git failure
  (dubious ownership...) is shown with git's own message: a warning in apply, an info line in
  `doctor` (whose "git hook" line is otherwise info too: missing is not a problem). Every git
  call runs with `LC_ALL=C` (find_repo reads "not a git repository" in English).
- The hook: `pre-commit` in the folder `git rev-parse --git-path hooks` reports (worktree
  aware), pure ASCII + LF, a marker comment, and `sh <launcher> hooks run` with the POSIX
  launcher path relative to the repository top (the project may be a subfolder of a bigger
  repo; non-ASCII folder names are written with `printf` escapes; `hooks.launcher_of` reads it
  back). `sh` explicitly (no dependence on the exec bit); git for Windows runs hooks with its
  own sh.exe, where the POSIX launcher works. A missing launcher makes the hook exit 0 (other
  checkouts); a launcher exit code above 1 (uv not found from a GUI client, a broken
  pytemplate.toml, an old runner without `hooks run`) also prints `git commit --no-verify` and
  `./deploy hooks uninstall`. `shellcheck -s sh`-clean (tested when installed). Any change to
  `hooks.hook_script` makes installed hooks "outdated": apply/setup rewrite them.
- Never overwritten: a hook without the marker, or any symlink (writing through a dangling
  link created a file in the work tree). `install --force` renames it to `pre-commit.local` (a
  link moves as a link; a dangling `.local` counts as existing) and ours runs it first;
  `uninstall` restores it. A marked hook whose launcher is another live project of the same
  repository (state "other", a monorepo) is left alone by apply, install and uninstall;
  `install --force` writes a FRESH copy of it as `pre-commit.local` (the script skips
  `pre-commit.local` when it is itself that file; older copies would recurse), so both checks
  run, once each. A project that an enclosing repository ignores (`git check-ignore -q deploy`,
  which refuses `--literal-pathspecs`) gets no hook unless forced. With `core.hooksPath` set
  nothing is written: install/status/doctor print the line to add (`sh ./deploy hooks run ||
  exit $?`); husky 9 (`.husky/_` holding `h` or `husky.sh`) is read through `.husky/pre-commit`.
- `hooks run` checks what the commit contains: staged files (`git diff --cached --name-only
  --no-renames --diff-filter=ACMRT -z`) and staged deletions (`D`: a deletion-only commit gets
  the project-wide checks too). Every git call passes `-c diff.relative=false`
  (`diff.relative=true` made every path relative to the project folder, and all were dropped).
  In ~0.3 s: ruff (active typing profile, `exit_zero` honoured) and `ruff format --check` on
  staged `.py/.pyi` under the code dirs via `uv run --quiet --frozen` (a stale lock is the lock
  check's finding; an exit code other than 0/1 is "could not run ruff", without a fmt hint). A
  file with unstaged changes is checked in its STAGED version (`git cat-file --filters
  :0:<path>`, the checkout form, fed to ruff with `--stdin-filename`); a staged file missing
  from the working tree fails with `git restore` / `git rm --cached`; the launcher checks
  (`shells.launcher_problems`) and the template repo's language guard read the staged content
  too. Project-wide and conservative (they read the working tree, so they may block a commit
  that touches none of their files): generated files up to date (`render.apply(check=True)`)
  and none unstaged/untracked; managed pyproject parts and `uv lock --check`; `pytemplate.toml`,
  `pyproject.toml`, `uv.lock` and the generated files committed together (once any is in the
  commit, none of the three config files may keep unstaged changes; the hints name the dirty
  config files with the generated ones, so following them never splits a source from its
  output). `lintc` on staged compiled modules (blocking only under the `mypyc` profile, reads
  the working tree). Never mypy (the user's choice: `./deploy check` does it).
- `hooks._run_bytes` is the module's only process start outside `proc.run`: raw bytes
  (proc.run's text mode turns CRLF into LF) and stdin, for `git cat-file` and ruff on stdin.
- Git hands hooks a relative `GIT_INDEX_FILE` and, in linked worktrees, `GIT_DIR` without
  `GIT_WORK_TREE`: `hooks` makes them absolute for its own git calls and removes them before
  starting uv/ruff (they would point git at the wrong repository for a sub-folder project).
- `test_hooks.py` runs git with `GIT_CONFIG_GLOBAL` at a missing file and
  `GIT_CONFIG_NOSYSTEM=1`: a developer's global core.hooksPath, commit.gpgsign,
  init.templateDir or diff.relative must not change the results.

### 5.7 Renaming (`rename.py`)

- `./deploy rename NEW_NAME [--force]`: `check_new_name` refuses a bad format, a keyword, a
  standard-library module of ANY Python the project can run on (`presets.shadows_stdlib`: the
  runner's `sys.stdlib_module_names` plus `STDLIB_OTHER_VERSIONS`; uv keeps whatever Python ran
  `./deploy` first, 3.11 on one machine and 3.15 on another), a dependency
  (`presets.check_name_free`, also used by `new` and `__init`) and any package of `uv.lock`
  (`locked_names`: indirect ones too, pygments via rich; the project's own entry excluded) and
  a backend name (`config.BACKENDS`: `src/mypyc/` would shadow mypy's compiler in the stage,
  and a later rename would rewrite conftest's backend strings); a broken pyproject.toml is a
  DeployError, never a traceback. The dirty-tree check
  (`git_changes`: `git status --porcelain -z -uall` with `LC_ALL=C`, paths relative to the
  project; the generated files and `state.json` (`derived_paths`) never count, nor, after a
  hand edit, `pytemplate.toml`) refuses without `--force` (a warning under `--dry-run`); git
  failing for another reason (dubious ownership) is refused the same way, never read as "no
  git". Then: move `src/<old_pkg>/` first (the step that can fail on a locked file; case-only
  renames use two moves), write the files (`apply_plan`: a write that fails undoes everything,
  old bytes back and the folder moved back), `cmd_env.ensure_lock` (a failure there says "the
  files are already renamed ... ./deploy apply"), `render.apply`, the ruff tidy-up, and the name
  of the `applied` record (`cmd_apply.rename_record`, only the project's own record).
- After a hand edit of `app.name` (src/<pkg>/ missing), rename starts from the name the project
  really has (`cmd_apply.applied_name`: the trusted record, else pyproject `[project] name`,
  whose package is in src/): `rename <the edited name>` finishes the job, `rename OTHER` goes
  from the real name to OTHER; with no such name it exits 2 ("put the old name back"). It never
  says "nothing to do" while src/<pkg>/ is missing.
- What changes (`rewrite`, whole words only; `myapp_extra` and `my-app-2` never match):
  - Python code (`tokenize`): the first name of `import pkg...`/`from pkg... import`, and, in a
    file that binds the package with `import pkg[.x]` (no `as`), every name that resolves to that
    import (`_package_uses`, an iterative `ast` scope pass: a function, class body or
    comprehension that binds `pkg` another way (assignment, parameter, loop/with/except target,
    global/nonlocal, walrus) keeps every `pkg` in it; a module that rebinds it keeps all of its
    own; they are reported, not changed). Keyword arguments (`f(pkg=1)`) and attributes never
    change; `f"{pkg=}"` is a use. When `ast` cannot parse the file (syntax newer than the
    runner's Python) the token rule is the fallback. String tokens are recognised by their
    `*STRING_START`/`*STRING_END` suffix (f-strings 3.12, t-strings 3.14, any later family).
  - Text (strings, comments, other files): every occurrence except `x.pkg` and a path segment
    right after the package itself (`src/pkg/pkg`, `src\pkg\pkg`: a submodule). When the
    old name equals the old package but the new name differs from the new package (`alpha` ->
    `My-Game` / `my_game`): paths, dotted names, `pkg:main`, "package"/"module",
    `import`/`from`, `-m` and `import_module`-like calls get the package; titles, other prose
    and artifact names (`alpha.exe`, `alpha-cpython-exe`) get the name.
  - `pytemplate.toml`: always by context (`contextual`, even when the new name is a package
    name), never a TOML key or table header (`_toml_key`: an app named `app`, `editor`,
    `console` or `bunnymark` keeps `[app]`, `editor =` and its buttons), never a key path in a
    comment (`app.gui` for an app named `app`: `_config_path`). The values of `MODULE_KEYS`
    (compile.modules/exclude/forbid_imports, `[[typing.mypy_overrides]] module`,
    deploy.exe.hidden_imports, deploy.exclude_modules, deploy.wheel.entry; table-aware and
    multi-line arrays included: `module_value_lines`) are package references even when bare
    (`modules = ["alpha"]`). Any other mention is reported, not changed ([tasks] can use
    `{name}` and `{pkg}`). `validate_config` validates the result in memory ("the renamed
    pytemplate.toml would be invalid ...; nothing was changed"). Decoded with `config._decode`
    (UTF-16/ANSI is a clear error) and written back with its own BOM and line endings.
  - `pyproject.toml`: `[project] name` (`presets.set_project_name`: the `[project]` table only,
    either quote style, any indentation; a table it cannot edit stops the plan) and the preset
    block; mentions in other tables are reported. Read as `utf-8-sig` (the BOM is not written
    back).
  - A Python file with a PEP 263 cookie is rewritten in its own encoding; other files that are
    not UTF-8 text but mention the old name are a warning (`Plan.unreadable`); binaries show
    only with `-v`. Files outside src/ and tests/ (README.md, scripts/, docs/, your own
    workflows) are only listed (`_mentions`: skips caches, environments, `.build`, `dist`,
    `.pytemplate`, `.claude`, the generated files and files over 2 MiB).
- ruff tidy-up (`tidy_before`/`tidy_after`; best effort, never fails the rename): the new name
  has another length and sort position, so `ruff check --fix-only --fixable I001` (acts only when
  the active typing profile selects I) and `ruff format` run on the rewritten Python files, each
  only on the files ruff accepted BEFORE the rename (`Tidy`: a file kept unformatted or unsorted
  stays so), with the active profile's `.build/cfg/ruff-*.toml` (`cmd_dev._profile_file`), like
  the hook.
- Invariant (tested for the 3 presets, LF and CRLF, 7 name pairs, random names and the round
  trip A -> B -> A): renaming the skeleton of name A to B is byte-identical to the skeleton of B,
  so `presets.pristine` stays true.
- Common words as names (`app`, `game`, `core`) also rewrite prose in src/ and tests/: that is
  why the tree must be clean and `--dry-run` shows sample lines.

### 5.8 Applying `pytemplate.toml` (`cmd_apply.py`)

- `./deploy apply [--force]` brings the whole project in line with pytemplate.toml; `./deploy
  setup [--force]` is the same operation under its first-time name (`cmd_env.cmd_setup` calls
  `cmd_apply.apply(command="setup")`; only the header and the last line differ). Idempotent: a
  second run syncs the environments and changes no file (no uv add/remove/lock).
- `make_plan` computes everything and makes every refusal before the first write:
  `read_project` (a BOM is fine, broken TOML is exit 2), `applied_state`, a hand-edited
  `app.preset` (exit 2: a preset decides src/, tests/, the dependencies and pyproject.toml, so
  it cannot switch in place: put it back, `./deploy new DIR --preset P`),
  `render.check_pyproject`, `dependency_changes`, whether PyPy is new (tool.uv environments has
  no PyPy yet), then the rename plan (`rename.check_new_name`, `rename.plan`,
  `rename.validate_config`) or, when only pyproject `[project] name` differs, its new text.
- Order of `apply`: dirty-tree check (rename only; `--force` skips it) -> PyPy newly supported:
  `cmd_mode._precheck_py311` (it runs `uv run --locked`, before anything changes the lock) ->
  the rename (`rename.report`, `tidy_before`, `apply_plan`) or the `[project] name` line ->
  `uv remove --frozen` / `uv add --frozen` of the option-driven requirements (dev group with
  `--dev`) -> `cmd_env.ensure_lock` -> `envs.sync` of `cmd_env._envs_for(cfg, "all")` ->
  `cmd_env._fix_exec_bit` -> the hook (`ensure_installed` when `hooks.pre_commit`,
  `hooks.uninstall` of pytemplate's own hook when false; another tool's, another project's or a
  core.hooksPath setup is left alone) -> `render.apply` -> `rename.tidy_after` -> `save_record`
  -> the unused-environment note (`unused_envs`: every `.venv*` of this side no supported
  backend uses, `.venv-jit` of older templates included; never deleted) -> warnings for missing
  references (`reference_problems`: src/<pkg>/, compile.modules, app.assets, deploy.exe.icon,
  deploy.upx.path) -> a summary.
- `--frozen`, never `--no-sync`: `flet-cli==V` pins `flet==V`, so a resolving `uv add` of one
  group alone has no solution; `--frozen` only edits pyproject.toml and one `uv lock` follows.
  When the edits or the lock fail, pyproject.toml gets its old bytes back and the record is not
  written: nothing half-applied, the next apply retries.
- Option-driven requirements: the preset.toml entries with an `{option}`
  (`presets.option_dependencies`: flet's `flet`, `flet-desktop`, dev `flet-cli` `=={version}`;
  raylib's `{package}=={version}`), compared with pyproject by normalized name and version
  (`req_key`: uv writes `raylib_sdl` as `raylib-sdl`; extras and markers ignored), never
  verbatim. Plain preset dependencies (`rich`, `types-cffi`) belong to the project after `new`.
  An old value is removed only when the last applied options produced it (a `raylib` the user
  added next to `raylib_sdl` stays). `[preset.<name>]` wins over a hand `./deploy add flet==X`.
- The `applied` record (top-level key of `.pytemplate/state.json`, committed; `render._save_state`
  keeps it): `{name, preset, dependencies, dev}`, written at the end of each apply (its name by
  rename). It counts only when its name is `app.name` or pyproject `[project] name`
  (`trusted_record`): the template's own record, copied by `./deploy new`, is ignored. Without
  a record: the preset is the one whose option-driven dependencies pyproject declares
  (`_applied_preset`; script leaves no trace, so `app.preset` counts), the applied requirements
  are the preset.toml defaults (what `__init` wrote), the old name is pyproject `[project] name`
  when its package is in src/.
- `pending` / `doctor` (one call from `cmd_env.cmd_doctor`): an `[XX]` line per change not
  applied yet (hand-edited app.name or app.preset, `[preset.*]` vs pyproject.toml,
  `hooks.pre_commit = false` with pytemplate's hook installed), else `[ok] pytemplate.toml
  applied`; missing references are `[--]` notes; a broken pyproject.toml is a line, never a
  traceback.
- Every hint for a pyproject.toml that does not match pytemplate.toml (`render.auto`, doctor,
  `render --check`, the pre-commit hook) names `./deploy apply`: `./deploy lock` applies only
  the managed block, so after a `[preset.raylib] package` switch it moved no-build-package to
  the new name and kept the old dependency.

## 6. Configuration and generated files

### 6.1 `pytemplate.toml` (`config.py`)

- Reading (`config.read_text`, used by `load`, `update_file` and `mode`): UTF-8, a UTF-8 BOM is
  dropped, line endings are kept. UTF-16/32 (BOM; PowerShell 5.1 `>`/`Out-File`), NUL bytes
  (UTF-16 without a BOM) and invalid UTF-8 (ANSI, `Set-Content`) are a `DeployError` (exit 2)
  naming the encoding or the byte and its line, and how to save the file again; never a
  traceback, for every command (`help` still prints, without the custom tasks).
- Loader (`config._build`): types come from the dataclass annotations (`typing.get_type_hints`),
  recursively: list items (`key[i]`), table values (`key.k`; non-bare keys are quoted in the
  path). `Any`-typed values (`[vscode] settings`, `[preset.<p>]` options, mypy override options)
  must be JSON-like: no TOML date/time, `nan`/`inf` (`_check_free`). NUL characters are rejected
  everywhere.
- `schema` must equal `config.SCHEMA` (1; a missing line means 1). It is checked before the
  other keys, so a file from another template version fails with that reason instead of an
  unknown key. There is no migration logic.
- `[app]`: `name` (`[A-Za-z][A-Za-z0-9_-]*`; `pkg = name.replace("-", "_").lower()`), `preset`
  (`config.validate`: `[a-z][a-z0-9_-]*` and `PRESETS/<name>/preset.toml` must exist, checked
  without importing `presets.py`), `gui`, `assets` (`"assets"` = bundle `src/assets/`, `""` =
  none; any other name is rejected: `resources.assets_dir` and the portable/pyz bootstraps
  hard-code `assets`).
- `[backend]`: `active`, `supported` (non-empty subset of `cpython, pypy, mypyc` without
  duplicates, contains `active`).
- `[python]`: `cpython` (`[0-9]+\.[0-9]+`: ASCII digits, `\d` also matches other scripts'),
  `pypy` (`pypy@[0-9]+\.[0-9]+\.[0-9]+`, exact: a loose request picks the newest PyPy, and PyPy
  8.0 changed the extension ABI to pp80; in September 2026 raylib, numpy and cffi published no
  pp80 wheels. Bump the pin only once the dependencies ship wheels for the new ABI, then
  `./deploy lock`). The removed CPython JIT keys (`jit`, `jit_interpreter`) fail like any
  unknown key (no compatibility shim: each project carries its own runner). `Config.min_python`
  is the lowest minor in use (CPython always, PyPy's `Config.pypy_minor` while supported),
  compared as numbers.
- `[typing]`: `profile = auto|mypyc|strict|warn|off`, `relaxed = off|warn|strict` (what `auto`
  means on cpython/pypy), `editor = pylance|basedpyright`, `[[typing.mypy_overrides]]`
  (`config._check_override`: `module` required, a name/pattern or a non-empty list of them
  (dotted identifiers or `*`, `{pkg}` token); `strict` forbidden: mypy would apply it to ALL
  modules; option names identifier-like, values booleans, numbers, one-line strings or lists of
  them: each becomes a `.mypy.ini` line). `backend.active = "mypyc"` rejects `warn`/`off`.
- `[compile]`: `modules`, `exclude`, `forbid_imports` (dotted names checked), `annotate` (every
  mypyc build writes the annotate report, section 9), `opt_level "0".."3"`, `multi_file`,
  `separate`, `strict_dunder_typing`.
- `[deploy]`: `optimize 0|1|2`, `default {backend: method}` (merged over
  `config.DEFAULT_METHODS` in `DeployConfig.__post_init__`: a backend left out keeps its method,
  cpython/mypyc `exe`, pypy `portable`; each method must be allowed by `cmd_build.COMPAT` for its
  backend, and `flet` needs `app.preset = "flet"`: `config._check_default_methods`),
  `exclude_modules` (dotted names: PyInstaller `--exclude-module`, Nuitka `--nofollow-import-to`;
  the flet preset sets `["PIL"]`), `[deploy.exe] mode console icon hidden_imports strip
  extra_args` (`hidden_imports`: dotted names; `strip`:
  PyInstaller `--strip`, Linux/macOS only), `[deploy.portable] runtime prune archive env`
  (`env` names must be identifiers), `[deploy.pyz] targets`, `[deploy.wheel] entry`,
  `[deploy.nuitka] mode lto pgo pgo_args extra_args` (`lto` in `auto|yes|no`; `config._check_nuitka`
  refuses `pgo_args` without `pgo`, and `pgo` with `app.gui = true` or a non-empty `app.assets`:
  Nuitka's profiling run starts the app while building, so the build waited for its window to be
  closed, and the data files were not in place yet; mypyc and macOS are refused at build time,
  section 10), `[deploy.flet] target cleanup exclude extra_args` (`target` is not validated;
  `cleanup` = `--cleanup-app --cleanup-packages`), `[deploy.upx] enabled level lzma exclude
  path` (`level` in `1..9|best|brute|ultra-brute`; `path` relative to the project root).
- `[hooks]`: `pre_commit` (apply/setup install the git hook when true and remove pytemplate's
  own when false; section 5.6).
- Every `*.env` table (`tasks.X.env`, `deploy.portable.env`) takes string values only, and names
  a process environment can hold (not empty, no `=` or NUL; checked by the loader).
- `[tasks.<name>]`: `cmd` (argv), `deps`, `env`, `backend`, `uv = true`, `cwd`, `help`,
  `background` (long-running dev server; section 12). Name regex `[a-z][a-z0-9_-]*`; `cmd` or
  `deps` required; a non-empty program (`cmd[0]`); `env` names `[A-Za-z_][A-Za-z0-9_]*`; in
  `cmd`, `env` values and `cwd` only bare placeholders (`config.task_format_error`: never `{}`,
  `{0}`, `{root.x}`, `{root!r}`, a lone brace; literal braces doubled `{{ }}`). Checked when
  the task runs (`tasks.run_task`), not at load: unknown placeholder names, `deps` quoting and
  empty entries (all parsed before the first dep runs), `cwd` is a folder (not in a dry run),
  `backend = "pypy"` in `backend.supported` (only where its environment is used). `vscode.scan`
  renders a task whose deps do not parse.
- `[preset.<name>]`: option overrides (`config._check_preset_tables`): `<name>` must be a preset
  of this template, and each key and its value type must match that preset's `preset.toml`
  `[options]`, read with `tomllib` (no `presets` import); the script preset has none.
  `./deploy apply` applies them to the dependencies (section 5.8): raylib `package`/`version`,
  flet `version`.
- `[vscode]`: `settings` (merged into `.vscode/settings.json`; keys NOT validated, values must be
  JSON values: a TOML date/time, nan/inf or NUL is a config error naming the key, from the loader
  (`config._check_free`) and again from `vscode.settings`), `buttons` (each first word must be a
  builtin command or a `[tasks]` name: `config.validate`).
- `config.set_value(text, table, key, value)` edits the TOML text itself: a small scanner
  (`config._statements`: the four string kinds, multi-line arrays and inline tables, comments,
  dotted and quoted keys) finds the value's span, which may cover several lines (taplo, the
  LazyVim TOML formatter, expands long arrays), and replaces only that span: the comment after
  it, the other lines and the line endings stay. A missing key goes after the table's last key
  (a missing table at the end) with the file's line ending. The result is re-parsed and must
  equal the old data with only that key changed; anything else (the table written as an inline
  table, the key defined as a table) is a `DeployError` asking to edit it by hand.
  `config.toml_value` escapes DEL and refuses lone surrogates.
- `config.update_file` applies every change in memory first, writes only when something changed
  and never under `--dry-run`, keeps a UTF-8 BOM and the line endings, and never writes broken
  TOML. `mode` drops changes whose value is already set, so their spelling stays.

### 6.2 Generated files and `state.json`

Generated (committed, never hand-edited): `.python-version`, `.mypy.ini`, `.ruff.toml`,
`pyrightconfig.json`, `.vscode/{settings,extensions,launch,tasks}.json`, `.lazy.lua`,
`.pytemplate/editor.json`, `.github/workflows/ci.yml` (only while `templates/ci.yml` exists),
`.pytemplate/state.json`. Also managed: parts of `pyproject.toml` (6.3), `uv.lock` (only via
`./deploy apply|setup|lock|add|remove|mode|rename|__init`), `typings/raylib/__init__.pyi` (raylib
task `stubs`). `state.json` also holds the `applied` record of `./deploy apply` (section 5.8).

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
  warns when `pyproject_outdated` (the hint names `./deploy apply`).
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
  quoted key, any indent), is rewritten to `">=<min_python>"` (the lowest supported minor:
  usually PyPy's 3.11 with PyPy, else `python.cpython`) and inserted under `[project]` when
  missing (`render._set_requires_python`); a `requires-python` of another table is never
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
  `apply`/`setup` and `rename` (via `cmd_env.ensure_lock`) and `__init`, because the change
  needs re-locking.
- Not a managed part, but kept in line by `./deploy apply` (section 5.8): the option-driven
  preset requirements in `[project] dependencies` and the dev group (flet's three pins,
  raylib's `{package}=={version}`), through `uv remove/add --frozen` and one `uv lock`.
- Preset tables go between `# >>> pytemplate-preset` and `# <<< pytemplate-preset`
  (`presets.EXTRA_BEGIN/EXTRA_END`).
- Never add `[build-system]` to the project's `pyproject.toml`: the project is an app (uv would
  treat it as a package); the wheel method synthesises its own build project.

## 7. Environments and uv invariants

- `UV_PROJECT_ENVIRONMENT` and `UV_PYTHON` must ALWAYS be set together (`envs.env_vars`): with
  only one of them uv silently recreates the env with the wrong interpreter.
- `UV_PYTHON_PREFERENCE=only-managed` for `.venv` and `.venv-pypy` (also
  `python-preference = "only-managed"` in the managed block).
- `runtime_env(backend)`: `pypy` -> `.venv-pypy`; `cpython`/`mypyc` -> `.venv`. `tool_env` is
  always `.venv`: every tool (mypy, ruff, mypyc, PyInstaller, pytest for selftest) runs there.
- Every tool call is `uv run --locked` (syncs when needed, fails on a stale lock; the git
  hook's ruff uses `--frozen`, section 5.6). `cmd_env.ensure_lock` runs `uv lock --check` and
  then `uv lock` if needed; under `--dry-run`, when the managed pyproject parts would change, it
  echoes `uv lock` instead (the check would read the unwritten file and pass).
- `envs.sync` = `uv sync --locked --all-groups` (apply/setup, sync, mode): every dependency group of
  `pyproject.toml` is installed, so `./deploy add --group G pkg` survives the next sync and
  reaches a fresh clone (an exact sync of the default groups removed it); `uv run` syncs
  inexactly and never removes them. `add`/`remove` take `--dev` or `--group G`, not both.
- The oldest supported uv is `envs.MIN_UV` = 0.10.12, read from uv's own download metadata:
  the first uv that downloads `pypy@3.11.15` (0.10.11: "No download found for request");
  CPython 3.14 final needs 0.9.0 (0.8.x silently installs 3.14.0rc2) and `uv export --format
  requirements.txt` 0.6.15. `envs.uv` calls `envs.require_min_uv` right before uv would CREATE
  an environment (its dir does not exist): an older uv exits 3 with `envs.UV_UPDATE`; asked
  once per process, an unreadable version passes. doctor flags it. The managed `[tool.uv]` block
  also carries `required-version = ">=<MIN_UV>"`, so uv itself (>= 0.5.14) refuses every project
  command, the launcher's `uv run --script` included, with "Required uv version ... does not
  match" and `uv self update`. Bump it with the pins
  (`test_min_uv_matches_the_pinned_interpreters`) or a newer uv flag.
- `./deploy clean` (`cmd_env.cmd_clean`): `.build/` (`.build/wsl` in WSL), `dist/`, and with
  `--envs` this side's environments (WSL on /mnt: only `.venv*-wsl`; elsewhere every `.venv*`
  directory but those). A symlink or junction loses only the link; a folder it cannot remove
  completely (a file in use: the editor's mypy/ruff server runs from `.venv` on Windows) is an
  error, exit 1, with the others still removed.
- uv finds the project by walking up from the CWD: `envs.uv_run` adds `--project <ROOT>`
  whenever it runs with another `cwd` (a work dir with its own `pyproject.toml`, like the
  `flet build` stage, would otherwise become the project). Plain `envs.uv` calls with a `cwd`
  (nuitka, the stages without a pyproject) rely on the walk reaching `ROOT`.
- `git clean -fdx` is safe: only envs, `.build/`, `dist/`, caches and `.claude/` go; the next
  `uv run --locked` recreates `.venv` by itself (verified on a clone: `run`, `test all`, `check
  all`, `render --check`, `doctor` all pass without `setup`). pyz and portable run an env's
  python directly (`interpreter_info`, `uv pip install --python`), so they create a missing
  env first (`methods.common.ensure_env`: `uv sync --locked`); `build ... --no-check` works too.
- `environments` in the managed block is bounded to the CPython minor (e.g.
  `cpython and >=3.14,<3.15`), plus `pypy and >=3.11,<3.12` (PyPy's own minor,
  `Config.pypy_minor`, never `min_python`) when PyPy is supported. Without the
  bound uv resolves for 3.15+, where raylib has no wheels. Changing `python.cpython` needs
  `./deploy lock`.
- With PyPy supported the block adds
  `override-dependencies = ["cffi>=1.15.1; implementation_name == 'cpython'"]`: PyPy ships
  cffi built in and uv would otherwise try to build cffi from PyPI.
- Dev group (`pyproject.toml [dependency-groups] dev`): `debugpy`, `mypy` (needs the Rust
  `ast-serialize`: no PyPy wheels), `pyinstaller`, `ruff` and `setuptools` carry
  `implementation_name == 'cpython'`; `.venv-pypy` only gets the app deps plus pytest.
- WSL on `/mnt/*` (`project.IS_WSL`): separate `.venv*-wsl` envs and `.build/wsl`, so the
  Windows `.venv` is not turned into a Linux one (and `clean --envs` keeps the other side's).
- Tools outside `uv.lock` run through `uv run --locked --with <pin>` and are pinned in module
  constants: `cmd_dev.BASEDPYRIGHT = "basedpyright==1.40.1"` plus its Node.js runtime
  `cmd_dev.BASEDPYRIGHT_NODE = "nodejs-wheel-binaries==24.19.0"` (basedpyright's only
  dependency, `>=20.13.1`: unpinned, every new Node LTS on PyPI changed `check` and its
  glibc/macOS floor; bump both together) (`check` with `typing.editor = "basedpyright"`) and
  `methods.nuitka.NUITKA = "nuitka==4.2.2"` (no dependencies outside extras). Bump them
  deliberately; `NUITKA` together with `methods.nuitka.NUITKA_PYTHON` (the newest CPython minor
  it supports, `3.14`): raising `python.cpython` past it needs a newer Nuitka pin.
- `mode --supports -pypy` never deletes the old env: it prints a note that
  `./deploy clean --envs` removes the `.venv*` environments (all of them; `setup` recreates
  the ones in use). `apply` prints the same note for every unused `.venv*` of this side
  (`cmd_apply.unused_envs`), `.venv-jit` of older templates included.

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
  `--target`, `--no-check`; argparse with `allow_abbrev=False`. Unknown flags go to
  `req.extra` and are appended to the packager argv, for `PASSTHROUGH` methods (exe, nuitka,
  flet) only; `_check_arguments` refuses (exit 2, before the checks, also in `--dry-run`) extras
  for pyz/portable/wheel, `--onefile/--onedir` outside `ONEFILE_METHODS`, `--target` outside
  `TARGET_METHODS` (pyz), `GLOBAL_FLAGS` (`--dry-run`, `--no-render`) typed after the command,
  and a leading bare word (`_stray_word`: "unknown backend 'mypy': did you mean mypyc?", "did you
  mean --method pyz?"). Then `nuitka.check_python` and, for pyz, `common.check_key` on every
  key. Default method from `deploy.default`; `COMPAT` rejects exe/nuitka/flet with pypy. Runs
  `run_checks` unless `--no-check`. `payload`: the mypyc release stage, or `sync_tree(SRC,
  .build/payload/<backend>)`. The `done: ... (N MB)` size (`common.tree_bytes`) counts a
  symlinked file once (a bundled runtime's `bin/python3 -> python3.14`).
- Output: `dist_path(req, suffix)` = `dist/<app.name>-<backend>-<method><suffix>`; portable
  with a bundled runtime adds `-<target key>`, flet adds `-<target>`. The CI template hard-codes
  `dist/<NAME>-<BUILD_BACKEND>-pyz/<NAME>.pyz`: it is coupled to `BuildRequest.out_name`.
- Work dirs live under `.build/<name>/<backend>` (`exe-stage`, `pyinstaller`, `flet-pack`,
  `pyz`, `wheel`, `nuitka-stage`, `nuitka`, `flet-build`); portable builds straight into `dist/`.
- Target keys: `^(cp|pp)(\d)(\d+)-(windows|linux|macos)-(x86_64|aarch64)$`
  (`methods.common.KEY_RE`), e.g. `cp314-windows-x86_64`. `common.check_key` accepts only what
  uv.lock can serve: `cp<python.cpython>` on any OS/arch (another CPython minor got the build
  interpreter's binaries on the same OS, or an empty lib/ elsewhere: the export's markers only
  cover the locked minors), and a `pp` key only when it is the pypy build's own interpreter on
  this machine (`config_host_key`; uv installs PyPy wheels only with a real PyPy: CPython and
  PyPy builds are joined with `pyz-merge`). A pypy build may add `cp<minor>` keys (installed
  with the tools env).
- `common.install_deps` (`uv pip install --target --no-deps -r <export>`): a cross target gets
  `--python-platform UV_PLATFORMS[...] --python-version --only-binary :all:` (an sdist built for
  another OS would produce host binaries); a HOST target gets the same `--python-platform` floor
  when this machine can load it (`host_floor`: glibc >= 2.28 x86_64 / 2.35 aarch64, never musl;
  macOS >= `MACOS_FLOOR` 13.0, pinned through `MACOSX_DEPLOYMENT_TARGET` unless the user sets it),
  without `--only-binary`, and falls back to the host's own wheels with a warning when a
  dependency has no wheel for the floor. Without it uv picked the newest the build machine
  allows (manylinux_2_34 on Ubuntu 24.04: the result failed on Debian 11 / RHEL 8).
  `drop_install_junk` removes uv's `.lock`, `_virtualenv*` and the console/GUI script wrappers
  of `bin/` / `Scripts/` (names from `*.dist-info/entry_points.txt`; their shebang or `.exe`
  trampoline holds the build machine's `.venv` path), keeping other files there (ruff and uv
  wheels look up their native binary at `<target>/bin`) and a real `bin` package.
- `common.has_native`: a `*.dist-info/WHEEL` tag with an ABI or platform (also pure-Python
  platform wheels that ship an executable, e.g. imageio-ffmpeg), else a `.pyd/.so/.dll/.dylib`
  or `.so.N` file.
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
  uses `--contents-directory=.` on Windows only (on Linux the executable `dist/<n>/<n>` would be
  a FILE where the package folder `<pkg>/` of the mypyc extensions must go when
  `app.name == pkg`, the default; Linux keeps PyInstaller's `_internal/`); macOS never gets
  `--onedir` (`flet pack` rejects it and always builds a `.app` bundle; PyInstaller only logs a
  deprecation for onefile + `.app`). `deploy.exe.console` becomes `--debug-console=true` (flet
  pack adds `--noconsole` unless that option has a value) and UTF-8 mode goes through
  `--pyinstaller-build-args=--python-option=X utf8` (one argv item: flet pack forwards each
  value unchanged). It bundles
  the Flutter client (plain PyInstaller would
  download ~40 MB on first start). `flet` and `flet-desktop` must share a version, else Flet
  pip-installs `flet-desktop` at runtime, bypassing `uv.lock`.
- **portable**: `dist/<n>-<b>-portable-<key>/` (no `-<key>` with `runtime = "system"`, which
  bundles no interpreter) with `app/`, `lib/` (`uv pip install --target`), `runtime/` (pruned
  copy of the interpreter's `base_prefix` through `\\?\` extended paths; the `ignore` callback
  strips that prefix before comparing), `boot.py`, `<n>.cmd` / `<n>.sh`. The base's
  `__pycache__` folders are never copied. Prunes `include libs Tools share`, `tcl*` (Windows
  base), stdlib `test idlelib turtledemo ensurepip site-packages`, `test`/`tests` subfolders of
  stdlib packages (PyPy's `unittest/test`, `lib2to3/tests`...), `*.debug` (PyPy's detached debug
  symbols, 16 MB), PyPy `hpy/devel`, and Tk unless `src/` or an installed dependency in `lib/`
  imports tkinter/turtle (`common.uses_tkinter(lib)`: customtkinter, ttkbootstrap; a bytes
  pre-filter, then the AST; a file it cannot parse keeps Tk): tkinter, turtle and every Tcl/Tk
  file `TCL_RE` matches in `lib/` (POSIX: `libtcl9.0.so`, `tcl9.0/`, `tk9.0/`, `itcl*`,
  `thread*`), `DLLs/` and `lib-dynload/` (`_tkinter.*`). Deletes `EXTERNALLY-MANAGED`; copies
  `vcruntime140*.dll` from the CPython base into PyPy runtimes on Windows (PyPy's zip lacks
  them). `compile_calls` (console `python.exe`, `-B -f`, `--invalidation-mode checked-hash` so
  the Windows zip's 2-second local times cannot make them stale, `-s <out>` so no `.pyc` embeds
  the build folder): `lib/` and `app/` at levels 0 and `deploy.optimize`, the bundled stdlib
  (`runtime_stdlib`: `lib/pythonX.Y`, `lib/pypyX.Y` or `Lib`) only at the launchers' level,
  `-x` skipping PyPy's broken `lib2to3/tests` data: a read-only install never recompiles.
  The previous `<out>.zip`/`<out>.tar.gz` is deleted first; `archive` writes gztar on POSIX and,
  on Windows, `make_archive`'s zip (`strict_timestamps=False`; `.sh` entries as Unix entries with
  mode 0755: `create_system = 3` is needed too, unzip ignores MS-DOS mode bits).
  - Launchers: `.cmd` = ASCII + CRLF, `start ""` + `pythonw.exe` for GUI apps, env values with
    `%` written `%%` and values it cannot hold (non-ASCII, `"`, line breaks) rejected; `.sh` =
    0755, values through `shlex.quote`, its folder from `${BASH_SOURCE:-$0}` (niubash keeps the
    caller's `$0`) with symlinks resolved (a `readlink` loop, at most 40 links, each relative
    target joined to the `cd -P`/`pwd -P` folder of its link: a logical `cd`, and ksh93's `cd -P`
    on a relative path, fold `..` as text) and `CDPATH=''` (an exported CDPATH made `cd` print the
    folder or pick another one); its `_pt_*` helpers are unset. Both use `-s` plus `-O`/`-OO`,
    never `-I`/`-E` (Python would ignore `PYTHONUTF8` and the `PYTHON*` values of
    `deploy.portable.env`), and set `PYTHONUTF8=1`.
  - `runtime = "system"`: each launcher RUNS every candidate interpreter with a minimum-version
    probe (`.cmd`: `py -X.Y`, `python3`, `python`, exit 9009 when none fits: the legacy `py` can
    exist with no Python registered; `.sh`: `pythonX.Y`, `python3`, `python`, exit 127; PyPy:
    `pypy3`, `pypy`). With `app.gui` the `.cmd` run lines are `start "" pyw -X.Y`/`pythonw`/
    `pypyw` (`common.windowed`; the probe keeps the console names). The Python install
    manager's `py`/`python` install the requested version when NO runtime exists at all (its
    `automatic_install` default; the silenced probe hides it, so that first start can take a
    minute): the launchers leave `PYTHON_MANAGER_*` to the user. With native dependencies it warns
    that the folder only works on the host key.
  - Every bundled build starts its interpreter before reporting success (`_smoke_runtime`, after
    UPX, with the launchers' `-s -O` plus `-B`): it must run and its `sys.prefix` (a `PTPREFIX:`
    line) must be inside the folder's `runtime/`, else `DeployError` (a prune or UPX regression,
    a python-build-standalone/PyPy layout change, an interpreter that finds the uv base again);
    a copy without the interpreter file fails before `compileall` (`_check_interpreter`).
  - mypyc builds are smoke-tested (`_smoke_compiled`, code from `smoke_code`, run with the
    launchers' `-s -O` plus `-B`): the same
    `sys.path` as `boot.py` (`app/` first, then `lib/`), result read from a `PTSMOKE:` marker
    line because imported packages may print (raylib's banner); a failed import shows the
    traceback and raises `DeployError`.
- **pyz**: Python cannot import `.pyd/.so` from a zip, so `__main__.py` extracts to a per-build
  cache (`%LOCALAPPDATA%` / `~/Library/Caches` / an absolute `$XDG_CACHE_HOME` or `~/.cache`,
  then `<name>/pyz/<build_id>/<key|pure>/`), guarded by a `.complete` marker and an atomic
  `os.replace`; a folder left without its marker (an interrupted prune, a DLL still loaded) is
  moved aside and re-extracted (`_discard`). Every start touches its build folder; `_prune_old`
  deletes only builds beyond the 3 most recently started AND older than a day (`MIN_AGE`: a
  running build is never deleted), unlinking their `.complete` markers first, and tolerates
  folders that vanish under it. Without a usable cache (`Path.home()` raises for a UID without
  a passwd entry; a read-only home) it extracts into a per-run `tempfile.mkdtemp` folder removed
  at exit (never a predictable shared `/tmp` path: another user could plant code there).
  Layout: `common/lib` only when the build is "pure": every target site installed exactly the
  locked set (`common.skipped_requirements`: no pin was excluded by a `sys_platform`,
  `python_version` or `implementation_name` marker), the sites hold the same distributions and
  nothing is native (`has_native`); otherwise EVERY target gets `targets/<key>/lib` and the build
  warns which conditional pins restrict it ("runs on: <keys>"). `common/` never holds extensions
  (`bug:` check). The mypyc overlay `targets/<host>/app` holds only the extension files: the
  bootstrap extracts `common/` and `targets/<key>/` into ONE folder, so each `.pyd/.so` lands
  next to its `.py` and the extension loader wins. `_pyz.json`: `name`, `build_id`, `min_python`,
  `targets`, `pure`, `backend`, `host` (the key that built it), `deps`
  (`common.requirements_digest`: the pin lines of the export, not its header). The archive is
  written by `pyz._write_archive` (deflate, never zstd: it must open on 3.11 and PyPy;
  `strict_timestamps=False`: a payload file older than 1980, e.g. from the Nix store, used to
  crash `zipapp`; shebang `/usr/bin/env python3`, mode 0755). The `<n>.cmd` wrapper runs each
  candidate interpreter with a minimum-version probe, sets `PYTHONUTF8=1`;
  with `app.gui` its run lines are `start "" pyw/pythonw/pypyw` (`common.windowed`). `pyz-merge`
  (>= 2 parts; `_read_info` refuses a part without a valid `_pyz.json`) requires the same app
  name, `min_python`, `deps` and app code (`common/app`, CRLF-normalised: Windows CI checkouts);
  takes `common/app` and `__main__.py` from the first part, every `targets/<key>/lib` from ONE
  part (the one built on that platform, else the first), refuses two compiled overlays for one
  key, and when parts differ in purity moves each pure part's `common/lib` to
  `targets/<its host>/lib` (an older part without `host`: the single overlay key of a mypyc
  part, else "rebuild it") and keeps no `common/lib`. The merged `targets` come from the
  folders written; `host` is dropped. It also writes the `<out stem>.cmd` wrapper next to
  `--out` (`pyz.wrapper_path`; the parts' name and `min_python`, the pypy candidate order only
  when every part is a pypy build; an `--out` ending in `.cmd` is refused). `pyz.check_parts`
  runs the part checks in `--dry-run` too.
- **wheel**: synthetic build project in `.build/wheel/<b>` (`setuptools>=84`; for mypyc
  `mypy==<version locked in uv.lock>` in `build-system.requires`, a `setup.py` using mypycify
  with the same `compile.multi_file`, `separate` and `strict_dunder_typing` as the stage, and a
  compile `mypy.ini`). Assets go into `<pkg>/assets` (package data). Needs network (isolated
  build env). mypyc -> platform wheel; cpython/pypy -> `py3-none-any`.
- **nuitka**: `.build/nuitka-stage/<b>`, `uv run --locked --with nuitka==<NUITKA> python -m
  nuitka` with cwd = stage; `--include-package=<pkg>`, `--include-module` for the mypyc hidden
  imports the tools env can locate (`nuitka.includable`: top-level `find_spec` with the stage on
  `sys.path`, built-ins dropped, compiled modules and extensions always kept; Nuitka stops with
  FATAL on a module it cannot locate, e.g. a platform-guarded `import winreg`),
  `--python-flag=no_asserts/no_docstrings` from the shared `deploy.optimize` (an owner
  decision: no Nuitka-only switch), `--nofollow-import-to` per `deploy.exclude_modules`, the upx
  plugin when enabled, then `nuitka.optimization_args` BEFORE `deploy.nuitka.extra_args` and the
  command line (Nuitka takes the last value, so an `--lto` there still wins): always
  `--lto=<deploy.nuitka.lto>`, default `auto`, which Nuitka 4.2.2 resolves to yes for uv's
  python-build-standalone on Linux, Windows (MSVC) and macOS, BUT off when more than 250 modules
  are compiled (the stdlib goes in as bytecode and does not count; the app and the third-party
  code Nuitka follows or `--include-package`s do): script and raylib stay far below (a raylib
  app compiles ~18), the flet preset compiles ~794 (pygments 325, flet 280...), so `auto` means
  NO LTO there; never make `yes` its default (an ~800-module LTO link is unmeasured on MSVC).
  Measured with gcc 13 on a tiny script: `--lto=yes` built faster (9-10 s vs 22 s), smaller
  (7.21 vs 7.78 MB) and ran 0-5% faster. `pgo = true` adds `--pgo-c` and, when `pgo_args` is
  not empty, ONE item `--pgo-args=<shlex.join(pgo_args)>` (Nuitka shlex-splits it on every OS;
  uv gets an argv list) and prints `PGO_NOTE` (experimental in standalone/onefile per Nuitka;
  measured gain 10-15% on pure-Python loops only; a dependency with a pure-Python fallback such
  as msgpack may be profiled on that path). `nuitka.check_options` (from `cmd_build` before the
  checks, also in `--dry-run`, and from `build`) refuses PGO with the mypyc backend (the
  profiling run starts before `main.dist` holds the extension modules: ImportError, yet Nuitka
  reports success) and on macOS (Nuitka 4.2.2 has no clang profdata step); `--dry-run` prints
  `Nuitka options: ...` (the lto/pgo flags, then the extras). Standalone on Linux/macOS names the
  binary `<name>.bin` when `app.name.lower() == pkg` (the default): it sits in `main.dist/` next
  to the package folder `<pkg>/`, and a file with that name made Nuitka fail with
  NotADirectoryError (case-insensitive on macOS); onefile and Windows keep the plain name.
  `nuitka.check_python` (called by `cmd_build` before the checks): a `python.cpython` newer
  than `NUITKA_PYTHON` (the newest minor the pin supports; bump both together) exits 3 naming
  the pin, unless Nuitka's own `--experimental=python3.X` is passed; a failed Nuitka run also
  names the pin. Output found by file-name
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
  project `pyproject.toml` (no other table leaks in; `[tool.flet.app]` alone is kept) with
  `app.path` forced to `STAGE_APP` (`src`, where `build` stages the app; another value is
  ignored with a warning: flet looked for `<work>/<path>/main.py` and aborted after installing
  Flutter) and `requires-python = "==<python.cpython>.*"` (flet bundles the HIGHEST Python of
  its manifest matching it: `>=3.13` gave 3.14 and the cp313 mypyc extensions were silently not
  loaded; a minor its manifest lacks now fails loudly). Mobile/web targets (`apk aab ipa
  ios-simulator web`) cannot load extensions: a mypyc backend ships the `.py`. Desktop embeds
  the `python.cpython` minor, so the mypyc `.pyd`/`.so` files work. `cleanup`/`exclude` map to `--cleanup-app --cleanup-packages` / `--exclude`;
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
  reusing another level), Windows only: PyInstaller's `configure.get_config` turns UPX off on
  every other OS (packed `.so` files crash), so there `exe.size_args` passes `--noupx`,
  downloads nothing and warns that the exe is not packed (never set `PYINSTALLER_FORCE_UPX`);
  nuitka = its upx plugin (hard-codes `--best --lzma`, ignores our
  excludes: it packed `python314.dll` and the app still ran); portable (before the smoke test,
  so the packed `.pyd` files are what it loads) and flet = `upx.pack_tree` (PE `.exe/.dll/.pyd`
  on Windows, ELF executables but no `.so` on Linux, in parallel). Never packed: files over
  `MAX_INPUT` (600 MiB; UPX refuses 768 MiB), `BUILTIN_EXCLUDE` (C runtime, API sets,
  `python3*.dll`, `libpython3*`, and `flutter_windows.dll`: a packed Flutter engine hangs the
  app at startup with a 4 MB working set and no window, measured), binaries UPX rejects
  (`GUARD_CF`: never pass `--force`). `upx.find` order: `deploy.upx.path` (absolute, `~`, or
  relative to the project root, never the caller's cwd; handed to the tools absolute but not
  resolved: PyInstaller wants `<upx-dir>/upx`, Nuitka a file named `upx`), `upx` on PATH, the
  cache, a download: UPX 5.2.1 once (SHA-256 checked, written as `.part` then renamed so an
  interrupted write never looks cached) to
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

- `preset.toml`: `description`, `dependencies` / `dev_dependencies` (with `{option}`: those
  entries follow `[preset.<p>]` through `./deploy apply` (`presets.option_dependencies`); the
  plain ones belong to the project after `new`), `[options]` (defaults of `[preset.<p>]`),
  optional `[uv]` (extra managed `[tool.uv]` keys), optional `pyproject` string (extra tables,
  with `{{name}}`/`{{pkg}}`).
- `files/`: complete skeleton, including a full `pytemplate.toml` (`__init` overwrites the root
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
  with the current name (CRLF-normalised). `presets.init` refuses otherwise (use `--force`); it
  deletes `src/ tests/ typings/`, writes every skeleton file (including root
  `pytemplate.toml`, not covered by the pristine check), chmods `deploy` and `deploy.ps1` on
  POSIX, sets `project.name`, replaces the preset tables, `write_pyproject`, `uv remove/add
  --no-sync` for the dependency diff, `uv lock`, `render.apply(force=True)`.
- `presets.check_name_free` (in `new`, `__init`, their dry runs and rename/apply): the app name
  may not equal (normalised) a dependency of the resulting project: uv refuses
  self-dependencies and `src/<pkg>/` would shadow the library; nor a keyword or a
  standard-library module of any supported Python (`presets.shadows_stdlib`). It reads
  pyproject.toml as `utf-8-sig`. `new` names the app after the folder, so
  `./deploy new ../flet --preset flet` fails with a hint to use `--name`. `new` also checks the
  name format before copying (no half-made project).
- **[template repo]** Root `src/`, `tests/` and `pytemplate.toml` must equal
  `presets/script/files` rendered with `name = "myapp"` (verified: `presets.pristine` is
  true). Edit the preset, then regenerate the root with `./deploy __init script --name myapp
  --force` (changes no byte when they already match: `test_removals` checks it), or mirror the
  edit byte for byte.
- `copy_template(dest)` (used by `new`) skips `.git`, `.build`, `dist`, caches, `.flet`,
  `.venv*`, `template-repo` at any depth; `build/` and `.claude/` at the root; and
  `.github/workflows/template-*` (template CI files MUST use that prefix). `new` then runs
  the copy's own runner with `__init <preset> --name <n> --force` inside the copy, `git init`
  and `git add --chmod=+x deploy deploy.ps1`.
- `init` is internal only: `cli.INTERNAL["__init"]` (`cmd_mode.cmd_init`), reached by `new` and
  by the template maintainer, listed nowhere. `./deploy init` exits 2 with the hint `./deploy
  new DIR --preset P`: a project's preset is chosen when it is created.
- Hard-coded preset names in the runner: `render.ci_workflow` (raylib: apt GL/X11 libs, no
  PyPy on macOS), `methods/exe.build` (flet -> `flet pack`), `methods/flet.build` (flet only),
  `config._check_default_methods` (a `deploy.default` of `flet` needs the flet preset),
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
  selected interpreter; works under WSL); PyPy (only when supported; per-OS `python`; the
  debugger is unreliable on PyPy); "Run mypyc stage (compiled modules cannot be stepped into)"
  when mypyc is supported (program `.build/mypyc-dev/stage/main.py`, `.venv` python per OS
  through a `windows` block, `preLaunchTask: "deploy: compile"`, `pathMappings` src <->
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
  Task); under Remote-WSL on a Windows checkout the PyPy and mypyc launch configs point at
  the Windows-side paths (`.venv-pypy`, `.build/mypyc-dev`: no `-wsl` suffix, no `.build/wsl`);
  `\\wsl$` UNC paths do not work with `deploy.cmd`.

### 12.2 LazyVim / Neovim (`editors/nvim.py`, `.pytemplate/nvim/`, `cmd_nvim.py`)

Files:
- `.lazy.lua` (generated from `.pytemplate/templates/nvim/lazy.lua`): lazy.nvim's `local_spec`
  loads the first `.lazy.lua` found upward from Neovim's cwd, through `vim.secure.read` +
  `loadstring`. It MUST stay static (identical bytes in every mode and preset;
  `test_nvim_render.py` checks 6 configs): Neovim trusts it by the sha256 of its raw bytes,
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
  suffix, which the plugin adds itself),
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
  the mode (`mode`, `setup`, `sync`, `lock`, `add`, `remove`, `render`) get the
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
  runner fixes: portable smoke with `lib/`, lazy `{python}`, the pyz `.cmd` wrapper, binary
  preset files, `compile.annotate`, `sync_tree` ns mtimes, portable launcher quoting and
  version probes, unknown arguments, `app.preset`, pinned tools, flet pyproject, wheel
  options), `test_e2e_plan.py` (the pure planning of `e2e.py`), `test_build_methods.py` (argv and
  output discovery of exe, flet pack, Nuitka and flet build with the packager recorded;
  `cmd_build` argument checks; target keys, `install_deps` floors and junk; the pyz layout,
  `pyz-merge` and the real bootstrap run in subprocesses with the cache redirected; one REAL
  host pyz build run with `python -S`, skipped when uv cannot install offline; portable prune,
  launchers, precompile and the runtime smoke with real interpreters), `test_upx.py` (UPX
  flags, candidates per OS, the pinned download with fake archives), `test_removals.py` (no JIT
  key, env, launch config or `PYTHON_JIT` left; `./deploy init` exits 2 with its hint; `new`
  and the maintainer route through `__init`, for real in throwaway copies),
  `test_config_rules.py` (every schema field and validate rule with a positive and a negative
  case, the encodings, `set_value`/`update_file` on taplo-formatted, CRLF and BOM files, `mode`
  argument parsing, and real `mode --typing/--editor/--supports` round trips in a throwaway
  copy that must restore every byte), `test_cli_core.py` (the runner's core: global options,
  dispatch, render-before-command, help, the exit code of every outcome, every command rejecting
  a bogus argument, `proc.run` dry-run/errors/signals/threads, `base_env` per variable, Ctrl+C
  with real children that trap SIGINT, a closed stdout, `[tasks]` deps/cycles/placeholders/cwd/
  env/uv modes/arguments, `check`/`test`/`lint` semantics, and exit codes through `deploy.py` in a
  throwaway copy), `test_envs_core.py` (the section 7 contract, `MIN_UV`, clean, sync/add/remove/lock
  command lines, `ensure_lock`, exec bits in a throwaway git repository, compiler checks, doctor
  lines and exit code; real uv only offline in `.venv`), `test_hooks.py` (the hook in throwaway
  repositories, git runs it for real; the real ruff/uv command lines against `.venv`),
  `test_apply.py` (`./deploy apply`/`setup` in throwaway projects with a fake uv that edits
  pyproject.toml like `uv add/remove --frozen`: the per-key matrix, `[preset.*]` changes, the
  record, preset detection, hand-edited name/preset, refusals before any write, dry runs,
  idempotence, every hook state with real git, doctor lines, hints; real `./deploy` runs in a
  copy of the template, and `test_uv_frozen_edits_only_pyproject` checks offline the uv
  behaviour the fake imitates), `test_rename.py` (the skeleton invariant for 3 presets x 7
  pairs x LF/CRLF, random names and round trips, every rewrite rule, scopes, TOML keys and
  module keys, encodings, git states, rollback, the ruff tidy-up, the command in-process and a
  real run in a copy).
- **[template repo]** Language guard `test_no_spanish.py`: skipped unless
  `.pytemplate/template-repo` exists. Scans `git ls-files --cached --others --exclude-standard`
  (so new untracked files count) for accented Spanish letters and a list of Spanish words
  (including the template's former default app name). Add `lang: allow` to a line to allow it
  on purpose.
- Test rules: tests that need a missing tool or shell must skip cleanly (they also run on
  Linux/macOS CI), and so must tests that need the network: the real runs in `test_rename.py`
  and `test_apply.py` re-lock with `uv lock` and SKIP with "needs PyPI" when uv cannot reach
  the index (`rename.needs_pypi`); everything else in `./deploy selftest` works offline once
  `./deploy setup` has run. Tests that spawn `./deploy` must scrub `UV`, `VIRTUAL_ENV`,
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
harnesses, `[deploy.nuitka]` lto/pgo outside Linux (measured with Nuitka 4.2.2 and gcc 13 only:
PGO with MSVC and an ~800-module LTO link are unmeasured).

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
  to log files and timeouts that kill the process tree; `hooks._run_bytes` too, for raw bytes
  and stdin (section 5.6).
- Text files: `encoding="utf-8", newline="\n"`; write `"\ufeff"`, never a literal BOM; read
  `pytemplate.toml` with `config.read_text` (a bad encoding becomes a clear config error);
  parse Python sources as bytes (`imports.parse`).
  Generated `.cmd` files: ASCII, explicit `\r\n`, written with `newline=""`.
- Project paths from `project.*` (never the cwd); user-typed paths through
  `project.user_path`.
- Honour `proc.DRY_RUN` for side effects (section 5.4).
- Reject unknown arguments (section 5.2, item 5).
- Never design CLI syntax that needs `--`, empty-string arguments or cmd metacharacters
  (PowerShell and cmd mangle them).

Adding a command:
1. `Command(module, func, summary, usage, render, group)` in `cli.COMMANDS`; `render=False` if
   it must work without (or before) rendering. An internal step that must stay out of help,
   `editor.json`, the editors and completion goes to `cli.INTERNAL` instead (name `__x`).
2. `def cmd_x(cfg: Config, args: list[str]) -> int` in a `cmd_*.py`, argparse with
   `prog="./deploy x"`; reject unknown arguments (`cmd_dev.only_flags` for flag-only commands)
   and add it to `test_cli_core.MINIMAL` (or, if its arguments belong to another program, to
   `cli.FORWARDS`), and to `test_cli_core.NEVER_RENDER` when `render=False`. `-h`/`--help`
   after it is handled by `cli.dispatch`.
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
- `deploy` and `deploy.ps1` are 100755: `cmd_env._fix_exec_bit` repairs both on `setup`, the
  files' own exec bit on POSIX (with or without git: with `core.filemode=true` an index-only fix
  is undone by the next `git add`) and the git mode (`core.filemode=false` on Windows loses
  it); `presets.new` marks both, `presets.init` chmods both on POSIX. `deploy.cmd` stays 100644.
- Default app content lives in `presets/script/files/` (section 11).

## 15. Known issues and fragile points (still open)

Behaviour:
- `cmd_dev.split_backend` treats a first argument equal to `cpython`, `pypy` or `mypyc` (and
  `all` for `test`/`check`) as the backend: an app argument with that value must be preceded
  by an explicit backend (`./deploy run cpython mypyc`). By design: the usual fix, `--`, is
  eaten by PowerShell.
- Tasks: a task with `backend = "mypyc"` runs interpreted in `.venv` unless it goes through a
  `deps` entry such as `compile` and runs the stage.
- Ctrl+C waits for the running child (section 5.3): a child that ignores SIGINT keeps the
  runner waiting (Ctrl+\ or closing the terminal ends both), as with `uv run`. Windows: a
  closed stdout pipe is `OSError` EINVAL there, so `./deploy help | more` quitting early still
  prints a traceback (untested).
- `clean --envs` removes every `.venv*` of this side (WSL on /mnt: only the `-wsl` ones),
  including the environments in use; there is no "unused only" option (`mode` and `apply`
  only print a note about leftovers). On Windows close the editor first (its mypy/ruff servers run from
  `.venv`), or clean fails with exit 1.
- raylib + PyPy on macOS arm64: no PyPy wheel for that platform (raylib 6.0.1.0 still has
  none) and `no-build-package`, so `./deploy setup` of a raylib project fails to sync
  `.venv-pypy` on Apple Silicon (confirmed on `macos-latest`: "marked as `--no-build` but has
  no binary distribution"). The generated `ci.yml` syncs only the matrix backends and
  `selftest --e2e` switches the project off PyPy first (`e2e.HOST_GAPS`); users there run
  `./deploy mode cpython --supports cpython,mypyc`. Possible fix: skip PyPy for raylib on
  macOS aarch64 in `apply`/`new`.
- `./deploy lock` re-locks without applying `[preset.*]`: after a `[preset.raylib] package`
  switch it moves `no-build-package` to the new name and keeps the old dependency until
  `./deploy apply` runs (every mismatch hint names apply). A `[preset.flet] version` edit that
  is not applied yet shows only in `doctor` (`cmd_apply.pending`): `render.auto` and the
  pre-commit hook compare the managed block, where flet leaves no trace.
- A hand-edited `app.name` is rendered into `editor.json` and `ci.yml` by the next command's
  `render.auto` before `apply` renames the package (harmless: apply's dirty-tree check ignores
  generated files, and a run in between still uses src/<old_pkg>/).
- WSL on `/mnt`: `pyrightconfig.json` (`venv: ".venv"`) and the PyPy/mypyc launch
  configs ignore `ENV_SUFFIX`, so VS Code and pyright inside WSL point at the Windows-side
  environments (the Neovim plugin adds `-wsl` itself). Untested.
- MSYS2 without a login shell (the xonsh `!m` route): `MSYSTEM_PREFIX`, `EXEPATH` and `SHELL`
  do not reach the runner, so `/home/...`-style paths typed for `new`/`pyz-merge` fall back to
  the current drive with a warning. Fix idea: the launcher exports the Windows path of
  `/usr/bin/cygpath` and `find_cygpath` checks it first.
- niubash: the generated portable `.sh` launcher leaks `HERE` and its exported variables into
  the calling session (niubash runs sh scripts in-process; its `_pt_*` helpers are unset). Its
  `cd -P`/`pwd -P`/`CDPATH=''` symlink resolution is verified with dash, bash, zsh, ksh, mksh,
  yash and busybox, not with niubash.
- Portable: a runtime layout change in python-build-standalone or PyPy (bin/python3 missing, the
  stdlib renamed) is only caught by `selftest --e2e`; a bundled PyPy portable build is not run
  on CI.
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
  `config._build`, `config._decode` and `presets._norm_name`; `cmd_apply` calls the private
  `cmd_env._envs_for`, `_env_dirs`, `_fix_exec_bit`, `cmd_mode._precheck_py311` and
  `rename._plan_pyproject`; `rename` and `cmd_env` import `cmd_apply` lazily (it imports both
  at module level).
- `upx.BUILTIN_EXCLUDE` must keep `flutter_windows.dll`; `nuitka._flet_client_archive` mirrors
  flet_desktop's download URL and its `flet_desktop/app/` lookup.
- `config._check_default_methods` imports `cmd_build.COMPAT` lazily (`cmd_build` imports
  `config`); `config._check_preset_tables` reads `preset.toml` `[options]` itself, like
  `config._presets` mirrors `presets.available`; `render.managed_block` needs `pypy_minor`
  (PyPy's environment) and `min_python` (requires-python) to stay distinct.
- `cmd_mode._config_from_text` and `e2e.preset_info` call the private `config._build`;
  `e2e.flet_build_reason` imports `methods.flet._developer_mode`; `cmd_nvim.c_compiler`
  imports `cmd_env._msvc` lazily (`cmd_env` imports `cmd_nvim`).
- `RULES_RE` / `tasks.parse_line` <-> `ui.error`, `ui.warn`, `str(lintc.Finding)` (5.3).
- `envs.MIN_UV` <-> the presets' `python.pypy` pin and default `python.cpython`, and the newest
  uv flag the runner uses (7); `hooks.launcher_of` <-> `hooks.sh_literal`; `cmd_env._msvc`
  <-> setuptools' `_find_vc2017` component choice (`test_msvc_component_matches_setuptools`);
  `cmd_nvim.c_compiler` lacks `cmd_env._xcode_problem` (the macOS xcrun shim check).
- `editor.json` <-> `cli.COMMANDS` (6.2); `cmd_nvim.EXTRAS` <-> the extras list in
  `templates/nvim/lazy.lua`; `vscode.MYPYC_STAGE` / `editor.json` `mypyc_stage` /
  `vscode._STAGE` (the pytest stage matcher) <-> `mypyc.profile(cfg, ...).stage`; the CI pyz path
  <-> `BuildRequest.out_name` (10; `test_ci_workflow_for_every_preset_and_backend_set`).
