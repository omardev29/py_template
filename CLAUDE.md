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
10. QUALITY BAR (set by the owner; never relax it, never argue it away). Bug density = confirmed
    bugs / lines of our code:
    - at most 1 bug per 1000 lines: ACCEPTABLE, the only state in which the work is done;
    - worse than 1 per 1000 but better than 1 per 500: TOLERABLE only with a written plan back
      under 1 per 1000 (every known bug listed with its fix);
    - 1 per 500 or worse: UNACCEPTABLE, fixing it comes before anything else;
    - worse than 1 per 100: UNRELIABLE software.
    Counted: a reproduced defect of OUR code (the runner, launchers, templates, presets, the
    Neovim plugin, the template's CI) that stops the user from doing something, at one of three
    severities:
    - critical: it loses or corrupts data (the user's files, the project, uv.lock), opens a
      security hole, or gives a silently wrong result (a build without what it should hold, a
      check that passes when it must fail);
    - serious: a command, build method or documented feature fails or cannot be used in a
      supported setup, and there is no reasonable workaround;
    - notable: it fails in a supported case but a workaround exists, or it leaves a half-made
      change or a broken state the user must repair by hand.
    Stability defects (what breaks by itself with time: a moving version, a schedule GitHub
    disables) count like bugs, at the same severities.
    Not counted, but still fixed when found: minor and cosmetic defects that stop nothing (a
    character printed wrong, an `n` with tilde garbled in a message, an unclear hint, layout).
    Also not counted: the user's code;
    a defect of a dependency (uv, PyInstaller, Nuitka, mypyc, flet, PowerShell, a shell...) that
    something should do and does not, when our workaround is documented in section 15.1
    (dependency and version, symptom, upstream issue, the workaround by symbol, the test that
    covers it, when it can go). An undocumented workaround counts as our bug. Lines and the
    measurements so far: section 13.4.

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
README.md, LICENSE                [template repo] the template's page and MIT license; a
                                  project gets its own README.md and keeps these two as
                                  .pytemplate/README.md and .pytemplate/LICENSE (section 11)
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
.pytemplate/presets/<p>/          preset.toml + files/ skeleton (+ constraints.txt: tested pins;
                                  + raylib/tools/raylib_stubs.py)
.pytemplate/nvim/                 local Neovim plugin pytemplate.nvim (lua/, tests/smoke.lua,
                                  tests/lazy-lock.json: the plugin commits selftest --nvim pins,
                                  README.md: setup, keymaps, options)
.pytemplate/tests/                runner tests (pytest) + mypy-runner.ini
.pytemplate/editor.json           generated data file for the Neovim plugin
.pytemplate/state.json            hashes of the generated files + the `applied` record (committed)
.pytemplate/template-repo         [template repo] marker, not copied by ./deploy new
.github/workflows/ci.yml          generated CI of the project
.github/workflows/template-*.yml  [template repo] selftest, launchers, nvim, e2e CI and the
                                  keepalive of their schedules (section 13.2); not copied
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
   propagate its exit code, with the caller's `UV_PYTHON` removed for uv only (`deploy`:
   `unset` before `exec`, `UV_PYTHON=''` for niubash; `deploy.cmd`: `set "UV_PYTHON="` under
   `setlocal`; `deploy.ps1`: removed and restored like the `PYTEMPLATE_*` pair; plugin and nu
   snippet: an empty value, which uv reads as unset). The runner then always runs on the
   project's Python (section 5.2); its own tools never used the caller's `UV_PYTHON` anyway
   (`proc.base_env`). Launchers never `cd`; besides the uv search and the install hints they
   contain no logic.

Launcher exit codes: 2 = no project found, 127 = uv not found (after the install hints),
126 = `deploy.ps1` could not start uv, or PowerShell runs it in ConstrainedLanguage mode;
anything else comes from the runner (3 when uv started it on a Python older than 3.11).

uv search order (the same in all three launchers and the plugin, `init.uv_candidates`;
`test_launcher_sh.test_uv_search_order` walks it with fake uvs for `deploy` and `deploy.ps1`):
`$UV` (must be a file) -> PATH (`deploy`: `command -v uv` accepted only when it prints a
path, which rejects aliases and functions; `deploy.ps1`: `Get-Command -CommandType
Application -All`, only a real `uv.exe` on Windows; the plugin: `exepath('uv.exe')` on
Windows, never a `uv.cmd`/`uv.bat` shim) -> `UV_INSTALL_DIR[/bin]`, `XDG_BIN_HOME`,
`XDG_DATA_HOME/../bin`, `~/.local/bin`, `CARGO_HOME/bin`, `~/.cargo/bin` -> Windows: WinGet
`Links` and `Packages/astral-sh.uv_*` (`LOCALAPPDATA`, then `ProgramFiles`), scoop shims
(`SCOOP`, `~/scoop`, `SCOOP_GLOBAL`, `ProgramData/scoop`), chocolatey, and last the user and
machine `Path` stored in the registry (a console opened before uv was installed; not in the
plugin) -> POSIX: `/opt/homebrew/bin`, `/usr/local/bin`, linuxbrew, `~/.nix-profile/bin` (the
plugin also tries the nix default profile, `/run/current-system/sw/bin` and `/usr/bin`). On
Windows the sh launcher uses `USERPROFILE` as home. On Linux/macOS a candidate needs an x bit
(`test -x` in `deploy`, `deploy.ps1`'s `Test-Uv` through `[IO.File]::GetUnixFileMode`, which
PowerShell < 7.3 lacks: then no mode check): a `uv` left without one is skipped.

Install hints: Windows prints the PowerShell installer, winget and scoop (never curl); POSIX
prints curl, brew and pipx. `deploy` (stdin and stderr are TTYs) and `deploy.ps1`
(`UserInteractive`, stdin not redirected) offer to run the official installer when `CI` is
unset; `deploy.cmd` never prompts. `test_launcher_sh.test_install_prompt_through_a_terminal`
answers y/n on a pseudo-terminal (a fake `curl` installs a fake uv) for both.

uv exports `UV` (its own path) to everything it starts, so `proc.find_uv` checks `$UV` first,
then `shutil.which("uv")`, else `DeployError(..., 3)`.

`PYTEMPLATE_LAUNCHER` values: `sh`, `sh:bash`, `sh:zsh`, `sh:niubash`, each with `:msys` (when
`/usr/bin/msys-2.0.dll` exists) or `:cygwin` (`cygwin1.dll`) appended (`sh:bash:msys`,
`sh:msys` for dash under MSYS2); `cmd`; `ps1:<PSEdition>:<major>.<minor>` (`ps1:Core:7.6`,
`ps1:Desktop:5.1`); `nvim`; `nu` (shell-setup snippet). The xonsh snippet sets none.
`project.native_path` reads the `:msys`/`:cygwin` suffix, `shells.guess_shell` the prefix (only
`ps1:` and `sh:niubash` first: `sh:bash`/`sh:zsh` name the interpreter of `#!/bin/sh`, bash on
macOS, Fedora and Arch, so `$SHELL` wins over them), and `./deploy doctor` prints the value
("unknown" when unset).

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
  `[[ ]]`, `${v//a/b}`, `local`, `$'...'`, `function`. No `set -e` / `set -u` in the file, but
  a caller's (`sh -eu deploy`, niubash with errexit, in-process) must not stop it: `${v:-}`
  for variables that may be unset, and every command that may fail sits in a condition or
  ends in `|| :` / `|| _pt_x=` (never `read -r x || x=`: a last line without a newline still
  fills x and returns 1). The lint rejects a `_pt_x=$(...)` without `||`;
  `test_launcher_survives_caller_errexit` runs `-eu` in 8 shells (uv off PATH, a stale `UV`,
  no uv).
- `printf '%s\n'`, never `echo`, for anything that may contain a backslash (dash's `echo`
  interprets `\`: `c:\Users` prints as `c: sers`).
- niubash hygiene (section 4.6): every variable and function name starts with `_pt_` and is
  `unset` before the one `exit`/`exec` at the end: errors (no project: 2, no uv: 127) only set
  `_pt_rc` and fall through to the cleanup, then `exit "$1"` (the lint rejects an `exit`
  before the `unset -f` cleanup; `test_in_process_run_leaves_no_name_behind` sources the
  launcher with an EXIT trap in bash, dash, busybox, ksh, mksh and yash and checks that no
  name is left on every path); never `cd`; `exit` only at top level or inside `if`/`case`
  bodies (`return` is fine anywhere); no comment on a `name() {` line; never write
  `"...$(cmd "$x")..."`: assign the substitution to a variable first.
- niubash is detected by `__RUBASH_SHELL_NAME`. There uv runs WITHOUT `exec` (exec would only
  end the in-process script) as `if UV_PYTHON='' "$@"` (errexit-proof; the session keeps its
  own `UV_PYTHON`), then the launcher unsets `PYTEMPLATE_CALLER_CWD` and `PYTEMPLATE_LAUNCHER`
  and exits with uv's code, so nothing leaks into the calling session. Elsewhere it runs
  `unset UV_PYTHON` and `exec "$@"`.
- Root discovery: `$BASH_SOURCE` -> zsh `${(%):-%x}` (read through `eval` so dash never parses
  it) -> `$0` -> walk up from `$PWD`. The walk stops at `/`, `C:` and `C:/`; relative
  candidates (`./`, `../`) are folded against `$PWD`, but the folded path is used only when it
  holds `.pytemplate/deploy.py`: `$PWD` is logical, and below a symlinked folder its `..` is
  not the folder the kernel found `../deploy` in; then the relative path stays (uv resolves it
  physically, like the kernel). A logical parent that is ANOTHER project still wins (fixing
  that needs `test -ef`, which `shellcheck -s sh` rejects, or a `pwd -P` fork).
- Windows detection (computed first, before any path helper): `OS=Windows_NT` and no
  `WSL_DISTRO_NAME`; `uname -s` only when `OS` is unset. Never `OSTYPE` (niubash fakes
  `msys`). Never trust the output format of `uname` or `cygpath`: in non-login MSYS2 shells on
  the maintainer's machine they resolve to WinuxCmd copies. `_pt_slashes` turns `\` into `/`
  only there: on POSIX a backslash is part of a file name (`/data/a\b/proj` works).
- On Windows the script path and `PYTEMPLATE_CALLER_CWD` are handed over as `C:\...`: `/c/x`
  and `/cygdrive/c/x` are converted in pure sh (drive letter upper-cased); `cygpath -m` runs
  only for paths inside the MSYS/Cygwin root such as `/home` or `/tmp`, and only an `X:/...`
  answer is accepted.
- `%NAME%` in registry values is expanded from the environment, retrying the upper-case name
  (MSYS2/Cygwin upper-case `SYSTEMROOT`, `PROGRAMFILES`...). Quoted entries
  (`"C:\Program Files\x"`, written by some installers) lose their quotes first.
- Registry lookup: `reg.exe query KEY` WITHOUT `/v` (MSYS rewrites `/v` into `V:/`). It only
  runs when every other lookup failed. The Windows-only helpers are plain sh:
  `test_windows_helpers_in_posix_shells` and `test_registry_path_quoted_entries` run them
  (`_pt_win=1`, a fake `reg.exe`) in 8 POSIX shells.
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
  `reg query KEY /v Path` (cmd has no MSYS rewriting) into a variable (`set "PT_LIST=%%B"`),
  never passed as `call` arguments: a quoted entry (`"C:\Program Files\x"`) would split them
  and leave the FOR set unclosed. The quotes are removed (`%PT_LIST:"=%`), then `call set`
  expands `REG_EXPAND_SZ` values (`test_cmd_registry_path_with_quoted_entries`, a fake
  `reg.cmd` on PATH, Windows only).
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
  variables and the removed `UV_PYTHON` (the `$names` list) are restored in `finally`; a
  variable that did not exist before is removed with
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
- PowerShell 7.3+ takes ANY native argument equal to `--%` (quoted or splatted too) for its
  stop-parsing token: it drops it, then splits and `%VAR%`-expands the rest. An argument
  `--%` switches that one call to legacy passing (`$PSNativeCommandArgumentPassing =
  'Legacy'` in the script's scope only), whose pre-quoted `"--%"` reaches uv intact.
- A typed `-X:v` reaches a script (and a function's `@args`) as two elements, `'-X:'` marked
  with a hidden `<CommandParameterName>` note, and `v`: the launcher joins them again, as
  PowerShell does for a native program. Limit: `-X: v` (a blank after the colon) arrives as
  `-X:v`. `pwsh -File deploy.ps1 ...` and `./deploy.ps1` typed in bash/zsh (the shebang
  route) are worse: pwsh itself splits every argument starting with `-` at its first colon
  before the script runs (`--x=a:b` -> `--x=a` `b`): from POSIX shells use `./deploy`.
- Pipeline input (`'x' | ./deploy.ps1 run`, `Get-Content f | ./deploy run`) goes to uv's
  stdin, like a direct native call; without a pipeline uv keeps the process stdin. The file
  NEVER names the automatic `$input` variable (`test_ps1_never_names_the_pipeline_variable`):
  a script whose text uses it makes `pwsh -File` and the shebang route re-read a redirected
  stdin as text lines (ConsoleHost `IsUsingDollarInput`: invalid UTF-8 becomes U+FFFD, CRLF
  becomes LF). It reads the variable by name
  (`$ExecutionContext.SessionState.PSVariable.GetValue('input')`) when
  `$MyInvocation.ExpectingInput`; Core prefixes the Invoke-Expression call with
  `$pipeIn | `. PowerShell encodes pipeline text with `$OutputEncoding`, exactly as for a direct
  native call (5.1: ASCII by default; on GitHub's Windows runners 5.1 also puts a BOM in front of
  it, even of an empty pipeline and whatever `$OutputEncoding` says):
  `test_ps1_forwards_pipeline_input_and_keeps_raw_stdin` compares the launcher with a direct uv
  call in the same session.
- ConstrainedLanguage mode (AppLocker/WDAC policies run unsigned scripts in it) blocks every
  .NET call, `[Console]` included: the launcher checks
  `$ExecutionContext.SessionState.LanguageMode` first and stops with one `Write-Error` naming
  `deploy.cmd`, exit 126.
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
  `& $uv ... @argv`. Its safety rests on `EscapeSingleQuotedStringContent`, which also doubles
  the typographic single quotes U+2018..U+201B (PowerShell reads them as quotes): never
  "simplify" it to `.Replace("'", "''")`. `test_ps1_hand_over_is_injection_safe` splats ~80
  hostile arguments (a payload per quote kind, `$(...)`, backticks, newlines, U+2028, a 100000
  character one) and fails on any argv change or executed payload. `selftest --shells` T1
  passes `~`, `~/x`, `~\x` and typographic quotes with a payload to PowerShell only
  (`shells.PS_ARGS`).
- uv: `Get-Command uv -CommandType Application -All`, and on Windows only a real `.exe` (a
  plain `Get-Command uv` can return an alias or function; a `uv.cmd`/`uv.ps1` wrapper would
  parse the arguments again). The registry `Path` is read with
  `[Environment]::GetEnvironmentVariable` (expands `%VARS%`). `Read-Host` is wrapped in `try`.
- Tests: every deploy.ps1 behaviour test runs wherever pwsh is installed (Linux and macOS
  too: the same Core hand-over), plus Windows PowerShell 5.1 on Windows; only the registry
  and cmd tests are Windows-only.
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
  `stdin_tty`, `stdin`, `root`, `python` (the runner's interpreter version)) and exits with
  EXIT. Launcher tests use it: keep the keys.
  The stdin line is read as BYTES and decoded UTF-8 with surrogateescape whatever the locale:
  a raw non-UTF-8 byte shows up as `\udcXX`, PowerShell's re-encoded text as U+FFFD.
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
  (`shells.cmd_shell_setup`; no argument guesses the shell (`shells.guess_shell`): the
  `ps1:`/`sh:niubash` prefix of `PYTEMPLATE_LAUNCHER`, then `XONSH_VERSION`, then the basename
  of `SHELL` (bash, zsh, fish, nu), and only then the `sh:zsh`/`sh:bash` prefix, which names
  the interpreter of `#!/bin/sh` (Git Bash/MSYS2 without `SHELL`); unknown shell exits 2):
  prints a `deploy` function/alias that works
  from any subfolder, plus where to paste it. Output is ASCII with LF even on Windows (written
  to `stdout.buffer`: it is appended to rc files). bash, zsh, niubash and msys2 share one POSIX
  function whose walk-up stops at `/`, `C:`, `C:/` and at a backslash `PWD` (the old `dirname`
  loop never ended on niubash's `C:/...` paths); niubash: paste it into `~/.niubashrc` AND the
  `$NIU_ENV` file; msys2: above the interactive guard of `.bashrc` (`!m` lines also need
  `BASH_ENV`). pwsh/powershell: a function that walks up and calls `deploy.ps1` (it forwards
  pipeline input with `$input` when `$MyInvocation.ExpectingInput`: fine in a profile
  function, never in deploy.ps1). nu: a `def --wrapped` that runs uv directly with
  `PYTEMPLATE_LAUNCHER=nu` and `UV_PYTHON` emptied. xonsh: an alias that runs uv directly
  (falls back to the launcher without uv; `@aliases.return_command` when the xonsh has it,
  else an unthreadable function alias) plus a registered completer whose words come from
  `cli.COMMANDS` and the project's `[tasks]` at print time (a new command appears once it is
  in `COMMANDS`, `cli.INTERNAL` routes never: `test_xonsh_completion_follows_cli_commands`;
  users print the snippet again to get it); unlike the launchers it keeps the
  caller's `UV_PYTHON` (a returned argv cannot change the environment), so the runner's
  version check (section 5.2) is its guard. `test_shells` executes the fish, pwsh and xonsh
  snippets in their shells (argv, exit codes, walk-up, the xonsh completer, pwsh pipeline
  input); the nu one only where nu is installed (the macOS jobs of template-selftest and
  template-launchers install nushell).
- `shells.doctor(check)` (from `./deploy doctor`): step "launchers": the launcher that started
  the run, then `deploy` (`#!/bin/sh`, LF, ASCII, git mode 100755, exec bit on POSIX),
  `deploy.cmd` (CRLF, ASCII) and `deploy.ps1` (LF, ASCII, no BOM; a mode other than 100755 is
  only a note), each problem with the command that fixes it. Step "shell" (Windows): `bash` =
  WSL stub note, the execution policy of 5.1 and 7; WSL info.

## 5. Runner architecture

### 5.1 Modules (`.pytemplate/runner/`)

| Module | Responsibility |
|---|---|
| `deploy.py` (one level up) | Stops with exit 3 and one `error:` line (no traceback) when uv started it on Python < 3.11 (a `uv run` by hand with an old `UV_PYTHON`), BEFORE importing the runner; reconfigures stdout/stderr to UTF-8, puts its own dir on `sys.path`, calls `runner.cli.main`. |
| `cli.py` | `COMMANDS` table of `Command(module, func, summary, usage, render, group)`, modules imported lazily; `FORWARDS` / `HELP_PASSES_THROUGH` (section 5.2); `INTERNAL` (routes listed nowhere: `__init`). `_parse_globals`, `dispatch` (also the exit-2 hint of the removed `init`), `main`/`_main` (exception -> exit code, closed stdout), `cmd_help` (commands and `[tasks]` entries), `cmd_tasks`, `cmd_selftest` (plain, `--shells`, `--nvim`, `--e2e`), the `__probe` route, `EXAMPLES`. |
| `config.py` | Dataclass schema (`SCHEMA`, `DEFAULT_METHODS`), `read_text` (UTF-8 only, clear error otherwise), strict loader (`_build`: unknown key or wrong type -> error with the full key path), `validate`, derived values (`pkg`, `min_python`, `pypy_minor`, `profile_for`, `pypy_enabled`), `compiled_paths`, comment-preserving editor `set_value` / `update_file` (section 6.1), `toml_value`. |
| `project.py` | Paths (`ROOT`, `SRC`, `BUILD`, `DIST`, `TEMPLATES`, `PRESETS`...), `IS_WINDOWS/IS_MACOS/IS_WSL`, `ENV_SUFFIX`, `venv_python`, `host_os/host_arch` (uv names), `rel`, `code_dirs`, `native_path`, `find_cygpath`, `caller_cwd`, `user_path`. |
| `ui.py` | All runner output to stderr; `DeployError(msg, code)`; `VERBOSE/QUIET`; `report` (never hidden by `-q`); colours (`color_enabled`, `enable_vt_mode`); `check_line` (doctor lines `[ok]`, `[XX]`, `[--]`). |
| `proc.py` | `find_uv`, `base_env` (`UV_SELECTION`), `run` (echo, `DRY_RUN`, cwd defaults to `ROOT` and must be a folder, UTF-8 capture, waits through Ctrl+C), `output`, `show` (display quoting only), `exit_code` (signal N -> 128+N), `vs_installer_dir`, `CommandFailed`, `Interrupted`. |
| `envs.py` | `PyEnv(key, dir, request, preference)`; `cpython_env`, `pypy_env`, `tool_env` (always CPython), `runtime_env(backend)`, `env_vars`, `uv`, `uv_run` (= `uv run --locked`, plus `--project <ROOT>` when `cwd` is not the root: section 7), `sync` (all groups), `interpreter_info` (with `platform`); `MIN_UV`, `uv_version`, `uv_problem`, `require_min_uv`, `UV_UPDATE`, `uv_error` (uv's `error:` message). |
| `render.py` | Every generated file (`outputs`), hand-edit detection (`apply`, `auto`), typing profiles (`load_profile`), `mypy_ini`, `mypy_cli_args`, `pyright_config`, `ruff_config`, `to_toml`, `jsonc`, `ci_workflow`, managed pyproject parts (`managed_block`, `write_pyproject`, `pyproject_outdated`, `check_pyproject`). |
| `editors/vscode.py` | `.vscode/settings.json`, `extensions.json`, `launch.json`, `tasks.json` (`catalog`, `scan`, `problem_matchers`; section 12.1). |
| `editors/nvim.py` | `.lazy.lua` (verbatim template copy) and `.pytemplate/editor.json` (`editor_data`; section 12.2). |
| `presets.py` | Preset discovery/loading (`load`: a broken `preset.toml` is a `DeployError` naming it), option merge, `uv_extras`, `dependencies`, `skeleton`, `pristine`, name rules (`APP_NAME` and `NAME_RULE`, defined in `config`; `name_from_folder`, `check_name_free`, `locked_names`), tested pins (`constraints`, `constraints_text`), `plan_init` + `init` (run by `./deploy __init`; with rollback), `copy_template`, `new`; for apply and rename: `default_options`, `option_dependencies` (the requirements with an `{option}`), `set_project_name` (checked `_set_project_name`) / `project_name` (the `[project]` table only), `shadows_stdlib` (`STDLIB_OTHER_VERSIONS`). |
| `mypyc.py` | `compiled_sources`, incremental stage (`sync_tree`, `remove_stale_extensions`), `spec.json` + `COMPILED_STAMP` (+ `COMPILER_ENV`), spawning `tools/mypyc_build.py` (`MYPYC_REJECTED`), `ANNOTATE_HTML`, `hidden_imports` (+ `importable`), `exe_stage`, `runtime_env_vars`, `has_compiler_hint`. |
| `imports.py` | AST import extraction that skips `if TYPE_CHECKING:` blocks (`imports_of`, `iter_runtime_nodes`); parses bytes (tolerates a BOM); `parse_error`, `local_module`, `is_local`. |
| `lintc.py` | Extra AST rules for compiled modules (section 9): `lint_file(cfg, path)`, `lint`, `Finding`, `NATIVE_CLASS_DECORATORS`, `relative_file_at_import`. |
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

1. Launcher -> `uv run --quiet --script .pytemplate/deploy.py ARGS` (the caller's `UV_PYTHON`
   removed, section 4.1). uv reads the `.python-version` found from the script's folder upward
   (the project's, i.e. `python.cpython`) and the managed `python-preference`, so the runner
   runs on the project's managed CPython (in a cached ephemeral env, maybe downloaded first)
   whatever the caller's cwd or a `.python-version` there says (measured with uv 0.8 and
   0.12; `test_launcher_sh.test_runner_runs_on_python_cpython_whatever_the_caller_pins`), and
   uv exports `UV`. Changing `python.cpython` changes the runner's Python too: the runner must
   stay 3.11 code (the PEP 723 floor) and work on newer versions.
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
  `envs.MIN_UV`, a program, compiler, interpreter, Neovim/git with `--require`, the runner
  itself started on Python < 3.11 by `deploy.py`'s check); 130 = Ctrl+C;
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
  read-only (`uv run --locked --no-sync`, no `uv sync` first) and is skipped when `.venv` does
  not exist (`uv run --no-sync` would create it).
- `__init` runs `presets.plan_init` (every check of the real run: name, `check_name_free`,
  pristine, the new `pytemplate.toml` and `pyproject.toml`) and lists each file as `-` deleted,
  `+` new or `~` replaced, the dependencies removed and added, the pinned versions, and what
  happens to `pyproject.toml` and `uv.lock`. `new` checks the destination and the name
  (format, `check_name_free`) and prints destination, preset, name, the number of pins and the
  `__init` step it would run in the copy. `pyz-merge` validates its inputs (`pyz.check_parts`:
  valid `_pyz.json`, one app, one build) and prints inputs and outputs (the `.pyz` and its
  `.cmd`).
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
| `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `UV_PYTHON_PREFERENCE` | `envs.env_vars` | Environment selection (section 7). The caller's own `UV_PYTHON` never reaches the runner: the launchers, the Neovim plugin and the nu snippet remove or empty it (section 4.1); a `uv run` by hand that keeps a pre-3.11 one stops in `deploy.py` (exit 3) |
| `PYTHONUTF8=1` | `proc.base_env`, portable launchers, pyz `.cmd` wrapper, the Neovim mypy linter, every VS Code launch config (`vscode.DEBUG_ENV`) | mypy/mypyc otherwise read files as cp1252; F5 behaves like `./deploy run` |
| `PYTEMPLATE_BACKEND` | `cmd_dev.test_backend`, `mypyc.runtime_env_vars`, mypyc launch config | Backend under test (conftest) |
| `PYTEMPLATE_COMPILED` | `mypyc.runtime_env_vars` | Modules that must load from `.pyd/.so` (conftest) |
| `PYTEMPLATE_ASSETS` | `portable/boot.py`, `pyz/__main__.py` (setdefault) | Assets dir for `resources.assets_dir()` (raylib, flet) |
| `VSLANG=1033` | `mypyc.build`, wheel builds | English MSVC messages |
| `MACOSX_DEPLOYMENT_TARGET` | `methods.common.install_deps` for macOS targets, unless the user set it (`MACOS_FLOOR`, 13.0) | The oldest macOS the pyz/portable wheels must support |
| `CC`, `CFLAGS`, `CPPFLAGS`, `LDSHARED`, `LDFLAGS`, `ARCHFLAGS`, `CL`, `_CL_` | user | setuptools builds the mypyc extensions with them (a `CFLAGS` REPLACES Python's own: section 9); `mypyc.COMPILER_ENV`, a change forces a rebuild |
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
  and none unstaged/untracked; managed pyproject parts, what only `./deploy apply` brings in
  line (`cmd_apply.pending(hook=False)`: a hand-edited `app.name`/`app.preset`, the
  `[preset.*]` options) and `uv lock --check`; `pytemplate.toml`,
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
  runner's `sys.stdlib_module_names` plus `STDLIB_OTHER_VERSIONS`: the runner runs on
  `python.cpython` (5.2), but the app may also run on PyPy 3.11 and `python.cpython` can move), a
  dependency
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
- `[app]`: `name` (`config.APP_NAME`: a letter first, a letter or digit last, PEP 508; `pkg =
  name.replace("-", "_").lower()`), `preset`
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
  mypyc build writes the annotate report, section 9), `opt_level "0".."3"`,
  `no_semantic_interposition` (default true: a C flag on Linux gcc/clang, section 9),
  `multi_file`, `separate`, `strict_dunder_typing`. `config._validate_compile` checks the names
  against each other: no entry of `modules` inside another (or repeated: mypyc aborted with
  "Duplicate module"), every `exclude` strictly inside a `modules` entry (a module or a
  subpackage: it excludes everything below it). Whether they exist is checked by
  `mypyc.compiled_sources` (an `exclude` that names nothing is an error, not a silent no-op).
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
  match" and `uv self update`. uv reads that setting from the project it finds from the cwd:
  run from a folder outside the project (`/path/to/deploy ...`), an old uv starts the runner
  and `envs.require_min_uv` is the guard. Bump it with the pins
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
- Compiled modules in the generated configs (`render.mypy_ini`, `render.pyright_config`):
  `[mypy_compiled]` goes to `[mypy-<m>.*]` per `compile.modules` entry (`x.*` also covers `x`
  itself); each `compile.exclude` entry gets `[mypy-<ex>.*]` with those keys back to the
  global `[mypy]` value (mypy prefers the longer pattern), so interpreted glue may use `Any`.
  `mypy_ini` writes ONE section per module pattern and merges generated sections with
  `[[typing.mypy_overrides]]` (later options win; a list of modules becomes one section per
  pattern): mypy's `RawConfigParser` refuses a repeated section, and a pattern repeated in a
  comma list silently replaced the earlier options. pyright's `strict` list has no exclusion,
  so a compiled package that holds an excluded path is listed by its other files and folders
  (`render._paths_without`, only then does the list depend on the files in `src/`); with
  basedpyright the excluded paths come first in `executionEnvironments` (the first match wins)
  without the Any rules. The same sections reach the stage's compile-time `mypy.ini`.
- `cmd_dev.run_checks(cfg, backend, rules=True)`:
  1. `_profile_file` writes `.build/cfg/ruff-<profile>.toml` and `.build/cfg/mypy-<profile>.ini`
     (the profile of THAT backend, which may differ from the editor's active one).
  2. `ruff check --config ...` (`--exit-zero` when the profile says so).
  3. mypy unless `skip_mypy`; with PyPy supported `render.mypy_cli_args` adds
     `--python-version <min_python> --python-executable <tool python>`. Exit 1 is only a
     warning when the profile is not `blocking`.
  4. `lintc` rules on the compiled sources (when mypyc is supported and `rules`): errors only
     under the `mypyc` profile, warnings otherwise. A file the runner cannot parse is one
     finding (`imports.parse_error`: a syntax error, or syntax newer than the runner's own
     Python), never an internal error.
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
- Native ints (`mypy_extensions.i64`/`i32`) are a typing choice with runtime effects: compiled,
  their arithmetic is plain C and wraps silently on overflow; interpreted they are plain `int`
  (no wrap), so a test on the cpython backend never sees the overflow. Speed facts: section 9.

## 9. mypyc pipeline (`mypyc.py`, `tools/mypyc_build.py`)

- Compile in a COPY of `src/` (`.build/mypyc-{dev,release}/stage`), never in `src/`: a `.pyd`
  next to your `.py` would shadow your edits. The `.py` stays next to the `.pyd` in the stage
  (the extension loader wins) so pyz/portable can fall back to the `.py` on another interpreter.
- Profiles: `dev` (run, test, compile, report) keeps asserts, `debug_level "1"`; `release`
  (build, `compile --release`) strips asserts when `deploy.optimize >= 1`, `debug_level "0"`.
- `compiled_sources`: the `.py` files of `compile.modules` (never `__init__.py`), minus
  `compile.exclude` (exact module or package prefix; an entry naming nothing -> `DeployError`),
  deduplicated. Walks with `mypyc._walk`, like `sync_tree`.
- `sync_tree(src, dst, owned=())` copies changed files only and deletes removed ones. Change
  detection is size + `st_mtime_ns` (`copy2` preserves the exact mtime), so a same-size edit
  within one second is detected. `_walk` follows symlinked folders (`Path.rglob` does not
  descend into them: a linked `src/assets` arrived empty), skipping a link back to a folder on
  the current path (cycles; two links to one folder are both copied) and never entering
  `SKIP_DIRS`; a broken symlink is a warning. A path that turned from file to folder (or back)
  is replaced; a folder deleted from `src` goes with its caches (a `__pycache__` kept it
  importable as a namespace package). Extensions: only build outputs (`_mypyc_output`: a
  module in `owned`, a `*__mypyc` lib, or an extension next to its own `.py`) are never copied
  from `src/` (a stray in-place build would shadow the stage's, or reach a payload without its
  shared lib) nor deleted from `dst`; any other `.so`/`.pyd` in `src/` (a
  vendored native library, which must be `git add -f`ed past `.gitignore`) is app content and
  synced like any file, so `run mypyc` and every payload see what `run cpython` sees.
  `mypyc.build` passes `owned=modules`; `cmd_build.payload` and `methods/flet.py` use the
  default. Not handled: a case-only rename on a case-insensitive file system.
- `remove_stale_extensions(stage, modules, group, python=, separate=, src=)` runs BEFORE the
  sync (a folder it empties is then removed) and deletes: extensions whose ABI tag
  (`cpython-314-...`, `cp314-...`, a trailing `t` = free-threaded) is not `python.cpython` (old
  code that an interpreter of that version would still import: portable `runtime = "system"`,
  `flet build`); modules no longer compiled; shared libs this build does not use (`<group>__mypyc`,
  or one `<module>__mypyc` per module with `compile.separate = true`, which were deleted on every
  build). A non-output file mirrored from `src/` is left alone.
- `group_name = pkg` gives a stable shared lib `<pkg>__mypyc.<tag>.pyd`; `hidden_imports` and
  `remove_stale_extensions` depend on that name. `compile.separate = true` -> `group_name=None`.
- Forced rebuilds: setuptools rebuilds an extension only when a source is newer, and an option
  that only reaches the C compiler (`opt_level`, `no_semantic_interposition`; `debug_level`
  per profile; the compiler variables of the environment) leaves the C unchanged, so the old
  binary was kept (and shipped). `build` records the options of the last SUCCESSFUL compile of
  a profile in `<profile dir>/compiled-options.json` (`COMPILED_STAMP`: every spec key but
  `annotate`, `compile`, `files`, `force`, plus `env`: the `COMPILER_ENV` variables that are
  set, `CC CFLAGS CPPFLAGS LDSHARED LDFLAGS ARCHFLAGS CL _CL_`), deletes it
  before compiling (a failed or interrupted build forces the next one) and sets
  `spec["force"]` when it differs: `mypyc_build.py` then passes `build_ext --force`. `report`
  (`compile_c=False`) and `--dry-run` neither force nor record. A `.build` from before has no
  record: one full rebuild. mypyc dates a new C file 1 s ahead, so a very fast first build is
  redone once by setuptools itself.
- `spec.json` uses stage-relative `c_dir=../c`, `build_temp=../obj`, `build_lib=../lib`: short
  paths under MSVC's MAX_PATH.
- `tools/mypyc_build.py` runs in `.venv` with `VSLANG=1033`: `chdir(stage)`,
  `mypycify(..., group_name, target_dir=../c)`, then `setup(build_ext [--force] --inplace ...
  --parallel N)`. It uses the mypycify API because `python -m mypyc` cannot set
  `strip_asserts`, `group_name` or `multi_file` and always writes to `./build`. When mypycify
  exits or raises (mypy/mypyc rejected the code; the errors are printed) it returns
  `MYPYC_REJECTED` (4, mirrored in `mypyc.py`); a C build failure exits with setuptools' code.
  Every spec key it reads must be written by `mypyc.build` (a test parses the script).
- Extra C flags (`mypyc_build.extra_cflags`, appended to mypyc's own `extra_compile_args` of
  every extension, as a NEW list each: mypycify hands one shared list to all of them; the
  wheel's generated `setup.py` mirrors it). The compiler kind comes from `compiler_type()`,
  the way mypycify picks `-O3` or `/O2` (`distutils.ccompiler.new_compiler()` +
  `customize_compiler`); it is only looked up when compiling (`report` needs no compiler).
  - gcc/clang (`"unix"`): `-fno-strict-overflow`, always. It is in Python's own `CFLAGS`, but
    setuptools (84) REPLACES those with a `CFLAGS` environment variable (they also carry
    `-DNDEBUG`, which is then lost: the C asserts of mypyc's runtime come back), and without it
    the wrap-around of i64/i32 arithmetic is undefined behaviour in C. The user's `CFLAGS`
    stay (they come first; mypyc's `-O<opt_level>` after them wins); a repeated flag is harmless.
  - Linux gcc/clang with `compile.no_semantic_interposition` (default true):
    `-fno-semantic-interposition`, as CPython itself is built (`PY_CFLAGS_NODIST`, which
    extensions never get). mypyc's native functions are exported symbols of a `-fPIC` shared
    object, which another library could interpose, so gcc never inlines a call between two
    compiled functions, not even in one module: every call goes through the PLT. Measured (gcc
    13, `f` calling `g` in an i64 loop, N=1e8): 177 ms without, 0 ms with (gcc inlined `g` and
    folded the loop into `return max(n, 0)`). Not on macOS (Apple clang: not verified) nor
    MSVC. `test_mypyc_core` pins it with a real compile through a logging `CC` (objdump shows
    the call only without the flag, gcc only).
- Speed facts (measured with gcc 13, CPython 3.14, mypyc 2.3.1; README "Fast integers: `i64`"
  has the table): plain `int` loops always stay loops (tagged ints, overflow checks, slow paths
  into CPython). With `mypy_extensions.i64`/`i32` locals gcc/clang at `-O2`+ remove or
  vectorise them: `for i in range(1, 100_000_001): x += 1` became `return 100000000`. i64 wraps
  silently when compiled (`x + 1` at `2**63 - 1` gives `-2**63`), not when interpreted.
  `opt_level "0"` (unoptimised C, C asserts on) is 1.8x SLOWER than the interpreter: for
  debugging only. MSVC has no levels: `"1"`-`"3"` are all `/O2`, `"0"` is `/Od`.
- Output is captured unless `-v`; on failure it is printed. The compiler-install hint
  (`has_compiler_hint`) is added only when the exit code is not `MYPYC_REJECTED` (with `-v`
  the output was not captured, and every type error used to get the hint); the runner's own
  code stays 1 (`mypyc failed (exit code 1)`).
- After the build every compiled module must have an extension, else `DeployError` (and no
  record is written, so the next build is forced).
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
- `hidden_imports` (PyInstaller and Nuitka cannot see imports inside a `.pyd`): the compiled
  modules and mypyc's shared libs (`<pkg>__mypyc`, `<module>__mypyc`; never a vendored
  `.so`), plus what `imports_of` finds in the compiled sources (skips `if TYPE_CHECKING:` and
  the `else:` of `if not TYPE_CHECKING:`, resolves relative imports, drops imports beyond the
  top-level package). The app's own names are kept when they exist in `src/`
  (`imports.local_module`: `.py`, package folder or extension file). Every other name goes
  through `importable`: ONE `uv run --locked python -c _FIND_CODE` in the tools env (where the
  packagers run), which keeps a name when its top-level module exists and is not built in
  (Nuitka aborts on `--include-module` of a module it cannot find: a platform-guarded
  `import winreg`, an optional dependency), and `from X import a` candidates (`X.a`, from
  `imports_of(..., candidates)`) when that exact submodule exists: `from html import parser`
  needs `html.parser`, which `html/__init__` never imports (the exe crashed at startup). That
  check imports X's parent packages (they may print: the result is the last `PTMODS:` line);
  if it cannot run, a warning and every name unchecked. A file the runner cannot parse is a
  `DeployError` (2) naming `path:line`.
- `lintc` rules (compiled code only; findings sorted by `lint`, deduplicated per line and
  message; one finding for a file that cannot be parsed):
  - `compile.forbid_imports`, matched against `import a.b`, `from a import b` (also `a.b`) and
    never against relative imports (the app's own modules); one finding per statement.
  - `librt` (mypyc's runtime library): an error while PyPy is supported; otherwise only when it
    is not a `[project]` dependency (read from `PYPROJECT`): mypy installs it in the dev group
    only, so pyz/portable/wheel builds lacked it (`./deploy add librt --cpython-only`).
  - Class decorators: resolved through the module's absolute imports to full names
    (`_import_aliases`, star imports included) and compared with `NATIVE_CLASS_DECORATORS`,
    which mirrors mypyc (`dataclasses.dataclass`, `attr.s`, `attr.attrs`, `typing[_extensions].final`,
    `mypy_extensions.trait`/`mypyc_attr`): attrs' `define`/`frozen`/`mutable` are NOT native,
    `@attr.s` is; `@mypyc_attr(native_class=False)` silences it. A drift test compares the set
    with the locked mypyc's own source.
  - Nested classes and classes inside functions, each reported once (from its nearest class
    or function); t-strings; `if __name__ == "__main__"` (either order) at module level.
  - Module-level `__file__` ONLY when `compile.modules` is one top-level module file
    (`relative_file_at_import`): mypyc (>= 1.20.2) sets the real `__file__` before a module
    body runs, from the folder of the shared lib; with a single top-level module there is no
    shared lib and the body sees a relative `<mod><EXT_SUFFIX>`. It looks at what runs at
    import (class bodies, decorators, default values), not function or lambda bodies. Pinned
    by a real-compile test: if mypyc fixes that case, the test fails and the rule can go.
  `lintc` does not import `presets` or `mypyc`.
- With PyPy supported user code must be 3.11 syntax and API (no PEP 695;
  `typing_extensions.override`, not `typing.override`). `mode --supports +pypy` prechecks it
  (`cmd_mode._precheck_py311`): it syncs the tools env first (a stale `uv.lock` fails in uv's
  own step; not under `--dry-run`), then ruff `--target-version py311` syntax rules (exit 1 =
  findings; any other code = "could not run ruff"), then the mypy errors that appear only as
  3.11 (`PRECHECK_MYPY_FLAGS`: `--config-file=` so the project's `.mypy.ini` is never read,
  where the default `off` profile sets `ignore_errors`, and `--check-untyped-defs`); a mypy
  abort (exit 2) is a `DeployError` with mypy's output, never a silent pass.

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
  `__pycache__` folders are never copied. Prunes `include libs Tools share Scripts`, every `bin/` entry but the interpreter (`BIN_KEEP`:
  `python*`, `pypy*`, `libpypy*`; the base's console scripts, e.g. a 24 MB `ruff` installed into
  it, carried the build machine's paths), on Linux the shared `lib/libpython3.X.so*` when the
  interpreter does not list it in its ELF `DT_NEEDED` (`_keeps_libpython`, `_elf_needed`:
  python-build-standalone links the interpreter statically, so it was 33 MB twice; extension
  modules never link libpython on Linux; an unreadable interpreter keeps it), `tcl*` (Windows
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
- **pyz is THE portable method (owner decision: never narrow it, never make it host-only).**
  exe, nuitka and a bundled portable carry an interpreter, so each build serves one OS, one
  architecture and one libc floor. A pyz carries no interpreter: ONE file for every platform
  where a compatible Python runs. Keep these properties, and test them when pyz changes:
  - a pure build (no native dependency, no platform- or version-conditional pin) runs on ANY
    OS, architecture and libc with CPython or PyPy >= `min_python` (glibc, musl, Android/Termux,
    the BSDs, riscv64...): the bootstrap falls back to `common/` for any key it has no target for;
  - native dependencies travel per target key (`targets/<key>/lib`), installed cross-platform
    by uv for every key of `[deploy.pyz] targets` / `--target` (Windows, Linux and macOS on
    x86_64 and aarch64); PyPy keys and the mypyc extensions come from the build machine, and
    `pyz-merge` joins builds made on several machines into one file (the generated `ci.yml`
    does it on every push);
  - mypyc extensions are an overlay for the keys they were built on, with the `.py` as the
    fallback everywhere else (slower, same result);
  - what a non-pure pyz cannot reach is a limit to document, not a goal: its native targets
    need the exact CPython minor of the lock (`cp314` wheels load only in 3.14), glibc >= 2.28
    (x86_64) / 2.35 (aarch64), macOS >= `MACOS_FLOOR`; no musl or Android wheels.
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
- **wheel**: synthetic build project in `.build/wheel/<b>` (for mypyc a `setup.py` using
  mypycify with the same `compile.multi_file`, `separate`, `strict_dunder_typing` and extra C
  flags (`no_semantic_interposition`, section 9) as the stage, and a compile `mypy.ini`), built
  with `uv build --wheel --no-build-isolation --python <.venv python>` after
  `envs.sync(tool)`: the setuptools, mypy and project dependencies of `uv.lock` (the packages
  the mypyc stage uses), offline. An isolated env resolved
  `setuptools>=84` and mypy's uncapped dependencies from PyPI at every build, and mypycify
  there could not see the project's dependencies (a compiled `import rich` failed). `uv build`
  ignores `UV_PROJECT_ENVIRONMENT`, hence `--python`; `build-system.requires` only records the
  exact locked versions (`wheel._locked_version`: a clear error when missing). Package data =
  every file of the package (`"**/*"`: data files, `py.typed`, vendored native libraries; the
  copy skips caches and stray build outputs: an extension next to its `.py`, `*__mypyc`);
  assets go into `<pkg>/assets`. `app.gui` -> `[project.gui-scripts]` (no console window on
  Windows), else `[project.scripts]`. mypyc -> platform wheel; cpython/pypy -> `py3-none-any`
  (even with a vendored native library: the wheel is not retagged).
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
  `src/assets`. `resources.py` is a boundary module; module-level `__file__` is fine in
  compiled packages (section 9: only a single top-level compiled module sees a relative one).

## 11. Presets (`presets.py`, `.pytemplate/presets/<p>/`)

- `preset.toml`: `description`, `dependencies` / `dev_dependencies` (with `{option}`: those
  entries follow `[preset.<p>]` through `./deploy apply` (`presets.option_dependencies`); the
  plain ones belong to the project after `new`), `[options]` (defaults of `[preset.<p>]`), optional `[uv]` (extra managed `[tool.uv]` keys),
  optional `pyproject` string (extra tables, with `{{name}}`/`{{pkg}}`: `extra_tables`).
  Optional `constraints.txt` next to it: the tested pins (below). `presets.load` reads it as
  `utf-8-sig` and refuses (`DeployError` naming the file) invalid TOML or UTF-8, an unknown key
  and a wrong type (`PRESET_KEYS`): `render.managed_block` reads it on every run.
- `files/`: complete skeleton, including a full `pytemplate.toml` (`__init` overwrites the root
  one), `src/main.py`, `src/__pkg__/core/` (compiled) + a boundary, `tests/conftest.py`
  (identical in every preset), optional `typings/` and tools. It must be ruff-clean: the
  pre-commit hook of a fresh project runs `ruff format --check` and ruff on it
  (`test_presets.py` renders every preset with three names under every profile, py311 and
  py314 targets). No calendar year anywhere in a preset (it goes stale in later projects).
- Four templating syntaxes coexist: `__pkg__` in paths and `{{name}}`/`{{pkg}}` in text (plain
  `.replace`; `f"{{name}}: ..."` in `script/app.py` renders to the app name on purpose);
  `{option}` in `preset.toml` deps and `[uv]` (`str.format_map`); `__HEADER__`-style in
  `ci.yml`; `{root}`-style in `[tasks]` (`format_map`, double literal braces).
- A skeleton file is text (token replacement, LF) only if its suffix is in
  `presets.TEXT_SUFFIXES` or it has none, it holds no NUL byte and it decodes as UTF-8;
  anything else is copied byte for byte, and `pristine` never rewrites CRLF inside binaries.
- `pristine(cfg)`: `src/`, `tests/`, `typings/` equal the current preset skeleton rendered
  with the current name (CRLF-normalised). `init` (the internal step of `new`, below) refuses
  otherwise (use `--force`). `presets.plan_init` makes every check in memory first: name,
  `check_name_free`, pristine, the skeleton's `pytemplate.toml` validated,
  `render.check_pyproject` for the new configuration, and the new `pyproject.toml`
  (`pyproject_after_init`: name and managed parts without the old preset tables, then the new
  ones) parsed and checked: a preset table outside the markers, a damaged or repeated preset
  marker (`_extra_bounds`) or a `[project]` table without `name` is a `DeployError` (exit 2).
  `_set_project_name` only touches the `name` of `[project]` (either one-line quoting, CRLF
  kept: `rename` uses it too; a multi-line string is left alone, so the callers' check stops),
  and the text is split on `\n` only (a U+2028 inside a TOML string is not a
  line break). `--dry-run __init` prints the plan (`cmd_mode._plan_init`).
  `presets.init` then (1) writes `pyproject.toml` and runs `uv remove --frozen` (no
  resolution) for the old preset's requirements and every declared one the new preset adds in
  another form (`presets._dropped`: a `flet-cli==1.0.0` left in the dev group of a flet project
  with `[preset.flet] version = "1.0.0"` made the resolving `uv add flet==1.0.1` fail), `uv add
  --no-sync [--constraints]` for the
  new ones and `uv lock`: the only step that needs the network; a resolved `uv.lock` in which a
  package depends on the project itself (`presets._self_dependents`, section 15.1) is refused
  there, before any file of the skeleton is written; (2) renames `src/ tests/
  typings/` into `.pytemplate-init-*` (all or nothing: a locked file fails the rename before
  anything changed), writes every skeleton file (root `pytemplate.toml` included) and chmods
  `deploy`/`deploy.ps1` on POSIX. Any failure or Ctrl+C in (1) or (2) puts every file back
  (`presets._Undo`, prints "every file is back as it was") and is raised. (3) Deletes the
  aside folder and runs `render.apply(force=True)`.
- `presets.check_name_free` (in `new`, `__init`, their dry runs, and `rename`/`apply`, which
  keep its first line) refuses: a name uv refuses (`config.APP_NAME`: a letter first, a letter
  or digit last, PEP 508), a keyword, a standard-library module of any supported Python
  (`presets.shadows_stdlib`), a backend name (cpython, pypy, mypyc), a Windows device name (`WINDOWS_DEVICES`: `aux`,
  `con`, `nul`, `com1`...: the folder cannot exist there and git cannot check it out), the
  project's own folders and files (`RESERVED_PACKAGES`: tests, typings, build, dist, assets;
  plus the preset's `src/` entries such as `main`), and every package the project will lock:
  the declared requirements (minus the current preset's own), their tree in `uv.lock`
  (`locked_names`, markers ignored because uv refuses a self-dependency on any platform; the
  project's own entry excluded) and the preset's pins (`constraints.txt`: the preset's whole
  tested tree, so a raylib project, whose `uv.lock` has no rich, still refuses `new --preset
  script --name mdurl`). When a preset adds packages the lock does not have, uv may resolve
  them against this lock's versions, so every locked name counts too (conservative). Also a
  module one of those packages installs under another name (`presets.IMPORT_NAMES`, read from
  the pinned wheels' RECORD files: pytest's `py`, which pytest imports before the app,
  markdown-it-py's `markdown_it`, raylib's `pyray`, pyyaml's `yaml`, pillow's `PIL` in lower
  case...; `test_import_names_follow_the_installed_packages` checks it against what `.venv`
  installs). Only the presets' pinned packages are mapped (15.2).
  `new` derives the name from the folder with `name_from_folder` (NFKD -> ASCII, other runs ->
  `-`, no `-`/`_` at the ends) and checks it before copying, so `./deploy new ../flet --preset
  flet` fails with a hint to use `--name`.
- **[template repo]** Root `src/`, `tests/` and `pytemplate.toml` must equal
  `presets/script/files` rendered with `name = "myapp"` (`test_presets.py` checks it, and
  that every preset ships the same `tests/conftest.py`). Edit the preset, then regenerate the
  root with `./deploy __init script --name myapp --force` (changes no byte when they already
  match: `test_removals` checks it), or mirror the edit byte for byte.
- `copy_template(dest)` (used by `new`): in a git work tree only the files git tracks (`git
  ls-files --cached`, with their working-tree content): untracked files are listed as "not
  copied", ignored ones stay silent, so `.env`, `.idea/`, `htmlcov/`, `*.spec` never reach a
  new project. A maintainer's new file must be `git add`ed before `new` or `selftest --e2e`
  sees it. Without git, or when git does not track `.pytemplate/deploy.py` (a copy inside
  another repository, a project never committed), it copies every file; a git failure other
  than "not a git repository" (dubious ownership...) is a warning first (git runs with
  `LC_ALL=C`). Both skip (`presets._skipped`) `.git`, `.build`, `dist`, caches, `.flet`,
  `.venv*`, `template-repo` at any depth; `build/`, `.claude/`, `README.md` and `LICENSE` at
  the root; and `.github/workflows/template-*` (template CI files MUST use that prefix). A
  project made with `new` is another program, not the template (an owner decision): `new`
  (`presets._make_own`) writes its own `README.md` (`presets.project_readme`: name, preset
  description, getting started) and sets `[project] description` to the preset's
  (`_set_description` through `config.set_value`: a multi-line string is replaced whole, a
  missing key is added, an unusual layout is only a warning), and, from the template
  repository only (the `template-repo`
  marker), copies the template's `README.md` and `LICENSE` to `.pytemplate/README.md` (the
  manual of `./deploy`, of that version) and `.pytemplate/LICENSE` (the MIT notice that must
  travel with the copied runner): `presets.TEMPLATE_DOCS`. A project running `new` passes
  those two on as tracked files, and its own root `README.md`/`LICENSE` stay behind. `new`
  then runs the copy's own runner with `__init <preset> --name <n> --force` inside the copy,
  `git init -b
  main` (the generated CI runs on `main`; git < 2.28: plain `init` + `symbolic-ref HEAD
  refs/heads/main`; nothing inside an existing work tree) and `git add --chmod=+x deploy
  deploy.ps1`. When the copy or `__init` fails (a name uv refuses, no network, Ctrl+C) `new`
  removes what it created (the folder and the parents it made, or only the content of the
  empty folder it was given) and says so; a folder with content is refused before anything
  is written.
- `init` is internal only: `cli.INTERNAL["__init"]` (`cmd_mode.cmd_init`), reached by `new` and
  by the template maintainer, listed nowhere. `./deploy init` exits 2 with the hint `./deploy
  new DIR --preset P`: a project's preset is chosen when it is created.
- Tested pins: `presets/<p>/constraints.txt` (`name==version`, sorted, generated, never
  hand-edited) lists every package a project of the preset locks (script 24: the template's
  own `uv.lock`; raylib 26; flet 56), the ones the template's `uv.lock` also holds at its
  versions (a package locked at two versions, a fork by platform, cannot be pinned: none so
  far). `init` hands the ones the project does not lock yet to `uv add --constraints`: a one-off
  (nothing in `pyproject.toml` or the lock manifest, `uv lock --check` passes, `./deploy lock
  --upgrade` moves on), so a new project gets the versions CI tested instead of the newest of
  the day, also when `new` runs from a project of another preset (a raylib project has no
  rich), and packages the source project already locks keep their versions. The name check
  reads the whole list. Regenerate after changing a preset's pins or the root `uv.lock`
  (`test_presets.py::test_preset_pins_hold_the_whole_tested_tree` fails, offline, when the file
  is stale): `./deploy new <tmp>/p --preset <p>` from the template (delete the file first to
  take the newest versions of the packages the template's lock lacks), then write
  `presets.constraints_text(p, <tmp>/p/uv.lock)` to `presets.constraints_path(p)` (a
  `python -c` in `.venv` with `.pytemplate` on `sys.path`) and `git add` it. Rejected:
  `exclude-newer` (recorded in the lock: the next `uv lock --check` fails) and `==`
  `constraint-dependencies` in the managed block (permanent: blocks updates, takes the key
  from the user).
- Hard-coded preset names in the runner: `render.ci_workflow` (raylib: apt GL/X11 libs, no
  PyPy on macOS), `methods/exe.build` (flet -> `flet pack`), `methods/flet.build` (flet only),
  `config._check_default_methods` (a `deploy.default` of `flet` needs the flet preset),
  `e2e.SMOKE` / `e2e.COMPILED_MARK` (expected app output per preset). A new preset that needs
  special packaging or smoke checks must touch these.
- raylib: the upstream stub lies (returns/fields/params declared `bytes`/`list` that are cdata
  or int at runtime); mypyc checks simple types at runtime, so they raise `TypeError` only when
  compiled. `tools/raylib_stubs.py` regenerates `typings/raylib/__init__.pyi` (task `stubs`;
  deterministic, byte-identical to the shipped stub). It skips opaque structs (GLFWcursor...:
  `ffi.sizeof` raises) before reading `.fields`: cffi 2.x wheels that keep their C asserts
  (Linux) abort the process there (exit 134), which no `except` catches.
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
  `background = true`. `[tool.flet]` (read by `flet build` only, which embeds `copyright` in
  the app metadata): `org`, `company` and `copyright = "Copyright (C) {{name}}"` are
  placeholders; no year (dropping the key would not help: Flet's own template default says
  "2026 Your Company").
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
  `compile` (mypyc only, hidden), `lint --fix`, `fmt`, `doctor`, `apply`, `setup`, and one task per
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
  all logic in the plugin. Editing the template forces every user to re-trust: avoid it
  (`test_lazy_lua_bytes_are_pinned` pins the sha256 in `LAZY_LUA_SHA256`, so a change is
  always deliberate; projects already made keep their own copy). Its unguarded read of
  lazy.nvim's internal `spec.modules` is an open fragile point (section 15).
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
  mypy_severity, python_version, basedpyright}` (`basedpyright` = `cmd_dev.BASEDPYRIGHT`, the
  pin of the uvx language server), `envs{tools, cpython, mypyc, pypy}` (without the `-wsl`
  suffix, which the plugin adds itself),
  `mypyc_stage`, `tasks[{name, help, background}]`, `commands[{name, usage, summary, group}]`
  (from `cli.COMMANDS`), `build{methods, default}`. The Lua side (`init.sanitize`) validates
  every value (whitelists, patterns; `basedpyright` only as `basedpyright==X.Y.Z`) and never
  runs a program named in it. `test_sanitize_keeps_every_generated_value` feeds it the
  editor.json of every test variant and of the three presets: nothing may change (a whitelist
  missing a new profile, backend or command would silently fall back to a default), and
  `test_lua_whitelists_match_the_runner` compares `BACKENDS`, `PROFILES`, `EDITORS` and
  `SEVERITIES` with the runner's. `init.info` re-reads
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
/ `jobstart` with a LIST, env `PYTEMPLATE_CALLER_CWD=<cwd>`, `PYTEMPLATE_LAUNCHER=nvim` and
`UV_PYTHON=""` (`init.deploy_env`; uv reads an empty value as unset, so the runner runs on the
project's Python like with the launchers). Never a string command (it would go through
`'shell'`, which may be xonsh or niubash) and never `deploy.cmd`/`deploy` unless uv is nowhere
(the launcher prints the install hints; on POSIX it runs as `/bin/sh <root>/deploy`, like the
VS Code tasks and the git hook, so a checkout without the exec bit still gets them). The uv
lookup mirrors the launchers because a GUI, launchd or MSYS2-login Neovim may have a minimal
PATH; on Windows PATH is searched for `uv.exe` exactly (`exepath('uv.exe')`: a `uv.cmd`/`uv.bat`
shim earlier on PATH would run through cmd.exe and parse the arguments again).

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
  `basedpyright-langserver`, else `uv tool run --from <typing.basedpyright> basedpyright-langserver
  --stdio` with the version `./deploy check` pins (an unpinned request re-resolves to the
  newest release whenever uv's index cache expires), else Mason); pyright comes from Mason and
  needs Node.js. Pylance exists only in VS
  Code. pyright/basedpyright find `.venv` through `pyrightconfig.json` `venvPath`/`venv`, so
  venv-selector's automatic activation is turned off.
- ruff server from `.venv` with `mason = false` (same version as `./deploy check`). Mason
  prepends its bin dir to PATH after `.lazy.lua` runs, so always use absolute `.venv` paths.
- mypy (nvim-lint, core in LazyVim): `cwd = root` (finds `.mypy.ini`; mypy then prints paths
  relative to it, the only form the parser matches), `--python-version <min_python>
  --python-executable <.venv python>` when PyPy is supported (like `render.mypy_cli_args`),
  severity from `typing.mypy_severity`, disabled with the `off` profile or without `.venv`.
  The linter is built once (at startup or on a refresh), but `.venv` may appear later (`./deploy
  setup` in a terminal, or any `uv run --locked` of run/test/check): `cmd` is a function
  resolved at every run, like `condition`, and the Windows PATH prefix is built from the path
  even before `.venv` exists (`test_mypy_linter_follows_a_venv_created_later`). On Windows
  nvim-lint wraps every linter in `cmd.exe /C`, where a quoted absolute path breaks
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
  the mode or `editor.json` (`mode`, `setup`, `apply` (the same `tasks.META` entry as
  `setup`), `sync`, `lock`, `add`, `remove`, `render`, `rename`) get the `pytemplate.refresh`
  component (re-read `editor.json`, LSP `didChangeConfiguration`, rebuild the mypy linter);
  `mode`, `setup`, `apply`, `lock` and `rename` also open their output. Without overseer, tasks
  run in a terminal split.
- Output parser `tasks.parse_line` (overseer `on_output_parse` -> diagnostics + quickfix):
  strips ANSI, honours the `error: `/`warning: ` prefixes, reads basedpyright
  `  path:l:c - sev: msg` and `path:l[:c]: [sev: ]msg` (mypy, ruff concise, pytest crash
  lines); skips notes, `site-packages` and `in <func>` frames. Relative paths resolve against
  the root; mypyc prints them relative to its stage (a copy of `src/`: `<pkg>/core/x.py`), so
  a relative path that exists under `src/` but not under the root lands on `src/` (like the VS
  Code MYPYC matcher).
- Keymaps under `<leader>j` (which-key group "deploy"; `tasks.KEYS`): `j` pick, `r`/`R` run /
  run on a backend with args, `t`/`T` test / all, `c`/`C` check / all, `b`/`B` build / on a
  backend, `l` lint --fix, `f` fmt, `m` switch backend, `k` `[tasks]` picker, `d` `dev` task,
  `p` mypyc report, `s` sync all, `S` setup, `D` doctor, `w` task list, `x` stop deploy tasks.
  `:Deploy ARGS` (completion; no args = help; quotes group words through `tasks.split_args`,
  like the `R`/`B` prompts: `:Deploy run cpython "a b"` passes `a b` as one argument).
  `<leader>j` was chosen because no LazyVim core or extra mapping uses it.

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
  `lazyvim.json` yet (start Neovim once). `bootstrap` clones the LazyVim starter (its newest
  commit, as LazyVim's own install steps do) and deletes its `.git`, only when the config dir
  does not exist. `sync` = `nvim --headless "+Lazy! install" +qa` with cwd = ROOT and
  `NVIM_LOG_FILE` in a temp dir: install only (`Lazy! sync` would also update every plugin
  of the user's config, rewriting their `lazy-lock.json`, and clean the plugins its spec does
  not name). It refuses with exit 3 while `.lazy.lua` is not trusted (the trust prompt would
  hang a headless run, and from any other folder the project's plugins are not in the spec).
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
  check the other branches (template-selftest runs it on all three OSes). The exit code is
  pytest's when it failed, else mypy's (mypy runs either way); `--shells`, `--nvim` and `--e2e`
  return their suite's own code. The arguments are ADDED after `.pytemplate/tests`: a file path
  does not narrow the run (pytest still collects the whole folder); select with `-k EXPR`.
- It must pass in every project made with `./deploy new` too (its CLAUDE.md says so; any
  preset, name, backend set): tests take the app name, package, preset and backends from the
  project they run in (`test_apply._preset_requirements`, `test_cli_core.own`, the copies'
  `pytemplate.toml` in the throwaway-copy tests) or build hermetic fixtures (`src/myapp` in
  tmp with `SRC` monkeypatched), and skip only what cannot apply there, with the reason: the
  template repository's own invariants (the `.pytemplate/template-repo` marker: the root is the
  script preset as `myapp`, the language guard, the workflow tests) and the real builds that
  import rich (`test_build_methods.needs_rich`: a raylib project locks none). template-selftest's
  new-project job proves it for a raylib and a flet project (a new project has its own
  README.md; the template's is `.pytemplate/README.md`). Tests that start what they built with
  `sys.executable` skip on an interpreter older than the build's Python
  (`test_build_methods.skip_when_older_than`: the floor job runs the suite on 3.11).
- `.pytemplate/tests/`: `test_runner.py` (config, render, lintc, imports, target keys),
  `test_no_spanish.py`, `test_launcher_sh.py` (static lint of the `deploy` header rules, `-n`
  syntax checks, `__probe` round-trips per shell found; a caller's `set -eu` in 8 shells,
  symlinked subfolders, backslashes in POSIX paths, niubash's in-process run simulated by
  sourcing (no `_pt_` name left on any exit path), `UV_PYTHON` and the runner's Python, the
  Windows-only sh helpers and a fake `reg.exe` in every POSIX shell, the uv search order with
  fake uvs for `deploy` and `deploy.ps1`, the install prompt on a pseudo-terminal),
  `test_launcher_win.py` (static rules on every OS, a PowerShell parser check; the deploy.ps1
  behaviour tests run wherever pwsh exists: injection safety of the Core hand-over, `--%`,
  `-X:v`, pipeline input and raw stdin, `UV_PYTHON`, ConstrainedLanguage, the x bit; cmd and
  the registry only on Windows),
  `test_paths.py` (path spellings, colours in a hidden console, dry runs in a throwaway copy),
  `test_render_core.py` (`render.apply`/`auto` and `state.json` in a sandbox, the render
  command's exit codes, the managed pyproject parts for every preset and backend set, typing
  profiles, the generated CI for 36 preset/backend combinations through a strict YAML reader and
  `actionlint` when installed, every output clean and hash-seed independent; real taplo and the
  pinned basedpyright when the uv cache has them), `test_shells.py` (also runs the fish, pwsh
  and xonsh snippets in their shells, nu where installed), `test_vscode.py` (also real
  tool output through each task's matchers and a Node `RegExp` cross-check),
  `test_nvim_render.py` (also loads the Lua modules in `nvim --headless --clean`: parser,
  uv lookup, launcher fallback, sanitize round trip, the mypy linter, `tasks.META`, `:Deploy`;
  the pinned `.lazy.lua` hash), `test_cmd_nvim.py` (`nvim` subcommands with fake Neovims,
  the `selftest --nvim` harness: pins, base reuse, smoke parsing, tree kill), `test_fixes.py` (regression tests of the
  runner fixes: portable smoke with `lib/`, lazy `{python}`, the pyz `.cmd` wrapper, binary
  preset files, `compile.annotate`, `sync_tree` ns mtimes, portable launcher quoting and
  version probes, unknown arguments, `app.preset`, pinned tools, flet pyproject, wheel
  options), `test_e2e_plan.py` (the pure planning of `e2e.py`, its pin/docs/file-state checks,
  git isolation, host detection, the HOST_GAPS drift guard against the generated CI, and
  template-e2e.yml's triggers and depths), `test_e2e_run.py` (its running parts with the steps
  faked: logs, timeouts that kill the tree, exit codes, the JSON report, SIGTERM/SIGHUP, the
  step kinds on small fake projects), `test_build_methods.py` (argv and
  output discovery of exe, flet pack, Nuitka and flet build with the packager recorded;
  `cmd_build` argument checks; target keys, `install_deps` floors and junk; the pyz layout,
  `pyz-merge` and the real bootstrap run in subprocesses with the cache redirected; one REAL
  host pyz build run with `python -S`, skipped when uv cannot install offline; portable prune,
  launchers, precompile and the runtime smoke with real interpreters), `test_upx.py` (UPX
  flags, candidates per OS, the pinned download with fake archives), `test_presets.py` (preset
  data and skeletons, ruff under every profile, name rules, pins, `copy_template`,
  `new`/`__init` failures with uv faked; real `new` per preset, the pins steering uv and the
  raylib stub generator when uv reaches the network, checked with `uv pip compile
  --no-cache`), `test_mypyc_core.py` (the PyPy
  precheck, lintc and imports tables, `[compile]` validation, `sync_tree`, stale extensions,
  `mypyc.build` with a fake compiler, `tools/mypyc_build.py`, hidden imports, the
  compiled-module sections of `.mypy.ini`/pyright, the wheel method; plus real runs that skip
  without `.venv` or a C compiler: mypy for the precheck and the exclude sections, a mypyc
  compile of a tmp project (loading, incremental, opt_level rebuild), the relative-`__file__`
  pin, pure and mypyc wheels, and the extra C flags through a logging `CC` with a user
  `CFLAGS`: in every compile command of the stage and the wheel, the i64 wrap-around, and on
  Linux the inlined call (objdump; without the option gcc keeps the call)),
  `test_removals.py` (no JIT key, env, launch config or `PYTHON_JIT` left;
  `./deploy init` exits 2 with its hint; `new` and the maintainer route through `__init`, for
  real in throwaway copies),
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
  real run in a copy), `test_selftest_harness.py` (the exit codes CI trusts: plain `selftest`
  with pytest and mypy faked, `--shells` with the probes faked but `_run_all` and the table
  real, `--nvim` with Neovim, the base and the smoke runs faked: 0, 1 on any FAIL, 2 usage, 3
  with `--require`), `test_workflows.py` (template repository only: the promises of the
  template-*.yml workflows, the keepalive covering every scheduled one, the gates, `-latest`
  labels, pinned actions, and actionlint on every workflow when installed).
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
  `@cache`, `nested class`, `__main__`; it asserts NO `__file__` finding for the default
  package), and so does `test_mypyc_core.py` (`cannot parse`, `regular (slow) Python class`,
  `relative path`, `dev group`, `does not exist on PyPy`, `matches no module`, `is inside`,
  `mypyc failed (exit code 1)`, `could not run ruff`, `mypy could not check`...): rewording
  those messages means updating the tests in the same commit.
- `./deploy selftest --shells`: section 4.9.
- `./deploy selftest --nvim [PRESET,...] [--keep] [--fresh] [--require] [--timeout S]
  [--dir DIR]` (`nvimtest.selftest`): isolated LazyVim under `--dir` (default `%TEMP%\pt\nvim`,
  `$TMPDIR/pt-nvim` elsewhere): `<dir>/x/{config,data,state,cache}` = the `XDG_*` homes,
  `<dir>/base.json` = the base is complete, `<dir>/p/<preset>` = scratch projects,
  `<dir>/logs/` = one log per step. It stops unless Neovim reports every stdpath inside
  `<dir>/x`, refuses a `--dir` inside the template, and only wipes a dir carrying its marker
  `.pytemplate-nvim-test`. Pinned, so a red run is a regression and not upstream drift: the
  LazyVim starter is cloned in full and checked out at `cmd_nvim.STARTER_REV`, and the plugins
  come from `nvimtest.LOCK` (`.pytemplate/nvim/tests/lazy-lock.json`, a green run's lock),
  copied into the isolated config before every Neovim run that installs (lazy.nvim rewrites the
  lock after each run, on disk and in memory, keeping only the plugins its spec named). The base
  takes two runs: `Lazy! install`, where a fresh config installs in rounds (LazyVim, then the
  plugins its specs name) and the lock is pruned between them, so LazyVim's own plugins come at
  their NEWEST commits (measured: nvim-treesitter), then `Lazy! restore`, which starts with
  everything installed and moves them to the lock. Each project's `Lazy! install` is one round
  (LazyVim is installed), so the extras' plugins are checked out at their locked commits
  (measured with an older overseer.nvim pin). After the restore and after each project's install
  `nvimtest.lock_drift` compares the resolved lock with `LOCK`: any plugin at another commit
  fails the run, naming it. Without `LOCK` it takes the latest of everything (a `--depth 1`
  clone of the starter HEAD, `Lazy! sync`). The base is made once and reused; `<dir>/base.json` records
  starter, rev, the lock's sha256, the Neovim version and the starter commit the clone held,
  and a base made for other pins or another Neovim is reinstalled (`--fresh` forces it). Per
  preset: `./deploy new` (name `pt-<preset>`), `./deploy sync cpython` (clean env), `./deploy
  mode --typing strict` (every preset ships `typing.relaxed = off`, which would leave the mypy
  linter untested), trust through the API, `Lazy! install` from the project, and
  `nvim --headless -c "doautocmd UIEnter" -c "luafile .pytemplate/nvim/tests/smoke.lua"` with
  `PT_ROOT=<project>` (timeout 600 s). Every child runs in its own session: a timeout or Ctrl+C
  kills the whole tree (`nvimtest.kill_tree`: SIGTERM to the group, so Neovim can stop its
  jobstart jobs, then SIGKILL after 5 s; `taskkill /T` on Windows). Per-preset table with
  timings; exit 1 on any FAIL, non-zero exit, timeout, a missing `DONE` line, a result count
  that differs from it, or a mypy check that did not run with a typing profile
  (`nvimtest.smoke_problem`). Without `nvim`/`git`: SKIP with exit 0, or exit 3 with
  `--require`; unknown preset: exit 2. First run ~1-2 min for the base, then ~7-35 s per
  preset. Each run copies the resolved `lazy-lock.json` and `starter-commit.txt` into
  `<dir>/logs/` (`nvimtest.record_pins`). Refreshing the pins: run it without the lock
  (`rm .pytemplate/nvim/tests/lazy-lock.json`); when it is green, copy `<dir>/logs/lazy-lock.json`
  back and set `cmd_nvim.STARTER_REV` to `<dir>/logs/starter-commit.txt`, in one commit
  (`test_the_shipped_pins_are_complete` checks the lock names every plugin `.lazy.lua`
  configures).
- `smoke.lua` output contract (parsed by `nvimtest.parse_smoke`): one stdout line per check,
  `ok   NAME`, `FAIL NAME` followed by the error indented 5 spaces, or `SKIP NAME (reason)`
  (only for things that need a network install: a language server via uvx/Mason, a treesitter
  parser; basedpyright is SKIPped after 150 s; and the mypyc debug check without a C
  compiler); each result starts on a fresh line (stdout is shared with anything that leaks
  there, a pty or a banner, and text without a newline would hide the next result) and the
  last line is `DONE <number of checks>`; exit 0 via `qa!`, 1 via `cq!`; a 20-minute
  watchdog; `PT_ROOT` optional. Parse only lines that start with those markers. It runs
  `./deploy help`, `render` and `lint`, and briefly creates `src/<pkg>/_pt_smoke_lint.py` and
  `_pt_smoke_mypy.py`. 20 checks, including the launcher fallback while the scratch project's
  `deploy` has no exec bit (POSIX), stage-relative mypyc paths in the output parser, the mypy diagnostics with the typing
  profile's severity, a debugger stopping at a breakpoint, and the mypyc launch configuration
  (overseer runs its preLaunchTask `deploy: compile`, then a breakpoint in `src/main.py`).
- `./deploy selftest --e2e [PRESET ...] [--backends B,..] [--methods M,..] [--quick|--full]
  [--gui auto|on|off] [--keep] [--reuse] [--json] [--base DIR]` (`e2e.selftest`; `e2e.plan`
  lists the rows of a preset): per preset (default script, raylib, flet) `./deploy new
  <base>/<preset>` from THIS template, then what a user does there. App names `e2escript`
  (named like its package, `e2e.SAME_AS_PACKAGE`: nuitka's `<app>.bin`, the package folder
  next to the executables), `e2e-raylib`, `e2e-flet` (name != package). Every depth:
  - `verify copy` (`do_verify`): nothing `new` must leave out (`template-repo`,
    `template-*.yml`, `.claude`, `.build`, `dist`, `build`, `.venv*`); a git repository with
    `deploy` and `deploy.ps1` at 100755; `uv.lock` at the tested versions (`lock_problems`:
    the template's own lock for the packages it holds, `constraints.txt` for the rest); the
    project's own README (`# <app>`, no myapp) and `[project] description` (the preset's), no
    root LICENSE, `.pytemplate/README.md` and `.pytemplate/LICENSE` byte-equal to the
    template's (`docs_problems`);
  - `pristine skeleton` (`--dry-run __init <preset> --name <app>`), `render --check`, `mode`
    only where the host cannot install a backend (`e2e.HOST_GAPS`: raylib + PyPy on macOS arm64
    and Linux arm64 -> `mode cpython --supports cpython,mypyc`, the pypy rows SKIP; the
    generated `ci.yml` leaves the same backends out for its runners' architectures:
    `test_host_gaps_match_the_generated_ci_matrix`), `setup`, `doctor`, `fmt --check`, the first
    commit (`git add -A` + `git commit` through the hook setup installed), raylib's `stubs`
    task (typings/ must not change), `check`, `test`, `run` per backend;
  - `build <b> --method <m> --no-check` (non-empty `dist/` output, size by `common.tree_bytes`)
    and smoke runs of the headless artifacts of console presets: exe, nuitka (`<app>.bin`
    too), the portable launcher after its folder was moved to `<base>/work` (put back
    afterwards), `python -S <pyz>` with its cache redirected, the wheel in a scratch venv;
    the output must contain the preset's text and, for mypyc, the compiled marker.
  `--quick`: each backend's `deploy.default` method only. Default: every pair
  `cmd_build.COMPAT` allows but nuitka, `./deploy selftest` in the project, a usage error
  (`build <b> pyz` must exit 2) and a rename round trip (`--dry-run rename`, `rename` to
  `e2e.renamed_app` (the other name shape), `render --check`, `test`, the wheel built and run
  under the new name, a commit through the hook (rename refuses a dirty tree), `rename` back).
  `--full`: + nuitka, a `mode --supports +/-pypy` round trip (back to the preset's active
  backend) and a `[preset.*]` option edit (`e2e.OPTION_EDITS` with `config.set_value`, then
  `./deploy apply`: pyproject.toml and uv.lock must follow, `doctor` passes, a second `apply`
  changes nothing). A round trip must give back the project's files (`project_state`: not
  `.git`, environments, builds, caches). `flet build` is SKIP unless Flutter and (Windows)
  Developer Mode are available; GUI runs are SKIP without a display (Linux uses `xvfb-run`)
  or on Windows/macOS CI. A filter shows what it leaves out (SKIP rows `<b> (not supported)`,
  `build (none selected)`); one that tests nothing in any preset exits 2 before anything is
  created (`selection_problem`).
  Isolation: children get `scrub_env` (no `UV`, `UV_PYTHON`, `UV_PROJECT_ENVIRONMENT`,
  `VIRTUAL_ENV`, `PYTHONHOME/PATH`, `PYTEMPLATE_*`, `GIT_*`) plus `isolate_git`:
  `GIT_CEILING_DIRECTORIES` = the base's PARENT (git ignores a ceiling equal to its cwd, and
  `new` looks from the base), so `new` runs `git init` and `setup` installs the hook in the
  project even under a base inside a work tree (the hook used to land in the outer repository
  and stay there); `GIT_CONFIG_GLOBAL` (a missing file) and `GIT_CONFIG_NOSYSTEM=1` keep a
  user's core.hooksPath, init.templateDir or commit.gpgsign out. A base whose ceiling would hide
  the template's own repository (a template in a subfolder of a bigger repository, the base
  next to it) is refused (`hidden_template_repository`: `new` would copy untracked files).
  Layout: `<base>/<preset>`, `<base>/logs/<preset>/NN-step.log`, `<base>/work/<preset>`
  (smoke scratch); default base `%TEMP%\pt\e2e` / `$TMPDIR/pt-e2e`; only a base carrying
  `.pytemplate-e2e` is wiped. Steps run with stdin closed and per-step timeouts (`TIMEOUTS`,
  `BUILD_TIMEOUTS`) that kill the whole process tree (each step in its own session on POSIX);
  a failed `new`, `setup` or host `mode` skips the rest of its preset; a row with `after`
  needs that row to PASS. Exit codes: 0; 1 on any FAIL; 2 usage; 130 interrupted: Ctrl+C, and
  SIGTERM/SIGHUP (`termination_as_interrupt`; a group signal from `timeout` or a closed
  terminal never reached the steps' sessions, and the running build went on as an orphan):
  the running step's tree is killed and the rows so far are reported. The base is kept on
  failure, interrupt or `--keep`; `--reuse` reuses kept projects. `--json` report to stdout
  (`ok`, `interrupted`, `base`, `kept`, `seconds`, `host`, `options`, `results[preset, step,
  status, seconds, detail, log]`).
  Measured (Linux, 4 CPUs shared with other work; ~2.3 min of each run is the project's own
  `./deploy selftest`): script default 4.5 min, raylib default 4.4 min and `--full` 5.7 min,
  flet `--full` 4.3 min (its exe and nuitka builds failed at once there: the sandbox proxy's
  CA broke the Flet client download), script `--full --methods nuitka,wheel` 14.9 min (5.3
  and 6.5 of them its two nuitka builds).
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
  `.pyz`, uploaded with the `<name>.cmd` wrapper `pyz-merge` writes next to it (a literal
  block `path: |`; `pyz.wrapper_path`). Two moving parts on purpose, explained in its header
  and pinned by `test_ci_workflow_keeps_its_moving_parts_on_purpose`: setup-uv gets no
  `version:`, so it installs the newest uv that satisfies pyproject's `required-version` (a
  pinned uv cannot download Pythons released after it), and the runner labels stay `-latest`
  (GitHub retires a pinned label about six months after a newer image is GA).
- **[template repo]** `template-selftest.yml` (push to `main`, pull requests, weekly and by
  hand; the gate): `./deploy render --check` first, then `setup` and `./deploy selftest` on
  ubuntu, macos and windows-latest (Windows through `deploy.ps1`, `--basetemp` in
  `RUNNER_TEMP`, git's default CRLF checkout), with what the tests look for: dash, zsh, ksh,
  mksh, yash, busybox, fish (apt); fish and nushell (brew); MSYS2 with dash and uv copied to
  `~\.local\bin` (the login-shell test); xonsh 0.24.2 and Neovim v0.12.5 everywhere; actionlint
  1.7.12 (SHA-256 checked) on Linux; the runner's own pinned UPX (`upx.find`) on Linux and
  Windows; taplo and the basedpyright pin in the uv cache (their tests run offline). Job
  `python-floor`: the runner starts on 3.11 (`uv run --python 3.11 --script`), and the suite
  runs in process on 3.11 (`uv run --no-project --python 3.11 --with pytest==<locked>`: the
  runner code itself on its floor; the tools the tests start stay in `.venv`). Not on PyPy:
  uv never starts the runner there, and PyPy only changes harness details (it resets an
  inherited SIG_IGN of SIGINT, no PEP 538 locale coercion). Job `uv-floor`: setup-uv with
  `resolution-strategy: lowest` takes the oldest uv the `required-version` accepts, checked
  against `envs.MIN_UV`, then setup and the suite, minus
  `test_init_round_trip_through_every_preset_is_byte_identical` (uv 0.10.12 writes redundant
  markers into uv.lock after a preset round trip: the same lock, other bytes). Job
  `new-project`: `./deploy new` of a raylib and a flet project named `Pt-<preset>`, then setup
  and selftest there. About 5 min on Linux, 8 on macOS, 15-20 on Windows (estimates: not run
  on GitHub yet).
- **[template repo]** `template-keepalive.yml` (weekly and by hand; the gate): in a public
  repository GitHub disables a workflow that has a schedule after 60 days without repository
  activity, and then none of its triggers run, pushes included, until it is enabled again. The
  job enables, with its `GITHUB_TOKEN` (`actions: write`) and `gh api --method PUT
  .../actions/workflows/<file>/enable`, every `template-*.yml` whose `on:` has `schedule:`
  (found by `grep '^  schedule:'`, itself included), skipping one `disabled_manually`; a
  workflow GitHub cannot return fails the job. That the call restarts the 60-day clock is the
  technique of liskin/gh-workflow-keepalive, not documented by GitHub. A new scheduled
  template workflow is covered by itself (`test_every_scheduled_workflow_is_kept_alive`).
  If the keepalive itself was disabled, enable it in the Actions tab (`gh workflow enable
  template-keepalive.yml`) and run it once.
- **[template repo]** `template-launchers.yml` also runs weekly and installs xonsh 0.24.2 on
  pushes and pull requests but the newest xonsh on the schedule (a red scheduled run with no
  commit behind it is upstream drift; the shell versions are logged: xonsh, fish, pwsh,
  busybox, MSYS2 runtime, Cygwin), xonsh on Windows too, and nushell on macOS.
  `template-nvim.yml` pins Neovim v0.12.5 (Ubuntu, Windows) and v0.11.2
  (`cmd_nvim.MIN_LAZYVIM`, Ubuntu: passed for the three presets when added) for pushes and
  pull requests, one log artifact per row; its canary job (weekly and by hand, Ubuntu, Neovim
  stable) deletes `.pytemplate/nvim/tests/lazy-lock.json` and always uploads the logs with the
  resolved `lazy-lock.json` and `starter-commit.txt`, the next pins after a green run (13.1).
- **[template repo]** `template-launchers.yml` (Linux/macOS shells + shellcheck, Windows with
  MSYS2, Cygwin and busybox-w32, optional WSL job; `selftest --shells` plus user-style
  invocations; a gate job checks the marker file), `template-nvim.yml` (Ubuntu + Windows,
  Neovim pinned (above), `fd` (venv-selector from LazyVim's `lang.python` errors on the first Python
  buffer without it), `selftest --nvim --require --dir $RUNNER_TEMP/pt-nvim` (the `runner`
  context is not allowed in a job-level `env`, hence the step env), logs on failure),
  `template-e2e.yml` (gate job, then 3 OS x 3 presets: `--quick` on pushes and pull requests
  that touch what `new` copies (`.pytemplate/**`, the launchers, `pyproject.toml`, `uv.lock`,
  `pytemplate.toml`, `.python-version`, `src/**`, `tests/**`, `.gitignore`, `.gitattributes`,
  the workflow), the default depth weekly (Monday cron), `--full` monthly (day-1 cron, named by
  its string in the `MODE` expression), any depth on dispatch; job timeout 120 min, 300 for
  full; Linux gets the raylib libs, `libgl1-mesa-dri` and `xvfb`; the JSON report is always
  uploaded and the logs on failure, one artifact name per matrix row;
  `test_e2e_plan.test_e2e_workflow_*` pin the depths and triggers). First run on GitHub in
  September 2026 (images ubuntu-24.04, macos-26-arm64, windows-2025-vs2026; uv 0.12, Neovim
  0.12.5).

### 13.3 Coverage limits

Developed on Windows 11: the Linux/macOS code paths (launcher branches, `.sh` launchers, xvfb,
pyz cache in `HOME`, the nvim harness) are exercised by the CI workflows. Not installed locally, CI only: zsh,
ksh, mksh, yash, fish, Cygwin, busybox-w32, WSL, macOS bash 3.2, and (macOS jobs only)
nushell. Since September 2026 template-selftest runs every Windows-only test (the deploy.cmd
and registry tests included) and nushell's on CI, and template-nvim runs Neovim 0.11.2 (it
passed once on Linux when the row was added); none of these new jobs had run on GitHub when
they were written. Untested anywhere so far:
PowerShell 6.x-7.2, a UNC current folder, uv found only in `ProgramFiles` or chocolatey, the
install prompt on Windows (POSIX `deploy` and pwsh
`deploy.ps1` answer it on a pseudo-terminal), Neovim 0.11 on Windows, pyright via Mason, VS
Code itself (buttons, Problems panel: only simulated), `flet build` outside Windows (verified by
hand there, section 10; no CI job installs Flutter), bundled PyPy portable builds on CI, Ctrl+C
handling of the nvim harness (its tree kill only simulated; `selftest --e2e`'s runs in
`test_e2e_run.py` on POSIX), `[deploy.nuitka]` lto/pgo outside Linux (measured with Nuitka
4.2.2 and gcc 13 only: PGO with MSVC and an ~800-module LTO link are unmeasured). The launcher
changes of
September 2026 were developed on Linux (pwsh 7.6 for `deploy.ps1`; niubash simulated by
sourcing `deploy` in bash, dash, busybox, ksh, mksh and yash): their Windows paths (Windows PowerShell 5.1, `deploy.cmd`, real niubash and MSYS2) run
only with `./deploy selftest` and `selftest --shells` on Windows (template-selftest and
template-launchers; real niubash only on the maintainer's machine).

### 13.4 Bug density (rule 1.10)

- Lines = non-blank lines that are not only a comment or a docstring, of the product code:
  `.pytemplate/runner/**/*.py`, `.pytemplate/deploy.py`, `.pytemplate/tools/*.py`, `deploy`,
  `deploy.cmd`, `deploy.ps1`, `.pytemplate/nvim/**/*.lua` (not its `tests/`),
  `.pytemplate/templates/**`, the preset skeletons and tools (`.py`, `.pyi`, `.toml`; not
  `typings/`) and `.github/workflows/template-*.yml`. Tests (`.pytemplate/tests/`,
  `.pytemplate/nvim/tests/`) are not in the denominator; a test defect counts as a bug only when
  it breaks `./deploy selftest` for a user or hides a product bug.
- Measure with a bug hunt on a fixed commit: every finding reproduced and confirmed by an
  independent verifier, duplicates merged, dependency defects moved to section 15.1. A hunt
  finds only part of the bugs: the reported density is the confirmed count (a lower bound);
  to estimate the total, run two independent hunts on the same commit and use capture-recapture
  (total ~ found by A x found by B / found by both).
- Measurements:
  - 2026-09-25, commit fc131b9, 10,837 lines: 172 confirmed bug findings (17 high, 78 medium,
    77 low; a few are duplicates of each other) and 44 stability defects (breakage that comes
    with time: moving versions, expiring schedules). That hunt's severities are not the three
    of rule 1.10 (some "low" findings stopped every command, e.g. a non-UTF-8 byte in
    pytemplate.toml), so the counted density lies between 1 per 91 lines (high and medium
    bugs and stability defects only) and 1 per 50 (all of them): UNRELIABLE either way. The
    September 2026 overhaul fixed or deliberately closed every one of the 172 bugs (wave 1)
    and took on the CI stability defects (wave 2); the code grew to 14,588 lines.
  - 2026-09-26, commit 87e28f9 (after the overhaul), 15,495 lines: two independent teams of 10
    hunters (one per area, the same brief, results never shared) reported 88 and 94 findings;
    one verifier per area reproduced or traced each, merged duplicates and matched the teams
    (4 rejected, 128 unique defects after merging across areas), and a skeptic per area tried
    to refute every critical and serious one and a third of the notable ones (30 of 32 upheld,
    2 downgraded to minor). Counted: 66 (9 critical, 1 serious, 56 notable; one of them a
    stability defect), plus 12 that the first CI runs on macOS and Windows found in the same
    commit and no hunter did (a false `doctor` error with Xcode.app, 11 test defects that broke
    `./deploy selftest` there): 78 confirmed, 1 per 199 lines. Capture-recapture on the hunt:
    A found 41 of the counted, B 47, both 22: about 87 (Chapman), about 99 with the CI's 12:
    1 per 157 lines. UNACCEPTABLE (worse than 1 per 500, better than 1 per 100). Both teams are
    the same model, so their finds are correlated and the estimate is likely low. The 66
    counted and 62 minor defects are fixed next; a new measurement follows.

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
  read `pyproject.toml`, `uv.lock` and the preset files (`preset.toml`, `constraints.txt`) as
  `utf-8-sig`, like uv (`presets._read_text`, `presets.load`); parse Python sources as bytes
  (`imports.parse`).
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
3. Generate its `constraints.txt` (section 11: the name check and `new` from other projects need
   its whole tested tree) and `git add` everything (`new` only copies tracked files).
4. It must pass `./deploy selftest` (`test_presets.py`: ruff-clean skeleton, valid config,
   pins), `./deploy selftest --e2e <p>` and `./deploy selftest --nvim <p>`.

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

### 15.1 Upstream defects we work around

A dependency's defect that our code works around is not our bug (rule 1.10) only while it is
listed here. One entry per workaround, grouped by dependency: what (DEFECT: it should work and
does not; LIMITATION: documented or by-design behaviour we must live with), the symptom without
the workaround, `Up:` the upstream issue (every DEFECT has one or "none found" after a search; a
LIMITATION only when an issue discusses it; "cf." = related, not the same), `Fix:` our
workaround by symbol and the section that explains it, `Test:` what covers it, `Goes:` when it
can be removed. Versions as observed in September 2026 unless an entry says otherwise: uv
0.12.19 (floor `envs.MIN_UV`), CPython 3.14.7 (the runner: 3.11+), PyPy 3.11.15, mypy/mypyc
2.3.1, setuptools 84.0.0, PyInstaller 6.22.3, Nuitka 4.2.2, Flet 1.0.1, raylib 6.0.1.0, cffi
2.1.1, ruff 0.16.9, UPX 5.2.1, Neovim 0.12.5 with the pinned LazyVim (`nvimtest.LOCK`),
PowerShell 7.6 and 5.1, niubash 1.1.4. A new workaround gets its entry in the same commit; when
an entry's `Goes:` comes true, the workaround and the entry go together.

uv:
- **`UV_PROJECT_ENVIRONMENT` needs `UV_PYTHON`** (LIMITATION): with only one of them uv silently
  recreates the environment with another interpreter. Fix: `envs.env_vars` sets both, always
  (7). Test:
  `test_envs_core.py::test_env_vars_pin_the_environment_and_the_interpreter_together`. Goes:
  never.
- **The user's uv variables move the runner's calls** (LIMITATION): `UV_PROJECT`,
  `UV_NO_PROJECT`, `UV_WORKING_DIR`, `UV_ISOLATED`, `UV_NO_DEV`, `UV_NO_DEFAULT_GROUPS`,
  `UV_NO_SYNC` change the project, environment or groups of `uv run --locked`;
  `UV_MANAGED_PYTHON`/`UV_NO_MANAGED_PYTHON` next to `UV_PYTHON_PREFERENCE` exit 2. Fix:
  `proc.base_env` drops `proc.UV_SELECTION` (5.5). Test:
  `test_cli_core.py::test_base_env_drops_what_would_move_uv_or_python`,
  `test_uv_runs_in_the_projects_environment_whatever_the_user_exported`. Goes: never.
- **`uv run --script` exports its throwaway environment** (LIMITATION): `VIRTUAL_ENV`, its
  `bin/`/`Scripts/` first on PATH and `UV` reach every child, which then took the runner's
  environment for the project's or skipped the launchers' own uv search. Fix: `proc.base_env`,
  `shells.child_env`, `e2e.scrub_env`, `nvimtest.runner_env` (5.5). Test:
  `test_cli_core.py::test_base_env_removes_the_runners_own_bin_folder`,
  `test_shells.py::test_child_env_drops_what_uv_run_added`, `test_e2e_plan.py::test_scrub_env`.
  Goes: never.
- **A caller's `UV_PYTHON` picks the runner's interpreter** (LIMITATION): `uv run --script`
  honours it, and a Python older than 3.11 crashed on `tomllib`. Fix: the launchers remove it,
  the Neovim plugin and the nu snippet empty it (uv reads "" as unset), `.pytemplate/deploy.py`
  exits 3 below 3.11 (4.1, 5.2). Test:
  `test_launcher_sh.py::test_launcher_clears_the_callers_uv_python`,
  `test_user_uv_python_older_than_3_11`, `test_entry_refuses_python_older_than_3_11`,
  `test_launcher_win.py::test_ps1_clears_the_callers_uv_python_and_restores_it`. Goes: never.
- **The project is found from the cwd** (LIMITATION): a work folder with its own
  `pyproject.toml` (the `flet build` stage) became the project ("Unable to find lockfile"). Fix:
  `envs.uv_run` adds `--project <ROOT>` (7). Test:
  `test_fixes.py::test_uv_run_pins_the_project_outside_the_root`. Goes: never.
- **`uv sync` is exact for the groups it installs** (LIMITATION): it removed a group added with
  `./deploy add --group G`. Fix: `envs.sync` passes `--all-groups` (7). Test:
  `test_envs_core.py::test_sync_installs_every_dependency_group`. Goes: never.
- **An old uv knows only the interpreters of its release** (LIMITATION): < 0.10.12 cannot
  download `pypy@3.11.15`, 0.8.x installs CPython 3.14.0rc2 without a word, < 0.6.15 has no `uv
  export --format requirements.txt`. Fix: `envs.MIN_UV`, `envs.require_min_uv`,
  `required-version` in `render.managed_block`, doctor (7). Test:
  `test_envs_core.py::test_min_uv_matches_the_pinned_interpreters`,
  `test_an_old_uv_is_refused_before_it_creates_an_environment`, `test_doctor_flags_an_old_uv`.
  Goes: never (raise it with the pins).
- **The lock resolves for every future Python** (LIMITATION): uv resolved for 3.15+, where
  raylib has no wheels. Fix: `environments` bounded to the CPython and PyPy minors in
  `render.managed_block` (7). Test: `test_runner.py::test_managed_block_bounds_cpython_minor`,
  `test_managed_block_with_pypy`. Goes: never.
- **uv's TOML edits re-attach comments** (LIMITATION, toml_edit): an end marker on a line of its
  own could be detached from the managed block by `uv add`/`remove`. Fix: the end marker is an
  inline comment on the block's last key (`render.managed_block`, 6.3). Test:
  `test_runner.py::test_managed_block_bounds_cpython_minor`. Goes: never.
- **`uv add` resolves each change alone** (LIMITATION): `flet-cli==V` pins `flet==V`, so adding
  the new flet to one group had no solution. Fix: `uv add`/`remove --frozen`, then one `uv lock`
  (`cmd_apply.apply`; `presets.init` first removes with `--frozen` the old preset's and every pin
  the new one adds in another form, `presets._dropped`; 5.8, 11). Test:
  `test_apply.py::test_uv_frozen_edits_only_pyproject`, `test_apply_flet_version_change`,
  `test_presets.py::test_init_from_a_flet_project_of_another_version_removes_its_pins_first`.
  Goes: never.
- **uv resolves a dependency of a dependency named like the project to the project itself**
  (DEFECT): a direct self-dependency is refused ("self-dependencies are not permitted"), but in a
  project named `mdurl` markdown-it-py's `mdurl~=0.1` was satisfied by the project (0.1.0):
  uv.lock recorded `mdurl` with `source = { virtual = "." }`, and the real library was missing
  from the lock, `.venv` and every build (rich's Markdown failed), without an error. Up: none
  found. Fix: `presets.check_name_free` refuses every name of the preset's tested tree
  (`constraints.txt`) and of uv.lock; `init` refuses a resolved uv.lock in which a package
  depends on the project (`presets._self_dependents`) and rolls back (11). Test:
  `test_presets.py::test_new_from_a_project_without_the_presets_tree_refuses_its_names`,
  `test_init_refuses_a_lock_that_resolves_a_dependency_to_the_project`, `test_self_dependents`.
  Goes: when uv refuses it (the name check stays: src/<pkg>/ would shadow the library).
- **uv writes normalized names** (LIMITATION, PEP 503): `raylib_sdl` became `raylib-sdl`, and a
  verbatim comparison never matched. Fix: `cmd_apply.req_key` (5.8). Test:
  `test_apply.py::test_req_key_normalizes_like_uv`. Goes: never.
- **`uv pip install --target` leaves build-machine files** (DEFECT for the `.lock`, LIMITATION
  for the rest): a `.lock`, `_virtualenv*` and console-script wrappers whose shebang or `.exe`
  trampoline names this machine's `.venv` shipped in pyz and portable builds. Up: cf.
  astral-sh/uv#11878 (the `.lock` uv left in a venv; 0.12.19 still leaves an empty one in a
  `--target` folder). Fix: `common.drop_install_junk` (10). Test:
  `test_build_methods.py::test_install_deps_removes_uv_junk_but_keeps_native_tools`. Goes: the
  `.lock` part when uv removes it; the rest never.
- **Wheels for this machine follow this machine** (LIMITATION): uv took the newest tags the
  build machine allows (manylinux_2_34 on Ubuntu 24.04: the pyz failed on Debian 11), its macOS
  default may move with a uv release, and an sdist built for another OS gives host binaries.
  Fix: `common.host_floor`, `common.UV_PLATFORMS`, `common.MACOS_FLOOR` through
  `MACOSX_DEPLOYMENT_TARGET`, `--only-binary :all:` for other targets (`common.install_deps`,
  10). Test: `test_build_methods.py::test_host_linux_target_gets_the_platform_floor`,
  `test_host_floor_falls_back_to_the_host_wheels`,
  `test_macos_targets_pin_the_deployment_target`. Goes: never.
- **PyPy wheels need a real PyPy** (LIMITATION): uv installs them for no other interpreter or
  machine. Fix: `common.check_key` takes a `pp` key only for the pypy build's own host;
  `pyz-merge` joins builds (10). Test:
  `test_build_methods.py::test_target_keys_the_lock_cannot_serve_are_refused`. Goes: never.
- **`uv build` builds outside the project environment** (LIMITATION): isolated, it resolved
  setuptools and mypy from PyPI at every build (floating, online), and mypycify there could not
  see the project's dependencies; without isolation it takes `./.venv` whatever
  `UV_PROJECT_ENVIRONMENT` says (wrong under WSL). Fix: `methods.wheel.build` runs `uv build
  --no-build-isolation --python <.venv python>` after `envs.sync` (10). Test:
  `test_mypyc_core.py::test_wheel_builds_in_the_locked_tools_env`. Goes: never.
- **`uv run --with` and `uvx` float** (LIMITATION): an unpinned tool re-resolves to the newest
  release whenever uv's index cache expires (basedpyright's Node.js runtime too). Fix:
  `cmd_dev.BASEDPYRIGHT`, `cmd_dev.BASEDPYRIGHT_NODE`, `methods.nuitka.NUITKA`, and editor.json
  `typing.basedpyright` for the plugin's uvx server (7, 12.2). Test:
  `test_cli_core.py::test_tools_are_pinned_exactly`, `test_basedpyright_runs_with_every_pin`.
  Goes: never (bump the pins deliberately).
- **uv's caches follow `XDG_*`** (LIMITATION): the isolated Neovim tree moves `XDG_CACHE_HOME`
  and `XDG_DATA_HOME`, and uv then started from empty caches. Fix: `nvimtest.nvim_env` keeps
  `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_DIR` as uv resolved them
  (`nvimtest.uv_dirs`, 13.1). Test: `test_cmd_nvim.py::test_env_isolation`. Goes: never.
- **uv 0.10.12 (the floor) keeps redundant markers in uv.lock after a preset round trip**
  (LIMITATION: an older lock writer; the lock resolves the same, and 0.12.19 writes the old
  bytes): only a byte comparison notices. Fix: the `uv-floor` job of `template-selftest.yml`
  deselects `test_presets.py::test_init_round_trip_through_every_preset_is_byte_identical`
  (13.2). Test: `test_workflows.py::test_what_the_selftest_workflow_reads_by_text_exists`.
  Goes: when `envs.MIN_UV` reaches a uv that writes the same bytes.

CPython and its standard library:
- **`subprocess.run` kills the child 0.25 s after Ctrl+C** (LIMITATION, 3.7+): an app's cleanup
  was cut short; under `uv run` the app kept running as an orphan. Up: python/cpython#70130
  (bpo-25942, where the grace period came from). Fix: `proc._wait_through_ctrl_c` (5.3). Test:
  `test_cli_core.py::test_ctrl_c_waits_for_a_child_that_cleans_up` and the other
  `test_ctrl_c_*`. Goes: never.
- **`Path.rglob` does not enter symlinked folders** (LIMITATION; `recurse_symlinks` from 3.13):
  a linked `src/assets` reached the mypyc stage empty. Up: python/cpython#77609. Fix:
  `mypyc._walk` (9). Test: `test_mypyc_core.py::test_sync_tree_follows_symlinked_dirs`,
  `test_compiled_sources_follow_a_symlinked_subpackage`. Goes: maybe once the runner needs 3.13
  (the cycle check stays ours).
- **ZIP stores no date before 1980, and `zipapp` cannot relax it** (LIMITATION): a payload file
  from the Nix store (mtime 1) crashed the pyz and the portable zip with ValueError. Up: cf.
  python/cpython#78278 (zipfile's `strict_timestamps`); none found for `zipapp`. Fix:
  `pyz._write_archive` and `portable.make_archive` with `strict_timestamps=False` (10). Test:
  `test_build_methods.py::test_pyz_accepts_payload_files_older_than_1980`. Goes: never.
- **ZIP times are 2-second local DOS times** (LIMITATION): timestamp `.pyc` files went stale
  after the portable zip, so a read-only install recompiled at every start. Fix:
  `portable.compile_calls` with `--invalidation-mode checked-hash` (10). Test:
  `test_build_methods.py::test_portable_pycs_survive_a_zip_round_trip`. Goes: never.
- **A zip written on Windows holds MS-DOS entries** (LIMITATION): `os.stat` reports 0o666 for a
  `.sh`, and unzip ignores DOS mode bits, so the launcher lost its x bit. Fix:
  `portable.make_archive` writes `.sh` entries with `create_system = 3` and mode 0755 (10).
  Test: `test_build_methods.py::test_portable_zip_keeps_the_sh_launcher_executable`. Goes:
  never.
- **`-I`/`-E` ignore `PYTHONUTF8` and `PYTHON*`** (LIMITATION): an isolated launcher dropped
  UTF-8 mode and `deploy.portable.env`. Fix: the portable and pyz launchers use `-s` only
  (`portable.write_launchers`, 10). Test:
  `test_build_methods.py::test_portable_sh_launcher_exit_code_and_environment`. Goes: never.
- **`open()` defaults to the ANSI code page on Windows** (LIMITATION until 3.15, PEP 686): mypy
  and mypyc read sources as cp1252, and an app behaved differently under F5. Fix: `PYTHONUTF8=1`
  in `proc.base_env`, the portable and pyz launchers, `vscode.DEBUG_ENV` and the Neovim mypy
  linter; PyInstaller `--python-option "X utf8"`; `.pytemplate/deploy.py` reconfigures its
  streams (5.5). Test: `test_cli_core.py::test_base_env_drops_what_would_move_uv_or_python`,
  `test_vscode.py::test_launch_configs_run_in_utf8_mode`,
  `test_build_methods.py::test_exe_default_argv_and_output`. Goes: when every supported Python
  is 3.15 or newer.
- **A closed stdout fails at exit** (LIMITATION): `./deploy help | head -1` printed a
  BrokenPipeError traceback. Fix: `cli._output_closed` (quiet exit 141; POSIX only, 15.2). Test:
  `test_cli_core.py::test_a_closed_stdout_is_not_a_runner_bug`. Goes: never.
- **`sys.stdlib_module_names` knows only the running version** (LIMITATION): a name that is a
  module of PyPy 3.11 or of another CPython passed the check. Fix:
  `presets.STDLIB_OTHER_VERSIONS` in `presets.shadows_stdlib` (5.7). Test:
  `test_rename.py::test_stdlib_names_do_not_depend_on_the_runner`. Goes: never (update it per
  Python release).
- **`shutil.rmtree` error hooks** (LIMITATION): `onerror` is deprecated from 3.12 and `onexc`
  does not exist in 3.11, and a read-only file (git objects on Windows) needs a chmod and a
  retry. Fix: the version switch in `cmd_nvim.remove_tree`, `presets._remove`, `e2e.rmtree`;
  `cmd_env._remove` retries after `cmd_env._make_writable` (7). Test:
  `test_cmd_nvim.py::test_remove_tree_read_only`,
  `test_presets.py::test_remove_deletes_read_only_entries`,
  `test_envs_core.py::test_clean_retries_read_only_contents`. Goes: the switch once the runner
  needs 3.12; the retry never.
- **`Path.is_symlink()` is False for a Windows junction** (LIMITATION; `Path.is_junction` from
  3.12): `clean --envs` would have deleted what a junctioned `.venv` points to. Fix:
  `cmd_env._is_link` reads `st_reparse_tag` (7). Test:
  `test_workarounds.py::test_a_windows_junction_counts_as_a_link`. Goes: once the runner needs
  3.12.
- **The tokenizer changed with PEP 701** (LIMITATION): an f-string is one STRING token on 3.11
  and many tokens from 3.12 (t-strings from 3.14). Fix: `rename` handles both (5.7). Test:
  `test_rename.py::test_fstring_fields_as_one_token`, `test_tokenizer_canary`. Goes: the 3.11
  form once the runner needs 3.12.
- **No extension module loads from a zip** (LIMITATION, zipimport): Fix: the pyz bootstrap
  (`templates/pyz/__main__.py`) extracts to a per-build cache (10). Test:
  `test_build_methods.py::test_pyz_bootstrap_picks_the_flavour` and the other bootstrap tests.
  Goes: never.
- **`Path.home()` raises for a UID without a passwd entry** (LIMITATION): the pyz crashed in a
  container with a random UID. Fix: the pyz bootstrap falls back to a `tempfile.mkdtemp` folder
  (10). Test: `test_build_methods.py::test_pyz_runs_without_a_usable_cache`. Goes: never.
- **A missing cwd is blamed on the program** (LIMITATION): subprocess raised FileNotFoundError
  naming the program (POSIX) or NotADirectoryError (Windows). Fix: `proc.run` checks the cwd
  first (5.3). Test: `test_cli_core.py::test_a_bad_working_folder_is_named`. Goes: never.

PyPy:
- **PyPy 8.0 changed the extension ABI to pp80** (LIMITATION): a loose request picked the newest
  PyPy, for which raylib, numpy and cffi had no wheels. Fix: `python.pypy` must be exact
  (`config.validate`, 6.1). Test: `test_config_rules.py::test_python_pypy_must_be_exact`,
  `test_pypy_pins_match_the_default`. Goes: never (move the pin once the wheels exist).
- **PyPy has cffi built in** (LIMITATION): uv tried to build cffi from its sdist for PyPy. Fix:
  `override-dependencies` in `render.managed_block` (7). Test:
  `test_runner.py::test_managed_block_with_pypy`. Goes: never.
- **No PyPy wheels for the dev tools** (LIMITATION): mypy (its Rust `ast-serialize`),
  PyInstaller, ruff, setuptools, debugpy. Fix: `implementation_name == 'cpython'` on them in the
  dev group of `pyproject.toml`; `./deploy add --cpython-only` (`cmd_env.cmd_add`, 7). Test:
  `test_workarounds.py::test_cpython_only_dev_tools_carry_the_marker`,
  `test_envs_core.py::test_add_remove_argv`. Goes: per tool, when it publishes PyPy wheels.
- **The PyPy Windows zip lacks the VC++ runtime** (LIMITATION, pypy.org/download.html: "you
  might need the VC runtime library installer"): a bundled PyPy portable build did not start on
  a machine without the redistributable. Fix: `portable.copy_runtime` copies `vcruntime140*.dll`
  from the CPython base (10). Test:
  `test_workarounds.py::test_portable_pypy_on_windows_gets_the_vc_runtime`. Goes: when PyPy's
  zip ships them.
- **PyPy ships lib2to3's broken test data** (LIMITATION): compileall fails on it. Fix:
  `portable.COMPILE_EXCLUDE` (`-x`, 10). Test:
  `test_build_methods.py::test_portable_precompiles_the_stdlib_at_the_launcher_level`. Goes:
  when PyPy drops lib2to3.
- **There is no `pypy3w.exe` or `python3w.exe`** (LIMITATION): Fix: `common.windowed` names
  `pypyw`, `pythonw`, `pyw` (10). Test: `test_build_methods.py::test_windowed_twins_exist`.
  Goes: never.

Python on the user's machine (the `runtime = "system"` launchers and the pyz wrapper):
- **`py` can exist with no Python registered, and `python3.exe` can be the Microsoft Store
  alias** (LIMITATION): a system launcher started the alias or failed silently. Fix: the system
  portable `.cmd`/`.sh` and the pyz `.cmd` run each candidate with a minimum-version probe
  (`portable.cmd_launcher`, `pyz._wrapper_cmd`, 10); `nvim doctor` flags the alias. Test:
  `test_fixes.py::test_system_launchers_probe_the_minimum_version`,
  `test_portable_system_cmd_launcher_fails_cleanly` (Windows). Goes: never.

mypy and mypyc:
- **Module-level `__file__` is relative in a lone top-level compiled module** (DEFECT): mypyc
  sets `__file__` from the folder of its shared lib, and one top-level module gets none, so its
  body sees `<mod><EXT_SUFFIX>` relative to the cwd. Up: mypyc/mypyc#700 (open). Fix: the
  `lintc` rule (`lintc.relative_file_at_import`, 9). Test:
  `test_mypyc_core.py::test_real_compile_single_top_level_module_sees_a_relative_file` (a pin:
  it fails once mypyc fixes it),
  `test_lintc_flags_module_level_file_for_a_single_top_level_module`. Goes: when that pin fails.
- **What mypyc compiles badly or not at all** (LIMITATION): a class decorator outside its native
  list makes a slow Python class; nested classes, classes in functions, t-strings and a
  module-level `if __name__ == "__main__"` are unsupported. Fix: the `lintc` rules,
  `lintc.NATIVE_CLASS_DECORATORS` (9). Test:
  `test_mypyc_core.py::test_lintc_native_decorators_follow_the_locked_mypyc`,
  `test_lintc_flags_t_strings`, `test_runner.py::test_lintc_rules`. Goes: per rule, when mypyc
  supports it.
- **`librt` comes only with mypy** (LIMITATION): compiled code that imports mypyc's runtime
  library lacked it in pyz, portable and wheel builds (dev group only), and PyPy has none. Fix:
  the `librt` rule of `lintc` (9). Test:
  `test_mypyc_core.py::test_lintc_librt_needs_a_runtime_dependency`,
  `test_cli_core.py::test_librt_is_forbidden_only_while_pypy_is_supported`. Goes: never.
- **Flet in compiled code** (LIMITATION, Flet 1.0.1): `async` handlers get no event, generator
  handlers never run, `@ft.component` fails at import, `@ft.control` loses its event types. Fix:
  the flet preset's `compile.forbid_imports` (flet, flet_desktop, flet_cli): the UI stays
  interpreted (11). Test: `test_runner.py::test_lintc_rules`,
  `test_mypyc_core.py::test_lintc_default_presets_are_clean`. Goes: when mypyc and Flet support
  them.
- **Compiled code holds the GIL** (LIMITATION): a heavy compiled call froze the Flet UI from a
  thread. Fix: the flet skeleton runs it in a `ProcessPoolExecutor` (`ui/app.py`, 11). Test:
  `test_workarounds.py::test_flet_skeleton_runs_compiled_work_in_a_process`. Goes: never.
- **The mypyc CLI cannot set `strip_asserts`, `group_name` or `multi_file`** (LIMITATION) and
  always writes to `./build`. Fix: `tools/mypyc_build.py` calls `mypycify` (9). Test:
  `test_mypyc_core.py::test_build_spec_matches_what_the_tool_reads`,
  `test_real_compile_roundtrip`. Goes: when the CLI takes them.
- **`mypycify` hands every extension the same `extra_compile_args` list** (LIMITATION): flags
  appended in place reached every extension once per extension. Fix: `tools/mypyc_build.py`
  gives each a new list (9). Test:
  `test_mypyc_core.py::test_build_script_adds_the_flags_to_every_extension_once`. Goes: never.
- **PyInstaller and Nuitka cannot see imports inside a `.pyd/.so`** (LIMITATION): a compiled exe
  crashed at startup on a missing module (`from html import parser` too). Fix:
  `mypyc.hidden_imports` with `mypyc.importable` (9). Test:
  `test_mypyc_core.py::test_hidden_imports_resolve_what_the_binaries_import`,
  `test_build_methods.py::test_exe_mypyc_hidden_imports_icon_and_extra_args_order`. Goes: never.
- **mypy config sections** (LIMITATION): `RawConfigParser` refuses a repeated section, a pattern
  repeated in a comma list replaces the earlier options, and `strict` in an override applies to
  every module. Fix: `render.mypy_ini` writes one section per pattern; `config._check_override`
  refuses `strict` (6.1, 8). Test:
  `test_mypyc_core.py::test_mypy_ini_merges_overrides_with_the_generated_sections`,
  `test_config_rules.py::test_mypy_overrides_invalid`. Goes: never.

setuptools:
- **An extension is rebuilt only when a source is newer** (LIMITATION): a new `opt_level`, C
  flag or compiler variable kept the old binary (and shipped it). Fix: `mypyc.COMPILED_STAMP`
  and `build_ext --force` (9). Test:
  `test_mypyc_core.py::test_build_forces_a_rebuild_for_every_binary_option`,
  `test_build_forces_a_rebuild_when_the_compiler_environment_changes`. Goes: never.
- **A `CFLAGS` environment variable replaces Python's own C flags** (LIMITATION): it drops
  `-fno-strict-overflow` (i64/i32 wrap-around becomes undefined behaviour) and `-DNDEBUG`. Fix:
  `tools/mypyc_build.py` `extra_cflags` always adds `-fno-strict-overflow`; the wheel's
  `methods.wheel.SETUP_PY` mirrors it (9). Test: `test_mypyc_core.py::test_extra_cflags`,
  `test_real_compile_adds_the_c_flags_and_inlines_compiled_calls`. Goes: never.

MSVC and Visual Studio:
- **VS 2026's `vcvarsall.bat` runs `vswhere.exe` by its bare name** (DEFECT): outside a
  developer prompt setuptools failed with "Unable to find a compatible Visual Studio
  installation". Up: none found. Fix: `proc.base_env` appends `proc.vs_installer_dir()` to PATH
  on Windows (5.5). Test:
  `test_workarounds.py::test_base_env_puts_the_vs_installer_on_path_on_windows`. Goes: when
  vcvarsall finds vswhere itself.
- **MSVC speaks the system language** (LIMITATION): its messages reached the terminal as
  unreadable cp1252 text. Fix: `VSLANG=1033` in `mypyc.build` and `methods.wheel.build` (5.5).
  Test: `test_workarounds.py::test_mypyc_build_asks_msvc_for_english_messages`,
  `test_mypyc_core.py::test_wheel_builds_in_the_locked_tools_env`. Goes: never.
- **uv installs x86_64 CPython even on Windows on ARM** (LIMITATION): the host's ARM64 tools are
  the wrong ones for it. Fix: `cmd_env._msvc` and `mypyc.has_compiler_hint` take the `.venv`
  Python's `sysconfig.get_platform()`, as setuptools' vswhere query does (7). Test:
  `test_envs_core.py::test_msvc_component_follows_the_venv_platform`,
  `test_msvc_component_matches_setuptools`,
  `test_mypyc_core.py::test_compiler_hint_names_the_msvc_tools_of_the_venv_platform`. Goes:
  never.

PyInstaller:
- **UPX only on Windows** (LIMITATION): `configure.get_config` turns UPX off elsewhere (packed
  `.so` files crash). Fix: `exe.size_args` passes `--noupx`, downloads nothing and warns (10).
  Test: `test_build_methods.py::test_exe_size_args_skip_upx_off_windows`. Goes: never.
- **Its UPX step takes no level** (LIMITATION): PyInstaller always adds `--lzma`, and its binary
  cache reused another level's output. Fix: the level in the `UPX` variable (`upx.env_value`),
  `--clean` (10). Test: `test_upx.py::test_level_flags_and_pyinstaller_env`,
  `test_build_methods.py::test_exe_size_args_use_upx_on_windows`. Goes: never.
- **No PyPy** (LIMITATION, PyInstaller and Nuitka): Fix: `cmd_build.COMPAT` refuses exe, nuitka
  and flet with pypy; portable is PyPy's standalone route (10). Test:
  `test_build_methods.py::test_build_refuses_methods_without_pypy`,
  `test_config_rules.py::test_deploy_default_follows_cmd_build_compat`. Goes: never.
- **What any import reaches is bundled** (LIMITATION, Nuitka too): flet's lazy `from PIL import`
  bundled Pillow (13 MB), and cffi's build-time imports bundled setuptools and pycparser into
  raylib games. Fix: the flet preset's `deploy.exclude_modules = ["PIL"]`, the raylib preset's
  `[deploy.exe] extra_args` (10, 11). Test:
  `test_workarounds.py::test_presets_exclude_what_the_packagers_drag_in`. Goes: never.
- **A frozen app's worker processes start the executable again** (LIMITATION, multiprocessing):
  without `freeze_support()` a `ProcessPoolExecutor` child ran the whole app once more. Fix:
  every preset's `src/main.py` calls `multiprocessing.freeze_support()` first (11). Test:
  `test_workarounds.py::test_every_preset_main_calls_freeze_support_first`. Goes: never.

Nuitka:
- **FATAL on a module it cannot locate** (LIMITATION): a platform-guarded `import winreg` in
  compiled code stopped the build. Fix: `nuitka.includable` (10). Test:
  `test_build_methods.py::test_nuitka_includable_drops_what_the_build_env_cannot_locate`. Goes:
  never.
- **A standalone binary named like the package folder** (DEFECT): `main.dist/<name>` is a FILE
  where the mypyc package folder `<pkg>/` must go: NotADirectoryError after minutes of work. Up:
  none found (cf. Nuitka/Nuitka#2483, a data file named like the binary). Fix: `<name>.bin` on
  POSIX standalone builds when `app.name.lower() == pkg` (`nuitka.build`, 10). Test:
  `test_build_methods.py::test_nuitka_standalone_binary_never_clashes_with_the_package`. Goes:
  when Nuitka reports or avoids the clash.
- **Python support lags** (LIMITATION): 4.2.2 stops with FATAL on 3.15 and only warns on later
  minors, then fails obscurely in the C compile. Fix: `nuitka.NUITKA_PYTHON`,
  `nuitka.check_python` (10). Test:
  `test_build_methods.py::test_nuitka_python_newer_than_the_pin_is_refused_before_any_work`,
  `test_nuitka_failure_names_the_pin`. Goes: never (bump with the pin).
- **PGO is experimental** (LIMITATION): its profiling run starts before `main.dist` holds the
  mypyc extensions (ImportError, yet success), before the data files are in place, and waits for
  a GUI window to close; macOS has no clang profdata step. Fix: `nuitka.check_options`,
  `config._check_nuitka` (6.1, 10). Test:
  `test_build_methods.py::test_nuitka_pgo_refuses_mypyc_and_macos_before_any_work`,
  `test_nuitka_options_config_rules`. Goes: per case, when Nuitka's PGO handles it.

Flet (flet, flet-desktop, flet pack, flet build):
- **The desktop client must be bundled and match** (LIMITATION): the flet-desktop wheel has no
  client (a plain PyInstaller or Nuitka build downloads ~40 MB at first start), and a
  flet-desktop of another version is pip-installed at runtime, bypassing uv.lock. Fix: one
  `[preset.flet] version` for flet, flet-desktop and flet-cli (5.8); exe through `flet pack`
  (`exe._flet_pack`); `nuitka._flet_client_archive` bundles flet_desktop's own release archive
  at `flet_desktop/app/` (10). Test:
  `test_apply.py::test_flet_version_change_replaces_the_three_pins`,
  `test_workarounds.py::test_nuitka_bundles_the_flet_client`. Goes: never.
- **flet loads its controls lazily** (LIMITATION): module `__getattr__` + `importlib`, which
  Nuitka cannot follow. Fix: `--include-package=flet --include-package=flet_desktop` in
  `nuitka.build` (10). Test: `test_workarounds.py::test_nuitka_bundles_the_flet_client`. Goes:
  never.
- **`flet pack` options** (LIMITATION): `-y` wipes `<cwd>/build` and the distpath, `--onedir` is
  refused on macOS (always a `.app`), it adds `--noconsole` unless `--debug-console` has a
  value, and each `--pyinstaller-build-args` value is one argument. Fix: `exe._flet_pack` runs
  in `.build/flet-pack/<b>`, never passes `--onedir` on macOS, maps `deploy.exe.console` to
  `--debug-console=true`, passes `--python-option=X utf8` as one item (10). Test:
  `test_build_methods.py::test_flet_pack_macos_is_never_onedir`,
  `test_flet_pack_console_and_utf8` (every `_flet_pack` test checks the cwd). Goes: never.
- **PyInstaller's flat onedir puts the executable where the package folder goes** (LIMITATION):
  with `--contents-directory=.` on Linux, `dist/<n>/<n>` is a FILE where the folder `<pkg>/`
  must go when `app.name == pkg` (the default). Fix: flat only on Windows (`exe._flet_pack`,
  10). Test: `test_build_methods.py::test_flet_pack_onedir_linux_keeps_internal`,
  `test_flet_pack_onedir_windows_is_flat`. Goes: never.
- **`flet build` ignores uv.lock and bundles the highest Python its manifest matches**
  (LIMITATION): `>=3.13` gave 3.14, and the cp313 mypyc extensions were silently not loaded.
  Fix: `methods.flet.build_pyproject` pins the exported versions and `requires-python =
  "==X.Y.*"` (10). Test: `test_build_methods.py::test_flet_build_pins_the_python_minor`,
  `test_fixes.py::test_flet_build_pyproject_takes_only_tool_flet`. Goes: never.
- **`flet build` looks for `<work>/<path>/main.py`** (LIMITATION): another `[tool.flet.app]
  path` aborted after installing Flutter. Fix: `methods.flet.build_pyproject` forces
  `methods.flet.STAGE_APP` with a warning (10). Test:
  `test_build_methods.py::test_flet_build_pyproject_points_at_the_staged_app`. Goes: never.
- **Flutter needs Developer Mode on Windows (symlinks), and mobile and web targets load no
  extension** (LIMITATION): Fix: `methods.flet._developer_mode` is checked first; mobile and web
  builds ship the `.py` (10). Test:
  `test_build_methods.py::test_flet_build_needs_developer_mode_on_windows`,
  `test_flet_build_mobile_and_web_ship_the_py_code`. Goes: never.

cffi and raylib:
- **The raylib stub does not match the runtime** (DEFECT, raylib 6.0.1.0): returns, fields and
  parameters declared `bytes`/`list` are cdata or int at runtime, and mypyc checks those types,
  so only compiled code raised TypeError; it also imports `warnings.deprecated` (3.13+). Up:
  none found. Fix: `presets/raylib/tools/raylib_stubs.py` regenerates
  `typings/raylib/__init__.pyi` (task `stubs`), picked up by `render.typings_dir`; a
  `[[typing.mypy_overrides]]` `ignore_errors` for `raylib` (11). Test:
  `test_presets.py::test_raylib_stubs_regenerates_the_committed_stub`. Goes: when the upstream
  stub matches the runtime.
- **cffi 2.x Linux wheels keep their C asserts** (DEFECT, cffi 2.1.1): reading `.fields` of an
  opaque struct aborts the process (exit 134), which no `except` catches. Up: none found. Fix:
  `raylib_stubs.py` skips a struct whose `ffi.sizeof` raises before reading its fields (11).
  Test: `test_presets.py::test_raylib_stubs_skips_opaque_structs`. Goes: when cffi raises
  instead.
- **raylib wheels** (LIMITATION): no PyPy wheel for arm64 (macOS and Linux: pp311 wheels for
  x86_64 only), and building raylib from its sdist needs the C library. Fix: `no-build-package`
  from the preset's `[uv]`; `e2e.HOST_GAPS` (macOS and Linux arm64) and `render.ci_workflow`
  (its arm64 runner, macOS) drop PyPy there (11, 15.2). Test:
  `test_render_core.py::test_ci_workflow_for_every_preset_and_backend_set`,
  `test_e2e_plan.py::test_host_gaps_switch_the_project_off_the_backend`,
  `test_e2e_plan.py::test_raylib_pypy_gap_on_linux_arm64`,
  `test_e2e_plan.py::test_host_gaps_match_the_generated_ci_matrix`,
  `test_apply.py::test_apply_raylib_package_switch`. Goes: when raylib ships those wheels.

UPX:
- **What packing breaks** (LIMITATION): UPX refuses Control Flow Guard PEs (and `--force` breaks
  them) and inputs over 768 MiB; packed C runtimes, API sets and Python DLLs gain nothing or
  break, packed `.so` files crash on Linux, and a packed `flutter_windows.dll` hangs the app at
  startup (measured with Flet 1.0.1). Fix: `upx.BUILTIN_EXCLUDE`, `upx.MAX_INPUT`,
  `upx.candidates` (Linux: ELF executables only), never `--force` (10). Test:
  `test_upx.py::test_candidates_skip_runtime_dlls_and_user_globs`,
  `test_candidates_on_posix_are_elf_executables`, `test_files_over_the_limit_are_never_packed`,
  `test_upx_messages_are_classified`. Goes: never.
- **No macOS support, no Windows arm64 release** (LIMITATION): UPX cannot pack current macOS
  binaries (and packing breaks their signature). Fix: `upx.unsupported_reason` turns UPX off on
  macOS with a warning; `upx.ASSETS` gives Windows arm64 the x64 build (emulated) (10). Test:
  `test_workarounds.py::test_upx_is_off_on_macos_and_windows_arm64_runs_the_x64_build`. Goes:
  per case, when UPX supports it.
- **The packagers look for UPX differently** (LIMITATION): PyInstaller wants `<upx-dir>/upx`,
  Nuitka a file named `upx`. Fix: `upx.find` hands them an absolute, unresolved path (10). Test:
  `test_upx.py::test_relative_upx_path_resolves_against_the_project_root`. Goes: never.

ruff:
- **`ruff format --check` prints nothing for stdin** (LIMITATION): a staged file fed on stdin
  failed without a word. Fix: `hooks._run_ruff` writes its own "Would reformat: <path> (its
  staged version)" line (5.6). Test:
  `test_hooks.py::test_partially_staged_python_file_is_checked_as_staged`,
  `test_workarounds.py::test_ruff_format_check_says_nothing_for_stdin` (a pin). Goes: when that
  pin fails.
- **ruff 0.16 changed the `format --check` output** (LIMITATION): 0.15 printed "Would reformat:
  path", 0.16 one `unformatted` diagnostic per file. Fix: `rename._UNFORMATTED` reads both for
  the rename tidy-up (5.7). Test: `test_rename.py::test_ruff_tidy_after_a_rename`. Goes: when
  the dev group requires ruff >= 0.16.

VS Code, its extensions, pyright and basedpyright:
- **Shell tasks run in the user's terminal profile** (LIMITATION): xonsh, niubash or MSYS2 as
  the default profile broke `"type": "shell"` tasks. Fix: every task is `"type": "process"`
  running `/bin/sh <root>/deploy` or `deploy.cmd` (12.1). Test:
  `test_vscode.py::test_every_task_runs_the_launcher_as_a_process`. Goes: never.
- **Per-OS `args` replace the default ones** (LIMITATION, tasks 2.0.0): Fix: both blocks carry
  the full argv (12.1). Test: `test_vscode.py::test_every_task_runs_the_launcher_as_a_process`.
  Goes: never.
- **`information` is no problem-matcher severity** (LIMITATION): VS Code maps it to Ignore, then
  Error, so basedpyright's information lines showed as errors. Up: cf. microsoft/vscode#452
  ("Note" and "Hint" shown as errors). Fix: the PYRIGHT matcher captures only `info` (12.1).
  Test: `test_vscode.py::test_matcher_samples`. Goes: never.
- **`isBackground` needs a background matcher** (LIMITATION): `flet run -r` prints no stable
  ready line, and VS Code would wait forever for such a dependency. Fix: `background = true`
  tasks become restartable RUN tasks (12). Test: `test_vscode.py::test_custom_tasks`. Goes:
  never.
- **Ctrl+C in a Windows task asks cmd's "Terminate batch job (Y/N)?"** (LIMITATION): Fix:
  run-like tasks get `runOptions` `instanceLimit 1`, `terminateOldest` (12.1). Test:
  `test_vscode.py::test_run_tasks_restart_and_check_tasks_reveal_problems`. Goes: never.
- **The debugger quotes for the terminal's shell by its name** (DEFECT): with xonsh or `niu.exe`
  as the default profile, `runInTerminal` got cmd syntax and F5 failed. Up: cf.
  microsoft/debugpy#1853 (the same with nushell). Fix: the automation profiles (cmd.exe,
  `/bin/sh`) in `templates/vscode/settings.json` (12.1). Test:
  `test_vscode.py::test_settings_and_extensions`. Goes: when the debugger quotes for the shell
  it starts.
- **basedpyright's extension writes into the Workspace settings** (LIMITATION): its conflict
  prompt stored `python.languageServer` and `python.analysis.typeCheckingMode` in the generated
  `.vscode/settings.json`, which then counted as hand-edited. Fix:
  `vscode.BASEDPYRIGHT_SETTINGS` ships the answers (12.1). Test:
  `test_vscode.py::test_basedpyright_settings_prevent_the_conflict_prompts`. Goes: never.
- **actboy168.tasks (0.16.1)** (LIMITATION): it prefixes the task's own icon to the label and
  creates the buttons in tasks.json order. Fix: bare labels, button tasks first (12.1). Test:
  `test_vscode.py::test_buttons_map_to_tasks_in_order`. Goes: never.
- **pyright's `strict` list has no exclusion** (LIMITATION): an excluded module inside a
  compiled package got the strict rules. Fix: `render._paths_without`; with basedpyright the
  excluded paths come first in `executionEnvironments` (8). Test:
  `test_mypyc_core.py::test_pyright_config_leaves_compile_exclude_out_of_the_compiled_rules`.
  Goes: never.

Neovim, lazy.nvim, LazyVim and the plugins the integration configures:
- **Neovim trusts a file by the sha256 of its bytes and its real path** (LIMITATION,
  `vim.secure`): any byte change (CRLF, a BOM, a mode-dependent value) untrusted `.lazy.lua`.
  Fix: `.lazy.lua` is a static copy (`editors/nvim.py`), `.gitattributes` keeps it LF, all logic
  lives in `.pytemplate/nvim/` (12.2). Test:
  `test_nvim_render.py::test_lazy_lua_is_identical_in_every_mode`,
  `test_lazy_lua_bytes_are_pinned`, `test_gitattributes_keeps_lazy_lua_lf`. Goes: never.
- **`vim.secure.trust`** (LIMITATION): the `path` form exists from 0.12 only (0.11 needs a
  buffer), and it writes its database with `io.open(<state>/trust, "w")`, which fails while the
  state folder does not exist. Fix: the trust snippet of `cmd_nvim.trust_file` creates the
  folder and picks the form (12.2). Test: `test_cmd_nvim.py::test_real_nvim_query_and_trust`.
  Goes: the buffer form once 0.12 is the minimum.
- **Headless Neovim** (LIMITATION): a Lua error still exits 0, `confirm()` never returns,
  `VeryLazy` never fires, and an unwritable `NVIM_LOG_FILE` drops `nvim.log` into the cwd. Fix:
  the `PTNVIM{json}` marker of `cmd_nvim.headless`; trust through the API first (`nvim sync`
  refuses while `.lazy.lua` is untrusted); `doautocmd UIEnter` in `nvimtest`; `NVIM_LOG_FILE`
  always set (12.2). Test: `test_cmd_nvim.py::test_parse_marker_skips_noise`,
  `test_nvim_sync_refuses_an_untrusted_lazy_lua`, `test_nvim_sync_installs_only`,
  `test_real_nvim_query_and_trust`. Goes: never.
- **lazy.nvim installs a fresh config in rounds and prunes the lock between them** (DEFECT):
  LazyVim's own plugins came at their newest commits despite the pinned lock. Up: cf.
  folke/lazy.nvim#1279 (its startup install ignores and rewrites the lock; closed as not
  planned). Fix: `nvimtest.prepare_base` installs, then runs `Lazy! restore` in a second Neovim;
  the lock is copied in before every installing run; `nvimtest.lock_drift` (13.1). Test:
  `test_cmd_nvim.py::test_prepare_base_pins_the_starter_and_restores_the_lock`,
  `test_prepare_base_fails_when_a_pin_does_not_hold`. Goes: when one install run honours the
  lock.
- **The LazyVim starter and the plugins change every day, without releases** (LIMITATION): a
  smoke test of the latest of everything turned red with no commit of ours. Fix: `selftest
  --nvim` pins the starter (`cmd_nvim.STARTER_REV`) and the plugins (`nvimtest.LOCK`), and
  template-nvim.yml pins Neovim; its weekly canary runs without the lock and uploads the next
  pins (13.1, 13.2). Test:
  `test_cmd_nvim.py::test_prepare_base_pins_the_starter_and_restores_the_lock`,
  `test_workflows.py::test_nvim_workflow_pins_neovim_and_runs_a_canary`. Goes: never.
- **`Lazy! sync` also updates and cleans** (LIMITATION): it rewrote the user's `lazy-lock.json`
  and removed plugins its spec did not name. Fix: `./deploy nvim sync` runs `Lazy! install`
  (12.2). Test: `test_cmd_nvim.py::test_nvim_sync_installs_only`. Goes: never.
- **The local spec comes after the user's** (LIMITATION): an extra imported from `.lazy.lua`
  trips LazyVim's import-order check. Fix: `.lazy.lua` sets `vim.g.lazyvim_check_order = false`
  only then; `./deploy nvim extras` (12.2). Test:
  `test_nvim_render.py::test_lua_modules_in_headless_neovim`. Goes: never.
- **LazyVim reads `vim.g.lazyvim_python_lsp` at its first import** (LIMITATION): set from
  `.lazy.lua` it came too late. Fix: `integrations.lsp` (Lua) switches
  `opts.servers.<x>.enabled` in lspconfig's `opts` (12.2). Test:
  `test_workarounds.py::test_nvim_plugin_workarounds[lsp]`. Goes: never.
- **Mason prepends its bin folder to PATH after `.lazy.lua` runs** (LIMITATION): a Mason ruff or
  mypy of another version ran. Fix: absolute `.venv` paths (`pytemplate.tool`,
  `integrations.lsp`, `integrations.mypy_linter`; 12.2). Test:
  `test_nvim_render.py::test_mypy_linter_follows_a_venv_created_later`. Goes: never.
- **nvim-lint** (LIMITATION): a linter with `env` gets that table INSTEAD of the environment,
  and on Windows every linter runs through `cmd.exe /C`, where a quoted absolute path breaks on
  spaces or `& ^ %`. Fix: `integrations.mypy_linter` passes the whole environment plus
  `PYTHONUTF8=1`, minus `VIRTUAL_ENV`, and on Windows the bare `mypy` with `.venv\Scripts` first
  on PATH (12.2). Test: `test_workarounds.py::test_nvim_plugin_workarounds[mypy env]`,
  `test_nvim_render.py::test_mypy_linter_follows_a_venv_created_later` (Windows). Goes: never.
- **nvim-dap** (LIMITATION): it spawns adapters with a raw `uv.spawn` (no PATHEXT: Mason's
  `.cmd` shim fails on Windows), waits 4 s for `initialize` (a cold adapter needs more), and
  expands `${workspaceFolder}` to Neovim's cwd. Fix: `dap.adapter` (Lua) returns an absolute
  python, `initialize_timeout_sec = 30` unless the configuration sets one, `dap.launch_configs`
  for a cwd below the root (12.2). Test:
  `test_workarounds.py::test_nvim_plugin_workarounds[dap]`, `[dap adapter]`, `[dap subfolder]`.
  Goes: never.
- **neotest-python finds the interpreter by globbing `*/pyvenv.cfg`** (DEFECT): with `.venv` and
  `.venv-pypy` it built a broken path, and its `uv run` fallback syncs the project. Up: none
  found. Fix: `integrations.neotest` sets `python` and `discovery.filter_dir` (12.2). Test:
  `test_workarounds.py::test_nvim_plugin_workarounds[neotest]`. Goes: when neotest-python
  handles several environments.
- **overseer also loads the `.vscode/tasks.json` provider** (LIMITATION): every label twice,
  tasks through `deploy.cmd`, shell tasks through `'shell'`. Fix: `integrations.overseer` adds
  `overseer.template.vscode` to `disable_template_modules` (12.2). Test:
  `test_workarounds.py::test_nvim_plugin_workarounds[overseer]`. Goes: never.
- **venv-selector (LazyVim's `lang.python`) needs `fd`** (LIMITATION, documented): without it it
  raises an error on the first Python buffer, which failed every smoke check that opens one.
  Fix: `template-nvim.yml` installs `fd` (`fdfind` on Ubuntu) (13.2). Test: CI only (that
  workflow's smoke run fails without it). Goes: never.
- **A grandchild keeps a pipe open** (LIMITATION, every OS): git or Mason outliving a killed
  Neovim blocked the harness's wait forever. Fix: the harnesses write to files and kill the
  whole tree (`nvimtest._run_logged`, `nvimtest.kill_tree`; `e2e`, `shells` alike; 13.1). Test:
  `test_cmd_nvim.py::test_run_logged_timeout_kills_the_whole_tree`,
  `test_run_logged_ctrl_c_kills_the_whole_tree`. Goes: never.

git and husky:
- **`diff.relative=true` makes `git diff` print project-relative paths** (LIMITATION): the hook
  dropped every staged path. Fix: `-c diff.relative=false` on every git call of `hooks` (5.6).
  Test: `test_hooks.py::test_staged_and_unstaged_files_ignore_diff_relative`. Goes: never.
- **The hook's environment** (LIMITATION): git exports a relative `GIT_INDEX_FILE` and, in a
  linked worktree, `GIT_DIR` without `GIT_WORK_TREE`. Fix: `hooks.git_env` makes them absolute;
  `hooks.run` removes them for uv and ruff (5.6). Test:
  `test_hooks.py::test_git_env_pins_relative_paths`, `test_install_in_a_linked_worktree`. Goes:
  never.
- **`git check-ignore` refuses `--literal-pathspecs`** (LIMITATION): Fix: `hooks._git(...,
  literal=False)` for it (5.6). Test:
  `test_hooks.py::test_ensure_installed_skips_an_ignored_project`. Goes: never.
- **`GIT_CEILING_DIRECTORIES` never excludes the current folder** (LIMITATION, documented): with
  the e2e base itself as the ceiling, `new` (it asks git from the base) still found a repository
  around the base and skipped `git init`, and `setup` installed the hook into that repository.
  Fix: `e2e.isolate_git` puts the ceiling at the base's PARENT (13.1). Test:
  `test_e2e_plan.py::test_isolated_git_never_sees_a_repository_around_the_base`. Goes: never.
- **git's messages follow the locale** (LIMITATION): "not a git repository" was not recognized.
  Fix: `LC_ALL=C` for every git call (`hooks`, `rename.git_changes`, `presets`; 5.6). Test:
  `test_rename.py::test_git_changes_reads_git_in_english`,
  `test_hooks.py::test_find_repo_reports_other_git_errors`. Goes: never.
- **git < 2.28 has no `init -b`** (LIMITATION): Fix: `presets` falls back to `git init` +
  `symbolic-ref HEAD refs/heads/main` (11). Test:
  `test_presets.py::test_git_init_falls_back_without_b`. Goes: when git 2.28 (2020) is the
  minimum.
- **A new repository starts on `main` or `master`** (LIMITATION, `init.defaultBranch`): Fix: the
  generated CI triggers on both; `new` creates `main` (13.2). Test:
  `test_render_core.py::test_ci_workflow_for_every_preset_and_backend_set`,
  `test_presets.py::test_git_init_makes_a_main_branch`. Goes: never.
- **The exec bit gets lost** (LIMITATION): `core.filemode=false` (Windows), zip downloads and
  copies drop it, and with `core.filemode=true` an index-only fix is undone by the next `git
  add`; Git for Windows runs hooks with its own sh. Fix: `cmd_env._fix_exec_bit` (the files and
  the index); `sh <launcher>` in the hook; `/bin/sh <root>/deploy` in VS Code tasks and the
  Neovim fallback (5.6, 12, 14). Test:
  `test_envs_core.py::test_fix_exec_bit_repairs_the_index_and_the_files`,
  `test_hooks.py::test_git_runs_the_hook`,
  `test_vscode.py::test_every_task_runs_the_launcher_as_a_process`. Goes: never.
- **CRLF checkouts** (LIMITATION): `* text=auto eol=native` with `core.autocrlf=true` checks
  text out with CRLF on Windows. Fix: generated-file hashes are normalized (`render._norm`),
  comparisons are CRLF-normalized (`presets.pristine`, the pyz parts' app code), the hook reads
  staged files through `git cat-file --filters` (`hooks.staged_blob`), `.gitattributes` pins LF
  for `deploy`, `deploy.ps1`, `.lazy.lua` and CRLF for `*.cmd` (6.2, 14). Test:
  `test_render_core.py::test_crlf_and_bom_checkouts_are_neither_edits_nor_rewritten`,
  `test_hooks.py::test_staged_blob_is_the_staged_version_as_checked_out`. Goes: never.
- **husky 9 points `core.hooksPath` at `.husky/_`** (LIMITATION): its generated pre-commit runs
  the user's `.husky/pre-commit`. Fix: `hooks._hooks_path_file` (5.6). Test:
  `test_hooks.py::test_core_hooks_path_husky_layout`. Goes: never.
- **MSYS2's own git prints `/c/...` paths** (LIMITATION): Fix: `hooks.find_repo` passes them
  through `project.native_path` (4.8). Test: `test_paths.py::test_native_path_windows` (the
  conversion; the call site is untested). Goes: never.

GitHub Actions and hosted runners:
- **setup-uv publishes no floating major tags since v8** (LIMITATION): `@v10` does not resolve.
  Up: astral-sh/setup-uv#830. Fix: the exact release in `templates/ci.yml` and the template
  workflows (13.2). Test:
  `test_render_core.py::test_ci_workflow_for_every_preset_and_backend_set`,
  `test_workflows.py::test_actions_are_pinned`. Goes: never.
- **GitHub disables a scheduled workflow after 60 days without repository activity**
  (LIMITATION, public repositories): then none of the file's triggers run, pushes included,
  until someone enables it again. Up: GitHub docs, "Disabling and enabling a workflow"
  (docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows). Fix:
  `template-keepalive.yml`, job `keepalive`: every week `gh api --method PUT
  .../actions/workflows/<file>/enable` for each `template-*.yml` that has a schedule, itself
  included, with the job's token (`actions: write`); one `disabled_manually` stays so (13.2).
  Test: `test_workflows.py::test_every_scheduled_workflow_is_kept_alive`. Goes: never.
- **`shell: bash` steps run with `-e -o pipefail`** (LIMITATION): `cmd | head -n1` kills a
  writer that is not done yet (SIGPIPE) and fails the step at random. Fix: the template
  workflows take one line with `sed -n 1p` or `grep -m1` (13.2). Test: untested (a race).
  Goes: never.
- **`uv tool install` puts its commands in a folder that is not on PATH on every hosted
  runner** (LIMITATION, Windows at least): the xonsh tests and probes skipped. Fix: the
  template workflows append `uv tool dir --bin` to `$GITHUB_PATH` after installing xonsh
  (13.2). Test: untested (the `-rs` skip list of the CI logs). Goes: never.
- **The `runner` context is not allowed in a job-level `env`** (LIMITATION): GitHub rejects the
  whole workflow file. Fix: the step env of `template-nvim.yml` (13.2). Test:
  `test_workarounds.py::test_template_workflows_use_contexts_where_actions_allows_them`
  (actionlint, when installed). Goes: never.
- **Hosted runners have no display, no OpenGL 3.3 on Windows and macOS, and `xvfb-run` starts an
  8-bit screen, which has no GLX visuals** (LIMITATION): raylib and Flet windows could not open.
  Fix: `e2e.detect_host` skips GUI runs on Windows/macOS CI and wraps Linux runs in `xvfb-run -a
  -s "-screen 0 1280x720x24"` (13.1). Test: `test_e2e_plan.py::test_detect_host_gui_modes`,
  `test_workarounds.py::test_xvfb_screen_has_24_bit_depth`. Goes: never.
- **WSL setup on hosted runners is slow and sometimes fails** (LIMITATION): Fix: the WSL job of
  `template-launchers.yml` is `continue-on-error`. Test: untested (CI only). Goes: when it is
  reliable.

PowerShell (details: section 4.5):
- **A `.ps1` runs inside the caller's session** (LIMITATION): what it sets stays there, and the
  caller's strict mode and preferences reach it (with `'Stop'`, 7.3+ turned a runner exit code
  into an error and 5.1 turned redirected stderr into a bogus exit 126; 5.1 also printed module
  progress). Fix: `deploy.ps1` restores every variable in `finally`, never assigns `$env:PATH`,
  resets strict mode and the preferences in its own scope. Test:
  `test_launcher_win.py::test_ps1_round_trip_in_a_session`,
  `test_ps1_restores_every_variable_it_sets`. Goes: never.
- **A `param()` block would take `-v`, `-h`, `-q`** (LIMITATION): Fix: `deploy.ps1` has none.
  Test: `test_launcher_win.py::test_ps1_has_no_param_block_and_leaves_path_alone`. Goes: never.
- **`$null` passed to a .NET string parameter becomes `''`** (LIMITATION; with .NET 9, in
  PowerShell 7.5+, `''` no longer removes a variable): `SetEnvironmentVariable($n, $null)` left
  an empty variable. Up: PowerShell/PowerShell#24637 (closed as a duplicate). Fix: `deploy.ps1`
  uses `Remove-Item Env:NAME`. Test:
  `test_launcher_win.py::test_ps1_clears_the_callers_uv_python_and_restores_it`. Goes: never
  (`Remove-Item` stays right).
- **Native arguments before 7.3 (and in `Legacy` mode)** (DEFECT): empty arguments were dropped
  and embedded quotes mangled; 5.1's quote counter ignores backslashes. Up:
  PowerShell/PowerShell#1995 (fixed by 7.3's `$PSNativeCommandArgumentPassing`). Fix:
  `deploy.ps1` pre-quotes every argument (`""` on 5.1, `\"` on 7). Test:
  `test_launcher_win.py::test_ps1_round_trip_in_a_session` (Legacy mode, 5.1 on Windows),
  `test_ps1_hand_over_is_injection_safe`. Goes: when Windows PowerShell 5.1 is dropped.
- **PowerShell 7 rewrites native arguments that are not quoted literals** (DEFECT): splatted
  `@argv` included, it globs on Linux/macOS (`'*'` reached uv as a file list) and expands `~`.
  Up: PowerShell/PowerShell#24178 (the globbing; none found for `~`). Fix: on Core `deploy.ps1`
  rebuilds the call from single-quoted words (`EscapeSingleQuotedStringContent`, typographic
  quotes too) for `Invoke-Expression`. Test:
  `test_launcher_win.py::test_ps1_hand_over_is_injection_safe`; `shells.PS_ARGS` in `selftest
  --shells`. Goes: when splatted arguments pass through verbatim.
- **7.3+ takes any native argument equal to `--%`, quoted or splatted, for the stop-parsing
  token** (LIMITATION): it drops it, then splits and `%VAR%`-expands the rest. Fix: `deploy.ps1`
  switches that call to `Legacy` passing in its own scope. Test:
  `test_launcher_win.py::test_ps1_passes_a_literal_stop_parsing_token`. Goes: never.
- **A typed `-X:v` reaches a script as two elements** (DEFECT): `'-X:'` and `v`. Up:
  PowerShell/PowerShell#6360 (closed for inactivity; 7.6 still splits it). Fix: `deploy.ps1`
  joins them again (limit: `-X: v`; `pwsh -File` and the shebang route split at the colon before
  the script runs, 4.5). Test:
  `test_launcher_win.py::test_ps1_keeps_typed_colon_arguments_whole`. Goes: when fixed upstream.
- **A script that names `$input` gets a redirected stdin re-read as text** (DEFECT): under `pwsh
  -File` and the shebang route, invalid UTF-8 became U+FFFD and CRLF became LF. Up: none found.
  Fix: `deploy.ps1` never names the variable and reads it by name. Test:
  `test_launcher_win.py::test_ps1_never_names_the_pipeline_variable`,
  `test_ps1_forwards_pipeline_input_and_keeps_raw_stdin`. Goes: never.
- **ConstrainedLanguage mode blocks every .NET call** (LIMITATION, AppLocker/WDAC): Fix:
  `deploy.ps1` checks the language mode first: one error, exit 126. Test:
  `test_launcher_win.py::test_ps1_constrained_language_gives_one_clear_error`. Goes: never.
- **`Get-Command uv` may return an alias, a function or a `uv.cmd`/`uv.ps1` wrapper**
  (LIMITATION): a wrapper parses the arguments again. Fix: `-CommandType Application -All` and
  only a real `uv.exe` on Windows (the plugin: `exepath('uv.exe')`; 4.1). Test:
  `test_launcher_sh.py::test_uv_search_order`,
  `test_nvim_render.py::test_lua_modules_in_headless_neovim`. Goes: never.
- **`[IO.File]::GetUnixFileMode` needs .NET 7 (PowerShell 7.3+)** (LIMITATION): Fix: older
  versions skip the x-bit check of a uv candidate (4.1). Test:
  `test_launcher_win.py::test_ps1_skips_a_uv_without_exec_bit`. Goes: once 7.3 is the minimum.
- **Windows PowerShell 5.1 reads a BOM-less script as ANSI and removes a bare `--`**
  (LIMITATION): Fix: `deploy.ps1` is pure ASCII; no CLI syntax needs `--` (14). Test:
  `test_launcher_win.py::test_ps1_is_ascii_lf_without_bom`. Goes: never.
- **Windows PowerShell 5.1 writes UTF-16 (`>`, `Out-File`), ANSI (`Set-Content`) and BOMs**
  (LIMITATION, editors add BOMs too): Fix: `config.read_text` names the encoding and how to save
  the file again; the other readers take a BOM (`utf-8-sig`: `render._read_state`,
  `render.load_profile`, `presets.load`; `imports.parse` reads bytes); a UTF-16 `state.json`
  counts as empty (6.1, 6.2, 14). Test:
  `test_config_rules.py::test_a_config_that_is_not_utf8_is_a_config_error`,
  `test_render_core.py::test_corrupt_state_counts_as_empty`,
  `test_profiles_tolerate_a_bom_and_crlf`,
  `test_mypyc_core.py::test_imports_parse_honours_a_bom_and_crlf`. Goes: never.

cmd.exe and CreateProcess (details: section 4.4):
- **How cmd reads a batch file** (LIMITATION): in the OEM code page, labels break with LF, `( )`
  blocks close early on a PATH holding `(x86)`, delayed expansion eats `!`, `%` expands inside
  `rem`, and `call` expands `%*` twice. Fix: `deploy.cmd` and the generated `.cmd` files
  (`portable.cmd_launcher`, `pyz._wrapper_cmd`) are ASCII and CRLF, with no blocks, no delayed
  expansion and `%*` only on the uv line. Test:
  `test_launcher_win.py::test_cmd_is_ascii_with_crlf`,
  `test_cmd_has_no_blocks_and_no_delayed_expansion`,
  `test_cmd_forwards_arguments_only_on_the_uv_line`,
  `test_fixes.py::test_pyz_wrapper_is_ascii_crlf_without_blocks`. Goes: never.
- **`%~dp0` is wrong when cmd found the file through PATH from a quoted name** (DEFECT): Up:
  none found. Fix: `deploy.cmd` checks `%~dp0.pytemplate\deploy.py`, else walks up from `%CD%`.
  Test: `test_launcher_win.py::test_cmd_walks_up_from_the_current_folder` (Windows). Goes:
  never.
- **cmd re-parses `%*`** (LIMITATION, the BatBadBut class): `% ! " ^` and unquoted `& | < >`
  inside arguments do not survive, whatever quoting a CreateProcess caller applies. Fix: no CLI
  syntax needs them (14); `shells.cmd_quote` for the probes. Test:
  `test_shells.py::test_cmd_quote`. Goes: never.
- **A quoted registry PATH entry splits `call` arguments** (LIMITATION): Fix: `deploy.cmd` keeps
  the value in a variable, drops the quotes, then `call set`. Test:
  `test_launcher_win.py::test_cmd_keeps_the_registry_path_out_of_call_arguments`,
  `test_cmd_registry_path_with_quoted_entries` (Windows). Goes: never.
- **`set "K=v"` in a generated launcher** (LIMITATION): a literal `%` must be written `%%`, and
  non-ASCII text, `"` and line breaks cannot be held. Fix: `portable._cmd_value` (10). Test:
  `test_fixes.py::test_portable_env_values_are_quoted`. Goes: never.

niubash (1.1.4) and WinuxCmd (details: section 4.6):
- **niubash runs `#!/bin/sh` scripts and `sh -c` inside the calling shell** (DEFECT): it ignores
  the shebang, `$0` is the caller's (`$(dirname "$0")` pointed into `%TEMP%`), variables,
  functions, aliases and `cd` leak into the session, and `exit`/`exec` only end the script. Up:
  none found. Fix: `deploy` finds itself through `$BASH_SOURCE`, prefixes every name with `_pt_`
  and unsets it, never changes folder, and runs uv without `exec` with a single exit (4.3); the
  portable `.sh` launcher uses `${BASH_SOURCE:-$0}` (10). Test:
  `test_launcher_sh.py::test_in_process_run_leaves_no_name_behind`, `test_lint`,
  `test_niubash_leaves_nothing_behind` (Windows). Goes: never.
- **niubash's parser** (DEFECT): `exit` is ignored inside `a || exit`, `a && exit` and `{ ...;
  }`; a comment on a `name() {` line is a syntax error; `"...$(cmd "$x")..."` keeps the inner
  quotes. Up: none found. Fix: the `deploy` rules, enforced by `test_launcher_sh.lint` (4.3).
  Test: `test_launcher_sh.py::test_lint`, `test_lint_detects`. Goes: when niubash fixes them.
- **niubash fakes its platform** (LIMITATION): `OSTYPE=msys`, `uname -s` = `MSWindows_NT`,
  `$PWD` like `C:/Users/...`. Fix: `deploy` detects Windows by `OS=Windows_NT` without
  `WSL_DISTRO_NAME`, and its walk-up stops at `C:` and `C:/` (4.3). Test:
  `test_launcher_sh.py::test_windows_helpers_in_posix_shells`. Goes: never.
- **A niubash session keeps stale exports** (LIMITATION): Fix: `project.caller_cwd` trusts
  `PYTEMPLATE_CALLER_CWD` only while it names the process cwd (4.8). Test:
  `test_paths.py::test_stale_caller_cwd_is_ignored`. Goes: never.
- **`niu -c`, niubash scripts and the xonsh `!` route read only `$NIU_ENV`** (LIMITATION): Fix:
  `shell-setup niubash` says to paste the function into both files (4.9). Test:
  `test_shells.py::test_snippets_are_ascii_and_say_where_to_paste`. Goes: never.
- **WinuxCmd's `cygpath.exe` knows no MSYS root** (DEFECT): it turns `/tmp/x` into `\tmp\x`. Up:
  none found. Fix: `project.find_cygpath` takes only a cygpath next to `msys-2.0.dll` or
  `cygwin1.dll`; `deploy` accepts only an `X:/...` answer (4.3, 4.8). Test:
  `test_paths.py::test_cygpath_without_the_msys_runtime_is_ignored`. Goes: never.

xonsh and bash:
- **xonsh on Windows maps `#!/usr/bin/env bash` to `bash`** (LIMITATION): that can be the WSL
  stub. Fix: `deploy` starts with `#!/bin/sh` (4.3). Test:
  `test_launcher_sh.py::test_shebang_lf_ascii`. Goes: never.
- **Older xonsh has no `@aliases.return_command`** (LIMITATION): Fix: the xonsh snippet falls
  back to an unthreadable function alias (4.9). Test:
  `test_shells.py::test_xonsh_snippet_runs_and_completes`. Goes: when the oldest supported xonsh
  has it.
- **xonsh changes the name and default of its subprocess raise-error setting** (LIMITATION):
  0.24 raises `CalledProcessError` from a failing `![...]` unless
  `$XONSH_SUBPROC_CMD_RAISE_ERROR` is off (0.18 did not raise), so a probe that relied on one
  setting's name got exit 1 instead of the child's code. Fix: the xonsh probe of
  `shells.command_text` names no setting and takes the code from `CalledProcessError` too (4.9).
  The template workflows still install xonsh 0.24.2 for pushes and pull requests and
  template-launchers the newest on its weekly run (13.2): a stable signal, and drift that
  shows. Test: `test_shells.py::test_xonsh_probe_exit_code_ignores_raise_settings` (every
  setting forced on), `test_command_text_per_family`. Goes: never.
- **bash rejects CRLF in an rc file** (LIMITATION): Fix: `shell-setup` writes LF bytes on every
  OS (4.9). Test: `test_shells.py::test_snippets_are_ascii_and_say_where_to_paste`. Goes: never.

MSYS2, Cygwin, Git Bash and busybox-w32 (details: section 4.7):
- **A minimal PATH** (LIMITATION): MSYS2 login shells (`MSYS2_PATH_TYPE=minimal`) and Cygwin
  with `CYGWIN_NOWINPATH` hide uv, and a console opened before uv was installed has the old
  PATH. Fix: the launchers search the install folders, then the PATH stored in the registry
  (4.1). Test: `test_launcher_sh.py::test_uv_search_order`, `test_registry_path_quoted_entries`,
  `test_msys2_login_minimal_path` (Windows). Goes: never.
- **MSYS rewrites arguments and variables; the others do not** (LIMITATION): `/v` becomes `V:/`
  and `/c/x` `C:/x`, `SYSTEMROOT`/`PROGRAMFILES` are upper-cased, while Cygwin, niubash and
  busybox-w32 pass `/c/x` through. Fix: `reg.exe query KEY` without `/v`; `%NAME%` expansion
  retries the upper-case name; `project.native_path` takes every spelling (4.3, 4.8). Test:
  `test_launcher_sh.py::test_registry_path_quoted_entries`,
  `test_windows_helpers_in_posix_shells`, `test_paths.py::test_native_path_windows`. Goes:
  never.
- **Cygwin's command-line parser mangles argv from a Windows program** (LIMITATION): it mangles
  `\"`, expands `*` and drops `'`. Fix: `shells` hands the probe commands over in `$PTCMD`
  (4.9). Test: `test_shells.py::test_invocation`. Goes: never.
- **An inherited `PWD`** (LIMITATION): MSYS2 bash started in a stale folder. Fix:
  `shells.child_env` drops `PWD` and `OLDPWD` (4.9). Test:
  `test_shells.py::test_child_env_drops_what_uv_run_added`. Goes: never.

POSIX shells (dash, zsh, ksh93 and the rest; details: section 4.3):
- **dash's `echo` interprets backslashes** (LIMITATION, POSIX allows it): `c:\Users` printed as
  `c: sers`. Fix: `printf '%s\n'` (4.3). Test: `test_launcher_sh.py::test_lint_detects`. Goes:
  never.
- **zsh names a sourced file only in `${(%):-%x}`** (LIMITATION): dash cannot parse it. Fix:
  `deploy` reads it through `eval`, then `emulate sh`. Test:
  `test_launcher_sh.py::test_posix_shell_runs_the_launcher`. Goes: never.
- **A caller's `set -eu`** (LIMITATION): `sh -eu deploy` and niubash with errexit stopped the
  launcher at the first failing test. Fix: `${v:-}` and `|| :`/`|| _pt_x=` everywhere. Test:
  `test_launcher_sh.py::test_launcher_survives_caller_errexit`. Goes: never.
- **`command -v` prints aliases and functions** (LIMITATION): Fix: `deploy` accepts only an
  answer that is a path (4.1). Test: `test_launcher_sh.py::test_uv_search_order`. Goes: never.
- **`$PWD` is logical** (LIMITATION): below a symlinked folder its `..` is not where the kernel
  found `../deploy`. Fix: `deploy` keeps the relative path unless the folded one holds the
  project. Test: `test_launcher_sh.py::test_relative_launcher_from_a_symlinked_subfolder`. Goes:
  never.
- **An exported `CDPATH`, and ksh93's `cd -P` on a relative path** (LIMITATION; DEFECT for
  ksh93): `cd` printed the folder or picked another one, and ksh93 folded `..` as text. Up: none
  found (ksh93). Fix: the portable `.sh` launcher sets `CDPATH=''` and joins each relative link
  target to the `pwd -P` folder of its link (`portable.sh_launcher`, 10). Test:
  `test_build_methods.py::test_portable_sh_launcher_via_symlinks_cdpath_and_spaces`. Goes:
  never.

Windows:
- **MAX_PATH (`LongPathsEnabled=0`)** (LIMITATION): deep paths broke MSVC (mypyc), PyPy runtime
  copies, `compileall` and the Flet client extraction. Fix: stage-relative `c_dir`, `build_temp`
  and `build_lib` in `mypyc.build`'s spec; `\\?\` paths in `portable.long_path` and
  `e2e.rmtree`; short default folders (`nvimtest.default_dir`, `e2e.default_base`, and pytest's
  `--basetemp` under `RUNNER_TEMP` in template-selftest's Windows job); doctor reports the
  setting (`cmd_env._long_paths`; 1.7, 9, 10, 13.2). Test:
  `test_mypyc_core.py::test_build_spec_matches_what_the_tool_reads`,
  `test_cmd_nvim.py::test_default_dir_is_short`, `test_e2e_plan.py::test_default_base_is_short`.
  Goes: never.
- **Reserved device names** (LIMITATION): `src/aux/` cannot exist, and git cannot check it out.
  Fix: `presets.WINDOWS_DEVICES` in `presets.check_name_free` (11). Test:
  `test_presets.py::test_check_name_free_refuses`. Goes: never.
- **CreateProcess resolves a relative program against the parent's folder** (LIMITATION): a
  task's `tools/gen.sh` ran from the caller's folder, not the task's cwd. Fix: `tasks.run_task`
  anchors it (6.1). Test: `test_cli_core.py::test_a_relative_program_runs_from_the_task_cwd`.
  Goes: never.
- **A command line holds 32767 characters** (LIMITATION): Fix: `hooks.ARG_LIMIT` batches file
  arguments (5.6). Test: `test_hooks.py::test_batches`. Goes: never.
- **A file stays locked after its process ends** (LIMITATION): a just-exited exe, an antivirus
  scan or a DLL still loaded. Fix: `e2e.rmtree` and `e2e._move` (the portable smoke moves the
  folder away and back) retry, `e2e.run_logged` leaves a stdout file a killed tree still holds
  to the base's cleanup; the pyz bootstrap moves an incomplete cache aside (`_discard`) (10,
  13.1). Test: `test_build_methods.py::test_pyz_repairs_an_incomplete_cache`,
  `test_e2e_run.py::test_move_retries_a_locked_folder`,
  `test_e2e_run.py::test_run_logged_timeout_kills_the_whole_tree`. Goes: never.
- **The classic console needs ANSI turned on** (LIMITATION): and the `os.system("")` trick
  started a cmd.exe on every run. Fix: `ui.enable_vt_mode` (`SetConsoleMode`, 5.3). Test:
  `test_paths.py::test_ui_never_spawns_cmd_for_colors`,
  `test_windows_console_gets_ansi_without_cmd`. Goes: never.
- **Ctrl+C ends a console program with `STATUS_CONTROL_C_EXIT`** (LIMITATION): Fix: `cli.main`
  maps `proc.STATUS_CONTROL_C_EXIT` to 130 (5.3). Test:
  `test_cli_core.py::test_main_maps_every_outcome_to_its_exit_code`. Goes: never.
- **Case-insensitive file systems** (LIMITATION, macOS too): a case-only rename needs two moves,
  and `Lib/` also matches `lib/`. Fix: `rename.apply_plan`; `portable.runtime_stdlib` looks for
  `lib/pythonX.Y` first (5.7, 10). Test:
  `test_rename.py::test_case_only_folder_fix_on_a_case_insensitive_file_system`. Goes: never.
- **A venv is specific to its OS** (LIMITATION, WSL on `/mnt`): Fix: `.venv*-wsl` and
  `.build/wsl` (`project.ENV_SUFFIX`, 7). Test:
  `test_envs_core.py::test_runtime_and_tool_environments`, `test_clean_envs_on_the_wsl_side`.
  Goes: never.
- **`wsl -l -q` prints UTF-16** (LIMITATION): Fix: `shells.wsl_distros` decodes it. Test:
  `test_workarounds.py::test_wsl_distros_are_read_as_utf16`. Goes: never.

macOS:
- **`/usr/bin/cc`, `gcc` and `clang` exist without the developer tools** (LIMITATION, xcrun
  shims that open an install dialog): Fix: `cmd_env._xcode_problem` (the active developer
  folder must hold a clang: `cmd_env.XCODE_CLANG`, the Command Line Tools or an Xcode.app
  toolchain), `cmd_nvim.c_compiler` (7, 12.2). Test: `test_envs_core.py::test_c_compiler_rejects_macos_xcode_shims`,
  `test_cmd_nvim.py::test_c_compiler_skips_the_macos_shims_without_developer_tools`. Goes:
  never.

### 15.2 Our open issues and fragile points

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
- raylib + PyPy on arm64 (macOS and Linux): no PyPy wheel for those platforms (raylib 6.0.1.0
  publishes pp311 wheels for x86_64 only) and `no-build-package`, so `./deploy setup` of a
  raylib project fails to sync `.venv-pypy` on Apple Silicon (confirmed on `macos-latest`:
  "marked as `--no-build` but has no binary distribution") and on Linux arm64 (Raspberry Pi,
  Graviton, Docker on a Mac; simulated with uv's `--python-platform`). The generated `ci.yml`
  syncs only the matrix backends (its Linux runner is x86_64) and `selftest --e2e` switches the
  project off PyPy first (`e2e.HOST_GAPS`); users there run `./deploy mode cpython --supports
  cpython,mypyc`. Possible fix: skip PyPy for raylib on arm64 in `apply`/`new`.
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
- flet presets: `constraints.txt` gives a new project httpx 0.28.1, but flet 1.0.1 only asks
  for `httpx>=0.28.1`, so `./deploy lock --upgrade` takes httpx 1.x once it is final, and its
  1.0 dev releases drop `AsyncClient`, which `flet.auth` (OAuth; not used by the skeleton)
  needs. Not bounded in the preset on purpose (a direct dependency every flet project would
  have to remove when Flet moves on): an app that uses `flet.auth` runs `./deploy add
  "httpx<1"`.
- Vendored native libraries (`.so`/`.pyd` in `src/`, `git add -f`): the stage, the payloads,
  portable and wheel carry them; PyInstaller/Nuitka only bundle what they detect (a library
  loaded with ctypes needs `[deploy.exe] extra_args = ["--add-binary", ...]`), pyz leaves
  extensions out of `common/`, and a cpython/pypy wheel stays tagged `py3-none-any`.
- `sync_tree` does not detect a case-only rename (`Data.py` -> `data.py`) on a
  case-insensitive file system: the stage keeps the old spelling until `./deploy clean`.
- The name check (`presets.check_name_free`) knows the import names only of the packages the
  presets pin (`presets.IMPORT_NAMES`): a dependency the user adds is compared by its
  distribution name (`beautifulsoup4` refuses `beautifulsoup4`, not `bs4`). Reading them needs
  the installed wheels (an environment of the project, never there for `new`'s next preset).

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
- `.lazy.lua` reads lazy.nvim's internal `require("lazy.core.config").spec.modules` without a
  guard (LazyVim reads the same field in several places, and lazy.nvim has not changed it
  since 2023): if it ever changes, lazy.nvim reports "Failed to load `.lazy.lua`" and the
  whole integration is missing. It also names the five extras and the plugins' repositories.
  Fixing any of it changes `.lazy.lua`'s bytes (a re-trust; projects already made keep their
  own copy). Recommended in one go: make `.lazy.lua` a minimal loader that finds the root and
  returns `dofile(root .. "/.pytemplate/nvim/spec.lua")(root)` (no pcall: lazy.nvim reports
  its errors), with today's body in `spec.lua` and the internal read guarded
  (`pcall(require, ...)`, `type(...) == "table"`, else no extras). `.pytemplate/nvim/**` is
  already trusted with `.lazy.lua`, so later fixes there need no re-trust.
- The pinned `selftest --nvim` (`nvimtest.LOCK`, `cmd_nvim.STARTER_REV`) stays green while
  upstream moves: only a run without the lock (template-nvim's weekly canary) shows drift coming, and
  users' LazyVim follows its own `lazy-lock.json`.
- Neovim on Windows: a new lint run cancels the running one of the same linter (LazyVim lints on
  BufReadPost, InsertLeave and BufWritePost), and nvim-lint then kills only the `cmd.exe`
  wrapper of the cancelled mypy. In `selftest --nvim` on the Windows runner a mypy run started
  right before LazyVim's own published no diagnostics within 180 s (flet preset, twice;
  script and raylib passed); since the smoke check waits for LazyVim's run it passes. Whether
  users lose diagnostics this way (until the next save) is not verified.
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
  imports `cmd_env._msvc` and `_xcode_problem` lazily (`cmd_env` imports `cmd_nvim`).
- `RULES_RE` / `tasks.parse_line` <-> `ui.error`, `ui.warn`, `str(lintc.Finding)` (5.3).
- mypyc internals mirrored by the runner (checked by `test_mypyc_core` against the locked
  mypy): `lintc.NATIVE_CLASS_DECORATORS` <-> mypyc's native decorators;
  `lintc.relative_file_at_import` <-> when mypyc builds no shared lib; `mypyc.remove_stale_extensions`
  <-> mypyc's lib names (`<group>__mypyc`, `<module>__mypyc`). `mypyc.MYPYC_REJECTED` <->
  `tools/mypyc_build.py`; the spec keys the script reads <-> `mypyc.build`;
  `mypyc_build.extra_cflags`/`compiler_type` <-> the wheel's `SETUP_PY`
  (`test_wheel_setup_py_adds_the_same_flags_as_the_stage`); `mypyc.COMPILER_ENV` <-> the
  variables setuptools' `configure_system` reads.
- `envs.MIN_UV` <-> the presets' `python.pypy` pin and default `python.cpython`, and the newest
  uv flag the runner uses (7); `hooks.launcher_of` <-> `hooks.sh_literal`; `cmd_env._msvc`
  <-> setuptools' `_find_vc2017` component choice (`test_msvc_component_matches_setuptools`)
  and `mypyc.has_compiler_hint(platform)` (the winget `--add` component).
- `editor.json` <-> `cli.COMMANDS` (6.2); `cmd_nvim.EXTRAS` <-> the extras list in
  `templates/nvim/lazy.lua` (`test_lazy_lua_extras_match_cmd_nvim`); `vscode.MYPYC_STAGE` /
  `editor.json` `mypyc_stage` / `vscode._STAGE` (the pytest stage matcher) <->
  `mypyc.profile(cfg, ...).stage` (`test_editor_json_stage_matches_the_runner`); the CI pyz path
  <-> `BuildRequest.out_name` and the merged upload's `.cmd` <-> `pyz.wrapper_path` (10;
  `test_ci_workflow_for_every_preset_and_backend_set`).
- template-selftest.yml reads pins from the code by text: `envs.MIN_UV` (`MIN_UV = "..."`),
  `cmd_dev.BASEDPYRIGHT`, the taplo pin of `test_render_core.py` and the pytest pin of
  `uv.lock`; it deselects `test_init_round_trip_through_every_preset_is_byte_identical` by
  name in the uv-floor job. `test_workflows.py` checks the workflow texts it relies on.
- `editor.json` `typing.basedpyright` <-> `cmd_dev.BASEDPYRIGHT` (bumping the pin changes a
  generated file: re-render); the Lua whitelists `BACKENDS`, `PROFILES`, `EDITORS`,
  `SEVERITIES` in `nvim/lua/pytemplate/init.lua` <-> the runner's
  (`test_lua_whitelists_match_the_runner`); `nvimtest.LOCK` <-> `cmd_nvim.STARTER_REV` (refresh
  both from one green run, 13.1); `.lazy.lua`'s bytes <-> `test_nvim_render.LAZY_LUA_SHA256`;
  `tasks.META.apply` <-> `tasks.META.setup` (one operation, two names).
