# CLAUDE.md: py_template for coding agents

Technical map of this project: architecture, invariants, workarounds and fragile points. Read
the section you need before touching a file. Cite code by symbol (`render.apply`,
`envs.env_vars`), never by line number.

- This file is copied into every project made with `./pyt new`. Items tagged
  **[template repo]** only apply to the template repository itself, which is recognised by the
  marker file `.pytemplate/template-repo` (`./pyt new` does not copy it).
- `<pkg>` is the app package: `app.name` in snake_case (`myapp` in the template repo).

## 1. Ground rules

1. Drive everything through `./pyt` (`./pyt help`, `./pyt help COMMAND`). It sets the
   environment variables uv, mypy and mypyc need (sections 5.5 and 7). A bare `uv run`, `mypy` or
   `pytest` in the project can pick the wrong interpreter or silently recreate `.venv`.
2. Never hand-edit generated files (section 6.2). Change `pytemplate.toml`,
   `.pytemplate/templates/**` or the generator, run `./pyt render`, and commit the
   regenerated files together with the change.
3. The runner (`.pytemplate/runner/`) is stdlib only, Python 3.11 compatible and
   `mypy --strict` clean on linux, darwin and win32. Coding rules: section 14.
4. Verify with `./pyt selftest` (must pass), `./pyt render --check` and `./pyt doctor`.
   `selftest` needs `.venv`: run `./pyt setup` once.
5. Everything written into the repository is English: code, comments, messages, docs, preset
   UI strings, TOML comments, commit messages and PR text (**[template repo]** enforced by the
   language guard, section 13.1). Avoid non-ASCII: write `...` and `->`. Launchers and
   generated `.cmd` files must be pure ASCII. Talk to the owner in the language of their
   prompt (Spanish so far); only the repository content is English.
6. Git: never edit git config (the machine may have no global identity). Commit with
   `git -c user.name="..." -c user.email="..." commit`, copying the identity of the existing
   commits (`git log -1 --format='%an <%ae>'`). Keep `pyt` AND `pyt.ps1` at mode 100755
   (`git ls-files -s pyt pyt.ps1`; fix with `git update-index --chmod=+x <file>`;
   `./pyt setup` repairs both).
7. Keep scratch paths short (`C:\t\p1`, `%TEMP%\pt\...`): with `LongPathsEnabled=0` deep paths
   break MSVC (mypyc), PyPy runtime copies, `compileall` and the Flet client extraction.
8. Never touch user-global state from tests or tools: shell rc files, the user's Neovim
   config/data/state, PATH, the registry, installed programs. The one exception is what the
   user asks for by name: `./pyt install` writes the installed template into the user data
   folder and the launchers into uv's tool bin folder, and `uninstall` removes exactly those
   (section 5.9); neither ever edits PATH, an rc file or the registry, and their tests move
   HOME, XDG_DATA_HOME, LOCALAPPDATA and UV_TOOL_BIN_DIR into tmp_path.
9. Calling `./pyt` from an agent on Windows: a Git Bash tool runs `pyt` (sh); a
   PowerShell tool resolves `./pyt` to `pyt.ps1`; in cmd, Claude Code sessions set
   `NoDefaultCurrentDirectoryInExePath=1`, so type `.\pyt.cmd`, never a bare `pyt`.
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
11. `pytemplate.toml` IS A STABLE CONTRACT (set by the owner; never relax it). Once a key or an
    allowed value (a backend, method, profile, preset or option name) has shipped in a
    template version, it keeps its name, its place, its meaning and its default:
    - adding keys and values is fine; renaming, moving or removing one, changing what it
      means or its default, and bumping `schema` are not;
    - the only exception is a change that is truly unavoidable (for example a whole backend
      removed), and only after telling the owner and getting their explicit approval for that
      very change;
    - even then the runner keeps reading the old files: a retired key or value is recognised,
      gives a warning that says what happened and what to do, and is ignored or mapped to its
      replacement; it never stops a command. Keys that never existed stay errors (typos);
    - the goal: a user upgrades by taking the new template and bringing their own
      `pytemplate.toml`, their code (`src/`, `tests/`) and their dependencies (`[project]`
      dependencies and dependency groups of `pyproject.toml`, `uv.lock` for the versions),
      without editing any of them.
    The contract starts with the keys and values of September 2026: the template had no users
    before, so the CPython JIT keys removed then stay unknown keys (owner decision).

## 2. What this is

- A uv-based Python project template. One codebase, three backends:
  - `cpython`: `.venv`, uv-managed CPython (`python.cpython`, a minor version).
  - `pypy`: `.venv-pypy`, PyPy pinned exactly (`python.pypy = "pypy@3.11.15"`); code must be
    3.11 syntax and API while PyPy is supported.
  - `mypyc`: AOT compilation of `compile.modules` (default `<pkg>.core`); runs on the `.venv`
    CPython. Needs a C compiler (MSVC Build Tools on Windows).
- Presets `script`, `raylib`, `flet` (`.pytemplate/presets/*`): skeleton + deps + config. The
  preset is chosen when the project is created (`./pyt new DIR --preset P`); there is no
  public `init` (section 11).
- Six build methods: `exe` (PyInstaller / `flet pack`), `portable`, `pyz`, `wheel`, `nuitka`,
  `flet` (`flet build`).
- `./pyt` is a Justfile-like runner. The only hard requirement is uv.
- `pytemplate.toml` is the single source of truth; every derived config is generated. After
  editing it, `./pyt apply` brings the whole project in line (section 5.8); `./pyt setup`
  is the same operation under its first-time name.
- Editor integrations are generated too: VS Code (`.vscode/*.json`) and LazyVim (`.lazy.lua`,
  `.pytemplate/editor.json`, local plugin `.pytemplate/nvim/`).

## 3. Layout (tracked files)

```
pyt, pyt.cmd, pyt.ps1             launchers (section 4); all logic is in .pytemplate/
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
.pytemplate/pyt.py             PEP 723 entry (requires-python >=3.11, dependencies = [])
.pytemplate/runner/               the runner package (section 5)
.pytemplate/runner/editors/       vscode.py, nvim.py: editor file generators
.pytemplate/runner/methods/       exe, portable, pyz, wheel, nuitka, flet (+ common.py)
.pytemplate/tools/mypyc_build.py  runs INSIDE .venv (needs mypy + setuptools), calls mypycify
.pytemplate/tools/mutation_cr.py  Cosmic Ray's side of selftest --mutation (PEP 723; its uv script
                                  lock mutation_cr.py.lock next to it; an environment of its own)
.pytemplate/templates/            typing/*.toml, vscode/settings.json, nvim/lazy.lua, ci.yml,
                                  portable/boot.py, pyz/__main__.py
.pytemplate/presets/<p>/          preset.toml + files/ skeleton (+ constraints.txt: tested pins;
                                  + raylib/tools/raylib_stubs.py)
.pytemplate/nvim/                 local Neovim plugin pytemplate.nvim (lua/, tests/smoke.lua,
                                  tests/lazy-lock.json: the plugin commits selftest --nvim pins,
                                  README.md: setup, keymaps, options)
.pytemplate/tests/                runner tests (pytest; conftest.py: the Hypothesis profiles) +
                                  pytest.ini (the suite's own pytest settings) + mypy-runner.ini
.pytemplate/editor.json           generated data file for the Neovim plugin
.pytemplate/state.json            hashes of the generated files + the `applied` record (committed)
.pytemplate/template-repo         [template repo] marker, not copied by ./pyt new
.pytemplate/installed.json        never tracked: the record `./pyt install` writes into the
                                  installed template only (section 5.9); `new` never copies it
.github/workflows/ci.yml          generated CI of the project
.github/workflows/template-*.yml  [template repo] selftest, launchers, nvim, e2e CI, the
                                  Linux CI image, the flet builds and the keepalive of their
                                  schedules (section 13.2); not copied
.github/workflows/template-ci-image/  [template repo] the Linux CI image: Dockerfile, pins.py
                                  (its pins and tag), system, warm, build; not copied
.github/workflows/template-flet/  [template repo] template-flet.yml's steps: check.py (the
                                  target edit, the Flet version, the browser and .apk checks),
                                  browser.txt (the pinned Playwright); not copied
ignored: .venv*/ .build/ dist/ build/ *.spec *.pyd *.so .flet/ tool caches,
         .claude/worktrees/ .claude/settings.local.json
```

## 4. Launchers and shells

### 4.1 Contract

The three launchers `pyt`, `pyt.cmd`, `pyt.ps1` and the Neovim plugin (section 12.2)
implement the same contract: change them together.

1. Find the project root: the directory that holds `.pytemplate/pyt.py`, first from the
   launcher's own location (a symlink to `pyt` or `pyt.ps1` is followed to the file it
   names, sections 4.3 and 4.5), else by walking up from the current directory; on POSIX, when
   no logical parent holds one, again from its physical folder (`pwd -P`), as git finds its
   repository: from `~/game-src -> ~/code/game/src` the logical walk found no project (`pyt`
   `_pt_walk`, `pyt.ps1` `$walked`; Windows walks the logical path only, section 15.2;
   `test_launcher_sh.test_a_folder_reached_through_a_symlink_into_a_project_finds_it`,
   `test_the_walk_up_from_the_physical_folder_keeps_the_ownership_rule`). A project made
   before the launchers were renamed holds `.pytemplate/deploy.py` instead: that is its runner
   (a folder with both runs `pyt.py`; `pyt` `_pt_entry_in`, `pyt.ps1` `Get-Entry`, `pyt.cmd`
   `PT_ENTRY`). A walked-up root
   must be the user's: on POSIX its `.pytemplate/pyt.py` AND the `.pytemplate` folder pass `test
   -O` (anyone may create `/tmp/.pytemplate/pyt.py`, and it ran as the caller; a hard link keeps
   the owner of the file it links, and on macOS any user may link a `pyt.py` of yours, the
   installed template's, into a `.pytemplate` of theirs next to a `runner/` of theirs, which
   `pyt.py` imports from its own folder); on Windows, whose owners are not read
   (an Administrators-owned checkout would fail), a drive root is never taken. Otherwise exit 2
   naming it (`pyt` `_pt_foreign`, `pyt.ps1` `Test-Foreign` through `/bin/sh -c '[ -O ... ]'`,
   `pyt.cmd` stops before the drive root;
   `test_launcher_sh.test_a_launcher_outside_a_project_never_runs_another_users_one`,
   `test_a_link_to_your_own_pyt_py_in_another_users_folder_is_never_run`,
   `test_the_walk_up_never_takes_a_drive_root_on_windows`).
   No project found: the installed template of `./pyt install` (section 5.9), where
   `cmd_install.snapshot_dir` puts it: `%LOCALAPPDATA%\pytemplate\template` on Windows (else
   below `%USERPROFILE%\AppData\Local`; neither set: none), elsewhere
   `$XDG_DATA_HOME/pytemplate/template` for an absolute `XDG_DATA_HOME` (the XDG spec ignores a
   relative one), else `$HOME/.local/share/pytemplate/template` for an absolute `HOME` (`pyt`
   `_pt_installed`, the `$data` of `pyt.ps1`, `pyt.cmd` `:installed`). It runs, in global mode,
   only when its `.pytemplate/pyt.py` passes the same ownership rule (it lives where the
   environment says). Without it: exit 2 with the no-project line and how to install pyt
   (`test_launcher_sh.test_outside_a_project_the_launcher_runs_the_installed_template`,
   `test_the_installed_template_of_another_user_is_never_run`,
   `test_launcher_win.test_ps1_outside_a_project_runs_the_installed_template`,
   `test_cmd_outside_a_project_runs_the_installed_template`).
2. Find uv (order below).
3. Export `PYTEMPLATE_CALLER_CWD` (the caller's cwd; `C:\...` form with an upper-case drive on
   Windows) and `PYTEMPLATE_LAUNCHER`, and `PYTEMPLATE_GLOBAL=1` for the installed template
   only: for a project's runner the variable is removed, so a stale export never turns it into
   global mode (`pyt`: `unset`; `pyt.cmd`: `set "PYTEMPLATE_GLOBAL="`; `pyt.ps1`: removed and
   restored like the other two; plugin: an empty value, which the runner reads as unset, like
   the three below; `test_launcher_sh.test_a_projects_runner_never_runs_in_global_mode`,
   `test_launcher_win.test_cmd_never_hands_a_project_the_global_mode`,
   `test_nvim_render.test_a_neovim_that_inherited_the_global_mode_runs_the_projects_runner`: a
   Neovim that inherited it ran every task in global mode, exit 2). `pyt.cmd` also exports
   `PYTEMPLATE_LAUNCHER_FILE`, its own path: cmd reads it again once uv returns, so the runner's
   install and uninstall must know it (section 4.4).
4. Run `uv run --quiet --python=REQUEST --python-preference PREF --script
   <root>/.pytemplate/pyt.py ARGS...` with argv untouched and propagate its exit code.
   `project.launcher_python` gives both, and the launchers mirror it (`pyt` `_pt_py`/`_pt_pref`,
   `pyt.cmd` `PT_PY`/`PT_PREF`, `pyt.ps1` `$python`/`$preference`, plugin `M.tool("python")`).
   While the project has an environment (its interpreter is there: POSIX
   `.venv-wsl/bin/python` or `.venv/bin/python`, a link to its base Python that must not dangle;
   Windows `.venv\Scripts\python.exe`), REQUEST is empty and PREF `only-managed`: an empty
   `--python=` is no request (uv 0.10.12 and 0.12.19, as for an empty `UV_PYTHON`), so uv follows
   the `.python-version` it finds from the script's folder upward, `python.cpython`, as the
   launchers always did, and only its own CPython. Without an environment REQUEST is `>=3.11`
   (`project.ANY_RUNNER_PYTHON`: any CPython 3.11 or newer; a version request never matches PyPy
   nor a pre-release) and PREF `managed` (a system CPython too), where `.python-version` would
   stop every command on a machine uv has no such CPython for (Android/Termux, the BSDs: section
   15.1). The runner moves the commands that need `python.cpython` onto uv's own CPython of that
   minor itself (`cli._restart`, section 5.2). One word, `--python=`: Windows PowerShell 5.1
   drops an empty argument. The environment decides, never `>=3.11` alone: uv reuses the cached
   environment of a script whenever its interpreter satisfies the request, so the first CPython
   a machine offered (a distribution's 3.12) stayed the runner's for good, and every command paid
   a restart; `.python-version` with `only-managed` makes uv make it again on `python.cpython`
   (`managed` reused one a system 3.14 had built; measured with both uv versions). Nor the
   environment's interpreter as a path: on Windows that file is there even when its base Python
   was uninstalled, and uv stopped every command, `setup` included, on it.
   The caller's `UV_PYTHON`, `UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON`, `PYTHONHOME`,
   `PYTHONPATH` and `UV_WORKING_DIR` are removed for uv only (`pyt`: `unset` before `exec`,
   prefix assignments for niubash; `pyt.cmd`: `set "X="` under `setlocal`; `pyt.ps1`: removed
   and restored like the `PYTEMPLATE_*` pair; plugin: values in the job's environment). Where
   a value must stand in for "unset" (niubash, plugin), `UV_PYTHON`, `PYTHONHOME` and
   `PYTHONPATH` are empty (uv reads an empty `UV_PYTHON` as unset, CPython an empty
   `PYTHONHOME`/`PYTHONPATH`), the two flags are `false` (uv refuses an empty one, "expected a
   boolish value", and either one set next to `--python-preference`: "cannot be used with") and
   `UV_WORKING_DIR` is `.` (uv refuses an empty one: "a value is required for '--directory'").
   The runner then always starts on the Python chosen here, started cleanly (a `PYTHONHOME` made
   every command die with `Failed to import encodings module`, a `PYTHONPATH` could shadow the
   stdlib), in the caller's folder (`UV_WORKING_DIR` moved it, and `new DIR` or `pyz-merge`
   paths with it) (section 5.2); its own tools never used them anyway (`proc.base_env`).
   `proc.runner_argv` builds the same call for the runner's own starts of a runner (`new`'s
   `__init`, `selftest --e2e` and `--nvim`). Launchers never `cd`; besides the uv search, the
   install hints and the choice of REQUEST they contain no logic
   (`test_launcher_sh.test_the_runner_starts_on_python_cpython_once_the_project_has_an_environment`,
   `test_launcher_win.test_every_launcher_starts_the_runner_on_the_same_python`).

Every launcher carries a `pytemplate-launcher:` line in its header (`#`, `rem` in `pyt.cmd`):
`./pyt install` overwrites, and `uninstall` removes, only a file of uv's tool bin folder that
has it (`cmd_install.MARKER`; `test_install.test_every_launcher_carries_the_marker`).

Launcher exit codes: 2 = no project found (and no installed template), 127 = uv not found
(after the install hints), 126 = `pyt.ps1` could not start uv, or PowerShell runs it in
ConstrainedLanguage mode; anything else comes from the runner (3 when uv started it on a Python
older than 3.11).

uv search order (the same in all three launchers and the plugin, `init.uv_candidates`;
`test_launcher_sh.test_uv_search_order` walks it with fake uvs for `pyt` and `pyt.ps1`):
`$UV` (must be a file) -> PATH (`pyt`: `command -v uv` accepted only when it prints a
path, which rejects aliases and functions; `pyt.ps1`: `Get-Command -CommandType
Application -All`, only a real `uv.exe` on Windows; the plugin: `exepath('uv.exe')` on
Windows, never a `uv.cmd`/`uv.bat` shim) -> `UV_INSTALL_DIR[/bin]`, `XDG_BIN_HOME`,
`XDG_DATA_HOME/../bin`, `~/.local/bin`, `CARGO_HOME/bin`, `~/.cargo/bin` -> Windows: WinGet
`Links` and `Packages/astral-sh.uv_*` (`LOCALAPPDATA`, then `ProgramFiles`), scoop shims
(`SCOOP`, `~/scoop`, `SCOOP_GLOBAL`, `ProgramData/scoop`), chocolatey, and last the user and
machine `Path` stored in the registry (a console opened before uv was installed; not in the
plugin; only its absolute entries, and one naming a variable that is not defined never loses
it: sections 4.3 to 4.5) -> POSIX: `/opt/homebrew/bin`, `/usr/local/bin`, linuxbrew, `~/.nix-profile/bin` (the
plugin also tries the nix default profile, `/run/current-system/sw/bin` and `/usr/bin`). On
Windows the sh launcher uses `USERPROFILE` as home. On Linux/macOS a candidate needs an x bit
(`test -x` in `pyt`, `pyt.ps1`'s `Test-Uv` through `[IO.File]::GetUnixFileMode`, which
PowerShell < 7.3 lacks: then no mode check): a `uv` left without one is skipped. So is a
symbolic link whose target is gone (an uninstalled uv's link: pipx, Homebrew, WinGet's `Links`
folder), which `File.Exists` and `if exist` see as a file: `pyt` asks `test -f`, `pyt.ps1`'s
`Test-Uv` and `pyt.cmd`'s `:opens` open the file (it won, and the run stopped with exit 126;
`test_launcher_win.test_ps1_skips_a_uv_link_whose_target_is_gone`,
`test_a_uv_link_whose_target_is_gone_is_skipped_on_windows`). `pyt.cmd`'s PATH lookup names
only the first `uv.exe` on PATH: after a stale one it goes on with the install folders.

Install hints: Windows prints the PowerShell installer, winget and scoop (never curl); POSIX
prints curl, brew and pipx. `pyt` (stdin and stderr are TTYs) and `pyt.ps1`
(`UserInteractive`, stdin not redirected) offer to run the official installer when `CI` is
unset; `pyt.cmd` never prompts. `test_launcher_sh.test_install_prompt_through_a_terminal`
answers y/n on a pseudo-terminal (a fake `curl` installs a fake uv) for both.

uv exports `UV` (its own path) to everything it starts, so `proc.find_uv` checks `$UV` first,
then `shutil.which("uv")`, else `PytError(..., 3)`.

`PYTEMPLATE_LAUNCHER` values: `sh`, `sh:bash`, `sh:zsh`, `sh:niubash`, each with `:msys` (when
`/usr/bin/msys-2.0.dll` exists) or `:cygwin` (`cygwin1.dll`) appended (`sh:bash:msys`,
`sh:msys` for dash under MSYS2); `cmd`; `ps1:<PSEdition>:<major>.<minor>` (`ps1:Core:7.6`,
`ps1:Desktop:5.1`); `nvim`. `project.native_path` reads the `:msys`/`:cygwin` suffix,
`presets.next_steps` the `ps1:`/`sh:niubash`/`cmd` prefix (how the hint after `new` quotes the
folder and which launcher it names: then `XONSH_VERSION` and `NU_VERSION`, which those shells
export, name xonsh and nushell, behind `cmd` on Windows too, where they type `./pyt.cmd`;
`sh:niubash` wins over an inherited one), and `./pyt doctor` prints the value ("unknown" when
unset).

### 4.2 Which launcher runs

| Caller | Launcher | Why |
|---|---|---|
| sh, bash, zsh, dash, ksh, busybox (Linux, macOS, WSL) | `pyt` | POSIX sh |
| Git Bash, MSYS2 (any MSYSTEM, login or not), Cygwin, busybox-w32, niubash | `pyt` | POSIX sh |
| cmd | `pyt.cmd` | PATHEXT; type `.\pyt` (see rule 1.9) |
| xonsh on Windows, nushell, Python `subprocess`, VS Code tasks on Windows | `pyt.cmd` | can only start PATHEXT files; `./pyt` resolves to `pyt.cmd` |
| PowerShell 7, Windows PowerShell 5.1 | `pyt.ps1` | `./pyt` resolves to `pyt.ps1` in both |
| VS Code tasks on Linux/macOS | `/bin/sh <root>/pyt` | no exec bit needed |
| Neovim plugin | none: runs uv directly | no shell, no cmd.exe; launcher only when uv is nowhere |

PowerShell on Windows: a full path WITHOUT extension (`C:\proj\pyt`) resolves to the
extensionless sh launcher, which Windows hands to its file association (no association: exit 0,
no output). Type `C:\proj\pyt.ps1`. On Linux/macOS the extensionless file is the sh launcher
itself, so pwsh users type `./pyt.ps1`.

The installed launchers (`./pyt install`, section 5.9) sit together in uv's tool bin folder:
`pyt` alone on Linux/macOS; on Windows `pyt`, `pyt.cmd` and `pyt.ps1`, as npm's cmd-shim lays
out its commands. A bare `pyt` then runs `pyt.cmd` in cmd (PATHEXT; cmd never takes an
extensionless file), `pyt.ps1` in PowerShell (its command discovery looks for `pyt.ps1` before
`pyt` plus each PATHEXT extension and the bare name; 7.x folder by folder, 5.1 pattern by
pattern across the whole PATH, so there a `pyt.ps1` anywhere on PATH wins) and the sh launcher
in Git Bash, MSYS2 and the other POSIX shells. Each installed launcher finds the project it runs
in by walking up (its own folder holds none), else the installed template
(`test_install.test_the_installed_pyt_runs_from_cmd_powershell_and_pwsh`, Windows only).

### 4.3 `pyt` (POSIX sh)

Every rule below exists because of a verified failure; `test_launcher_sh.lint` enforces the
header rules (with detector tests proving each rule fires).

- First line `#!/bin/sh`, LF (`.gitattributes`: `pyt text eol=lf`), git mode 100755, ASCII.
  `#!/usr/bin/env bash` is wrong: on Windows xonsh maps it to `bash`, which can be the WSL
  stub. `shells.doctor` checks the shebang, LF, ASCII and the git mode.
- POSIX sh only (dash, bash 3.2, busybox ash, ksh; zsh through `emulate sh`): no arrays,
  `[[ ]]`, `${v//a/b}`, `local`, `$'...'`, `function`. No `set -e` / `set -u` in the file, but
  a caller's (`sh -eu pyt`, niubash with errexit, in-process) must not stop it: `${v:-}`
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
  end the in-process script) as `if UV_PYTHON='' PYTHONHOME='' PYTHONPATH='' UV_WORKING_DIR=.
  "$@"` (errexit-proof; the session keeps its own values), then the launcher unsets
  `PYTEMPLATE_CALLER_CWD`, `PYTEMPLATE_LAUNCHER` and `PYTEMPLATE_GLOBAL` and exits with uv's
  code, so nothing leaks into the calling session. Elsewhere it runs `unset UV_PYTHON
  PYTHONHOME PYTHONPATH UV_WORKING_DIR` and `exec "$@"`. `PYTEMPLATE_GLOBAL` is exported as 1
  for the installed template and unset for a project's runner before either (a stale export of
  the session never reaches a project).
- The runner: `_pt_entry_in` sets `_pt_entry` to `pyt.py`, else `deploy.py` (a project made
  before the launchers were renamed), for the launcher's own folder and each walked-up one;
  `_pt_foreign` and the relative-path folding check that entry. When the walk-up finds nothing
  (and no other user's project), `_pt_installed` names the installed template (section 4.1,
  empty when nothing names it) and `_pt_global=1` goes with it, under the same `_pt_foreign`
  rule.
- Root discovery: the first name the shell gives for this file, zsh `${(%):-%x}` (read through
  `eval` so dash never parses it), else `$BASH_SOURCE`, else `$0`; when its folder holds no
  runner, walk up from `$PWD` (`_pt_walk`), then, when no folder there holds one and the shell
  is no Windows one, from `pwd -P` (section 4.1). Only that first name counts, and it must name a file there
  (`_pt_from_launcher`): in a run inside the calling shell (niubash, a sourced file) `$0` is the
  caller's (`niu` under `-c`, or a script of another project), and its folder was taken for the
  launcher's without the walk-up's ownership rule: at `C:\` a `.pytemplate\pyt.py` any user may
  create ran, and a helper at project A's root running `cd ../B/src && pyt ...` ran A's runner
  (`test_an_in_process_run_never_takes_the_callers_folder_for_its_own`, root only;
  `test_an_in_process_run_runs_the_project_it_is_typed_in`). The walk stops at `/`, `C:` and `C:/`; relative
  candidates (`./`, `../`) are folded against `$PWD`, but the folded path is used only when it
  holds `.pytemplate/pyt.py`: `$PWD` is logical, and below a symlinked folder its `..` is
  not the folder the kernel found `../pyt` in; then the relative path stays (uv resolves it
  physically, like the kernel). A logical parent that is ANOTHER project still wins (fixing
  that needs `test -ef`, which `shellcheck -s sh` rejects, or a `pwd -P` fork). A symlink to
  the launcher (`~/bin/mypyt -> proj/pyt`) is followed to the file it names
  (`_pt_from_launcher`: `[ -h ]`, then `readlink`, at most 40 links; a relative target is
  joined to its link's folder as text and the kernel resolves its `..`; without `readlink` the
  link's own folder stays): the link's folder was taken for the launcher's, exit 2
  (`test_a_launcher_reached_through_a_symlink_finds_its_project`).
- Windows detection (computed first, before any path helper): `OS=Windows_NT` and no
  `WSL_DISTRO_NAME`; `uname -s` only when `OS` is unset. Never `OSTYPE` (niubash fakes
  `msys`). Never trust the output format of `uname` or `cygpath`: in non-login MSYS2 shells on
  the maintainer's machine they resolve to WinuxCmd copies. `_pt_slashes` turns `\` into `/`
  only there: on POSIX a backslash is part of a file name (`/data/a\b/proj` works).
- On Windows the script path and `PYTEMPLATE_CALLER_CWD` are handed over as `C:\...`: `/c/x`
  and `/cygdrive/c/x` are converted in pure sh (drive letter upper-cased); `cygpath -m` runs
  only for paths inside the MSYS/Cygwin root such as `/home` or `/tmp` (the root's own
  `/usr/bin/cygpath` first, then the first one on PATH: a non-login MSYS2 shell has no
  `/usr/bin` on PATH, and WinuxCmd's copy may be there), and only an `X:/...` answer is
  accepted. Without one the POSIX path is handed over unchanged: MSYS converts it for uv.exe
  itself, while the old `\home\x` named a folder on the current drive and every command
  failed (`test_winpath_inside_the_msys_root`).
- `%NAME%` in registry values is expanded from the environment, retrying the upper-case name
  (MSYS2/Cygwin upper-case `SYSTEMROOT`, `PROGRAMFILES`...). Quoted entries
  (`"C:\Program Files\x"`, written by some installers) lose their quotes first. An entry that
  names a variable not defined is skipped (`_pt_expand` returns 1), and so is one that is not
  absolute (`X:\`, `X:/`, a share `\\server`: `_pt_uv_in_list`): a relative one names a folder
  below the current one, a root-relative `\bin` one at the drive root, which any user may create
  (`test_registry_path_skips_relative_entries`, whose shares are `//tmp/...` paths the kernel
  reads as `/tmp/...`).
- Registry lookup: `reg.exe query KEY` WITHOUT `/v` (MSYS rewrites `/v` into `V:/`). It only
  runs when every other lookup failed. The Windows-only helpers are plain sh:
  `test_windows_helpers_in_posix_shells` and `test_registry_path_quoted_entries` run them
  (`_pt_win=1`, a fake `reg.exe`) in 8 POSIX shells.
- Overhead over a bare shell start (Windows, quiet machine, median of 15): Git dash +31 ms,
  MSYS2 dash +40 ms, MSYS2 `bash -lc` +44 ms, Git sh +52 ms, niubash script +78 ms, niubash
  `-c` +124 ms; the registry fallback adds ~80 ms; a project under an MSYS-root path adds two
  `cygpath` calls (60-100 ms). Under heavy machine load expect 5-10x.
- **[template repo]** CI runs `shellcheck -s sh pyt` (the launchers job of `template-ci-image.yml`,
  in the CI image). It
  passes with shellcheck 0.8-0.11 and NO `# shellcheck disable=` directive: keep it so.
- Testing launcher code without writing files: pass it through the environment,
  `sh -c 'eval "$PT_CODE"'` (section 4.7: Cygwin mangles argv). Syntax checks: `dash -n`,
  `bash --posix -n`, `sh -n` (niubash has none, section 4.6).

### 4.4 `pyt.cmd`

- Pure ASCII (cmd reads it in the OEM code page); CRLF (`.gitattributes`: `*.cmd text
  eol=crlf`; labels and `goto` break with LF).
- No `( )` blocks: a PATH that contains `(x86)` closes them early. No delayed expansion. `%*`
  appears only on the uv line (`call` would expand it a second time). No `%` in comments:
  cmd expands them even on `rem` lines.
- Root: `%~dp0` has a trailing backslash and can be wrong when cmd found the file through PATH
  from a quoted name: check `%~dp0.pytemplate\pyt.py` first, else walk up from `%CD%` (the
  start is normalised so a drive root works). A symlink to `pyt.cmd` is not followed (cmd
  cannot read a link): run from outside the project it finds no project. `%CD%` is logical, and
  it is walked only as typed: from a folder reached through a junction into a project the
  project is not found (section 15.2).
- A `UV` variable that names a folder is rejected. The registry is read with
  `reg query KEY /v Path` (cmd has no MSYS rewriting) into a variable (`set "PT_LIST=%%B"`),
  never passed as `call` arguments: a quoted entry (`"C:\Program Files\x"`) would split them
  and leave the FOR set unclosed. The quotes are removed (`%PT_LIST:"=%`), then a child cmd
  expands `REG_EXPAND_SZ` values as Windows does (`for /f` over its `echo`: a command line keeps
  a variable that is not defined as it is; that echo has no redirection, since echo prints the
  blank before one, `%%~L` then kept the closing quote and the FOR set built from the list broke
  with ": was unexpected at this time.", exit 255: no uv from the registry, no install hints;
  `test_cmd_echo_in_a_for_f_command_has_no_redirection`; the quotes a variable brings, a quoted
  `JAVA_HOME`, are removed after it the same way; `test_cmd_finds_uv_in_a_plain_registry_entry`
  runs both on Windows). `call set` did it before, and in a batch file an
  undefined `%JAVA_HOME%` expands to nothing: `%JAVA_HOME%\bin` became `\bin`, a folder of the
  drive root any user may create, and the uv.exe there ran. Each entry then reaches `:try_entry`
  in `PT_E`, never as `call` arguments (call would expand them again, the batch file's way), and
  only an absolute folder is probed (`X:\`, `X:/`, `\\`; `:try_absolute`). Their names start
  with no other label's name (a `call :probe` must never meet a `:probe_x` first, whatever cmd
  makes of a longer label). `test_cmd_registry_path_with_quoted_entries`,
  `test_cmd_never_takes_uv_from_a_registry_entry_that_is_not_absolute` (a fake `reg.cmd` on
  PATH, Windows only).
- The helper variables are cleared on the uv line itself
  (`set "PT_ROOT=" & set "PT_UV=" & set "PT_ENTRY=" & set "PT_GLOBAL=" & "%PT_UV%" run ...`):
  cmd expands the whole line first, so uv still gets their values and the runner sees only the
  `PYTEMPLATE_*` variables (`PYTEMPLATE_GLOBAL` only for the installed template).
- Nothing follows `%*` on the uv line, and the exit code is taken on the next line, `exit /b
  %ERRORLEVEL%` (`test_cmd_takes_the_exit_code_on_the_line_after_uv`): an argument with an odd
  number of double quotes (a `"` that CreateProcess callers write `\"`) swallows the rest of its
  line, which then reached uv as arguments, and a bare `exit /b` after uv on the same line gave
  every `cmd /c` caller (VS Code tasks, Python, xonsh, nushell) exit 0 whatever uv returned
  (measured on the Windows runners).
- cmd reads a batch file one line at a time and opens it again by name, at the byte where the
  last line ended, for each line: deleted while it ran (`pyt uninstall` through the installed
  `pyt.cmd`), it said "The batch file cannot be found." (exit 1, twice), and replaced (`pyt
  install`) it would run what the new file holds at that offset. So `pyt.cmd` names itself for
  the runner, `PYTEMPLATE_LAUNCHER_FILE=%~f0`, and `cmd_install.run_by_cmd` tells the file cmd
  runs for this very run: uninstall handles it last and writes `cmd_install.self_deleting` in its
  place (a file that holds, at the end of the old uv line, `(goto) 2>nul & del "%~f0" &
  "%ComSpec%" /d /c exit %ERRORLEVEL%`: the batch ends, the file goes and the exit code stays
  uv's; started on its own it says what it is, exit 1), and install refuses before any write to
  replace it with other bytes (it names `.\pyt.cmd install` in the clone)
  (`test_launcher_win.test_cmd_goes_on_reading_what_uninstall_leaves`,
  `test_install.test_uninstall_leaves_a_self_deleting_stand_in_for_the_pyt_cmd_cmd_runs`,
  `test_install_never_replaces_the_pyt_cmd_cmd_runs`).
- The installed template (`:installed`, reached when the walk-up hits the drive root):
  `%LOCALAPPDATA%\pytemplate\template\`, else `%USERPROFILE%\AppData\Local\...`; with neither
  variable set it goes straight to `:no_root` (never a path below the current drive root).
- cmd re-parses `%*`: `% ! " ^` and unquoted `& | < >` inside arguments do not survive. Every
  caller that starts a `.cmd` through CreateProcess/`list2cmdline` inherits this (xonsh on
  Windows, Python `subprocess`, VS Code tasks): the BatBadBut class. Never design CLI syntax
  that needs these characters; users pass such values through `./pyt` or `pyt.ps1`.
- Ctrl+C asks "Terminate batch job (Y/N)?" once uv has exited. UNC current directories are not
  supported (cmd.exe replaces them).

### 4.5 `pyt.ps1`

- ASCII, LF, no BOM (`.gitattributes`: `pyt.ps1 text eol=lf`): xonsh and Unix kernels read
  its shebang `#!/usr/bin/env pwsh`, and Windows PowerShell 5.1 reads BOM-less files as ANSI.
  Git mode 100755 so `./pyt.ps1` works from bash/zsh on Linux/macOS (`shells.doctor` only
  notes a wrong mode: PowerShell itself runs it without the x bit).
- No `param()` block: it would turn `-v`, `-h`, `-q` into PowerShell parameters.
- A script gets a typed list (`--supports cpython,mypyc`, `--tests T1,T2`) and an array value
  (`$files`, `(Get-ChildItem -Name)`) alike, as ONE array element; a native program gets the
  list as one argument, its items joined with commas, and the value's items as separate
  arguments. `Get-Typed` tells them apart by the text of the call: the internal
  `InvocationInfo.ScriptPosition` of `Get-PSCallStack`'s frames (read by reflection; no public
  property has it on 5.1) parsed with `Parser.ParseInput`, where a typed list is an
  `ArrayLiteralAst`. It counts the arguments as PowerShell binds them (`-X:v` gives two, a bare
  `--` none), reads a splatted `@args` where that caller was called (a profile function such
  as `function drun { ./pyt.ps1 run @args }`), and lets ONE other splatted variable
  (`@files`, a wrapper's `@rest` copy of `$args`) take the arguments the others leave, as values:
  its arrays pass their items one by one, as a native call splats them (a typed list forwarded
  through such a copy is split, for a native program too; Windows PowerShell 5.1's
  `ConvertFrom-Json` returns a JSON array as one element, which `@l` then splats). The launcher
  joins a typed list and passes the items of a value (and of any other collection,
  `List[string]`: never typed) one by one. What it cannot tell, it treats as a typed list: every
  array when the words do not line up (two splatted variables, a wrapper whose `param()` binds
  some of them) or the text cannot be read (`test_ps1_keeps_typed_comma_lists_whole`, also
  through a wrapper function, `test_launcher_win.PWSH_WRAPPER`; `test_ps1_passes_array_values_like_a_native_call`
  compares every case with a direct native call, the wrapper that splats a copy included).
- A `$null` argument (an unset `$env:X`, an optional variable), the `$null` items of an array
  and a `-X:` whose value is `$null` are dropped, as a native call drops them (`./pyt build
  $backend` with `$backend` unset gave the runner an empty backend); an empty string passes, and
  so does a `List[string]`'s null item, as an empty string (PowerShell 7.3+ native passing:
  `test_ps1_drops_null_arguments_like_a_native_call`).
- A `.ps1` runs inside the caller's session: never assign `$env:PATH`. The three `PYTEMPLATE_*`
  variables (`PYTEMPLATE_GLOBAL` set for the installed template, removed for a project's
  runner) and the removed `UV_PYTHON`, `PYTHONHOME`, `PYTHONPATH` and `UV_WORKING_DIR` (the
  `$names` list) are restored in `finally`; a
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
  Never design CLI syntax that needs `--`; users quote it (`'--'`) or use `pyt.cmd`.
- PowerShell 7.3+ takes ANY native argument equal to `--%` (quoted or splatted too) for its
  stop-parsing token: it drops it, then splits and `%VAR%`-expands the rest. An argument
  `--%` switches that one call to legacy passing (`$PSNativeCommandArgumentPassing =
  'Legacy'` in the script's scope only), whose pre-quoted `"--%"` reaches uv intact.
- A typed `-X:v` reaches a script (and a function's `@args`) as two elements, `'-X:'` marked
  with a hidden `<CommandParameterName>` note, and `v`: the launcher joins them again, as
  PowerShell does for a native program. Limit: `-X: v` (a blank after the colon) arrives as
  `-X:v`. `pwsh -File pyt.ps1 ...` and `./pyt.ps1` typed in bash/zsh (the shebang
  route) are worse: pwsh itself splits every argument starting with `-` at its first colon
  before the script runs (`--x=a:b` -> `--x=a` `b`): from POSIX shells use `./pyt`.
- Pipeline input (`'x' | ./pyt.ps1 run`, `Get-Content f | ./pyt run`) goes to uv's
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
  `pyt.cmd`, exit 126.
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
  `[Environment]::GetEnvironmentVariable` (expands `%VARS%`, and keeps one that is not defined
  as it is), and only its absolute entries count (`$absolute`: `X:\`, `X:/`, `\\`; a relative
  one names a folder below the current one). `Read-Host` is wrapped in `try`.
  A uv that cannot start (a broken download with its x bit) gives exit 126 and ONE line with
  the innermost exception's message: the outer message (and the error record) carry the
  position of the Invoke-Expression call (`test_ps1_uv_that_cannot_start_gives_one_line`).
- Tests: every pyt.ps1 behaviour test runs wherever pwsh is installed (Linux and macOS
  too: the same Core hand-over), plus Windows PowerShell 5.1 on Windows; only the registry
  and cmd tests are Windows-only.
- Root: `$PSScriptRoot`, else walk up from `Get-Location` (its `ProviderPath` when the provider
  is FileSystem, else `[Environment]::CurrentDirectory`), which is also the caller cwd, and off
  Windows, when no folder there holds a project, from its physical folder (`$walked`, `/bin/sh`'s
  `cd -P`/`pwd -P`: the location is logical, section 4.1). A
  symlink to `pyt.ps1` (`$PSCommandPath` a `ReparsePoint` whose `LinkType` is
  `SymbolicLink`) is followed to the file its `Target` names first, at most 40 links; off
  Windows a relative target is joined to the PHYSICAL folder of its link (`/bin/sh`'s
  `cd -P`/`pwd -P`: .NET folds `..` as text) (`test_ps1_reached_through_a_symlink_finds_its_project`).
  `Get-Entry` takes `.pytemplate/pyt.py`, else `.pytemplate/deploy.py`; `Test-Foreign` is the
  ownership rule. No project: the installed template (`$data`: `$env:LOCALAPPDATA` or
  `USERPROFILE\AppData\Local` on Windows, an absolute `$env:XDG_DATA_HOME` or `$env:HOME` (never
  the session's `$HOME`, which does not follow a changed `$env:HOME`) elsewhere), in global mode.
- Execution policy `Restricted`/`AllSigned`, or Mark-of-the-Web on a copy from a downloaded
  zip: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, `Unblock-File .\pyt.ps1`, or
  use `pyt.cmd`. `shells.doctor` reports the policy of 5.1 and 7 separately (`PS_EDITIONS`):
  a blocking one is `[XX]` only for the edition that started the run (`PYTEMPLATE_LAUNCHER`
  `ps1:<PSEdition>:`), else a note: 5.1 is `Restricted` by default on client Windows, and
  users of cmd, the POSIX shells, xonsh or the other PowerShell never run pyt.ps1 there
  (`test_doctor_counts_an_execution_policy_only_for_the_powershell_in_use`). The policy is the
  one a new window gets: `shells._ps_policies` asks without the caller's
  `PSExecutionPolicyPreference`, the Process scope a session started with `-ExecutionPolicy
  Bypass` (the VS Code PowerShell console) hands its children, which won over a Restricted one
  and hid it from doctor and install (`test_ps_policies_answer_what_a_new_window_gets`).
- 5.1 started with `-EncodedCommand` prints the calling session's module-loading progress as
  CLIXML on stderr; the launcher silences only its own.

### 4.6 niubash (1.1.4) quirks, all verified

- Runs `#!/bin/sh` files and nested `sh -c` IN-PROCESS and ignores the shebang. `$0` is the
  caller's (`niu` under `-c`; the temp script `%TEMP%\xonsh-shell-kit\bang-<pid>-<n>.sh` on
  the xonsh `!` route). `$BASH_SOURCE` is correct. This was the original bug:
  `$(dirname "$0")/.pytemplate/pyt.py` pointed into `%TEMP%`.
- Variables, functions and `cd` leak into the calling shell; `exit`/`exec` end only the script.
  Aliases expand inside scripts (oh-my-niu defines `g`, `ga`, `gd`, `gl`, `gp`...).
- `exit` is silently ignored inside `a || exit`, `a && exit` and `{ ...; }`.
- A comment on a `f() {` line is a syntax error.
- `"...$(cmd "$x" 2>&1)..."` keeps the inner quotes in the result.
- `$PWD` looks like `C:/Users/...`, PATH is `;`-separated, `OSTYPE=msys` is fake, `uname -s`
  prints `MSWindows_NT`.
- `bash -n` is WinuxCmd's `bash.exe` and returns 0 even on a broken script: niubash has no
  syntax check, only the behavioural tests cover it.
- A session keeps stale exports. `./pyt` no longer leaves `PYTEMPLATE_*` behind (section
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
  `dash.exe pyt ARGS`) goes through Cygwin's own command-line parser, which mangles `\"`,
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
  (junction, symlink). Otherwise `Path.cwd()`; a deleted cwd raises `PytError`, which only
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

- `./pyt __probe EXIT STDIN(0|1) ARGS...` (`shells.probe`; `cli.main` routes it before
  `_parse_globals`: no config load, no render, not in help). Prints ONE line `PTPROBE{json}`
  to stdout (ASCII JSON with keys `argv`, `cwd`, `caller_cwd_raw`, `caller_cwd`, `launcher`,
  `stdin_tty`, `stdin`, `root`, `python` (the runner's interpreter version), `global` (the
  `PYTEMPLATE_GLOBAL` the launcher handed over: `1` for the installed template, else empty)) and
  exits with EXIT. Launcher tests use it: keep the keys.
  The stdin line is read as BYTES and decoded UTF-8 with surrogateescape whatever the locale:
  a raw non-UTF-8 byte shows up as `\udcXX`, PowerShell's re-encoded text as U+FFFD.
- `./pyt selftest --shells [NAME,...] [--list] [--json] [--keep] [--project DIR]
  [--tests T1,...] [--jobs N] [--timeout S]` (`shells.selftest`; default jobs min(8, CPUs),
  60 s per probe; `-h`/`--help` prints `shells.selftest_help` on stdout, exit 0, as the other
  suites' argparse does: it was `unknown option -h`, exit 2). Discovers the installed shells (Windows: cmd, powershell, pwsh, xonsh,
  niubash, niubash-shx, git-bash/sh/dash, msys2-<msystem> login shells, msys2-shx, msys2-dash,
  Cygwin, busybox-w32, nu, fish, WSL distros; POSIX: sh, bash, dash, zsh, ksh, mksh, yash,
  busybox, fish, nu, pwsh, xonsh; `MSYS2_ROOT`/`CYGWIN_ROOT` point at non-standard installs).
  A NAME prefix selects a family (`msys2` = every `msys2-*`; a second install's shells are
  `msys2-2-*`, `git-2-*`, `cygwin-2`, so they belong to it too). Tests: T1 argv round-trip, T2
  exit code, T3 cwd (from `src/` and from outside), T4 the xonsh-shell-kit route (niubash /
  MSYS2 non-login script mode; the shell's cwd must not change), T5 minimal PATH, T6 stdin, T7
  install hints with uv hidden. Table with ms per test and the launcher each shell reported;
  `--json` to stdout; exit 1 on any FAIL. T7 is SKIP where uv cannot be hidden (PowerShell
  reads the registry PATH itself; the MSYS2 login profile puts `reg.exe` back on PATH). A WSL
  distribution without a uv of its own is SKIP as a whole (`shells._wsl_without_uv`: one probe
  first; the Linux launcher inside it exits 127), never seven FAILs. Ctrl+C, SIGTERM and SIGHUP
  (`e2e.termination_as_interrupt`, as for the other harnesses: their default action killed the
  runner and left its `pts-*` scratch folder behind) kill the running probes (each in a session
  of its own), start no other one (`Context.stopped`, read by `shells.spawn`) and remove the
  scratch folder: `error: interrupted`, exit 130
  (`test_selftest_harness.test_shells_termination_signal_kills_the_probe_and_removes_the_scratch_folder`).
- How the probes reach each shell: POSIX shells get the command in `$PTCMD`
  (`sh -c 'eval "$PTCMD"'`, never in argv: section 4.7), fish `eval $PTCMD`, WSL through
  `WSLENV`, script mode a script file; cmd a hand-built line whose arguments are free of
  `% ! " ^`; PowerShell `-EncodedCommand`; nushell `run-external` with every word a raw string
  (`shells.nu_quote`, the launcher's path too: a `^'<path>'` word ended at a quote of the project
  folder's path, `/home/o'brien`). `shells.child_env` drops `UV`, `VIRTUAL_ENV`,
  `UV_*` selection variables, `PYTHONHOME/PATH`, `PWD`, `OLDPWD` and `PYTEMPLATE_*`. Outputs go
  to files and every probe has a timeout. `--project DIR` probes another copy (to test launcher
  candidates before they land).
- `shell-setup` (it printed a `pyt` function or alias per shell) was removed (owner decision,
  September 2026): `pyt install` puts the launchers themselves on PATH. Its pwsh function's
  coverage of `@args` forwarding lives on in `test_launcher_win.PWSH_WRAPPER`.
- `shells.doctor(check, in_project=True)` (from `./pyt doctor`): step "launchers": the
  launcher that started the run (`_started_by`), then `pyt` (`#!/bin/sh`, LF, ASCII, git mode
  100755, exec bit on POSIX), `pyt.cmd` (CRLF, ASCII) and `pyt.ps1` (LF, ASCII, no BOM; a mode
  other than 100755 is only a note), each problem with the command that fixes it. Step "shell"
  (printed only on Windows and in WSL, the only places it has lines): `bash` = WSL stub note, the
  execution policy of 5.1 and 7; WSL info. Outside a project (`in_project=False`, global mode:
  `cmd_env._machine`) only the launcher line and the Windows shell checks: the installed
  template's own launcher files are no project's, and the WSL line is about a checkout.

## 5. Runner architecture

### 5.1 Modules (`.pytemplate/runner/`)

| Module | Responsibility |
|---|---|
| `pyt.py` (one level up) | Stops with exit 3 and one `error:` line (no traceback) when uv started it on Python < 3.11 (a `uv run` by hand with an old `UV_PYTHON`, a `.venv` made by hand with an older Python), BEFORE importing the runner; reconfigures stdout/stderr to UTF-8; in global mode (the same rule as `project.detect_global`) sends the bytecode cache of the runner's modules to `<user cache>/pytemplate/pycache` (`sys.pycache_prefix`; `_user_cache`: `LOCALAPPDATA`, an absolute `XDG_CACHE_HOME`, `~/.cache`; none: `sys.dont_write_bytecode`), so nothing is written into the installed template; puts its own dir on `sys.path`, calls `runner.cli.main(argv, entry=True)` (the restart on `python.cpython`, section 5.2). |
| `cli.py` | `COMMANDS` table of `Command(module, func, summary, usage, render, group)`, modules imported lazily; `FORWARDS` / `HELP_PASSES_THROUGH` (section 5.2); `INTERNAL` (routes listed nowhere: `__init`); global mode: `GLOBAL_COMMANDS`, `GLOBAL_SUMMARIES`, `_dispatch_outside_a_project`, `_outside_a_project`/`_unknown_outside`/`INIT_OUTSIDE` (the exit-2 messages), `_help_outside_a_project`, `_prog`. `_parse_globals`, `dispatch` (also the exit-2 hint of the removed `init`), `main`/`_main` (exception -> exit code, closed stdout), the restart on `python.cpython` (`RUNS_ON_ANY_PYTHON`, `_started_by_uv`, `_python_needed`, `_restart`: section 5.2), `cmd_help` (commands and `[tasks]` entries), `cmd_tasks`, `cmd_selftest` (plain, `--shells`, `--nvim`, `--e2e`), the `__probe` route, `EXAMPLES`. |
| `config.py` | Dataclass schema (`SCHEMA`, `DEFAULT_METHODS`), `read_text` (UTF-8 only, clear error otherwise), strict loader (`_build`: unknown key or wrong type -> error with the full key path), `validate`, derived values (`pkg`, `min_python`, `pypy_minor`, `profile_for`, `pypy_enabled`), `compiled_paths` (`import_path`), comment-preserving editor `set_value` / `update_file` (section 6.1), its TOML statement scanner `scan` (render reads pyproject.toml with it, 6.3), `toml_value`. |
| `project.py` | Paths (`ROOT`, `SRC`, `BUILD`, `DIST`, `TEMPLATES`, `PRESETS`...), `GLOBAL` (`detect_global`, `INSTALL_RECORD`: section 5.2), `IS_WINDOWS/IS_MACOS/IS_WSL` (`detect_wsl`: `wsl_kernel`, `windows_checkout`), `ENV_SUFFIX`, `venv_python`, `launcher_python`/`ANY_RUNNER_PYTHON` (the Python the launchers start the runner on: section 4.1), `host_os/host_arch` (uv names), `rel`, `code_dirs`, `native_path`, `find_cygpath`, `caller_cwd`, `user_path`, `scratch_name`, `check_private_dir` and `make_private_dir` (the harnesses' scratch folders: checked before and once made), `write_whole` (a file rewritten through a temporary file and `os.replace`, never half-written, owner and hard links kept: `_give_owner`, `_write_in_place`). |
| `ui.py` | All runner output to stderr; `PytError(msg, code)`; `VERBOSE/QUIET`; `report` (never hidden by `-q`); colours (`color_enabled`, `enable_vt_mode`); `check_line` (doctor lines `[ok]`, `[XX]`, `[--]`). |
| `proc.py` | `find_uv`, `base_env` (`UV_SELECTION`, no `PYTEMPLATE_GLOBAL`), `run` (echo, `DRY_RUN`, cwd defaults to `ROOT` and must be a folder, UTF-8 capture, waits through Ctrl+C and passes SIGTERM/SIGHUP on), `output`, `show` (display quoting only), `exit_code` (signal N -> 128+N), `vs_installer_dir`, `CommandFailed`, `Interrupted`; a second runner: `runner_argv` (the launchers' uv call), `runner_env` (cli._restart's). |
| `envs.py` | `PyEnv(key, dir, request, preference)`; `cpython_env`, `pypy_env`, `tool_env` (always CPython), `runtime_env(backend)`, `env_vars`, `uv`, `uv_run` (= `uv run --locked`, plus `--project <ROOT>` when `cwd` is not the root: section 7), `sync` (every group but those `left_out` names), `interpreter_info` (with `platform` and `cc`); python.cpython's interpreter (`find_cpython`, `cpython_downloads`, `ensure_python`, `no_download_problem`, `this_platform`, `RUNS_ON_ANY_PYTHON`: section 5.2); `MIN_UV`, `uv_version`, `uv_problem`, `require_min_uv`, `UV_UPDATE`, `uv_error` (uv's `error:` message). |
| `render.py` | Every generated file (`outputs`), hand-edit detection (`apply`, `auto`), typing profiles (`load_profile`), `mypy_ini`, `mypy_cli_args`, `pyright_config`, `ruff_config`, `to_toml`, `jsonc`, `ci_workflow`, managed pyproject parts (`managed_block`, `write_pyproject`, `pyproject_outdated`, `check_pyproject`). |
| `editors/vscode.py` | `.vscode/settings.json`, `extensions.json`, `launch.json`, `tasks.json` (`catalog`, `scan`, `problem_matchers`; section 12.1). |
| `editors/nvim.py` | `.lazy.lua` (verbatim template copy) and `.pytemplate/editor.json` (`editor_data`; section 12.2). |
| `presets.py` | Preset discovery/loading (`load`: a broken `preset.toml` is a `PytError` naming it), option merge, `uv_extras`, `dependencies`, `skeleton`, `pristine`, name rules (`APP_NAME` and `NAME_RULE`, defined in `config`; `name_from_folder`, `check_name_free` (`IMPORT_NAMES`, `_installed_import_names`, `INTERPRETER_COMMANDS`), `locked_names`), tested pins (`constraints`, `constraints_text`), `plan_init` + `init` (run by `./pyt __init`; with rollback), `copy_template` (`_tracked_template`: git's tracked files, or every file of the installed template, `_installed`, never its `INSTALL_RECORD`; `_copy_entry` per entry, `_raise_copy_errors`), `new` (`next_steps`); for apply and rename: `default_options`, `option_dependencies` (the requirements with an `{option}`), `set_project_name` (checked `_set_project_name`) / `project_name` (the `[project]` table only), `shadows_stdlib` (`STDLIB_OTHER_VERSIONS`). |
| `mypyc.py` | `compiled_sources`, incremental stage (`sync_tree`, `remove_stale_extensions`; `copy_writable`, `make_writable`, `remove_tree` for every scratch copy of the app), `spec.json` + `COMPILED_STAMP` (+ `COMPILER_ENV`), spawning `tools/mypyc_build.py` (`MYPYC_REJECTED`, `COMPILER_MISSING`), `ANNOTATE_HTML`, `hidden_imports` (+ `importable`), `exe_stage`, `runtime_env_vars`, `has_compiler_hint`. |
| `imports.py` | AST import extraction that skips `if TYPE_CHECKING:` blocks (`imports_of`, `iter_runtime_nodes`); parses bytes (tolerates a BOM); `parse_error`, `local_module`, `is_local`. |
| `lintc.py` | Extra AST rules for compiled modules (section 9): `lint_file(cfg, path)`, `lint`, `Finding`, `NATIVE_CLASS_DECORATORS`, `relative_file_at_import`. |
| `tasks.py` | `[tasks]`: `Placeholders` (lazy `{python}`), `deps` (each once per invocation), cycle detection, `run_task`, `describe`, `list_tasks`. |
| `cmd_env.py` | `setup` (= `cmd_apply.apply(command="setup")`), `doctor` (`_tools`, then `_project` in a project, which calls `cmd_apply.doctor`, or `_machine` outside one, then the steps of both modes: `cmd_nvim.doctor`), `sync`, `lock`, `add`, `remove`, `clean` (`_env_dirs`, `_remove`, `_is_link`); `ensure_lock`; `_fix_exec_bit`; `_c_compiler` (the one setuptools runs: `$CC`, else the `.venv` Python's sysconfig CC), `_msvc(platform)`, `_xcode_problem`, `_long_paths`. |
| `cmd_apply.py` | `./pyt apply [--force]` / `setup [--force]` (section 5.8): `make_plan` (every refusal before the first write), `apply`, `_print_plan` (--dry-run), the `applied` record (`load_record`, `save_record`, `trusted_record`, `project_record`, `record_of`, `rename_record`), `state_file`, `applied_state` / `_infer_preset` (the record, else `_traced`: a preset's traces in pyproject.toml, `_block_options`/`_unformat`: the options the managed block was written with, `_marks`: extra tables) / `applied_name` / `_other_package`, `dependency_changes` (`DepChanges`, `req_key`), `read_project`, `pending` + `doctor` (changes not applied yet), `reference_problems`, `unused_envs`, `_restore`. |
| `cmd_mode.py` | `mode` (+ the Python 3.11 precheck before enabling PyPy), `render`, `new`, the internal `__init` (`cmd_init`), and their `--dry-run` planners (`_plan_mode`, `_plan_init`). |
| `cmd_dev.py` | `run`, `compile`, `check` (`run_checks`), `lint`, `fmt`, `test` (`test_backend`, `stage_pythonpath`), `report`; `split_backend`; `only_flags`; `_profile_file` and `config_arg` (ruff's `--config`, relative to ROOT: 6.2); `BASEDPYRIGHT`, `BASEDPYRIGHT_NODE`. |
| `cmd_build.py` | `build`: backend + method resolution, `COMPAT`, `check_lock`, `payload`, `BuildRequest`, `dist_path`; `pyz-merge`. |
| `methods/*.py` | One `build(req: BuildRequest) -> Path` per method; `common.py` has target keys (`parse_key`, `check_key`, `targets_for`), `UV_PLATFORMS`/`host_floor`, `ensure_env`, `export_requirements`, `install_deps`, `drop_install_junk`, `has_native`, `skipped_requirements`, `copy_app`, `uses_tkinter`, `windowed`, `tree_bytes`; `nuitka.NUITKA`/`NUITKA_PYTHON`, `check_python`, `check_options`, `optimization_args` (`[deploy.nuitka]` lto/pgo); `pyz.check_parts`, `merge`. |
| `shells.py` | `__probe`, launcher/shell doctor checks (`ps_policies`: the PowerShell execution policies, asked once per run; `BLOCKING_POLICIES`), `selftest --shells` (section 4.9). |
| `cmd_nvim.py` | `./pyt nvim ...` and `doctor(check)` (section 12.2). |
| `nvimtest.py` | `selftest --nvim` (section 13.1). |
| `e2e.py` | `selftest --e2e` (section 13.1). |
| `mutation.py` | `selftest --mutation` (section 13.1): `OPERATORS`, `test_map`/`ordered`/`junit_seconds`, `changed_lines`/`parse_diff`, `select`/`skipped_spans`/`handler_classes`, `own_mutant` (ExceptionReplacer's), `made` (what is skipped), `classify`, `Driver` (tools/mutation_cr.py), `list_mutants` (the snapshot), the workers (`make_copy`, `sync_copy`, `worker_env`, `set_mtime`, `Runs`, `kill_run`/`descendants`, `run_all`), `Report`/`print_report`, `deferred_interrupts`, the base (`default_base`, `prepare_base`, `base_lock`). |
| `hooks.py` | `./pyt hooks [install [--force]\|uninstall\|run\|status]`, `ensure_installed` (apply/setup), `doctor`: the native git pre-commit hook (section 5.6); `find_repo` (`NotInGit`), `classify` (`runs_checks`), `hook_state`/`own_local` (a copy chained after another project's hook), `hook_script`/`launcher_of`, `install`/`uninstall` (apply removes the hook when `hooks.pre_commit = false`), `chain_hint`/`chain_advice`, `hooks_path_runner`, `checks`. |
| `cmd_install.py` | `./pyt install` / `uninstall` and doctor's "pyt install" step (section 5.9): where things go (`data_home`, `snapshot_dir`, `bin_dir`), `MARKER`/`is_launcher`/`not_ours`/`not_an_install`, the record (`read_record`, `recorded_bin`, `source_state`, `describe`, `age`), PATH (`on_path`, `path_state`, `path_problem`, `first_pyt`, `shadowing`), Windows (`pathext_shadows`, `policy_notes`), `make_plan` (every refusal before the first write, in one message: `_refuse`), `_Swap` + `install` (all or nothing; `_new_folder`, `_terminations_interrupt`), `_remove_earlier`, `remove_leftovers`, `remove_installed`, `cmd_uninstall`, `doctor`. |
| `rename.py` | `./pyt rename NEW_NAME [--force]` and the rename step of apply: pure `plan` / `apply_plan` (undoes itself when a write fails) / `rewrite` (tokenizer + `ast` scopes + context rules, `MODULE_KEYS`), `check_new_name` (`locked_names`), `git_changes`, `dirty_tree_message`, `validate_config`, `tidy_before`/`tidy_after` (ruff, `Tidy`), `report`, `cmd_rename` (section 5.7). |
| `upx.py` | Optional UPX packing: pinned download (`VERSION`, `ASSETS` with SHA-256), `locate`, `find`, `uses`, `preflight` (from `cmd_build`, before any work), `active`, `level_flags`, `env_value`, `excludes`, `candidates`, `pack_file`, `pack_tree`, `MAX_INPUT` (section 10). |

### 5.2 Call flow

1. Launcher -> `uv run --quiet --python=REQUEST --python-preference PREF --script
   .pytemplate/pyt.py ARGS` (section 4.1: no request and `only-managed` while the project has an
   environment, so uv reads the project's `.python-version`, `python.cpython`, found from the
   script's folder upward whatever the caller's folder or a `.python-version` there says; else
   any CPython 3.11+, a system one too; the caller's `UV_PYTHON`, `UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON`, `PYTHONHOME`,
   `PYTHONPATH` and `UV_WORKING_DIR` removed)
   (`test_launcher_sh.test_runner_runs_on_python_cpython_whatever_the_caller_pins`); uv runs
   the runner in a cached environment of its own (`VIRTUAL_ENV`) and exports `UV`. The runner
   must stay 3.11 code (the PEP 723 floor) and work on newer versions.
   Then the runner itself (`pyt.py` calls `cli.main(argv, entry=True)`; `cli._main` runs
   `_restart` after `_parse_globals`): the commands of `cli.RUNS_ON_ANY_PYTHON` (`help`,
   `doctor`, `new`, `install`, `uninstall` as builtins, the internal `__init`), a builtin's `-h`
   (its help), a name that is no command (dispatch says so), global mode, and a pytemplate.toml
   that does not read or names no valid `python.cpython` (dispatch reports it) run on that
   Python (`cli._python_needed`, which reads pytemplate.toml with tomllib, never config.load:
   its warnings would print twice). Any other command or `[tasks]` entry (a task named `install`
   too) runs on `python.cpython`, the CPython uv manages, never another one of that minor
   (`cli._runs_on_python_cpython`: with the project's environment the launchers asked uv for its
   own, `only-managed`; without it `envs.find_cpython` names uv's, compared with `sys.base_prefix`,
   so Fedora's or Homebrew's 3.14, or Termux's 3.13 for a `python.cpython` of "3.13", never runs
   the command). Started by uv on another Python
   (`cli._started_by_uv`: `VIRTUAL_ENV` is this runner's own environment), the runner asks
   `envs.ensure_python` for it (`uv python find --system`, else `uv python install --no-bin
   --no-registry`, else PytError 3: `envs.no_download_problem` for a platform or version uv has
   no build of, the network or `UV_PYTHON_DOWNLOADS=never` for a failed install) and runs
   `<python.cpython> -s [-B] .pytemplate/pyt.py ARGS` (the whole command line, global options
   included) through `proc.run` (`echo=False`: it runs under `--dry-run` too, which the second
   runner then does; `-v` shows it), in the caller's folder, with `proc.runner_env()` (the
   launcher's environment without this runner's `VIRTUAL_ENV` and bin/ on PATH: the second
   runner is not "started by uv", so it never restarts again). Its exit code is this run's; a
   Ctrl+C or SIGTERM it got too is reported by it alone (`proc.Interrupted` -> its code, 130 for
   `STATUS_CONTROL_C_EXIT`). A caller in the same process (the tests) calls `main(argv)` and
   never restarts, and so does a runner started by hand on a Python (`python pyt.py ...`, the
   test harnesses' `sys.executable`), so the python-floor CI job keeps running project
   commands on 3.11 (`test_cli_core.test_a_runner_started_on_another_python_moves_project_commands`
   does it for real through uv).
2. `cli.main`: `__probe` short-circuit, then `_parse_globals` (global flags must come BEFORE the
   command: `-v/--verbose`, `-q/--quiet`, `--dry-run`, `--no-render`, `-h/--help`; `-h` keeps
   the command: `./pyt -h run` = `help run`; `--dry-run` switches `-q` off), then
   `dispatch`. Typed after the command they are its arguments: refused by most commands (`build`
   names them, `cmd_build.GLOBAL_FLAGS`), passed on by those of `cli.FORWARDS` and a task with a
   `cmd`, whose command then runs for real (`./pyt run --dry-run` runs the app); README's Commands
   section says so (`test_docs.test_the_manual_says_where_a_global_option_typed_after_the_command_goes`:
   it said an error). A dry run still gets `python.cpython` (`envs.ensure_python` in `_restart`).
3. `dispatch`: `help` needs no config. `-h`/`--help` anywhere after a builtin prints `help
   COMMAND` (no config load, nothing runs), except after `cli.HELP_PASSES_THROUGH` (`run`,
   `test`, `lock`, `selftest`), where it goes to the app, pytest, uv or the suite. Otherwise
   `config.load(set(COMMANDS))` (validates; a task may not take the name of a builtin of the
   contract, `config.CONTRACT_COMMANDS`: rule 1.11's first version; one a later builtin took,
   `install` or `uninstall`, stays the project's: dispatch runs the task, `-h` goes to it and
   `help NAME` describes it (`cli._task_named_like`), and the builtin runs outside the project;
   `./pyt shell-setup` says what replaced it, `config.RETIRED_COMMANDS`, and so does `help
   shell-setup`, in a project and outside one: it said `unknown command`). Builtins run
   `render.auto(cfg)` first when `Command.render` is true and `--no-render` is not set, then
   `module.func(cfg, args)`. Names in `[tasks]` run `render.auto` and
   `tasks.run_task(cfg, name, args, dispatch)`; `-h` after a deps-only task prints its help.
   `help NAME` also describes a `[tasks]` entry (cmd, deps, backend, cwd, env); an unknown
   NAME, or a second one, exits 2. `cli.INTERNAL` routes (`__init`) dispatch like builtins but are
   listed nowhere (help, `editor.json`, the editors' task lists and their completion read
   `COMMANDS` only). `init` is no longer a command: unless a `[tasks]` entry took the name, it
   exits 2 with the hint `./pyt new DIR --preset P` (`cli.INIT_REMOVED`; `help init` too).
   Global mode (item 6) takes another path before any of this.
4. Commands with `render=False`: `clean`, `render`, `new`, `install`, `uninstall`, `pyz-merge`,
   `tasks`, `selftest`, `help`, `hooks` (the hook must not rewrite generated files in
   the middle of a commit), `apply` and `setup` (they render at the end: a refused apply, e.g.
   a hand-edited `app.preset`, writes nothing), `rename` (renders after its checks, never with
   a hand-edited `app.name` before the dirty-tree check), and the internal `__init` (it renders
   with `--force` at its end; rendering the copied configuration first, `new` warned about the
   source project's hand-edited files). `test_cli_core.NEVER_RENDER` pins this list.
5. Commands reject unknown arguments with exit 2 (a typo is never silently ignored):
   argparse commands, `render`/`mode`/`__init`/`new` (`cmd_mode._parse`: an unknown option is
   named before argparse runs, which bound the value after it to a positional: `mode --typ
   strict` said only "argument backend: invalid choice: 'strict'"), `lint`, `fmt`,
   `clean`, `apply`, `setup` (only `--force`), `doctor`, `tasks`, `install`, `uninstall`
   (`cmd_dev.only_flags`), `check` and `sync` (extra
   positionals), `help`, a `[tasks]` entry without `cmd`. By design
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
6. Global mode (`project.GLOBAL`, `project.detect_global`): `pyt install` copies the template
   into a folder of the user's (its snapshot: POSIX `$XDG_DATA_HOME/pytemplate/template`, an
   absolute XDG_DATA_HOME only, else `~/.local/share/pytemplate/template`; Windows
   `%LOCALAPPDATA%\pytemplate\template`) with the template repository's marker, README.md and
   LICENSE and no `.git`, records the install in `.pytemplate/installed.json`
   (`project.INSTALL_RECORD`) and puts the launchers in `uv tool dir --bin`. Launchers that find
   no project run the snapshot's `pyt.py` with `PYTEMPLATE_GLOBAL=1`: `project.ROOT` is the
   snapshot. The record alone also makes the mode (a command typed inside the snapshot's folder,
   its launcher run by its path): the snapshot is never a project. `dispatch` then goes to
   `cli._dispatch_outside_a_project` before anything else: only `cli.GLOBAL_COMMANDS` run (`new`,
   `doctor`, `help`, `install`, `uninstall`; `__probe` too, routed in `main`; `install` only says
   how to install or update pyt from a clone, `cmd_install._refuse_elsewhere`, and help says so:
   `cli.GLOBAL_SUMMARIES`), with the global
   options, the snapshot's `pytemplate.toml` loaded as the source of `new` (its name checks read
   the snapshot's lock, as `new` from the template repository does), never `render.auto`. Any
   other builtin or internal route exits 2 with "`pyt X` needs a project: run it in a project
   folder (any subfolder works), or create one: pyt new DIR [--preset P]" (`cli._outside_a_project`);
   any other name (a project's `[tasks]` entry, a typo) is `unknown command` (the snapshot's own
   `[tasks]` are the template's and are never read); `init` has its own hint (`INIT_OUTSIDE`).
   `help` lists what runs there and the project commands (`_help_outside_a_project`), `help X`
   and `X -h` (after `run`/`test`/`lock`/`selftest` too) print X's usage, prefixed `pyt`, plus
   "Needs a project" where it does; so do the usage errors of `new` (its argparse `prog` is
   `cli._prog()`'s: they said `./pyt new`). Nothing is written into the snapshot: `pyt.py` sends the
   runner's bytecode cache to the user's cache folder (5.1), `new` copies it (every file, no git
   asked, no note: `presets._installed`; never the install record: `presets._skipped`; the
   snapshot's entries one by one, so the project's folder never takes the snapshot's own mode
   and times, as copytree's copystat of the root gave it: 0700, a setgid folder's bit lost, EPERM
   in a folder the user does not own) and runs
   the copy's `__init` as a project (`proc.base_env` drops `PYTEMPLATE_GLOBAL`, and so do
   `shells.child_env`, `e2e.scrub_env` and `nvimtest.runner_env`), `doctor` checks the machine
   only (`cmd_env._machine`: git, the C compiler of mypyc from the runner's own sysconfig, the
   launcher of the run and the Windows shell checks, all notes; `cmd_nvim.doctor` without the
   `.lazy.lua` trust; only an old uv fails it). `test_global.py` runs each allowed command in a
   fake snapshot and compares it byte for byte.

### 5.3 Exit codes and output

- 0 ok; 1 = check/test failures, doctor problems, any FAIL in a selftest suite, or an internal
  runner error (traceback printed); 2 = usage/config (`PytError` default, argparse; also a
  program that cannot be started: no exec bit, no `#!` line, a folder, on Windows a file
  CreateProcess cannot start, a `.sh` or `.py` (WinError 193: `proc.WINDOWS_START_HINT`, never
  the `#!` hint); a working folder that does not exist, or that the child cannot enter
  (`proc._start_error`: the error's filename is the cwd on POSIX, ERROR_DIRECTORY on Windows; it
  blamed the program, `test_cli_core.test_a_working_folder_it_cannot_enter_is_named`); bad
  `[tasks]` entries; a file under `.build/` or `dist/` it may not write, left
  by another user: `cli._scratch_denied`; a command that needs a project, typed outside one:
  global mode, 5.2); 3 = missing requirement (uv, a uv older than
  `envs.MIN_UV`, a program (or the interpreter a script's `#!` line names: `proc._not_found`;
  a `#!` line that ends in a carriage return, a CRLF checkout used from WSL, is named as that,
  never as a missing `/bin/sh`: `test_cli_core.test_a_script_with_windows_line_endings_says_so`),
  a C compiler mypyc cannot start, an interpreter, Neovim/git with `--require`, the runner
  itself started on Python < 3.11 by `pyt.py`'s check; what uv itself reports missing, an
  interpreter it can neither find nor download or a program `uv run` cannot spawn (a `uv =
  true` task), ends with uv's code, 2: the runner streams uv's output and cannot tell that
  error from uv's others, section 15.2); 130 = Ctrl+C; 143/129 = a SIGTERM/
  SIGHUP sent to the runner (POSIX);
  141 = the reader of stdout went away (`./pyt help | head -1`: quiet, no traceback; POSIX
  only, Windows reports a closed pipe as `OSError` EINVAL, unhandled). A write that finds no
  room (`./pyt help > /dev/full`, a full disk, a quota: `cli.NO_ROOM`, ENOSPC, EDQUOT, EFBIG)
  is one `error:` line naming the file when the error does, exit 1, never an internal error
  (quiet when stderr has no room either). `run`, `test BACKEND`
  (pytest's own code: 5 = no tests collected, 4 = usage error) and tasks return the child's
  exit code (`test all`: 0 or 1, after testing every backend even when one fails to build);
  `proc.CommandFailed` carries the failed child's code. A child killed by signal N gives
  128 + N (`proc.exit_code`, like sh and uv; `cli.main` also maps a negative code a command
  returns), never 256 - N.
- Ctrl+C (`proc._wait_through_signals`): the child got the same Ctrl+C (terminal process group,
  console), so `proc.run` (a `subprocess.Popen` it waits for itself) waits for it instead of
  letting `subprocess.run` SIGKILL it 0.25 s later (an app's cleanup was cut short; under `uv
  run` the app kept running as an orphan). A no-op Python handler records the Ctrl+C, only in
  the main thread and only while SIGINT has its default handler (a runner started with SIGINT
  ignored keeps passing SIG_IGN on). Then `proc.Interrupted` (a KeyboardInterrupt) stops the
  command, so `check`, `test all` and task deps never go on to the next step; `cli.main` prints
  `error: interrupted` and exits with the child's code, or 130 when it exited 0 (an
  interrupted command never reports success) or died of the Ctrl+C (`STATUS_CONTROL_C_EXIT` on
  Windows). A child that ignores SIGINT is waited for, like `uv run` does. Ctrl+C in the
  runner's own Python code: KeyboardInterrupt, 130.
- SIGTERM and SIGHUP (POSIX; `proc._Waiter.forward`): usually sent to the runner alone (`kill
  PID`, a supervisor, `docker stop`, `Popen.terminate()`, which `uv run --script` passes on to
  the runner). Their default action killed the runner at once and left uv and the app running
  as orphans that never got the signal. While a child runs they are passed on to it, like `uv
  run` does, and the child is waited for; then `proc.Interrupted(code, signum)` stops the
  command like Ctrl+C: `error: terminated (SIGTERM)`, the child's code, or 128 + N when it
  exited 0. Same conditions (main thread, default handler: `nohup` keeps SIG_IGN). A signal
  sent to the whole process group (`kill -TERM -PGID`) reaches the app more than once: from
  the group, and again from each process between that passes it on (measured: 3 times through
  `./pyt` and the inner `uv run`, twice under a plain `uv run`). Not fixable here: a signal's
  sender does not say whether it hit the group, and a child in a process group of its own
  would lose the terminal's Ctrl+C. Outside a child (in-process work) the default action still
  ends the runner.
- Runner output goes to stderr through `ui` so the app keeps stdout. Exceptions, printed to
  stdout on purpose: `help`, `__probe`, and the `--json` reports of
  `selftest --shells` (also with `--list`), `selftest --e2e` and `selftest --mutation`.
- `-q` hides progress (`ui.step`, `ui.command`, `ui.ok`, `ui.info`), never what was asked for:
  `ui.report` (the `tasks` list, the stderr of a failed query, the `selftest --shells` list,
  result table, failure/skip lines and the scratch folder `--keep` kept, the `selftest --nvim`
  table with each FAIL's reason, its SKIP lines, pins and logs folder: `nvimtest._table`, the
  `selftest --e2e` table, each failed step with its log tail and path, and the kept base, the
  `selftest --mutation` results (`mutation.print_report`: the counts per file, the score, every
  survivor and error with its change, a failed baseline, and the logs folder it kept), the
  tool output and hint of a pre-commit check that did not pass: `hooks._print`, what
  `rename` and `apply` leave to review: the lines left unchanged and the other files that
  mention the old name, and why a captured step failed: mypyc's errors and the C compiler's
  (`mypyc.build`), the wheel's `uv build` (`wheel.build` captures it under `-q`: uv's `--quiet`
  dropped the build backend's output), a portable smoke run, the PyPy precheck's mypy abort;
  they said "fix the errors above" with nothing above,
  `test_mypyc_core.test_build_shows_why_it_failed_even_with_q`), warnings, errors and
  `check_line` always print, and a dry run ignores `-q` (its output is the plan). Results are
  `ui.report` too, so `-q` keeps them: the `mode` display (`cmd_mode._describe`; only its
  `ui.step` header goes), the paths `render` lists (`cmd_mode.cmd_render`: outdated, updated,
  would update) and the `render --diff` output (`render.apply`). uv's own progress
  (`Resolved`, `Installed`, `Checked`...) is progress too: under `-q`, `envs.uv` passes
  `--quiet` to every uv call whose output reaches the terminal (the output of what `uv run`
  starts is untouched; basedpyright's install step, which echoes no command line, too), never
  to a captured query the runner reads. uv's `--quiet` also hides uv's warnings and change
  summaries (uv has no level that keeps them; errors still print), so the uv commands the user
  drives with their own arguments keep their whole output (`envs.uv(..., quiet=False)`):
  `./pyt lock ARGS` (`Updated rich v14 -> v15`, what `--dry-run` would change) and the `uv
  add|remove` step of `add`/`remove` (`does not have an extra named ...`); their sync stays
  quiet. Limit: a uv warning of a quiet step (a deprecated setting in pyproject.toml) shows
  only without `-q`.
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
- `selftest --shells|--nvim|--e2e|--mutation` refuse `--dry-run` (exit 2): they start shells,
  Neovim, `./pyt` and pytest runs themselves, in their own scratch folders.
- `render.apply` behaves like `--check` (writes nothing); `render.auto` prints "would update";
  `render` prints `would update: ...`.
- `clean` prints `would remove X` per target. `build` validates its arguments, the pyz target
  keys, the Nuitka pin and PGO rules (`nuitka.check_python`, `check_options`), the flet preset
  and Developer Mode (`flet.check_options`), the portable launchers' env values
  (`portable.check`), the lock (`cmd_build.check_lock`: a read-only `uv lock --check`, a stale
  uv.lock fails) and the UPX binary (`upx.preflight`: a missing `deploy.upx.path` fails) as
  a real build does, prints the checks (unless `--no-check`) and `(--dry-run) build B -> M:
  would output dist/<name>-<b>-<m>*` (nuitka: also `Nuitka options: --lto=... [--pgo-c ...]
  <extras>`; flet: `flet build <target>` and, for a mobile or web target, the markers and pins
  the build project gets instead of uv.lock's, `flet.plan_lines`; a method that packs with UPX on this host: `upx: <path>` or `upx: would download
  <url> into <cache>` (`... if the build holds a binary to pack` for a `runtime = "system"`
  portable build), never the download), then stops. `report` builds nothing and never opens
  the browser. No success line for a skipped step: `compile`, `report` and `check` print
  `(--dry-run) would ...`/`were not run` instead of their `ok` lines, and `test all` no summary
  of `[ok]` rows. A check that fails in a dry run still fails it: `test all` lists the backends
  that failed theirs (a `compile.exclude` naming nothing) and exits 1, as a real run does.
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
  (format, `check_name_free`), fails where uv cannot install the new project's `python.cpython`
  as the real run does (`envs.no_download_problem`), and prints destination, preset, name, that
  Python (where it is, or that uv would install it), what it would copy (the
  test `copy_template` makes, `presets.copy_scope`: the files git tracks, or every file and
  why), the number of pins `__init` would pass (the ones this `uv.lock` lacks, as `plan_init` counts them) and the
  `__init` step it would run in the copy, then `git init -b main`, or why not (DIR inside the
  work tree of another repository that does not ignore it, `cmd_mode._work_tree_top`, with the
  CI warning of section 13.2; no git). `pyz-merge` validates its inputs (`pyz.check_parts`:
  valid `_pyz.json`, one app, one build, the platform of a pure part next to per-platform ones) and prints inputs and outputs (the `.pyz` and its
  `.cmd`).
- `nvim trust`, `extras`, `bootstrap` and `sync` print what they would do.
- `rename` runs the real checks (a dirty git tree is only a warning) and prints the move, each
  file with its reference count and sample lines, the pytemplate/pyproject lines, the lines left
  unchanged, the other files that mention the old name, what happens to `uv.lock`
  (`rename._lock_forecast`: a new normalized project name or changed managed parts re-lock;
  otherwise a read-only `uv lock --check` in the project, as the real run's `ensure_lock` asks:
  a stale lock is re-locked even by a case-only rename) and the generated files it would
  re-render (`render.apply(new_cfg)` in check mode: exactly what the real run writes). `hooks
  install`/`uninstall` only print.
- `apply` and `setup` run every check and refusal of the real run (`cmd_apply.make_plan`: a
  hand-edited preset, `render.check_pyproject`, the new name, the renamed pytemplate.toml; the
  dirty tree is only a warning; the PyPy 3.11 precheck runs read-only while `uv lock --check`
  passes, else a note says it waits for the re-lock) and print one row per step
  (`cmd_apply._print_plan`): app.name (then the rename plan, as `rename --dry-run` prints it),
  app.preset, `[preset.<name>]` (the `uv remove/add` it would run), pyproject.toml, uv.lock
  ("would re-lock", or up to date by a read-only `uv lock --check`; a new project name re-locks
  only when its normalized form changes), environments, git hook, generated files, then the
  unused-environment note and the reference warnings (for a rename, checked where the files
  are before the move).
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
| `PYTEMPLATE_CALLER_CWD` | launchers, Neovim plugin (`init.caller_cwd`: Neovim's cwd when inside the project, else the root) | Caller's cwd; read only through `project.caller_cwd` |
| `PYTEMPLATE_LAUNCHER` | launchers, Neovim plugin (`nvim`) | Which launcher/shell ran (section 4.1) |
| `PYTEMPLATE_LAUNCHER_FILE` | `pyt.cmd` (`%~f0`) | The batch file cmd runs, and reads again once uv returns: `cmd_install.run_by_cmd` (section 4.4) |
| `PYTEMPLATE_GLOBAL=1` | launchers that find no project (they run the installed template); removed for a project's runner (the Neovim plugin empties it: `init.pyt_env`) | Global mode (`project.GLOBAL`, read at import, and `pyt.py`; section 5.2); `__probe` reports it; `proc.base_env`, `shells.child_env`, `e2e.scrub_env` and `nvimtest.runner_env` drop it, so no child inherits it |
| `XDG_DATA_HOME`, `HOME`, `LOCALAPPDATA`, `USERPROFILE` | user, OS | Where `./pyt install` puts the installed template and the launchers look for it (`cmd_install.snapshot_dir`, section 4.1) |
| `UV_TOOL_BIN_DIR`, `XDG_BIN_HOME` | user | Through `uv tool dir --bin`: where `./pyt install` writes the launchers (`cmd_install.bin_dir`) |
| `UV` | uv | uv's own path; `proc.find_uv` and the launchers use it |
| `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON`, `UV_PYTHON_PREFERENCE` | `envs.env_vars` | Environment selection (section 7). The caller's own `UV_PYTHON` does not reach the runner: the launchers and the Neovim plugin remove or empty it (section 4.1); a `uv run` by hand with a pre-3.11 one stops in `pyt.py` (exit 3) |
| `PYTHONUTF8=1` | `proc.base_env`, portable launchers, pyz `.cmd` wrapper, the Neovim mypy linter, every VS Code launch config (`vscode.DEBUG_ENV`) | mypy/mypyc otherwise read files as cp1252; F5 behaves like `./pyt run` |
| `PYTEMPLATE_BACKEND` | `cmd_dev.test_backend`, `mypyc.runtime_env_vars`, mypyc launch config | Backend under test (conftest) |
| `PYTEMPLATE_COMPILED` | `mypyc.runtime_env_vars` | Modules that must load from `.pyd/.so` (conftest) |
| `PYTEMPLATE_ASSETS` | `portable/boot.py`, `pyz/__main__.py` (assigned, never inherited) | The app's assets folder, for the app's own code; `resources.assets_dir()` (raylib, flet) finds the same folder next to its package (section 10) |
| `VSLANG=1033` | `mypyc.build`, wheel builds | English MSVC messages |
| `MACOSX_DEPLOYMENT_TARGET` | `methods.common.install_deps` for macOS targets, unless the user set it (`MACOS_FLOOR`, 13.0) | The oldest macOS the pyz/portable wheels must support |
| `CC`, `CFLAGS`, `CPPFLAGS`, `LDSHARED`, `LDFLAGS`, `ARCHFLAGS`, `CL`, `_CL_` | user | setuptools builds the mypyc extensions with them (a `CFLAGS` REPLACES Python's own: section 9); `mypyc.COMPILER_ENV`, a change forces a rebuild |
| `RUFF_OUTPUT_FORMAT=concise` | VS Code tasks with the ruff matcher, Neovim tasks that parse output | One-line ruff output for the parsers |
| `NO_COLOR` (non-empty), `TERM=dumb` | user | Disable runner colours |
| `CI` | CI | Disables the install prompt of `pyt` and `pyt.ps1`; `selftest --e2e` skips GUI runs on Windows/macOS CI |
| `__RUBASH_SHELL_NAME` | niubash | Detected by `pyt` (section 4.3) |
| `PT_TRUST_FILE`, `PT_ROOT`, `PTCMD` | `cmd_nvim.trust_file`, `nvimtest`, `shells` | File to trust headless; project the smoke test expects; probe command text |

`proc.base_env` removes `VIRTUAL_ENV`, `PYTHONHOME`, `PYTHONPATH` and the user's uv variables
that would move the runner's `uv run --locked` calls (`proc.UV_SELECTION`, each measured with
uv 0.12): `UV_PROJECT_ENVIRONMENT` and `UV_PYTHON` (`envs.env_vars` sets both), `UV_PROJECT`,
`UV_NO_PROJECT`, `UV_WORKING_DIR` (another project or none: `--locked` is ignored, relative
paths move), `UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON` (exit 2 next to
`UV_PYTHON_PREFERENCE`), `UV_ISOLATED` (a throwaway env instead of `.venv`), `UV_NO_DEV`,
`UV_NO_DEFAULT_GROUPS`, `UV_NO_GROUP` (`=dev` wins over `--all-groups`: sync uninstalled the
tools; no mypy/ruff/pytest: a PATH-wide one of another version runs) and
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

- `./pyt apply` (and `setup`) calls `hooks.ensure_installed(cfg)` when `[hooks] pre_commit`
  is true (the default): it installs the hook or updates this project's own, never fails the
  command, and is silent outside git (`hooks.NotInGit`: not a work tree, or no git and no `.git`
  in or above the project). With a `.git` but no git on PATH (`hooks.git_missing_here`: a git
  GUI's own git) apply warns and its summary, doctor's "git hook" line and its `applied` line
  (without hooks.pre_commit) say the hook was not checked: it said "not a git work tree", left
  the hook of a `pre_commit = false`, and doctor called hooks.pre_commit applied. When
  `pre_commit` is false, apply removes pytemplate's own hook (`hooks.uninstall`, which restores
  somebody else's `pre-commit.local`) and never touches another one (section 5.8). Any other
  git failure (dubious ownership...) is shown with git's own message: a warning in apply, an
  info line in `doctor` (whose "git hook" line is otherwise info too: missing is not a
  problem; for a project below the repository's top it adds an info line that the generated
  `ci.yml` never runs there, section 13.2, unless that repository ignores the project: its
  "git hook" line then says to give it a repository of its own). Every git call runs with `LC_ALL=C` (find_repo
  reads "not a git repository" in English). A hooks folder the user may not change (another
  user's, read-only, immutable) is a warning in apply and one error naming the file in `hooks
  install`/`uninstall` (`hooks.cmd_hooks`; it was an internal-error traceback).
- The hook: `pre-commit` in the folder `git rev-parse --git-path hooks` reports (worktree
  aware), pure ASCII + LF, a marker comment, and `sh <launcher> hooks run` with the POSIX
  launcher path relative to the repository top (the project may be a subfolder of a bigger
  repo; non-ASCII folder names are written with `printf` escapes; `hooks.launcher_of` reads it
  back). `sh` explicitly (no dependence on the exec bit); git for Windows runs hooks with its
  own sh.exe, where the POSIX launcher works. A missing launcher makes the hook exit 0 (other
  checkouts); a launcher exit code above 1 (uv not found from a GUI client, a broken
  pytemplate.toml, an old runner without `hooks run`) also prints `git commit --no-verify` and
  `./pyt hooks uninstall`. `shellcheck -s sh`-clean (tested when installed). Any change to
  `hooks.hook_script` makes installed hooks "outdated": apply/setup rewrite them.
- Never overwritten: a hook without the marker, or any symlink (writing through a dangling
  link created a file in the work tree). `install --force` renames it to `pre-commit.local` (a
  link moves as a link; a dangling `.local` counts as existing) and ours runs it first, a shell
  script (`hooks.SHELLS`, its `#!` line read by `hooks.CHAIN_LINES`, flags kept) sourced with
  `$0` = `<hooks>/pre-commit`: husky v4 and yorkie pick their job from `basename "$0"` and find
  their helpers next to it, and run as `pre-commit.local` they checked nothing. One without a
  `#!` line is sourced only when it is text: a compiled hook (a NUL byte in its first 64 bytes,
  read with `od`; `hooks.interpreter` gives `""`, and `reads_its_name` is false for it) is
  executed, as git executes it (sourced, every commit failed with a shell syntax error:
  `test_hooks.test_a_compiled_hook_kept_by_force_runs_first`). It runs only with its x bit
  (`[ -x ]`), as git runs no hook without one: `install --force` and the status then say it
  does not run and name `chmod +x` (`hooks._not_run`; they said it ran first). A hook in
  another language that reads its own name (`hooks.reads_its_name`: overcommit's Ruby) is left
  alone by `--force`, which prints the line to add to it instead; `uninstall` restores it. A marked hook whose launcher is another live project of the same
  repository (state "other", a monorepo; live: the launcher file is there, or its folder still
  holds `.pytemplate/pyt.py` or `deploy.py`, `hooks._other_project`: a project upgraded in place
  since the launchers were renamed has a hook that calls `deploy`, which another project's apply
  took for a gone project's and replaced,
  `test_hooks.test_another_projects_hook_by_the_old_launcher_name_is_left_alone`) is left alone
  by apply, install and uninstall;
  `install --force` writes a FRESH copy of it as `pre-commit.local` (the script skips
  `pre-commit.local` when it is itself that file; older copies would recurse), so both checks
  run, once each. That copy stays the chained project's own (`hooks.own_local`; state
  "chained" of `hooks.hook_state`): its `uninstall`, and its apply with `pre_commit = false`,
  delete it (never restore it as `pre-commit`), doctor/`pending` count it, `install` there says
  the checks already run (or rewrites the copy in place when it is not this runner's hook:
  `hooks._own_local_outdated`), and once the other project's hook goes stale its `install`
  replaces that hook and deletes the copy (the checks ran twice). A marked hook, or chained
  copy, whose launcher is in this project's folder is this project's whatever the file's name
  (`hooks._in_this_project`): a hook written before the launchers were renamed calls `deploy`
  (that file gone, or still next to `pyt`), is "outdated", and apply rewrites it; taken for
  another project's, it was left alone and its checks were skipped
  (`test_hooks.test_a_hook_that_calls_the_launcher_by_its_old_name_is_updated`,
  `test_a_chained_copy_that_calls_the_launcher_by_its_old_name_is_updated`). A third project cannot be chained
  (`pre-commit.local` taken: `install --force` refuses): `hooks.chain_hint`/`chain_advice` then
  say so instead of suggesting `--force`. A hook pytemplate does not manage runs the checks
  ("calls") only with a command that is not a comment and calls THIS project's launcher with
  `hooks run`, the launcher being the program it runs (after only keywords, a shell,
  `exec`/`env`/`time`/`nohup`, their options and variable assignments: `hooks._before_the_program`;
  `echo Tip: also run ./pyt hooks run` counted) (`hooks.runs_checks`: the script split into commands and words by
  `hooks._shell_commands`, quotes and `$(...)` kept whole; `./pyt`'s global options may come
  before `hooks`; a relative launcher is resolved against the top, or the folder a `cd` before it
  moved to, a subshell's `cd` staying in it; a launcher or `cd` folder with a shell expansion
  (`"$ROOT"/apps/a/pyt`, `$(git rev-parse --show-toplevel)/...`) cannot be resolved and
  counts); another project's command, or a quoted string, is "foreign". A project that
  an enclosing repository ignores (`git check-ignore -q pyt`, which refuses
  `--literal-pathspecs`) gets no hook unless forced. With `core.hooksPath` set nothing is
  written: install/status/doctor print the line to add (`hooks.run_line`: `[ ! -f ./pyt ] ||
  sh ./pyt hooks run || exit $?`, which skips a checkout without the launcher as our own hook
  does: in a global hooks folder the unguarded line failed every commit of every other
  repository; the hint says so for a hooks folder outside the repository), and
  apply's summary says whether that hook already runs it (`hooks.hooks_path_runner`); husky 9
  (`.husky/_` holding `h` or `husky.sh`) is read through `.husky/pre-commit`.
- `hooks run` checks what the commit contains: staged files (`git diff --cached --name-only
  --no-renames --diff-filter=ACMRT -z`) and staged deletions (`D`: a deletion-only commit gets
  the project-wide checks too). Every git call passes `-c diff.relative=false`
  (`diff.relative=true` made every path relative to the project folder, and all were dropped).
  In ~0.3 s: ruff (active typing profile, `exit_zero` honoured) and `ruff format --check` on
  staged `.py/.pyi` under the code dirs via `uv run --quiet --frozen` (a stale lock is the lock
  check's finding; an exit code other than 0/1 is "could not run ruff", without a fmt hint,
  except `ruff format --check`'s exit 2 for a file it cannot parse, `hooks._UNPARSABLE`: "a
  staged file does not parse", which pointed at uv or the environment before). A
  file with unstaged changes is checked in its STAGED version (`git cat-file --filters
  :0:<path>`, the checkout form, fed to ruff with `--stdin-filename`); a staged file missing
  from the working tree fails with `git restore` / `git rm --cached`; the launcher checks
  (`shells.launcher_problems`) and the template repo's language guard read the staged content
  too. Project-wide and conservative (they read the working tree, so they may block a commit
  that touches none of their files): generated files up to date (`render.apply(check=True)`)
  and none unstaged/untracked; managed pyproject parts, what only `./pyt apply` brings in
  line (`cmd_apply.pending(hook=False)`: a hand-edited `app.name`/`app.preset`, the
  `[preset.*]` options) and `uv lock --check`; `pytemplate.toml`,
  `pyproject.toml`, `uv.lock` and the generated files committed together (once any is in the
  commit, none of the three config files may keep unstaged changes; the hints name the dirty
  config files with the generated ones, and `.pytemplate/state.json`, which render rewrites with
  them (`hooks.check_generated`), so following them never splits a source from its output nor
  blocks the next commit). `lintc` on staged compiled modules (blocking only under the `mypyc` profile, reads
  the working tree). Never mypy (the user's choice: `./pyt check` does it).
- Paths: `hooks.find_repo` takes the project's prefix from git (`rev-parse --show-prefix`: the
  folders' case on disk), checked to name the project's folder with `os.path.samefile`
  (`_same_folder`), and `project_paths` folds case where git says the disk does
  (`Repo.ignore_case`: core.ignorecase, which git sets on macOS's default APFS; always on
  Windows). On macOS `project.ROOT` keeps the case typed (`cd ~/projects/myapp` for MyApp): the
  prefix computed from ROOT against git's top put the project outside its work tree (every hook
  command failed), and a prefix in another case than git's paths dropped every staged file (the
  checks were skipped without a word). `test_hooks` stands a symlink in for the other spelling.
- `hooks._run_bytes` is the module's only process start outside `proc.run`: raw bytes
  (proc.run's text mode turns CRLF into LF) and stdin, for `git cat-file` and ruff on stdin.
- Git hands hooks a relative `GIT_INDEX_FILE` and, in linked worktrees, `GIT_DIR` without
  `GIT_WORK_TREE`. Without `GIT_DIR`, git reads a relative `GIT_INDEX_FILE` from the top of the
  work tree wherever it runs, so `hooks.git_env` leaves it as it is (joined to the cwd, a user's
  hook that runs `cd apps/a && ./pyt hooks run` read a missing index: a first commit passed
  unchecked); with `GIT_DIR` it makes them absolute against the hook's cwd for its own git calls.
  A `GIT_DIR` without `GIT_WORK_TREE` makes the cwd the top of the work tree, which that `cd`
  moved: in a checkout whose `.git` is a file (a linked worktree, a submodule, a
  `--separate-git-dir` clone: git exports an absolute `GIT_DIR` and `GIT_INDEX_FILE` there)
  apps/a became the top, the staged files read as none and every committed file as untracked.
  So `hooks._found_from` lets git find the repository from the project folder instead, when that
  finds the very `GIT_DIR` git exported (the index git exported stays); otherwise the cwd rule
  stays (`test_a_hook_that_cds_into_the_project_finds_the_top_of_a_checkout_whose_git_is_a_file`).
  `hooks` removes them before starting uv/ruff (they would point git at the wrong repository for
  a sub-folder project).
- `test_hooks.py` runs git with `GIT_CONFIG_GLOBAL` at a missing file and
  `GIT_CONFIG_NOSYSTEM=1`: a developer's global core.hooksPath, commit.gpgsign,
  init.templateDir or diff.relative must not change the results.

### 5.7 Renaming (`rename.py`)

- `./pyt rename NEW_NAME [--force]`: `check_new_name` refuses a bad format, a keyword, a
  standard-library module of ANY Python the project can run on (`presets.shadows_stdlib`: the
  runner's `sys.stdlib_module_names` plus `STDLIB_OTHER_VERSIONS`: the runner runs on
  `python.cpython` (5.2), but the app may also run on PyPy 3.11 and `python.cpython` can move), a
  dependency
  (`presets.check_name_free`, also used by `new` and `__init`) and any package of `uv.lock`
  (`locked_names`: indirect ones too, pygments via rich; the project's own entry excluded) and
  a backend name (`config.BACKENDS`: `src/mypyc/` would shadow mypy's compiler in the stage,
  and a later rename would rewrite conftest's backend strings); a broken pyproject.toml is a
  PytError, never a traceback. The dirty-tree check
  (`git_changes`: `git status --porcelain -z -uall` with `LC_ALL=C`, paths relative to the
  project; the generated files and `state.json` (`derived_paths`) never count, nor, after a
  hand edit, `pytemplate.toml`) refuses without `--force` (a warning under `--dry-run`); git
  failing for another reason (dubious ownership) is refused the same way, never read as "no
  git". Then: move `src/<old_pkg>/` first (the step that can fail on a locked file; case-only
  renames use two moves), write the files (`apply_plan`: each through a temporary file next to
  it and `os.replace` (`_replace_bytes` = `project.write_whole`: mode, owner and group kept, a
  symlink stays a link, a read-only file is an error; a file with other hard links, or one whose
  owner the runner cannot give a new file, is rewritten in place with its old bytes put back on
  failure: a new inode dropped the links and made the files of a bind-mounted project root's),
  so a write cut short (disk full, a quota, `ulimit -f`) never leaves one half-written; a write that fails undoes everything, old bytes back and the folder moved back,
  and the error names whatever it could not undo), the name of the `applied` record right away
  (`cmd_apply.rename_record`, only the project's own record: named after the old app it is no
  longer trusted), `cmd_env.ensure_lock` (a failure there says "the files are already renamed
  ... ./pyt apply"), `render.apply` and the ruff tidy-up; a Ctrl+C or SIGTERM during those
  says the same before it stops the command.
- After a hand edit of `app.name` (src/<pkg>/ missing, or the very same folder: `alpha` ->
  `Alpha`), rename starts from the name the project really has (`cmd_apply.applied_name`, asked
  first, as apply does: the trusted record, else pyproject `[project] name`, whose package is in
  src/): `rename <the edited name>` finishes the job, `rename OTHER` goes from the real name to
  OTHER; with no such name and src/<pkg>/ missing it exits 2 ("put the old name back"). It never
  says "nothing to do" while doctor and the hook still report the edit as not applied: an app.name
  set by hand to ANOTHER package of src/ is refused as apply refuses it (`_check_the_name_is_applied`
  with `cmd_apply._other_package`: `rename other --force` moved that package and left the app
  where it was), and a pyproject.toml [project] name edited by hand, with NEW_NAME = app.name,
  exits 2 naming `./pyt apply`.
- What changes (`rewrite`, whole words only; `myapp_extra` and `my-app-2` never match):
  - Python code (`tokenize`): the first name of `import pkg...`/`from pkg... import`, and, in a
    file that binds the package with `import pkg[.x]` (no `as`), every name that resolves to that
    import (`_package_uses`, an iterative `ast` scope pass: a function, class body or
    comprehension that binds `pkg` another way (assignment, parameter, loop/with/except target,
    global/nonlocal, walrus) keeps every `pkg` in it; a module that rebinds it keeps all of its
    own; they are reported, not changed). Keyword arguments (`f(pkg=1)`) and attributes never
    change; `f"{pkg=}"` is a use. When `ast` cannot parse the file (syntax newer than the
    runner's Python) the token rule is the fallback. String tokens are recognised by their
    `*STRING_START`/`*STRING_END` suffix (f-strings 3.12, t-strings 3.14, any later family); such
    a string is one region whose `{fields}` are code, as 3.11's single token (`_in_fstring_field`:
    a format spec `{x:f}` or a string inside a field is kept and reported, on every Python). A
    lone CR is a line break, as for the compiler (`_python_code` reads it as LF, one character
    for one: classic Mac line endings once ended in an internal error).
  - Strings are syntax first (`_classify`, `_escaped`): a string prefix (`f`, `r`, `b`, `rb`...:
    before the opening quote) is never the name, and an occurrence right after an odd number of
    backslashes is an escape when the string is not raw and its first letter makes one (`\n`,
    `\x89`, `\a`...: skipped) and else (`r"\d"`, `r"src\alpha"`, an invalid `"\myapp"`) kept and
    reported, never changed (a new name could turn it into an escape or another regex). The
    same for TOML basic and literal strings (`_toml_strings`: pytemplate.toml, pyproject.toml
    and the `.toml` files of src/ and tests/) and JSON strings (`_json_strings`, `DATA_STRINGS`:
    `.json` files, notebooks `.ipynb`, `.jsonc`, `.geojson`, `.jsonl`, `.ndjson`; and YAML's
    double-quoted strings with `_YAML_ESCAPES`: an app named n once turned a notebook's `\n` and
    YAML's `"hello\n"` into `\b`); comments and other text files (Markdown, INI, YAML's plain and
    single-quoted scalars) have no escapes: `a\n` there is a path like any other, `src\n` the
    package and `a\n` another folder named n (a JSON fixture's `"a\n"` once became `"a\tool"`). A one-letter occurrence that ends a format directive in a string (`"%d"`,
    `"%(k)-s"`, `"{:d}"`, `"{0:,d}"`, `"{!r}"`, and any letter after a `%`: strftime's `"%Y"`,
    `"%-m"`, `"%^a"`, which became `"%Tool"`; `_directive`) or is a struct format character after
    a byte order or count (`">I"`, `"<2H"`: `_STRUCT_BEFORE`; the flet skeleton's PNG encoder
    packed `">q"` chunk lengths for an app named I renamed to q; a bare `"I"` stays the name) is
    kept and reported too (whether the string is ever formatted is unknown). Names of one or two letters are legal, and the
    flet skeleton's PNG signature `b"\x89PNG\r\n..."` once changed silently for `r` and `n`.
  - Text (strings, comments, other files): every occurrence except `x.pkg`, a path segment
    right after the package itself (`src/pkg/pkg`, `src\pkg\pkg`: a submodule) and a file named
    after the app (`_names_a_file` with `_package_modules`: `pkg.png`, `sfx/pkg.wav`,
    `"pkg.json"`, a `.` and a word that is no module of src/<pkg>/, outside an import, `-m` or
    loader argument; the file keeps its name, so the reference is kept and reported: it became
    `asset("beta.png")`; an artifact name, `pkg.exe`, follows the new name) or a folder named
    after the app (`_names_another_folder`: only src/<pkg>/ moves, so under another folder a
    segment is the package only right inside `src/`, and a path that starts with it only on its
    way into an entry of src/<pkg>/, `_package_entries`, or ending there, `pkg/`; `tests/pkg/data`,
    `assets/pkg/logo.png`, `asset("pkg/logo.png")` keep their name and are reported: they became
    `tests/beta/data` while `tests/alpha/` stayed, and the tests failed). For a package
    named `src` (a project made by hand: `new` and `rename` refuse the name) a `src/` segment
    that is not right inside another `src/` is the project's own folder and never changes
    (`src/src/x` -> `src/beta/x`; it once became `beta/beta/x`). When the
    old name equals the old package but the new name differs from the new package (`alpha` ->
    `My-Game` / `my_game`): paths, dotted names, `pkg:main`, "package"/"module",
    `import`/`from`, `-m` (`_DASH_M_BEFORE`: a prefixed string after it too, `"-m", f"alpha.{x}"`)
    and the module-name arguments of loader calls get the package
    (`LOADERS`: the name and package of `import_module`/`find_spec`, `__import__`, `files` and
    the other importlib.resources functions, `pkgutil.get_data`, `runpy.run_module`; positional
    or as a `MODULE_ARGUMENTS` keyword, tracked per open bracket: `files(package="alpha")` once
    got the display name, which is no module name; an f-string argument too, whose 3.12+ tokens
    once skipped the rule: `import_module(f"alpha.{x}")`); titles, other prose and artifact names
    (`alpha.exe`, `alpha-cpython-exe`) get the name.
  - `pytemplate.toml`: always by context (`contextual`, even when the new name is a package
    name, and when the old name is not its package's spelling: for My-Game or Auto the package
    word goes through the same rules and the display name is prose; `tools/my_game.py` and
    every `"auto"` value were rewritten, the latter into an invalid file that stopped rename and
    apply). A path there is the package only right inside `src/` (`_after_src`: `src/alpha/data`
    moves with the folder; `tools/alpha.py`, `assets/alpha.ico` and, for an app named pyt,
    `./pyt apply` are other files), and a dotted word only when it names a module or
    subpackage of src/<pkg>/ (`_package_modules`: `alpha.core`; not `alpha.ico` nor, for an app
    named uv, `uv.lock`). Never a TOML key or table header (`_toml_key`: an app named `app`,
    `editor`, `console` or `bunnymark` keeps `[app]`, `editor =` and its buttons), never a key
    path in a comment (`app.gui` for an app named `app`: `_config_path`). The values of
    `MODULE_KEYS` (compile.modules/exclude/forbid_imports, `[[typing.mypy_overrides]] module`,
    deploy.exe.hidden_imports, deploy.exclude_modules, deploy.wheel.entry; table-aware and
    multi-line arrays included: `module_value_lines`) are package references even when bare
    (`modules = ["alpha"]`, `exclude = ["alpha.slow"]`). Any other mention is reported, not
    changed ([tasks] can use `{name}` and `{pkg}`). `validate_config` validates the result in
    memory ("the renamed pytemplate.toml would be invalid ...; nothing was changed"). Decoded
    with `config._decode` (UTF-16/ANSI is a clear error) and written back with its own BOM and
    line endings.
  - `pyproject.toml`: `[project] name` (`presets.set_project_name`: the `[project]` table only,
    either quote style, any indentation; a table it cannot edit stops the plan) and, in the
    preset block, the values the preset writes with the name (`rename._named_keys`: `{{name}}`
    or `{{pkg}}` in a preset.toml `pyproject`; flet's `org = "com.example"` stays, which for an
    app named com became `beta.example`); mentions in other tables are reported. Read as bytes decoded `utf-8-sig`: its line
    endings stay (a CRLF checkout used to come back LF), the BOM is not written back.
  - A file of src/ or tests/ that cannot be read (root-owned, locked by another program) stops
    the plan with a PytError naming it: nothing changed, never an internal-error traceback.
    So does a folder there that cannot be listed (`_code_files`: os.walk skipped it silently, and
    its files kept the old imports inside the moved package).
  - A Python file with a PEP 263 cookie is rewritten in its own encoding; other files that are
    not UTF-8 text but mention the old name are a warning (`Plan.unreadable`; a UTF-16 or UTF-32
    file, PowerShell 5.1's `>` and Out-File, is searched through its BOM, `_bom_text`, as
    `_mentions` does: its NUL bytes made it a silent binary); binaries show only with `-v`. Files outside src/ and tests/ (README.md, scripts/, docs/, your own
    workflows) are only listed (`_mentions`: skips caches, environments, `.build`, `dist`,
    `.pytemplate`, `.claude`, the generated files, the template's own launchers and CLAUDE.md
    (`ROOT_SKIP`), files over 2 MiB and anything that is not a regular file: `_mentions_name`,
    since opening a named pipe waited for a writer forever).
  - Symbolic links and Windows junctions in src/ and tests/ are never followed nor rewritten
    (`_code_files` with `cmd_env._is_link`: their target may be shared with other projects, and
    os.walk enters a junction); those that dangle once src/<pkg>/ moves (`_dangles`: the target
    resolves into it, and read from where the link will be it no longer names the same file; the
    target's text is not searched: an absolute link through the project's own folder, named like
    the app, was reported) or whose file or folder mentions it are a warning (`Plan.linked`,
    `_link_mentions`, shown in the dry run and the real run): a linked subpackage that imports
    the old package used to break silently. `_mentions` skips links and junctions too.
- ruff tidy-up (`tidy_before`/`tidy_after`; best effort, never fails the rename): the new name
  has another length and sort position, so `ruff check --fix-only --fixable I001` (acts only when
  the active typing profile selects I) and `ruff format` run on the rewritten Python files, each
  only on the files ruff accepted BEFORE the rename (`Tidy`: a file kept unformatted or unsorted
  stays so), with the active profile's `.build/cfg/ruff-*.toml` (`cmd_dev._profile_file`), like
  the hook.
- Invariant (tested for the 3 presets, LF and CRLF, 7 name pairs, names Hypothesis makes up and
  the round trip A -> B -> A): renaming the skeleton of name A to B is byte-identical to the
  skeleton of B, so `presets.pristine` stays true.
- Common words as names (`app`, `game`, `core`) also rewrite prose in src/ and tests/: that is
  why the tree must be clean and `--dry-run` shows sample lines.

### 5.8 Applying `pytemplate.toml` (`cmd_apply.py`)

- `./pyt apply [--force]` brings the whole project in line with pytemplate.toml; `./pyt
  setup [--force]` is the same operation under its first-time name (`cmd_env.cmd_setup` calls
  `cmd_apply.apply(command="setup")`; only the header and the last line differ). Idempotent: a
  second run syncs the environments and changes no file (no uv add/remove/lock).
- `make_plan` computes everything and makes every refusal before the first write:
  `read_project` (a BOM is fine, broken TOML is exit 2), `applied_state`, a hand-edited
  `app.preset` (exit 2: a preset decides src/, tests/, the dependencies and pyproject.toml, so
  it cannot switch in place: put it back, `./pyt new DIR --preset P`),
  `render.check_pyproject`, `dependency_changes`, whether PyPy is new (tool.uv environments has
  no PyPy yet), then the rename plan (`rename.check_new_name`, `rename.plan`,
  `rename.validate_config`) or, when only pyproject `[project] name` differs, its new text
  (`rename.check_new_name` too). An app.name that names ANOTHER package of src/ (the record's
  name, or without a record pyproject's, has its own package there: `_other_package`) is
  refused, as `rename` refuses it (`src/<new>/ already exists`), whatever `[project] name` says
  (edited to it too, or to a third name); it used to rewrite only `[project] name`. A record named like app.name means only pyproject.toml was edited: apply
  puts its name back (`_old_name`: also when that name differs only in case or `-`/`_`, so its
  package is app.name's folder; it was taken for the real name, and a rename myapp -> MyApp
  rewrote the user's prose). Without a record either line may be the edited one, and the refusal says
  both ways out.
- Order of `apply`: dirty-tree check (rename only; `--force` skips it) -> the rename
  (`rename.report`, `tidy_before`, `apply_plan`, then the record under the new name: a later
  failure must not leave it naming the old app, which is no longer trusted) or the `[project]
  name` line -> `uv remove --frozen` / `uv add --frozen` of the option-driven requirements (dev
  group with `--dev`) -> `cmd_env.ensure_lock`, whose re-lock, when it first resolves PyPy
  (`render.gains_pypy`), is followed by `cmd_mode._precheck_py311` (it syncs the tools
  environment with THIS configuration, `uv sync --locked`, so only once pyproject.toml and
  uv.lock follow it: with a python.cpython change in the same edit uv refused the old lock) ->
  `save_record` (everything after it can still fail:
  a record written only at the end kept the old options after a failed sync, and a revert then
  kept both raylib packages) -> `envs.sync` of `cmd_env._envs_for(cfg, "all")` ->
  `cmd_env._fix_exec_bit` -> the hook (`ensure_installed` when `hooks.pre_commit`,
  `hooks.uninstall` of pytemplate's own hook when false, the copy chained after another
  project's hook included; another tool's, another project's or a core.hooksPath setup is left
  alone; the summary line follows `hooks.hook_state`) -> `render.apply` -> `rename.tidy_after`
  -> the unused-environment note (`unused_envs`: every `.venv*` of this side no supported backend
  uses, `.venv-jit` of older templates included; never deleted) -> warnings for missing
  references (`reference_problems`: src/<pkg>/, compile.modules (the path `config.import_path`
  gives, which must be a file or hold a `.py`/`.pyi` file: a folder left holding only
  `__pycache__` is missing), app.assets, deploy.exe.icon, deploy.upx.path (a `~user` this machine
  lacks is missing too: expanduser's RuntimeError ended doctor and apply in a traceback, and
  `upx.locate` makes it exit 3); the `--dry-run` of a
  rename looks for them where they are before the move)
  -> a summary. A state.json or pyproject.toml that cannot be written is a `PytError`
  naming it. A Ctrl+C, SIGTERM or SIGHUP once the app is renamed (`cmd_apply._finish`, the steps
  after the rename) says "the app is already renamed: run ./pyt apply to finish", as `rename`
  does (it said only `terminated`).
- `--frozen`, never `--no-sync`: `flet-cli==V` pins `flet==V`, so a resolving `uv add` of one
  group alone has no solution; `--frozen` only edits pyproject.toml and one `uv lock` follows.
  When the edits, the lock or the PyPy precheck fail, pyproject.toml and uv.lock get their old
  bytes back (`_restore`) and the record is not written: nothing half-applied, the next apply
  retries. `--dry-run` runs the precheck read-only only while `uv lock --check` passes (its
  `uv run --locked` would fail on a stale lock alone) and says so otherwise; its uv.lock row
  says "would re-lock" for the managed parts, the dependencies, a stale lock, or a project name
  whose NORMALIZED form changes (`p` -> `P` re-locks nothing).
- Option-driven requirements: the preset.toml entries with an `{option}`
  (`presets.option_dependencies`: flet's `flet`, `flet-desktop`, dev `flet-cli` `=={version}`;
  raylib's `{package}=={version}`), compared with pyproject by normalized name and version
  (`req_key`: uv writes `raylib_sdl` as `raylib-sdl`; extras and markers ignored), never
  verbatim. What `uv add --frozen` gets keeps the declared requirement's marker (`req_marker`:
  uv replaces a requirement only with one of the same marker, keeping its extras, and appended
  an unmarked second pin, which no lock could solve, 15.1); a package switch carries the marker
  of the package it replaces. Plain preset dependencies (`rich`, `types-cffi`) belong to the project after `new`.
  An old value is removed only when the last applied options produced it (a `raylib` the user
  added next to `raylib_sdl` stays). `[preset.<name>]` wins over a hand `./pyt add flet==X`.
- The `applied` record (top-level key of `.pytemplate/state.json`, committed; `render._save_state`
  keeps it): `{name, preset, dependencies, dev}`, written by `./pyt new` (`presets._record`
  in `__init`: the new project's own, never the copied one), by each apply once the lock follows
  the options, and renamed by rename. It counts only when its name is `app.name` or pyproject
  `[project] name`, or its own package is in src/ (`trusted_record`: both lines edited by hand,
  to a new name, where apply skipped the rename, recorded the new name and lost the real one, or
  onto another package of src/, where apply said "applied" and recorded that package's name,
  doctor and the hook passed and a later `rename` moved that package; `_other_package` refuses
  it now: `test_apply.test_both_names_edited_onto_another_package_are_refused`). The trusted
  record decides the preset
  (`_infer_preset`): an app.preset that differs from it was edited by hand, whatever
  pyproject.toml holds (a script project may `./pyt add raylib` or flet). Without one (lost:
  a state.json merge conflict whose sides disagree on it, a deleted file) pyproject.toml's
  traces stand in (`_traced`): a preset's option-driven dependencies by name (with the default
  and current options, and those the managed block was last written with: `_block_options`
  reads raylib's `no-build-package` back through `_unformat`, from between the markers only,
  `Project.block` (`render.managed_values`): a script project's own `no-build-package = ["six"]`
  outside them read as raylib's `["{package}"]`, a hand switch apply refused), its extra tables (flet's
  `[tool.flet]`). The managed `[tool.uv]` KEYS are never a trace: `render.managed_block` writes
  them from app.preset, so `./pyt lock`, `mode` or `rename` after a hand edit wrote the new
  preset's keys and apply then accepted the switch. app.preset when it shows traces, else a
  preset that does (a hand switch), else, with no trace of any preset, the preset without traces
  (script): a guess (`Applied.guessed`), and the refusal and doctor say so and how to keep
  app.preset (restore its requirements with `./pyt add`, or set `[preset.<name>]` to what
  pyproject.toml declares). Without a record the applied requirements are those of the options
  the managed block names (else the preset.toml defaults, what `__init` wrote) and the old name
  is pyproject `[project] name` when its package is in src/.
- `pending` / `doctor` (one call from `cmd_env.cmd_doctor`): an `[XX]` line per change not
  applied yet (hand-edited app.name or app.preset, an app.name that names another package,
  `[preset.*]` vs pyproject.toml, `hooks.pre_commit = false` with pytemplate's hook installed,
  chained copy included), else `[ok] pytemplate.toml
  applied`; missing references are `[--]` notes; a broken pyproject.toml is a line, never a
  traceback.
- Every hint for a pyproject.toml that does not match pytemplate.toml (`render.auto`, doctor,
  `render --check`, the pre-commit hook) names `./pyt apply`: `./pyt lock` applies only
  the managed block, so after a `[preset.raylib] package` switch it moved no-build-package to
  the new name and kept the old dependency.

### 5.9 Installing `pyt` (`cmd_install.py`)

- `./pyt install` runs only in a clone of the template (the `template-repo` marker) and never
  in the installed template (`installed_here`: its record, or the folder `snapshot_dir` names):
  exit 2, naming `./pyt install in a clone of the template` (`FROM_A_CLONE`); typed outside any
  project (`PYTEMPLATE_GLOBAL=1`) it says that it installs nothing from there
  (`_refuse_elsewhere`). It takes nothing from the Config (it runs in the installed template's
  global mode too).
- The installed template (`snapshot_dir`, the lookup of section 4.1): the files git tracks
  (`presets._tracked_template`), with their working-tree content as `new` reads them, minus
  `.github/workflows/template-*`; the marker, `README.md` and `LICENSE` stay (so `new` from
  there copies `.pytemplate/README.md` and `LICENSE` as from the clone). Tracked links stay
  links (`presets._copy_link`), a submodule is copied as a folder. Untracked files and tracked
  ones deleted from the working tree are listed, not copied. Its record
  `.pytemplate/installed.json` (`presets.INSTALL_RECORD`, `RECORD`): schema, commit, `dirty`
  (uncommitted changes in tracked files), source folder, bin folder, launchers, UTC time; it is
  written only there, and `presets._skipped` keeps it out of every project. In the installed
  template `copy_template` copies every file of that folder (`presets._installed`: git is never
  asked), never git's list: a dotfiles repository around the data folder tracked some of them
  (`test_install.test_the_installed_template_copies_what_the_clone_copies`). Its folder has
  the mode a plain mkdir gives (`_new_folder`; tempfile.mkdtemp's 0700 became every new
  project's while copy_template still copied the root's mode, section 5.2).
- The launchers: `LAUNCHERS` (`pyt`; on Windows also `pyt.cmd` and `pyt.ps1`, section 4.2) into
  `uv tool dir --bin` (`bin_dir`; a relative answer, from a relative `UV_TOOL_BIN_DIR`, is refused),
  byte for byte, mode 0755, each checked like doctor checks it (`shells.launcher_problems`) and
  for its MARKER line. A file there without the MARKER, a symbolic link (`not_ours`), a bin
  folder inside the clone or the data folder, or one that holds `.pytemplate`, a data folder
  that exists without a record or is a symbolic link (`not_an_install`: it was refused as one
  that "holds no" record), a clone git refuses to read (dubious ownership: named with git's way
  to let it, `presets.git_refusal_fix`, where it said "not a git work tree ... use a git clone"),
  and on Windows a `pyt.com`, `pyt.exe` or `pyt.bat` of another tool in
  the bin folder (`pathext_shadows`: cmd, xonsh, nushell and Python's subprocess take the first
  pyt<ext> of a folder in PATHEXT order, before `pyt.cmd`) are refused before the first write,
  all of them in one message (`make_plan` gathers them, `_refuse`; only no data folder and a uv
  that cannot name its bin folder, exit 3, stop it at once).
- All or nothing (`_Swap`, `install`): the new copy is written into `.template-new-*` next to the
  installed template and the launchers are staged as `.pyt-install-*` in the bin folder; then
  the old copy moves aside (`.template-old-*`), the new one takes its name and the launchers are
  replaced one by one (their old bytes noted first). Any failure, Ctrl+C, SIGTERM or SIGHUP
  (`_terminations_interrupt`: POSIX, main thread, only while at the default handler; it raises
  `proc.Interrupted`, cli.main says `terminated (SIGTERM)`, 128 + N; the first ignores the next
  ones until the undo is done; their default action left a whole `.template-new-*` copy) undoes
  every step (`_Swap.undo`: the launchers' old bytes, the old copy back, the staged and new files
  deleted; a rename cut short is read back from the folders); what it could not undo is named.
  A launcher that cannot be replaced (another user's, chattr +i) is named, not its staged file,
  and was never changed: its entry goes before the undo (os.replace is all or nothing), which
  also skips a launcher that holds its old bytes and deletes the staged file of a put-back that
  fails (it said "could not put back" for an untouched launcher and leaked a `.pyt-install-*`).
  `remove_leftovers` deletes what a killed install left (only staged files with the MARKER).
  Windows renames are retried for a few seconds (a scanner holding a new file).
- Once the swap has succeeded, the launchers of the earlier install in the bin folder its record
  names, when uv's tool bin folder is another one now (`UV_TOOL_BIN_DIR` changed), are removed
  (`Plan.earlier`, `_remove_earlier`: only regular files with the MARKER; the `pyt.cmd` cmd runs
  gets `_retire`'s stand-in) and each is named: they stayed on PATH, and uninstall and doctor,
  reading the new record, never looked there again. One it cannot remove is a warning (15.2).
- It never edits PATH, an rc file or the registry: `path_problem` says when the bin folder is
  not on this process's PATH (`uv tool update-shell`) or only on the registry's (a console
  opened before the change), and `shadowing` names another `pyt` earlier on PATH (`first_pyt`:
  never the current folder, which shutil.which searches first on Windows). On Windows install
  (and doctor) also note each PowerShell whose execution policy blocks pyt.ps1 (`policy_notes`,
  with `shells.ps_policies`: Restricted, 5.1's default on client Windows, or AllSigned), which a
  bare `pyt` runs there before pyt.cmd: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or
  `pyt.cmd`.
- `uninstall` (outside a project, in the clone, or in a project made from this version; an
  older project's runner has no such command: run it from another folder there. In the installed
  template it deletes the files it runs from): the launchers with the MARKER in the current bin
  folder and in the recorded one, the installed template when it holds a record and is no link
  (`not_an_install`; a link is left, and named) and no launcher of ours it could not remove runs
  it (it stays with them, so pyt keeps working and `pyt uninstall` runs again from them: deleted,
  it left them saying "install it"), the leftovers (each named; one it cannot delete
  is a failure), and `<data home>/pytemplate` when empty; it names what it leaves and why.
  The installed template moves aside first (`remove_installed`: `.template-old-*`, a leftover
  the next uninstall or install deletes), else, where it cannot move (Windows: a terminal's
  folder inside it), it is deleted in place in rounds (`_remove_in_place`): what its runner does
  not need, then its `ENTRY` (`.pytemplate/pyt.py`, what the launchers look for), then the rest
  of the runner and `pytemplate.toml`, then its record. rmtree deleted the record first, and a
  file in use then left a folder that the next uninstall called the user's own and install
  refused, while both said to run uninstall again; and a file in use of the first round now
  leaves the runner whole (`_runs_pyt`: its `ENTRY` is there). `--dry-run` of both
  prints and writes nothing. The `pyt.cmd` cmd runs for this very run (`run_by_cmd`) goes last
  (`_retire`): after a failure it stays whole only while the installed template's runner does
  (`pyt uninstall` then runs again from it; kept without it, it said "install it"), else
  `self_deleting` takes its place and deletes itself as cmd reads on (section 4.4); install
  refuses, before any write, to replace that file with other bytes. A failure names `pyt
  uninstall` again only while a launcher of ours that runs the installed template is left, else
  `./pyt uninstall` in a project or in a clone
  (`test_install.test_the_pyt_cmd_kept_for_a_retry_can_run_uninstall_again`,
  `test_a_launcher_that_cannot_be_removed_keeps_the_installed_template`).
- `doctor(check)` (the last step of `./pyt doctor`, every mode): notes only (`[ok]`/`[--]`,
  never `[XX]`: pyt works without it): not installed, the installed template and its commit
  (no data folder: the variables that would name one, `data_home_names`), older than this clone
  (`age`, in the template repository), launchers missing, foreign or differing from the installed
  template's, launchers of this install left in the recorded bin folder when uv's is another one
  now, on Windows a pyt<ext> before pyt.cmd and a blocking execution policy, PATH.

## 6. Configuration and generated files

### 6.1 `pytemplate.toml` (`config.py`)

- Reading (`config.read_text`, used by `load`, `update_file` and `mode`): UTF-8, a UTF-8 BOM is
  dropped, line endings are kept. UTF-16/32 (BOM; PowerShell 5.1 `>`/`Out-File`), NUL bytes
  (UTF-16 without a BOM) and invalid UTF-8 (ANSI, `Set-Content`) are a `PytError` (exit 2)
  naming the encoding or the byte and its line, and how to save the file again; never a
  traceback, for every command (`help` still prints, without the custom tasks). So is a text
  tomllib rejects without its TOMLDecodeError (`config.TOML_ERRORS`: a plain ValueError for an
  integer of more than 4300 digits, RecursionError for arrays nested about a thousand deep;
  `config.toml_error` words it), in `config.load`, `set_value`, `update_file`,
  `cli._python_needed` and `cmd_mode._config_from_text`
  (`test_config_rules.test_a_config_tomllib_cannot_read_is_a_config_error`).
- Loader (`config._build`): types come from the dataclass annotations (`typing.get_type_hints`),
  recursively: list items (`key[i]`), table values (`key.k`; non-bare keys are quoted in the
  path). `Any`-typed values (`[vscode] settings`, `[preset.<p>]` options, mypy override options)
  must be JSON-like: no TOML date/time, `nan`/`inf` (`_check_free`). NUL characters are rejected
  everywhere, in the keys of the open tables too (`_check_key`: a `[vscode.settings]` key reached
  settings.json as `"a\u0000b"`).
- `schema` must equal `config.SCHEMA` (1; a missing line means 1). It is checked before the
  other keys, so a file from another template version fails with that reason instead of an
  unknown key. There is no migration logic: rule 1.11 allows a bump only with the owner's
  approval, and then the runner must still read the older layout, with warnings.
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
  `./pyt lock`). The removed CPython JIT keys (`jit`, `jit_interpreter`) fail like any
  unknown key: they went before the template had users, so rule 1.11 does not cover them.
  `Config.min_python`
  is the lowest minor in use (CPython always, PyPy's `Config.pypy_minor` while supported),
  compared as numbers.
- `[typing]`: `profile = auto|mypyc|strict|warn|off`, `relaxed = off|warn|strict` (what `auto`
  means on cpython/pypy), `editor = pylance|basedpyright`, `[[typing.mypy_overrides]]`
  (`config._check_override`: `module` required, a name/pattern or a non-empty list of them
  (dotted identifiers or `*`, `{pkg}` token); `strict` forbidden: mypy would apply it to ALL
  modules; option names identifier-like, values booleans, numbers, one-line strings or lists of
  them: each becomes a `.mypy.ini` line). `backend.active = "mypyc"` rejects `warn`/`off`.
- `[compile]`: `modules` (without the line: `<pkg>.core` of `app.name`, filled in by
  `config._build`; the dataclass default, the template's own `myapp.core`, sent every mypyc run
  of another app to `src/myapp/`), `exclude`, `forbid_imports` (dotted names checked), `annotate` (every
  mypyc build writes the annotate report, section 9), `opt_level "0".."3"`,
  `no_semantic_interposition` (default true: a C flag on Linux gcc/clang, section 9),
  `multi_file`, `separate`, `strict_dunder_typing`. `config._validate_compile` checks the names
  against each other: no entry of `modules` inside another (or repeated: mypyc aborted with
  "Duplicate module"), every `exclude` strictly inside a `modules` entry (a module or a
  subpackage: it excludes everything below it). Each `modules` entry is the path Python imports
  (`config.compiled_paths` through `config.import_path`: a folder with `__init__.py`, else
  `<name>.py`, else a namespace folder; a folder left holding only `__pycache__` never hides the
  module file, and the pyright strict list, lintc's `__file__` rule and apply's reference
  warnings follow it too). Whether they exist is checked by `mypyc.compiled_sources`: an
  entry that does not exist or holds no module, or an `exclude` that names nothing, is an error
  naming the candidates, never a silent no-op.
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
  `cleanup` = `--cleanup-app --cleanup-packages`, `false` = neither), `[deploy.upx] enabled level lzma exclude
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
  the task runs (`tasks.run_task`), not at load: unknown placeholder names (of the task and of
  every task its deps reach, `tasks._check_texts`: a typo was found only after all the deps
  had run), `deps` quoting and
  empty entries (all parsed before the first dep runs; `tasks.split_words`: blanks separate,
  quotes group, a backslash is a plain character, as in the plugin's `:Pyt`: POSIX shlex
  passed `C:\data\in.txt` as `C:datain.txt`), `cwd` is a folder (not in a dry run),
  `backend = "pypy"` in `backend.supported` (only where its environment is used). `vscode.scan`
  renders a task whose deps do not parse. `uv = false` on Windows: a bare program is looked up
  on the task's PATH with PATHEXT (`npm` -> `npm.cmd`; `tasks._on_windows_path`: never in the
  current folder, the caller's, which shutil.which searched first, so an `npm.cmd` in the folder
  `./pyt web` was typed in ran instead; a relative PATH entry is the task cwd's, as on POSIX),
  and a `.cmd`/`.bat` program runs
  through cmd.exe, which re-parses the `list2cmdline` line: an argument it would change (`%`,
  `"`, a line break; `^ & | < >` when list2cmdline leaves it unquoted: no space, tab or empty
  value) is refused with exit 2 (`tasks._batch_problem`), never passed changed
  (`npm install react@^18` installed react@18), and so is a batch file whose own path holds one
  (`C:\Users\R&D\...\eslint.cmd` ran as `C:\Users\R` and a second command:
  `test_cli_core.test_a_batch_file_whose_path_cmd_would_change_is_refused`).
- `[preset.<name>]`: option overrides (`config._check_preset_tables`): `<name>` must be a preset
  of this template, and each key and its value type must match that preset's `preset.toml`
  `[options]`, read with `tomllib` (no `presets` import); the script preset has none.
  `./pyt apply` applies them to the dependencies (section 5.8): raylib `package`/`version`,
  flet `version`.
- `[vscode]`: `settings` (merged into `.vscode/settings.json`; keys NOT validated, values must be
  JSON values: a TOML date/time, nan/inf or NUL is a config error naming the key, from the loader
  (`config._check_free`) and again from `vscode.settings`), `buttons` (each first word must be a
  builtin command or a `[tasks]` name: `config.validate`; one that names a command removed
  since the contract, `config.RETIRED_COMMANDS` (`shell-setup`), is left out with a warning, once
  per run, `config.warn_once`, and so is such a `deps` entry in `tasks.run_task`: they stopped
  every command, a break of rule 1.11:
  `test_config_rules.test_a_button_of_a_retired_command_is_left_out_with_a_warning`,
  `test_a_task_keeps_a_name_a_later_builtin_took`). Only a validation given the builtin
  commands leaves it out, so `mode` validates its new configuration with them too
  (`cmd_mode._builtins`: it rendered a `pyt: shell-setup` task into tasks.json, which `render
  --check` and the hook refused:
  `test_mode_leaves_out_a_button_of_a_retired_command_as_every_command_does`).
- `config.set_value(text, table, key, value)` edits the TOML text itself: a small scanner
  (`config._statements`: the four string kinds, multi-line arrays and inline tables, comments,
  dotted and quoted keys) finds the value's span, which may cover several lines (taplo, the
  LazyVim TOML formatter, expands long arrays), and replaces only that span: the comment after
  it, the other lines and the line endings stay. A list that replaces a multi-line array with
  comments inside keeps that layout (`config._array_lines`): an element that stays keeps its
  line, the comment on it and the comment lines above it, a new one gets a line of its own, and
  one that goes takes its comments with it (mode dropped them all). An array without comments,
  or one it cannot lay out (several elements on a line, numbers, nested values), is written on
  one line. A missing key goes after the table's last key
  (a missing table at the end) with the file's line ending. The result is re-parsed and must
  equal the old data with only that key changed; anything else (the table written as an inline
  table, the key defined as a table) is a `PytError` asking to edit it by hand.
  `config.toml_value` escapes DEL and refuses lone surrogates.
- `config.update_file` applies every change in memory first, writes only when something changed
  and never under `--dry-run`, keeps a UTF-8 BOM and the line endings, and never writes broken
  TOML; it writes through `project.write_whole`, so a full disk leaves the old file whole, and a
  file it may not write (read-only, immutable, locked) is a `PytError` naming it (it was an
  internal-error traceback; so was a template `render.load_profile` or `ci_workflow` could not
  read). `mode` drops changes whose value is already set, so their spelling stays.
- `mode` keeps the old bytes of `pytemplate.toml`, `pyproject.toml` and `uv.lock` (the lock next to
  `pyproject.toml`) and puts them back (`cmd_mode._restore`) when
  `config.update_file`, `cmd_env.ensure_lock` (its PyPy precheck included) or the new
  environment's `envs.sync` fails or is
  interrupted, saying which files it restored; the generated files are rendered only after that,
  so a failed mode leaves nothing half-applied and a second run locks again (it used to find
  nothing to change and report success with a stale `uv.lock`). A generated file it leaves
  untouched because it was edited by hand is named in a warning, as its dry run names it (it
  still describes the old mode).

### 6.2 Generated files and `state.json`

Generated (committed, never hand-edited): `.python-version`, `.mypy.ini`, `.ruff.toml`,
`pyrightconfig.json`, `.vscode/{settings,extensions,launch,tasks}.json`, `.lazy.lua`,
`.pytemplate/editor.json`, `.github/workflows/ci.yml` (only while `templates/ci.yml` exists),
`.pytemplate/state.json`. Also managed: parts of `pyproject.toml` (6.3), `uv.lock` (only via
`./pyt apply|setup|lock|add|remove|mode|rename|__init`), `typings/raylib/__init__.pyi` (raylib
task `stubs`). `state.json` also holds the `applied` record of `./pyt apply` (section 5.8).

`editor.json` lists `cli.COMMANDS` (name, usage, summary, group): adding or rewording a command
changes a generated file, so run `./pyt render` and commit `editor.json` + `state.json`
with it (`render --check` fails otherwise). Merge conflicts in those two files are resolved by
re-rendering.

`render.apply(cfg, force, check, show_diff)`:
- Hash = sha256 of the content with the BOM stripped and CRLF -> LF (`render._norm`,
  `_digest`), because `* text=auto eol=native` + `core.autocrlf=true` gives CRLF checkouts on
  Windows and editors/PS 5.1 add BOMs. Every runner write uses `newline="\n"`.
- Current hash == new hash -> skip. Recorded hash in `state.json` != current file -> hand-edited:
  not written (warning) unless `--force`. Otherwise write (LF) and record the hash. `--diff`
  prints a hand edit as a unified diff (`render._diff`; a lost last line break, which the hash
  counts, is marked `\ No newline at end of file`: the diff used to be empty).
- A missing or corrupt `state.json` counts as empty (`render._read_state`/`_load_state`: missing,
  not UTF-8 (PS 5.1 `>` writes UTF-16), not JSON, not an object, or `files` not an object; an
  entry whose value is not a sha256 counts as unrecorded): every generated file is overwritten
  without warning, and the next write is valid UTF-8 JSON. It is read as `utf-8-sig`, so a BOM
  does not disable hand-edit detection. A file with git conflict markers (README's procedure
  after such a merge is `./pyt render`) keeps the top-level keys both sides agree on
  (`render._unconflicted`, diff3 style too) without `files`: every generated file is written
  again and the `applied` record survives (it was lost, and a later apply refused with a false
  "app.preset was changed"); a key the sides disagree on is dropped (apply records it again).
  `cmd_apply.load_record` and `save_record` read the file through `render._read_state` too, so
  an `apply` run first after such a merge finds the record as well. `_save_state` keeps every
  other top-level key (`./pyt apply` records its own) and the key order. Hashes of files no longer generated stay recorded
  (a file that comes back keeps its hand-edit protection).
- `--check` and `--dry-run` write nothing. A folder in the way, or a read/write error, is a
  PytError naming the file. `.python-version` is written (a missing one too: a generated file
  deleted by hand) only once uv has that CPython (`envs.ensure_python`: `uv python find`, else
  `uv python install`, else a PytError (3) naming python.cpython), and before any other file:
  the launchers' `uv run --script` follows the file (5.2), and a typo (`3.41`) or a new minor
  offline written there stopped every command, `help` included, with nothing left to write it
  again once pytemplate.toml was fixed. When uv cannot provide it, `render.apply` raises
  `render.NoPython` (a PytError, 3) before its first write, so nothing is rendered.
- `render.auto` runs before most commands and prints one line when something changed; it also
  warns when `pyproject_outdated` (the hint names `./pyt apply`). On `render.NoPython` it renders
  nothing, warns (`generated files not rendered: ...`) and lets the command run: doctor, which
  runs on any Python (5.2), then reports the unusable python.cpython with its other checks, as
  README promises (it stopped with exit 3 before any check, and wrote the typo into a deleted
  `.python-version`: `test_render_core.test_doctor_runs_when_python_cpython_cannot_be_provided`);
  the other commands that render first need that CPython and stop in `cli._restart`.
- Changing `render.HEADER` rewrites every generated file (fine: only files whose current hash
  differs from the recorded one are protected).
- Templates are read as `utf-8-sig` (a BOM is fine); errors are PytErrors naming the file:
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
  `files = src, tests`, so mypy must run with cwd = ROOT (the `proc.run` default); the ruff
  copies under `.build/cfg` hold paths relative to ROOT (`render.ruff_config(relative_to=ROOT)`)
  and reach ruff as a `--config` relative to ROOT (`cmd_dev.config_arg`): ruff reads the paths
  of a `--config` file against its cwd, ROOT for every caller, and expands `$NAME` and
  `${NAME}` in its `src` and in that argument, so absolute ones failed in a project folder such
  as `app$v2` (15.1); the pyright copies use absolute paths (`absolute=True`) because pyright
  resolves paths relative to the config file; the compile-time `mypy.ini` (`render.mypy_ini`
  with `for_compile=` the folder it is written to: the mypyc profile dir, the wheel's work dir)
  only carries `mypy_path = $MYPY_CONFIG_FILE_DIR/<relative path to typings>`, because mypyc
  runs with cwd = stage and mypy splits `mypy_path` on `,` and `:` before expanding variables (an
  absolute path in a project folder with a comma lost typings/).

### 6.3 `pyproject.toml` managed parts

- `requires-python` of the `[project]` table, in any TOML string form (literal, multi-line,
  quoted key, any indent), is rewritten to `">=<min_python>"` (the lowest supported minor:
  usually PyPy's 3.11 with PyPy, else `python.cpython`) and inserted under `[project]` when
  missing (`render._set_requires_python`); a `requires-python` of another table is never
  touched. The key, the `[project]` and `[tool.uv]` headers and the headers around the markers
  (`render._headers`) are found with the TOML statement scanner (`config.scan`, the one
  `set_value` uses): a line of a multi-line string or array that looks like a header or the key
  never counts (a `[beta]` line in a multi-line description ended the `[project]` scan early,
  a second requires-python was inserted and lock, mode and apply refused a valid file). Text the
  scanner cannot read falls back to the line patterns, and `_verify` reports it.
- The `[tool.uv]` block between `# >>> pytemplate` and `# <<< pytemplate`
  (`render.managed_block`). The END MARKER IS AN INLINE COMMENT on the last key line
  (`python-preference = "only-managed"  # <<< pytemplate`) so uv/toml_edit insertions cannot
  detach it; `test_managed_block_bounds_cpython_minor` asserts it. The opening marker is a whole
  line `# >>> pytemplate[: ...]` and the closing one ends its line (`render._BEGIN_RE`,
  `_END_RE`): the preset markers `# >>> pytemplate-preset` never count (they once made `lock`
  replace `[tool.flet]` with the block). `render._managed_bounds`: both markers missing = the
  block is inserted after the `[tool.uv]` header (any spelling: `[ tool.uv ]`, a trailing
  comment) or in a new `[tool.uv]` table; one missing, duplicated, out of order, outside
  `[tool.uv]` or with a table header between them = PytError (restore them, or delete the
  whole block and run `./pyt lock`).
- Compared by MEANING: `pyproject_outdated` and `write_pyproject` parse both texts
  (`render._same_meaning`), so a TOML formatter (taplo: Even Better TOML, LazyVim's toml
  extra) re-indenting arrays or re-spacing comments is no change: `write_pyproject` then writes
  nothing (layout, BOM and CRLF stay; a real rewrite is LF without a BOM). A template that only
  rewords the block's comments does not rewrite old projects.
- `render._verify` refuses (PytError, nothing written) a rewrite that would give invalid TOML
  (e.g. a managed key repeated outside the markers), change anything but requires-python and the
  block's keys (a user key or table between the markers), or leave a managed value out of
  `[tool.uv]`. The block's keys are those a block can write (`render.block_keys`: its own
  `BLOCK_KEYS` and every preset's `[uv]` keys, which a preset switch through `__init` drops); any
  other key between the markers is named and refused ("move it out of the block"): every key
  found there used to count as managed, and lock, apply, mode and rename dropped a user's
  `index-url` or constraint without a word.
- Additive lists (`render.ADDITIVE_KEYS`: `override-dependencies`, `constraint-dependencies`,
  `build-constraint-dependencies`, `no-build-package`, `no-binary-package`,
  `no-build-isolation-package`) belong to the project when its `[tool.uv]` defines them outside
  the markers (`render._adopted`): `managed_block(cfg, leave=...)` then leaves the key out, and
  `_verify` requires the project's list to hold the block's entries (compared by
  `render._entry_key`: blanks, quote style and the PEP 503 name do not count), naming the
  missing ones and why (`render._WHY`). Before, the block owned the whole key: a project that
  kept its own overrides could not enable PyPy (a repeated key), and a raylib project could not
  add a `no-build-package`. A file that already repeats such a key (the project added its list
  while the block had one) is repaired by the next rewrite when the block and the rest each read
  on their own; any other invalid TOML stays an error.
  Entries typed INSIDE the markers are still replaced (the block is generated). `pyproject_outdated` never raises (an unusable file counts as outdated, and
  `./pyt lock` then explains); `render.check_pyproject(cfg)` runs the same checks without
  writing, as a preflight for commands that change other files first.
- `render.auto` only warns (`pyproject_outdated`); `write_pyproject` runs in `lock`, `mode`,
  `apply`/`setup` and `rename` (via `cmd_env.ensure_lock`) and `__init`, because the change
  needs re-locking. `lock` (`cmd_env.cmd_lock`) puts the old bytes of `pyproject.toml` and
  `uv.lock` back when its `uv lock` fails (offline, no solution, Ctrl+C, a full disk that cut
  the new `uv.lock` short) or writes no `uv.lock`
  (`cmd_env.LOCK_READ_ONLY`: `--check`, `--locked`, `--check-exists`, `--frozen`, `--dry-run`,
  `--script` (a script's own lock); `UV_LOCKED`/`UV_FROZEN` set): a rewritten pyproject.toml
  next to the old lock made every `uv run --locked` fail. `-h`/`--help`/`-V`/`--version`
  (`cmd_env.LOCK_INFO`) go to `uv lock` before pyproject.toml is touched.
- Not a managed part, but kept in line by `./pyt apply` (section 5.8): the option-driven
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
  echoes `uv lock` instead (the check would read the unwritten file and pass). A needed re-lock
  under the user's `UV_FROZEN` or `UV_LOCKED` is refused, naming the variable
  (`cmd_env._refuse_a_frozen_lock`): with it `uv lock` writes nothing and exits 0 (UV_FROZEN
  only checks the lock's validity), and mode, apply and rename went on with a stale uv.lock;
  refused, they put their files back.
- `envs.sync` = `uv sync --locked --all-groups` (apply/setup, sync, mode, add, remove): every
  dependency group of `pyproject.toml` is installed, so `./pyt add --group G pkg` survives the
  next sync and reaches a fresh clone (an exact sync of the default groups removed it); `uv run`
  syncs inexactly and never removes them. A group `--all-groups` cannot hold there is left out,
  one `--no-group` each with a note (`envs.left_out`): a non-default group that `[tool.uv]
  conflicts` pairs with another group of the project (a cpu/gpu split; a set that also names
  extras counts only its groups: no extra is enabled), and one whose `[tool.uv.dependency-groups]`
  requires-python excludes the environment's Python (`envs._excludes`, a subset of PEP 440:
  PyPy's exact X.Y.Z, or every patch release of `python.cpython`'s minor; what it cannot read,
  uv decides). With `--all-groups` alone uv refused the whole sync, so sync, setup, apply, add
  and remove failed in such a project while `uv run` worked; the default groups always stay
  (`uv run` needs them too). `add`/`remove` take `--dev` or `--group G`, not both,
  and run `uv add|remove --no-sync` (it still re-locks), then `envs.sync` of `.venv`: uv's own
  sync after `remove` is exact for the default groups and uninstalled every other group. uv has
  written pyproject.toml and uv.lock before that sync: when it fails (a package that locks but
  cannot be built: an sdist that needs a compiler or pg_config) or is interrupted,
  `cmd_env._add_remove` puts both back byte for byte (`_snapshot`, `_put_back`), as a plain
  `uv add` reverts its edits when its own sync fails; kept, they made every `uv run --locked`
  try to build that package again.
- The oldest supported uv is `envs.MIN_UV` = 0.10.12, read from uv's own download metadata:
  the first uv that downloads `pypy@3.11.15` (0.10.11: "No download found for request");
  CPython 3.14 final needs 0.9.0 (0.8.x silently installs 3.14.0rc2) and `uv export --format
  requirements.txt` 0.6.15 (its `pylock.toml` export and install work in 0.10.12). `envs.uv` calls `envs.require_min_uv` right before uv would CREATE
  an environment (its dir does not exist): an older uv exits 3 with `envs.UV_UPDATE`; asked
  once per process, an unreadable version passes. doctor flags it. The managed `[tool.uv]` block
  also carries `required-version = ">=<MIN_UV>"`, so uv itself (>= 0.5.14) refuses every project
  command, the launcher's `uv run --script` included, with "Required uv version ... does not
  match" and `uv self update`. uv reads that setting from the project it finds from the cwd:
  run from a folder outside the project (`/path/to/pyt ...`), an old uv starts the runner
  and `envs.require_min_uv` is the guard. Bump it with the pins
  (`test_min_uv_matches_the_pinned_interpreters`) or a newer uv flag.
- `./pyt clean` (`cmd_env.cmd_clean`): `.build/` (`.build/wsl` in WSL), `dist/`, and with
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
  `./pyt lock`.
- With PyPy supported the block adds
  `override-dependencies = ["cffi>=1.15.1; implementation_name == 'cpython'"]`: PyPy ships
  cffi built in and uv would otherwise try to build cffi from PyPI. A project with its own
  `override-dependencies` outside the markers keeps it, and that list must hold the cffi entry
  (section 6.3).
- Dev group (`pyproject.toml [dependency-groups] dev`): `debugpy`, `mypy` (needs the Rust
  `ast-serialize`: no PyPy wheels), `pyinstaller`, `ruff` and `setuptools` carry
  `implementation_name == 'cpython'`; `.venv-pypy` only gets the app deps plus pytest and
  hypothesis (the runner's property tests need it, a project's own tests may).
- WSL on a Windows checkout (`project.IS_WSL`): separate `.venv*-wsl` envs and `.build/wsl`, so
  the Windows `.venv` is not turned into a Linux one (and `clean --envs` keeps the other side's).
  WSL is read from the kernel (`project.wsl_kernel`: a `microsoft` release or WSL's
  `binfmt_misc/WSLInterop`; `WSL_DISTRO_NAME` alone missed sudo, sshd, cron and systemd units,
  and uv then replaced the Windows `.venv`), the checkout from the deepest mount above `ROOT`
  in `/proc/self/mounts` (`project.windows_checkout`: drvfs, 9p with `aname=drvfs`, or a drive
  or UNC share as its source, so `/d/...` of `mount -t drvfs D: /d` or `automount root = /`
  counts; unreadable: a path under `/mnt/`). `project.detect_wsl` joins the two (the mounts
  read only under a WSL kernel) and gives `IS_WSL`
  (`test_is_wsl_at_import_follows_the_kernel_and_the_mount_of_the_root` imports the runner
  afresh with the kernel release and the mounts faked around its root). The Neovim plugin
  mirrors both (`init.windows_checkout`, `test_lua_windows_checkout_matches_the_runner`) and
  their join (its `env_suffix`: `test_lua_wsl_environments_follow_the_runner` fakes uname,
  fs_stat and io.open and compares `venv_exe` with `project.detect_wsl`).
- Tools outside `uv.lock` run through `uv run --locked --with <pin>` and are pinned in module
  constants: `cmd_dev.BASEDPYRIGHT = "basedpyright==1.40.1"` plus its Node.js runtime
  `cmd_dev.BASEDPYRIGHT_NODE = "nodejs-wheel-binaries==24.19.0"` (basedpyright's only
  dependency, `>=20.13.1`: unpinned, every new Node LTS on PyPI changed `check` and its
  glibc/macOS floor; bump both together) (`check` with `typing.editor = "basedpyright"`) and
  `methods.nuitka.NUITKA = "nuitka==4.2.2"` (no dependencies outside extras). Bump them
  deliberately; `NUITKA` together with `methods.nuitka.NUITKA_PYTHON` (the newest CPython minor
  it supports, `3.14`): raising `python.cpython` past it needs a newer Nuitka pin.
- `mode --supports -pypy` never deletes the old env: it prints a note that
  `./pyt clean --envs` removes the `.venv*` environments (all of them; `setup` recreates
  the ones in use). `apply` prints the same note for every unused `.venv*` of this side
  (`cmd_apply.unused_envs`), `.venv-jit` of older templates included.

## 8. Typing profiles and `check`

- Profiles live in `.pytemplate/templates/typing/<p>.toml` (`mypyc`, `strict`, `warn`, `off`).
  Keys: `description`, `blocking`, `skip_mypy`, `[mypy]`, `[mypy_compiled]` (sections for the
  compiled modules), `[pyright]`, `[pyright_compiled].strict`, `[basedpyright_compiled]`,
  `[ruff] select/ignore/exit_zero`, `[vscode]` (merged into settings; its
  `"mypy-type-checker.severity"` also feeds `editor.json` `typing.mypy_severity`).
  `render.load_profile` reads them as `utf-8-sig` and checks the type of these keys (PytError
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
  (`render._paths_without`, only then does the list depend on the files in `src/`); a folder
  counts only when it holds a `.py`/`.pyi` file (`render._holds_python`, also for `tests/` in
  `.mypy.ini` and pyright's `include`, for `typings/` (`render.typings_dir`: `mypy_path`,
  `stubPath`), for the folders of the VS Code mypy matchers (`vscode._roots`), for the roots of
  basedpyright's `executionEnvironments` (`render._has_code`), and for a `compile.modules`
  entry, written as its `.py` file when its folder holds no code): a folder left holding only
  `__pycache__`, or emptied by hand (a subpackage deleted with `git rm -r`, a package turned
  into a module, stubs deleted with `rm -r`) made the committed file differ from a fresh
  clone's, and CI's `render --check` failed. An untracked `.py` file does count, like any
  source file: commit it with the regenerated `pyrightconfig.json`; with basedpyright the
  excluded paths come first in `executionEnvironments` (the first match wins) without the Any
  rules. The same sections reach the stage's compile-time `mypy.ini`.
- `cmd_dev.run_checks(cfg, backend, rules=True)`:
  1. `_profile_file` writes `.build/cfg/ruff-<profile>.toml` and `.build/cfg/mypy-<profile>.ini`
     (the profile of THAT backend, which may differ from the editor's active one).
  2. `ruff check --config ...` (`--exit-zero` when the profile says so).
  3. mypy unless `skip_mypy`; with PyPy supported `render.mypy_cli_args` adds
     `--python-version <min_python> --python-executable <tool python>`. Exit 1 is only a
     warning when the profile is not `blocking`. The same `python_version = <min_python>` is in
     the `[mypy]` section of `.mypy.ini` and the `.build/cfg` copies (`render.mypy_ini`; never
     the compile-time `mypy.ini`: mypyc compiles for the Python it runs on), so a mypy started
     with no arguments (VS Code's mypy extension) checks as 3.11 too: mypy's
     `python_executable` defaults to its own interpreter, so a config `python_version` never
     makes it look for a `python3.11` (`test_mypy_reading_the_generated_ini_alone_flags_what_pypy_lacks`).
  4. `lintc` rules on the compiled sources (when mypyc is supported and `rules`): errors only
     under the `mypyc` profile, warnings otherwise. A file the runner cannot parse is one
     finding (`imports.parse_error`: a syntax error, syntax newer than the runner's own
     Python, or a source nested too deeply for the compiler's stack; `imports.PARSE_ERRORS`,
     which `mypyc.hidden_imports` and `common.uses_tkinter` catch too), never an internal
     error; so is one it cannot read (`OSError`: another user's file, one locked by another
     program: `cannot read it: ...`; it ended check, build and the hook in a traceback). The
     rules walk the tree without recursion (`lintc._scope_statements`: a generated
     elif chain of ~1000 branches passed Python's recursion limit).
  5. With `typing.editor = "basedpyright"`: basedpyright (`BASEDPYRIGHT` pin) on
     `.build/cfg/pyright-<profile>.json` (`cmd_dev._run_basedpyright`). Exit 1 is only a
     warning when the profile is not `blocking`; any other code, or pins uv cannot install
     (offline, a cold cache: asked first with a `uv run --with ... python -c ""` that echoes
     no command line, because uv then exits 1 as well), fails the check. That step is not
     captured: on a cold cache it downloads basedpyright and Node.js (tens of MB), and uv's
     progress shows it (`-q` hides it; once cached it prints nothing).
- `check all` runs each distinct profile once (cpython and pypy usually share one) and the
  mypyc rules only once, with the strictest profile (`mypyc` when present), so each finding
  appears once in the Problems panel.
- `check` covers the app only: `src/` and `tests/` (`project.code_dirs`); `.ruff.toml` excludes
  `.build`, `dist`, `.pytemplate` and `typings`. The runner is checked by `./pyt selftest`
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
- `compiled_sources`: the `.py` files of `compile.modules` (never `__init__.py`; each entry
  resolved like Python's import by `config.compiled_paths`, section 6.1; an entry that does not
  exist or holds no module -> `PytError`), minus `compile.exclude` (exact module or package
  prefix; an entry naming nothing -> `PytError`), deduplicated. Walks with `mypyc.walk`, like
  `sync_tree` and `common.uses_tkinter`.
- `sync_tree(src, dst, owned=())` copies changed files only and deletes removed ones. Change
  detection is size + `st_mtime_ns` (`copy2` preserves the exact mtime), so a same-size edit
  within one second is detected. `walk` follows symlinked folders (`Path.rglob` does not
  descend into them: a linked `src/assets` arrived empty), skipping a link back to a folder on
  the current path (cycles; two links to one folder are both copied) and never entering
  `SKIP_DIRS`; a broken symlink is a warning, and a folder it cannot list (entered but not read:
  mode 0311, another user's 0711) a `PytError` naming it (os.walk skipped it without a word: every
  build shipped it empty, and the sync deleted the copy an earlier build had made;
  `test_sync_tree_refuses_a_folder_it_cannot_list`). A path that turned from file to folder (or back)
  is replaced; a folder deleted from `src` goes with its caches (a `__pycache__` kept it
  importable as a namespace package). Extensions: only build outputs (`_mypyc_output`: a
  module in `owned`, a `*__mypyc` lib, or an extension next to its own `.py`) are never copied
  from `src/` (a stray in-place build would shadow the stage's, or reach a payload without its
  shared lib) nor deleted from `dst`; any other `.so`/`.pyd` in `src/` (a
  vendored native library, which must be `git add -f`ed past `.gitignore`) is app content and
  synced like any file, so `run mypyc` and every payload see what `run cpython` sees.
  `mypyc.build` passes `owned=modules`; `cmd_build.payload` and `methods/flet.py` use the
  default. Not handled: a case-only rename on a case-insensitive file system. Every copy is
  owner-writable (`copy_writable`; an unchanged read-only copy an older build left is fixed in
  place): copy2 kept the mode of a read-only file of `src/` (a Perforce checkout, a link into
  the Nix store), and once it changed the next build could not replace the copy (Windows: not
  delete it either). The copies made from the payload or the stage (`common.copy_app`,
  `exe_stage`, exe and nuitka stages) use `copy_writable` too, the wheel's copies of `src/`
  also `make_writable` (copytree copies a folder's mode), and the scratch folders are removed
  with `remove_tree` (what rmtree cannot delete is made writable and removed again; a link
  goes as a link).
- `remove_stale_extensions(stage, modules, group, python=, separate=, src=)` runs BEFORE the
  sync (a folder it empties is then removed) and deletes: extensions whose ABI tag
  (`cpython-314-...`, `cp314-...`, a trailing `t` = free-threaded) is not `python.cpython` (old
  code that an interpreter of that version would still import: portable `runtime = "system"`,
  `flet build`); modules no longer compiled; shared libs this build does not use (`<group>__mypyc`,
  or one `<module>__mypyc` per module with `compile.separate = true`, which were deleted on every
  build). A non-output file mirrored from `src/` is left alone.
- Windows deletes no DLL a process has loaded (the app still running from the stage: `./pyt run
  mypyc` in another terminal, a debug session), only renames it, and setuptools' `build_ext
  --inplace` deletes each extension of the stage before it copies the new one in: every build
  that changed one failed there, with the compiler-install hint. So `set_aside` moves an
  extension that leaves the stage (a stale one, and before the build script runs each mypyc output
  of the stage) into `<profile dir>/old-extensions` (`SET_ASIDE`; `_empty_set_aside` empties it,
  best effort, at the next build); one it cannot move is a PytError asking whether the app still
  runs, and so is setuptools' own "could not delete" (never the hint)
  (`test_build_on_windows_sets_the_extensions_the_app_holds_aside`,
  `test_build_says_a_file_of_the_stage_is_in_use`).
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
  `spec["force"]` when it differs: `mypyc_build.py` then passes `build_ext --force`, and
  `mypyc.build` first deletes the profile's `mypy_cache` and generated `c` (with
  `compile.separate = true` mypyc is incremental and reused its cached IR and C, and
  `strip_asserts`/`strict_dunder_typing` are no part of mypy's cache key: `deploy.optimize`
  0 -> 1 kept the asserts in the release binary). `report`
  (`compile_c=False`) and `--dry-run` neither force nor record. A `.build` from before has no
  record: one full rebuild. mypyc dates a new C file 1 s ahead, so a very fast first build is
  redone once by setuptools itself.
- `spec.json` uses stage-relative `c_dir=../c`, `build_temp=../obj`, `build_lib=../lib`: short
  paths under MSVC's MAX_PATH.
- `tools/mypyc_build.py` runs in `.venv` with `VSLANG=1033`: `chdir(stage)`,
  `mypycify(..., group_name, target_dir=../c)`, then `setup(build_ext [--force] --inplace ...
  --parallel N)`. mypycify gets `--explicit-package-bases` (the stage, the cwd, is the base):
  mypy named a module from the highest folder holding an `__init__.py`, so a top-level namespace
  folder of `compile.modules` (`nsx/fast.py`) compiled as `fast` ("did not generate an extension
  for: nsx.fast"), and an app package without `__init__.py` as `core.bench` (the C build could
  not write it); the wheel's `setup.py` does the same with `MYPYPATH=src`. It uses the mypycify API because `python -m mypyc` cannot set
  `strip_asserts`, `group_name` or `multi_file` and always writes to `./build`. When mypycify
  exits or raises (mypy/mypyc rejected the code; the errors are printed) it returns
  `MYPYC_REJECTED` (4, mirrored in `mypyc.py`). When the C build fails (setuptools reports
  every failure as `SystemExit("error: ...")`; a crash of the C step too) it prints the error
  and asks `mypyc_build.missing_compiler` why: the program of the compiler or linker command
  (`CC`, `LDSHARED`, Python's own `cc`) is not found, on macOS it is a `/usr/bin` Xcode shim
  whose developer folder holds no clang (`mypyc_build.xcode_problem`, a mirror of
  `cmd_env._xcode_problem`: the usual Mac without the Command Line Tools), or MSVC cannot be
  set up -> `COMPILER_MISSING` (5), which `mypyc.build` turns into exit 3 (a missing requirement) with
  the install hint; any other C failure -> `C_BUILD_FAILED` (6; setuptools' own exit 1 was
  also uv's). Both are mirrored in `mypyc.py`.
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
- Output is captured unless `-v`; on failure it is printed (`ui.report`: with `-q` too). The compiler-install hint
  (`has_compiler_hint`, with the MSVC tools of the `.venv` Python's platform: `_venv_platform`,
  Windows only) is added only for `COMPILER_MISSING` and `C_BUILD_FAILED` (with `-v` the
  output was not captured, and every type error used to get the hint; a stale `uv.lock` failed
  in `uv run --locked` with exit 1 and got "mypyc needs a C compiler" too). The runner's own
  code is 1 (`mypyc failed (exit code 1)`) for `MYPYC_REJECTED` and `C_BUILD_FAILED`, 3 for
  `COMPILER_MISSING`; any other code is uv's or Python's (`mypyc failed (exit code N): see the
  error above`, exit N).
- After the build every compiled module must have an extension, else `PytError` (and no
  record is written, so the next build is forced).
- `compile.annotate = true`: every mypyc build (`run`, `test`, `compile`, `build`) also writes
  `mypyc.ANNOTATE_HTML` (`.build/reports/mypyc-annotate.html`); cost within timing noise.
- `./pyt compile [--release]` builds the stage without running it: the hidden VS Code task
  `pyt: compile` is the `preLaunchTask` of the mypyc debug config.
- `report` writes the same `ANNOTATE_HTML` with `compile_c=False` (no C compiler needed) plus
  mypy `--any-exprs-report` and `--lineprecision-report` into `.build/reports/`. mypy's exit 1
  (type errors) still writes them; any other code (2: a syntax error or a bad config stopped
  it) is an error, exit 1, never `ok Any expressions per module`.
- Test-time proof: `./pyt test mypyc` runs pytest with `-o pythonpath=...` and
  `PYTEMPLATE_COMPILED`. An `-o` value replaces the whole setting, so it repeats the project's
  own entries (`cmd_dev.pytest_pythonpath`: the first configuration file pytest itself reads in
  the project folder, `PYTEST_CONFIGS`) with the `src` one replaced by the stage
  (`cmd_dev.stage_pythonpath`; the stage first when none is `src`): an extra entry such as
  `tests/helpers` imports under mypyc too. Every preset's
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
  `PytError` (2) naming `path:line`, and one it cannot read a `PytError` (2) naming the file.
- `lintc` rules (compiled code only; findings sorted by `lint`, deduplicated per line and
  message; one finding for a file that cannot be parsed):
  - `compile.forbid_imports`, matched against `import a.b`, `from a import b` (also `a.b`) and
    never against relative imports (the app's own modules); one finding per statement.
  - `librt` (mypyc's runtime library): an error while PyPy is supported; otherwise only when it
    is not a `[project]` dependency (read from `PYPROJECT`): mypy installs it in the dev group
    only, so pyz/portable/wheel builds lacked it (`./pyt add librt --cpython-only`).
  - Class decorators: resolved to full names through the absolute imports of the scope the class
    statement runs in, over those of the scopes around it (`_import_aliases`: module level with
    its if/try/with blocks; a function's own import never decides a module-level decorator, as
    it once did when the file's last import won; star imports included) and compared with
    `NATIVE_CLASS_DECORATORS`, which mirrors mypyc (`dataclasses.dataclass`, `attr.s`,
    `attr.attrs`, `typing[_extensions].final`, `mypy_extensions.trait`/`mypyc_attr`): attrs'
    `define`/`frozen`/`mutable` are NOT native, `@attr.s` is; `@mypyc_attr(native_class=False)`
    silences it. A drift test compares the set with the locked mypyc's own source.
  - Metaclasses and bases (`_non_native_kind`, same message and silencer): a `metaclass=` other
    than `NATIVE_METACLASSES` (ABCMeta; mirrors mypyc's `is_implicit_extension_class`, drift
    test), a base in `NON_NATIVE_BASES` (every `enum` class: its metaclass is EnumMeta;
    NamedTuple; TypedDict), and a subclass of such a class of the same module (a NamedTuple's
    subclass is a mypyc error of its own). A base imported from another module is not looked
    into (no types here). A base of `NON_NATIVE_BASES` (and a subclass of one) is a note
    (`Finding.note`): a warning under every profile, never blocking in `check` nor in the hook
    (`hooks.check_mypyc`): an Enum, a NamedTuple or a TypedDict works compiled, only slower, and
    blocking every Enum of a mypyc project was no help; a metaclass of the user's own stays a
    finding like a foreign decorator.
  - Nested classes and classes inside functions, each reported once (from its nearest class
    or function); t-strings; `if __name__ == "__main__"` (either order) at module level.
  - Module-level `__file__` ONLY when `compile.modules` is one top-level module file, the one
    Python imports (`relative_file_at_import` reads `config.compiled_paths`: a leftover folder
    of that name without `__init__.py` does not turn the rule off): mypyc (>= 1.20.2) sets the
    real `__file__` before a module body runs, from the folder of the shared lib; with a single
    top-level module there is no shared lib and the body sees a relative `<mod><EXT_SUFFIX>`.
    It looks at what runs at import (class bodies, decorators, default values), not function
    or lambda bodies. Pinned by a real-compile test: if mypyc fixes that case, the test fails
    and the rule can go.
  `lintc` does not import `presets` or `mypyc`.
- With PyPy supported user code must be 3.11 syntax and API (no PEP 695;
  `typing_extensions.override`, not `typing.override`). Every re-lock that first makes uv.lock
  resolve for PyPy prechecks it, after the re-lock (`cmd_env.ensure_lock` for mode, apply and
  rename, and `cmd_env.cmd_lock`; both put pyproject.toml and uv.lock back when it fails, so the
  lock never resolves for PyPy unchecked: rename restores nothing itself, and a failed check there
  left PyPy locked in, which the next apply then took as checked):
  `render.gains_pypy` reads the managed block of pyproject.toml, never pytemplate.toml, where a
  hand edit of backend.supported, then any mode or lock, locked PyPy in unchecked and apply
  then skipped the check too; mode also syncs .venv-pypy then. The check
  (`cmd_mode._precheck_py311`, named after the default pin: it checks `Config.pypy_minor`, the
  Python of `python.pypy`, so a project pinned to `pypy@3.12.x` may use 3.12 code): it syncs the
  tools env first (a stale `uv.lock` fails in uv's own step; not under `--dry-run`), then ruff
  `--target-version py3XX` syntax rules (exit 1 = findings; any other code = "could not run
  ruff"), then the mypy errors that appear only as that version and not as `python.cpython`
  (`PRECHECK_MYPY_FLAGS`: `--config-file=` so the project's `.mypy.ini` is never read,
  where the default `off` profile sets `ignore_errors`, and `--check-untyped-defs`); a mypy
  abort (exit 2) is a `PytError` with mypy's output, never a silent pass. Both tools get the
  code folders that hold Python files (`render._holds_python`, as `.mypy.ini`'s `files`: a
  tests/ left with only `__pycache__` stopped mypy with "There are no .py[i] files"); with none,
  there is nothing to check.

## 10. Build methods (`cmd_build.py`, `methods/*`)

- `cmd_build`: backend (`split_backend`), `--method`, `--onefile/--onedir`, repeated
  `--target`, `--no-check`; argparse with `allow_abbrev=False`. Unknown flags go to
  `req.extra` and are appended to the packager argv, for `PASSTHROUGH` methods (exe, nuitka,
  flet) only; `_check_arguments` refuses (exit 2, before the checks, also in `--dry-run`) extras
  for pyz/portable/wheel, `--onefile/--onedir` outside `ONEFILE_METHODS`, `--target` outside
  `TARGET_METHODS` (pyz), `GLOBAL_FLAGS` (`--dry-run`, `--no-render`) typed after the command,
  and a leading bare word (`_stray_word`: "unknown backend 'mypy': did you mean mypyc?", "did you
  mean --method pyz?"). Then `nuitka.check_python`, for pyz `common.check_key` on every key,
  `flet.check_options` (the flet preset; Developer Mode on Windows for a windows target, exit
  3), `portable.check` (the `[deploy.portable] env` values a `.cmd` launcher cannot hold),
  `check_lock` (a read-only `uv lock --check`, exit 2 with uv's reason and `./pyt lock`: a
  uv.lock that pyproject.toml moved past used to stop a method only where it ran `uv ...
  --locked`, with `--no-check` after the payload, exe and nuitka after the previous output was
  removed; a check uv cannot answer, offline or without its interpreter, is exit 3 naming uv's
  reason, never "does not match") and `upx.preflight`: when the method packs with UPX on this host (`upx.uses`: exe on Windows,
  nuitka, portable, flet desktop targets) it resolves the upx binary now, downloading it if
  needed; a portable build with `runtime = "system"` (no interpreter: `upx._always_packs`) only
  checks `deploy.upx.path` and leaves the download to `upx.pack_tree`, which asks for upx only
  when the folder holds a candidate (a pure-Python one built offline before this check, and
  still must). What a method refuses (a stale lock, a missing `deploy.upx.path`, a failed
  download included) is refused before the checks, the mypyc compile and the removal of the
  previous output, and in `--dry-run` too (which only names the upx binary or its download).
  Default method from `deploy.default`; `COMPAT` rejects exe/nuitka/flet with pypy. Runs `run_checks` unless
  `--no-check` (a failure is exit 1, like `./pyt check`). `payload`: the mypyc release
  stage, or `sync_tree(SRC, .build/payload/<backend>)`; none for `cmd_build.OWN_PAYLOAD` (the
  wheel builds its own project from `src/`, and its mypyc backend compiled the release stage
  for nothing, a second full compile). A method whose result is missing or an
  empty folder is a PytError, never `ok done` (`build -v`: PyInstaller took `-v` for
  `--version`; the message says that `./pyt`'s `-v`/`-q` go before the command). The `done:
  ... (N MB)` size (`common.tree_bytes`) counts a symlinked file once (a bundled runtime's
  `bin/python3 -> python3.14`).
- Output: `dist_path(req, suffix)` = `dist/<app.name>-<backend>-<method><suffix>`; portable
  with a bundled runtime adds `-<target key>`, flet adds `-<target>`. The CI template hard-codes
  `dist/<NAME>-<BUILD_BACKEND>-pyz/<NAME>.pyz`: it is coupled to `BuildRequest.out_name`. Every
  method but flet build replaces its previous output whole or not at all, before its packager
  runs (`common.remove_output`: every path is first moved aside into one scratch folder in
  `dist/`, which Windows refuses while a file in it is in use, the app still running from it:
  exit 1 naming the file ("Is the app still running?"), the paths already moved come back,
  nothing deleted; what the moved copies still hold is a warning; portable passes its folder
  and both archives in one call, and exports its requirements first, wheel syncs first, so a
  failure there leaves the previous output alone);
  `pyz._write_archive` turns a `.pyz` in use into the same error. rmtree used to delete half of
  the folder, then fail with a traceback (for nuitka after minutes of work).
- Work dirs live under `.build/<name>/<backend>` (`exe-stage`, `pyinstaller`, `flet-pack`,
  `pyz`, `wheel`, `nuitka-stage`, `nuitka`, `flet-build`); portable builds straight into `dist/`.
- Target keys: `^(cp|pp)(\d)(\d+)-(windows|linux|macos)-(x86_64|aarch64)$`
  (`methods.common.KEY_RE`), e.g. `cp314-windows-x86_64`. `common.check_key` accepts only what
  uv.lock can serve: `cp<python.cpython>` on any OS/arch (another CPython minor got the build
  interpreter's binaries on the same OS, or an empty lib/ elsewhere: the export's markers only
  cover the locked minors), and a `pp` key only when it is the pypy build's own interpreter on
  this machine (`config_host_key`; uv installs PyPy wheels only with a real PyPy: CPython and
  PyPy builds are joined with `pyz-merge`). A pypy build may add `cp<minor>` keys (installed
  with the tools env). The host key's architecture is the INTERPRETER's (`common.host_arch`,
  the runner's, which is uv's managed `python.cpython` like `.venv`'s; not `project.host_arch`,
  the machine's, which upx and e2e use): `sysconfig.get_platform()` on Windows, where
  `platform.machine()` asks WMI for the native CPU (an x64 Python on Windows on ARM labelled its
  x64 wheels `aarch64`), and `x86`/`armv7l` for a 32-bit interpreter on a 64-bit kernel. The pyz
  bootstrap's `_arch` computes the same name (it said `i686` where the builder said `x86`).
- `common.export_requirements` (pyz, portable): `uv export --locked --no-default-groups
  --no-editable --no-emit-project` into `.build/deploy/requirements.txt`. `--no-default-groups`,
  never `--no-dev` (the flet build's export too): `[tool.uv] default-groups` may name other groups
  than dev, and a lint group's ruff went into every pyz (a pure one became host-only) and a flet
  apk build (`test_the_builds_export_no_dependency_group`). `--locked`: a `uv.lock` that
  `pyproject.toml` moved past fails like every `uv run --locked` (with `--frozen` a `--no-check`
  pyz or portable build shipped without the new dependency). `--no-editable`: a workspace or path
  dependency (`./pyt add ./libs/x`) is exported as a path, which `uv pip install --target`
  builds and installs (editable, it left only a `.pth` naming this machine's source folder).
  The same export is written as `.build/deploy/pylock.toml` next to it (`common.pylock_path`),
  which `install_deps` installs from: every file from the URL `uv.lock` names. A requirements.txt
  keeps no index and uv pip reads no `[tool.uv.sources]`, so a package of an `explicit = true`
  index (a private one, PyTorch's) failed every pyz and portable build with "No solution found"
  (`test_pyz_and_portable_install_a_package_of_an_explicit_index`); `skipped_requirements` and
  `requirements_digest` still read the requirements.txt. uv writes the pylock's relative paths
  (a local library) from the project, and reads them from the file's folder:
  `common._rebase_paths` moves them (15.1).
- `common.install_deps` (`uv pip install --target --no-deps -r <pylock.toml>`): a cross target gets
  `--python-platform UV_PLATFORMS[...] --python-version --only-binary :all:` (an sdist built for
  another OS would produce host binaries), plus `--no-binary <name>` for each package uv.lock has
  no wheel for (`common.source_only`, read from `common.LOCK`: an sdist-only release such as
  docopt, a path, git or URL source such as a workspace library; pip's rule, which uv follows:
  the later option wins for that package), which is built here and must come out pure
  (`_built_native`: a platform wheel is a PytError naming it, build that key on its platform
  and `pyz-merge`); before, every cross target failed for such a package. A HOST target gets
  the same `--python-platform` floor, with the interpreter's full `--python-version X.Y.Z` (uv
  reads X.Y as X.Y.0: a requirement marked `python_full_version >= 'X.Y.1'` was left out of
  the build for that very interpreter),
  when this machine can load it (`host_floor`: glibc >= 2.28 x86_64 / 2.35 aarch64, never musl;
  macOS >= `MACOS_FLOOR` 13.0, pinned through `MACOSX_DEPLOYMENT_TARGET` unless the user sets it),
  without `--only-binary`, and falls back to the host's own wheels with a warning when a
  dependency has no wheel for the floor: only when uv says so (its captured error matches
  `common._NO_FLOOR_WHEEL`, shown first); any other failure (the network, an index, a hash, a
  failed build) is raised with uv's code: it was retried without the floor, under that false
  warning, and a transient error lost the floor silently
  (`test_host_floor_is_kept_when_the_install_fails_for_another_reason`,
  `test_host_floor_falls_back_for_real_on_uvs_own_words`). Without the floor uv picked the newest the build machine
  allows (manylinux_2_34 on Ubuntu 24.04: the result failed on Debian 11 / RHEL 8).
  `drop_install_junk` removes uv's `.lock`, `_virtualenv*` and the console/GUI script wrappers
  of `bin/` / `Scripts/` (names from `*.dist-info/entry_points.txt`; their shebang or `.exe`
  trampoline holds the build machine's `.venv` path), keeping other files there (ruff and uv
  wheels look up their native binary at `<target>/bin`) and a real `bin` package; in each
  `*.dist-info` (`_drop_build_records`) uv's `uv_cache.json` and `uv_build.json`, and a
  `direct_url.json` whose URL is `file:` (a local library names its source folder on this
  machine), with their `RECORD` rows (a URL requirement keeps its `direct_url.json`).
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
  mypyc, `--add-data "<src>:<dest>"` (`:` is PyInstaller's documented separator; `exe._data_args`
  writes `<src>` relative to the folder of the spec, `--specpath` or flet pack's cwd, which
  PyInstaller reads a relative data source from: it splits the value at `:` AND at `os.pathsep`,
  and the absolute path of a project in a Windows folder named with `;` gave it two separators,
  `test_exe_assets_hold_no_separator_pyinstaller_splits_at`). The flet
  preset uses `flet pack` instead (`methods/exe._flet_pack`): it runs from its own cwd
  `.build/flet-pack/<b>` because `flet pack -y` wipes `<cwd>/build` and the distpath, which is
  the final `dist/<n>-<b>-exe` itself (on Linux flet pack writes `<n>.desktop` after PyInstaller,
  its absolute `Exec` built from `--distpath`: a build in `.build` moved to `dist/` shipped an
  entry that launched nothing); PyInstaller gets `--clean` through
  `--pyinstaller-build-args` as in the plain build (the UPX level, below); onedir
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
  copy of the interpreter's `base_prefix` through `\\?\` extended paths, `portable.long_path`:
  a share or a mapped drive, which resolve() gives as `\\server\share\...`, takes the
  `\\?\UNC\server\share\...` form, since `\\?\\\server` is no valid name (WinError 123), as
  `e2e.rmtree` does; the `ignore` callback reads the folders back with `portable.short_path`
  before comparing), `boot.py`, `<n>.cmd` / `<n>.sh`. A bundled folder whose `lib/` holds
  `flet_desktop` also carries the Flet client (`portable._bundle_flet_client`: the archive of
  `nuitka._flet_client_archive` in `lib/flet_desktop/app/`, and `nuitka.flet_client_env` in the
  launchers: on Linux `FLET_LINUX_DISTRO` and `FLET_DESKTOP_FLAVOR`, the parts of its name, since
  flet_desktop names the archive it looks for from the user's glibc and CURRENT folder); it
  downloaded ~40 MB at its first start. `runtime = "system"` (a Python of the user's machine, like
  a pyz) carries none: documented. The base's
  `__pycache__` folders are never copied. Prunes `include libs Tools share Scripts`, every `bin/` entry but the interpreter (`BIN_KEEP`:
  `python*`, `pypy*`, `libpypy*`; the base's console scripts, e.g. a 24 MB `ruff` installed into
  it, carried the build machine's paths), on Linux the shared `lib/libpython3.X.so*` when the
  interpreter does not list it in its ELF `DT_NEEDED` (`_keeps_libpython`, `_elf_needed`:
  python-build-standalone links the interpreter statically, so it was 33 MB twice; extension
  modules never link libpython on Linux; an unreadable interpreter keeps it), `tcl*` (Windows
  base), stdlib `test idlelib turtledemo ensurepip site-packages`, `test`/`tests` subfolders of
  stdlib packages (PyPy's `unittest/test`, `lib2to3/tests`...), `*.debug` (PyPy's detached debug
  symbols, 16 MB), PyPy `hpy/devel`, and Tk unless `src/` or an installed dependency in `lib/`
  imports tkinter/turtle (`common.uses_tkinter(lib)`: customtkinter, ttkbootstrap; `mypyc.walk`,
  so a symlinked folder of `src/` counts; a bytes pre-filter, then the AST; a file it cannot
  parse keeps Tk): tkinter, turtle and every Tcl/Tk
  file `TCL_RE` matches in `lib/` (POSIX: `libtcl9.0.so`, `tcl9.0/`, `tk9.0/`, `itcl*`,
  `thread*`), `DLLs/` and `lib-dynload/` (`_tkinter.*`). Deletes `EXTERNALLY-MANAGED`; copies
  `vcruntime140*.dll` from the CPython base into PyPy runtimes on Windows (PyPy's zip lacks
  them). `compile_calls` (console `python.exe`, `-B -f`, `--invalidation-mode checked-hash` so
  the Windows zip's 2-second local times cannot make them stale, `-s <out>` so no `.pyc` embeds
  the build folder): `lib/` and `app/` at levels 0 and `deploy.optimize`, the bundled stdlib
  (`runtime_stdlib`: `lib/pythonX.Y`, `lib/pypyX.Y` or `Lib`) only at the launchers' level,
  `-x` skipping PyPy's broken `lib2to3/tests` data: a read-only install never recompiles.
  The previous `<out>.zip`/`<out>.tar.gz` goes with the previous folder, all or nothing
  (`common.remove_output`; it was deleted first, and kept deleted when the folder was in use);
  `archive` writes gztar on POSIX and,
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
    minute): the launchers leave `PYTHON_MANAGER_*` to the user. With native dependencies, or
    requirements a marker left out on this interpreter (`common.skipped_requirements`, as pyz:
    tzdata on win32, backports-tarfile below 3.12, a local library for win32 only),
    `portable._warn_host_only` warns that `lib/` only fits
    the host key (the launchers start it on any OS and Python at or above the minimum).
  - Every bundled build starts its interpreter before reporting success (`_smoke_runtime`, after
    UPX, with the launchers' `-s -O` plus `-B`): it must run and its `sys.prefix` (a `PTPREFIX:`
    line) must be inside the folder's `runtime/`, else `PytError` (a prune or UPX regression,
    a python-build-standalone/PyPy layout change, an interpreter that finds the uv base again);
    a copy without the interpreter file fails before `compileall` (`_check_interpreter`).
  - mypyc builds are smoke-tested (`_smoke_compiled`, code from `smoke_code`, run with the
    launchers' `-s -O` plus `-B`): the same
    `sys.path` as `boot.py` (`app/` first, then `lib/`), result read from a `PTSMOKE:` marker
    line because imported packages may print (raylib's banner); a failed import shows the
    traceback and raises `PytError`.
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
  moved aside and re-extracted (`_discard`). On POSIX a member whose Unix mode has an x bit
  (`_executable`: an app's helper script, ruff's `bin/ruff`) gets x bits where it has r bits
  (`open()` made it 0644); `pyz._write_archive` stores each file's mode, and `pyz-merge` passes
  the parts' executable modes through (`modes`, a `common/app` file from any part), since it
  rewrites every member from a copy. Every start holds a lock on `<build>/.run-<pid>` for its
  whole life (`_hold`: `fcntl.flock`, `msvcrt.locking` on Windows; the OS drops it when the
  process ends, however it ends; removed at exit) and touches its build folder; `_prune_old`
  deletes only builds beyond the 3 most recently started AND older than a day (`MIN_AGE`) AND
  not in use (`_in_use`: a run file whose lock another process holds; a free one, left by a
  killed start, is removed; a file system without locks leaves `MIN_AGE` alone to protect a
  build: an app started more than a day ago was deleted under it), unlinking their `.complete`
  markers first, and tolerates folders that vanish under it. Without a usable cache (`Path.home()` raises for a UID without
  a passwd entry; a read-only home) it extracts into a per-run `tempfile.mkdtemp` folder removed
  at exit (never a predictable shared `/tmp` path: another user could plant code there).
  Layout: `common/lib` only when the build is "pure": every target site installed exactly the
  locked set (`common.skipped_requirements`: no requirement was excluded by a `sys_platform`,
  `python_version` or `implementation_name` marker; pins count by name and version, a direct
  URL (`name @ url`) and a local library by name: `--no-editable` exports a local library as a
  bare path or a `file:` URL, named through uv.lock's sources (`common._local_names`), and a
  path uv.lock does not name counts as excluded; only name==version lines were read, so a
  win32-only local library was missing from a pyz that said it ran anywhere), the sites hold
  the same distributions and nothing is native (`has_native`); otherwise EVERY target gets
  `targets/<key>/lib` and the build warns which conditional requirements restrict it ("runs on:
  <keys>"). `common/` never holds extensions
  (`bug:` check). The mypyc overlay `targets/<host>/app` holds only the extension files: the
  bootstrap extracts `common/` and `targets/<key>/` into ONE folder, so each `.pyd/.so` lands
  next to its `.py` and the extension loader wins. `_pyz.json`: `name`, `build_id`, `min_python`,
  `targets`, `pure`, `backend`, `host` (the key that built it), `deps`
  (`common.requirements_digest`: the pin lines of the export, not its header), `abi` (per key
  whose binaries carry one, the extension ABIs of `targets/<key>/`, `pyz._target_abis` with
  `common.extension_abis`/`abi_tag`: `cp314`, `cp314t`, `pypy311_pp73`; pyz-merge recomputes it).
  The bootstrap takes a target only when its own ABI (`_abi`, from `EXT_SUFFIX`) is one of them,
  else the pure flavour or its "no build for this interpreter" refusal naming both: a key does not
  tell PyPy 7.3 (pp73) from PyPy 8 (pp80), nor CPython 3.14 from 3.14t, and PyPy 8 took a PyPy
  7.3 build's target and died in an ImportError. Nor glibc from musl, nor their versions: `floor`
  (per key whose `lib/` wheels need anything, `pyz._target_floors` with `common.platform_floor`,
  read from their WHEEL tags, the least tag of each wheel: `glibc 2.28` for the newest manylinux
  one, `musl`, `macos 13.0`, `glibc` for a wheel built on the build machine, `common.this_libc`;
  pyz-merge recomputes it) must be met too (`_meets`: `_libc` reads `os.confstr` for glibc and
  `EXT_SUFFIX` for musl, whose version is never compared; `platform.mac_ver`, where `10.16`, an old
  SDK's name for macOS 11 and newer, counts as met): a musl Python (Alpine) took a glibc build's
  target and died in "No module named 'librt.base64'", as did a glibc older than its wheels
  (`test_pyz_bootstrap_meets_what_the_wheels_need`,
  `test_pyz_bootstrap_takes_no_target_this_machine_cannot_load`). The archive is
  written by `pyz._write_archive` (deflate, never zstd: it must open on 3.11 and PyPy;
  `strict_timestamps=False`: a payload file older than 1980, e.g. from the Nix store, used to
  crash `zipapp`; shebang `/usr/bin/env python3`, mode 0755). Whatever `python3` starts it, the
  bootstrap must reach its `min_python` check (`<name>: needs Python X.Y or newer`): nothing
  before `main()` may need a newer Python, hence its `from __future__ import annotations` (a
  `bool | None` annotation made macOS's python3 3.9 die with a TypeError;
  `test_pyz_bootstrap_reaches_its_version_check_on_an_old_python` runs every Python older than
  3.11 it finds). The `<n>.cmd` wrapper runs each
  candidate interpreter with a minimum-version probe, sets `PYTHONUTF8=1`;
  with `app.gui` its run lines are `start "" pyw/pythonw/pypyw` (`common.windowed`). `pyz-merge`
  (>= 2 parts; `_read_info` refuses a part without a valid `_pyz.json`) requires the same app
  name, `min_python`, `deps` and app code (`common/app`, CRLF-normalised: Windows CI checkouts);
  takes `common/app` and `__main__.py` from the first part, every `targets/<key>/lib` from ONE
  part (the one built on that platform, else the first), refuses two compiled overlays for one
  key, and when parts differ in purity moves each pure part's `common/lib` to
  `targets/<its host>/lib` (an older part without `host`: the single overlay key of a mypyc
  part, else "rebuild it") and keeps no `common/lib`. The merged `targets` come from the
  folders written; `host` is dropped and `merged: true` recorded: a merge of pure parts names no
  platform, so merging it again with a per-platform part is refused, `pyz._part_host`, naming the
  way that works (its original parts in one call; it blamed "an older ./pyt"). It also writes the `<out stem>.cmd` wrapper next to
  `--out` (`pyz.wrapper_path`; the parts' name and `min_python`, the pypy candidate order only
  when every part is a pypy build; an `--out` ending in `.cmd` is refused, and so is a name the
  ASCII wrapper cannot hold, `pyz._CMD_UNSAFE`: non-ASCII, control characters,
  `% ! " ^ & | < >`; the wrapper text is made before the `.pyz` is written). A part's `name` must be an app name
  (`config.APP_NAME`: the wrapper echoes it unquoted). `pyz.check_parts` runs the part and name
  checks in `--dry-run` too. An `--out` it cannot write (another user's folder, a read-only one,
  `/sys`) is one `pyz-merge: cannot write` line, exit 2 (`pyz._cannot_write`; it was an
  internal-error traceback), and `_write_archive` never leaves its `.tmp` behind.
- **wheel**: its `[project] dependencies` go through `wheel.dependencies`: a `[tool.uv.sources]`
  git or URL source becomes a direct reference (`name @ git+URL@REV`), with its `subdirectory`
  (`#subdirectory=`, `&subdirectory=` after the URL's own fragment: dropped for a URL, the
  install built the archive's root project, `test_wheel_declares_git_and_url_sources_as_direct_references`); a path, workspace or
  editable source, a named index, a marker or several sources is refused before the checks
  and the payload (`wheel.check`, from `cmd_build`, also in `--dry-run`): copied as a plain
  name, a local library became a PyPI requirement (an unrelated PyPI package of that name got
  installed). Synthetic build project in `.build/wheel/<b>` (for mypyc a `setup.py` using
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
  copy skips caches and stray build outputs: an extension next to its `.py`, `*__mypyc`,
  `wheel._stray_output`); `wheel._copy_tree` copies it as the stage does (`mypyc.walk`: a
  symlinked folder followed, a link back up its own path not, a broken link a warning; a file it
  cannot copy a PytError naming it): shutil.copytree ended on a dangling link or a cycle in an
  internal-error traceback (`test_wheel_copies_through_links_like_the_stage`);
  assets go into `<pkg>/assets`. The top-level entries of `src/` that `compile.modules` names
  besides the package (`wheel._outside_package`: a lone module becomes `[tool.setuptools]
  py-modules`, another package gets its package data) are copied too, for every backend: only
  `src/<pkg>/` was, so mypycify stopped with "Cannot read file 'src/fastbench.py'" and a
  cpython wheel left the module out. `app.gui` -> `[project.gui-scripts]` (no console window on
  Windows), else `[project.scripts]`. mypyc -> platform wheel; cpython/pypy -> `py3-none-any`
  (even with a vendored native library: the wheel is not retagged). A mypyc wheel loads only in
  its CPython minor: `requires-python = "==<python.cpython>.*"`, and the printed install line is
  `uv tool install --python <python.cpython> <file>` (15.1: uv takes its newest CPython otherwise,
  and the plain line failed for a 3.13 project). Its `uv build` is captured
  under `-q` and shown when it fails (5.3); a failed mypyc wheel asks `mypyc.missing_compiler`
  (`tools/mypyc_build.py`'s `missing_compiler`, run in `.venv`) and a C compiler that cannot
  start is exit 3 with `has_compiler_hint`, as for the stage (it was uv's exit 2 and its generic
  "build failures" hint: `test_wheel_a_compiler_that_cannot_start_is_a_missing_requirement`).
- **nuitka**: `.build/nuitka-stage/<b>`, `uv run --locked --with nuitka==<NUITKA> python -P -m
  nuitka` with cwd = stage (`-P`: `-m` put the stage first on Nuitka's own `sys.path`, so an app
  named `nuitka`, or like a module Nuitka imports, ran instead of the compiler; Nuitka finds the
  app from the folder of `main.py`); `--include-package=<pkg>`, `--include-module` for the mypyc hidden
  imports the tools env can locate (`nuitka.includable`: top-level `find_spec` with the stage on
  `sys.path`, built-ins dropped, compiled modules and extensions always kept; Nuitka stops with
  FATAL on a module it cannot locate, e.g. a platform-guarded `import winreg`),
  `--python-flag=no_asserts/no_docstrings` from the shared `deploy.optimize` (an owner
  decision: no Nuitka-only switch), `--nofollow-import-to` per `deploy.exclude_modules`, the upx
  plugin for a onefile build with UPX on (a standalone folder is packed afterwards, `upx.pack_tree`:
  "Size and UPX" below), then `nuitka.optimization_args` BEFORE `deploy.nuitka.extra_args` and the
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
  prefix (onefile) or `.dist` suffix; none found -> `PytError`. Builds take minutes (~4-7
  min measured). Flet (verified: runs and starts the client): `flet/__init__.py` loads its
  controls lazily (module `__getattr__` + `importlib`), which Nuitka cannot follow, so the
  method adds `--include-package=flet --include-package=flet_desktop` and the package data
  flet reads at runtime (`nuitka.FLET_PACKAGE_DATA`: `icons.json`, `cupertino_icons.json`;
  Nuitka bundles no package data by default, and an app using `ft.Icons` died with
  FileNotFoundError); the flet-desktop wheel
  has NO client, so `nuitka._flet_client_archive` downloads the release archive
  (`flet_desktop.get_artifact_filename()`, the same URL flet uses, or `FLET_CLIENT_URL` when set,
  as flet_desktop does) once into `.build/flet-client/<version>/`, copies it into the stage
  (`flet-client/`) and bundles it, named relative to the stage as the assets are (Nuitka splits a
  data source at `,` and `=` and globs it: under `game, v2` the client was left out with only a
  warning), at `flet_desktop/app/<archive>`, where flet_desktop looks for a bundled client (and never
  downloads one: a damaged archive would break the shipped app at its first start), with the
  name it looks for forced into the binary on Linux (`nuitka.flet_client_env` through
  `--force-runtime-environment-variable`: `FLET_LINUX_DISTRO`, `FLET_DESKTOP_FLAVOR`; the app
  computed another name from the user's glibc or a pyproject.toml in its current folder and
  downloaded the client, section 15.1), its fingerprint `<archive>.sha256` next to it
  (`nuitka.fingerprint_file`, written by `nuitka._fingerprint` when the archive is cached, as
  `flet pack` writes one: without it flet_desktop hashed the whole client at every onefile
  start) and, on Linux, `FLET_APP_ID=<app.name>` (the taskbar identity of flet pack's runtime
  hook; its Windows one, `FLET_APP_USER_MODEL_ID`, is the executable's path at runtime, which
  Nuitka cannot force: the window groups as flet there). So a
  download is cached only when it delivered every byte the server announced (Content-Length)
  and the archive reads to its end the way flet_desktop extracts it (`nuitka.archive_problem`:
  zipfile CRCs, or tarfile over gzip and the gzip trailer); a cached archive is checked again
  at every build and downloaded anew when damaged. 61 MB with UPX; ~25 min build.
- **flet** (`flet build`): requires `app.preset == "flet"`. Windows needs Developer Mode
  (Flutter symlinks; checked in the registry by `methods.flet._developer_mode`) and Visual
  Studio C++; `flet.check_options` refuses both from `cmd_build`, before the checks and the
  payload and also in `--dry-run`, and again in `build`. The stage `.build/flet-build/<b>` is
  persistent (Flutter cache); stale extensions are deleted from it before this payload's are
  copied (a desktop `.pyd` must not reach a mobile/web build). `flet build` ignores `uv.lock`, so `build_pyproject` pins the
  `uv export --frozen --no-default-groups --no-editable --prune flet-desktop` versions
  (`flet._pinned_requirements`; `flet.DESKTOP_CLIENT` goes with what only it needs, rich and
  pygments: a flet build app runs embedded, `FLET_PLATFORM` set by its Flutter host or Pyodide on
  the web, and never starts the desktop client, which was 3.1 of the 5.6 MB of the skeleton's web
  `app.zip`; what the app requires itself stays; a local
  library as `name @ file:///absolute/path`, `common.direct_reference`: editable it was
  `-e ./libs/x ; <markers>`, which pip refused, relative to the project and not the stage; for a
  mobile or web target a pin of a package uv.lock has no pure wheel for keeps only the project's
  own bounds and its markers, `common.unpin_binaries` with `binary_only` and
  `project_specifiers`, named with what is written instead in a warning
  (`flet.relaxed_message`, which also names a kept lower bound: `./pyt add numpy` writes the
  newest release on PyPI as one): those targets take their binaries from Flet's own index, and
  the web from its Pyodide release first, 15.1; for every mobile and web target
  `flet.target_markers` writes uv's `sys_platform` markers (and `os_name`) as `platform_system`
  ones, the only one flet build's pip reads for the target: every web app carried httpx and its
  tree, 1.2 MB more, and an apk lacked an Android-only requirement and got a Linux-only one,
  15.1) and
  serialises the PARSED `[tool.flet]` of the
  project `pyproject.toml` (no other table leaks in; `[tool.flet.app]` alone is kept) with
  `app.path` forced to `STAGE_APP` (`src`, where `build` stages the app; another value is
  ignored with a warning: flet looked for `<work>/<path>/main.py` and aborted after installing
  Flutter) and `requires-python = "==<python.cpython>.*"` (flet bundles the HIGHEST Python of
  its manifest matching it: `>=3.13` gave 3.14 and the cp313 mypyc extensions were silently not
  loaded; a minor its manifest lacks now fails loudly), plus `[project] description` (flet puts
  it in the app's metadata; `config.toml_value` writes it). Mobile/web targets (`apk aab ipa
  ios-simulator web`) cannot load extensions: a mypyc backend ships the `.py`. Desktop embeds
  the `python.cpython` minor, so the mypyc `.pyd`/`.so` files work. `cleanup = true` passes
  `--cleanup-app --cleanup-packages`; `cleanup = false` writes `app = false` and `packages =
  false` into the build's `[tool.flet.cleanup]` unless the project sets them (flet_cli cleans
  the packages by default and its flags have no negative form); `exclude` maps to `--exclude`;
  with UPX the finished folder goes through `upx.pack_tree` (desktop targets only). Verified on
  Windows (Developer Mode on): Flet 1.0.1 downloads ITS pinned Flutter (3.44.8, ~3 GB in
  `~/flutter`, ignoring a scoop Flutter) and a Python build (`~/.flet`); first build ~7 min,
  next ~3 min; 97 MB folder, 78 MB with cleanup + UPX, 38 MB zipped, no unpacking at start.
  Web, verified on Linux (September 2026, the skeleton): 122 s the first build once Flutter was
  there (its download ~100 s), 68 s the next; 12.6 MB (`app.zip` 1.2 MB; Pyodide 3.14 and
  CanvasKit come from CDNs), and in a headless Chromium its Python started in 5 s and Draw drew.
  template-flet.yml builds web and `apk` on Linux on every change to this method (13.2);
  Android apps are built, never run (an owner decision), and iOS apps are not built anywhere.
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
  nuitka onefile = its upx plugin (hard-codes `--best --lzma`; it packs the one binary, and the
  libraries inside the zstd payload never; left out when `upx.excluded` names the binary);
  nuitka standalone (once `main.dist` is in `dist/`), portable (before the smoke test, so the
  packed `.pyd` files are what it loads) and flet = `upx.pack_tree` (PE `.exe/.dll/.pyd` on
  Windows, ELF executables but no `.so` on Linux, in parallel; the level and every exclude
  apply): the plugin has no exclude option and packed every DLL of a standalone folder,
  `python314.dll` and `deploy.upx.exclude` included. Never packed: files over
  `MAX_INPUT` (600 MiB; UPX refuses 768 MiB), `BUILTIN_EXCLUDE` (C runtime, API sets,
  `python3*.dll`, `libpython3*`, and `flutter_windows.dll`: a packed Flutter engine hangs the
  app at startup with a 4 MB working set and no window, measured), binaries UPX rejects
  (`GUARD_CF`: never pass `--force`). `upx.find` order: `deploy.upx.path` (absolute, `~`, or
  relative to the project root, never the caller's cwd; handed to the tools absolute but not
  resolved: PyInstaller wants `<upx-dir>/upx`, Nuitka a file named `upx`, so another file name
  is refused, exit 2: `tools/upx-5.2.1` passed, then Nuitka searched PATH ("No UPX binary
  found", or another upx) and PyInstaller packed nothing without a word; on POSIX it must have
  its x bit, `upx._runnable`, or the preflight refuses it with the `chmod +x` line: a checkout
  from Windows passed it, and the portable build died in `upx.pack_file` with a traceback after
  the runtime copy; `pack_file` turns a upx that cannot start into a PytError), `upx` on
  PATH, the cache (a cached copy without its x bit is downloaded again), a download: UPX 5.2.1
  once (SHA-256 checked, written as `.part` then renamed so an interrupted write never looks
  cached; a cache folder it cannot make or write is exit 3 naming it, not a traceback) to
  `%LOCALAPPDATA%\pytemplate\tools\upx-5.2.1` / `$XDG_CACHE_HOME/pytemplate/tools`
  (`upx._cache_dir`: a relative value is ignored, as the XDG spec says and the pyz bootstrap
  does: it put the download under the caller's folder, `src/relcache/`, which the payloads
  ship, and gave Nuitka a relative `--upx-binary`; a upx found through a relative PATH entry is
  made absolute too); macOS is unsupported (`upx.unsupported_reason`). `flet pack` ships Flet's prebuilt FULL client
  zipped (40.5 MB, libmpv 28 MB inside) and unpacks it on first start into
  `~/.flet/client/flet-desktop-full-<version>-<fingerprint>` (97 MB); the "light" flavor
  exists only for Linux. PyInstaller follows imports inside functions: flet's lazy
  `from PIL import ...` (RawImage) drags Pillow (13 MB) in, hence the preset's
  `exclude_modules = ["PIL"]`.
- Assets at runtime: `resources.assets_dir()` (raylib and flet presets) tries
  `sys._MEIPASS/assets` (PyInstaller), then `<pkg>/assets` (wheel), then the `assets` folder
  next to the package (`src/`, the stage, a portable `app/`, a pyz's extracted `app/`, Nuitka's
  dist). It never reads `$PYTEMPLATE_ASSETS`, which the portable and pyz bootstraps set (and
  assign: `setdefault` kept the value an app started by another app inherited, and it read that
  app's assets first) for the app's own code and for the `resources.py` of projects made before.
  `resources.py` is a boundary module; module-level `__file__` is fine in
  compiled packages (section 9: only a single top-level compiled module sees a relative one).

## 11. Presets (`presets.py`, `.pytemplate/presets/<p>/`)

- `preset.toml`: `description`, `dependencies` / `dev_dependencies` (with `{option}`: those
  entries follow `[preset.<p>]` through `./pyt apply` (`presets.option_dependencies`); the
  plain ones belong to the project after `new`), `[options]` (defaults of `[preset.<p>]`), optional `[uv]` (extra managed `[tool.uv]` keys),
  optional `pyproject` string (extra tables, with `{{name}}`/`{{pkg}}`: `extra_tables`).
  Optional `constraints.txt` next to it: the tested pins (below). `presets.load` reads it as
  `utf-8-sig` and refuses (`PytError` naming the file) invalid TOML or UTF-8, an unknown key
  and a wrong type (`PRESET_KEYS`): `render.managed_block` reads it on every run.
- `files/`: complete skeleton, including a full `pytemplate.toml` (`__init` overwrites the root
  one), `src/main.py`, `src/__pkg__/core/` (compiled) + a boundary, `tests/conftest.py`
  (identical in every preset), optional `typings/` and tools. It must be ruff-clean for any
  name: the pre-commit hook of a fresh project runs `ruff format --check` and ruff on it
  (`test_presets.py` renders every preset with four names, `LONG_NAME` longer than a line,
  under every profile, py311 and py314 targets, and every length of name up to 104 under the
  strict profile: `test_the_rendered_skeleton_is_formatted_whatever_the_length_of_the_name`).
  An app name has no length limit and ruff (100 columns) re-wraps a line that holds a long one
  (a project named with 59 characters or more could not make its first commit), so every line
  of code that holds the name keeps its shape at any length: imports inside the package are
  relative (`from .core import bench`), outside it (`src/main.py`, `tests/`)
  `import <pkg>.x as x` (an import statement has no form to wrap into), and a string holding
  the name sits in a call exploded with a magic trailing comma (the script's `Table`, raylib's
  `InitWindow` and `ArgumentParser`) or, with no call at hand, carries `# fmt: skip` (flet's
  `page.title`); docstrings and comments are never re-wrapped. No calendar year anywhere in a
  preset (it goes stale in later projects).
- Four templating syntaxes coexist: `__pkg__` in paths and `{{name}}`/`{{pkg}}` in text (plain
  `.replace`; `title=f"{{name}}: ..."` in `script/app.py` renders to the app name on purpose);
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
  marker (`_extra_bounds`) or a `[project]` table without `name` is a `PytError` (exit 2).
  `_set_project_name` only touches the `name` of `[project]` (either one-line quoting, CRLF
  kept: `rename` uses it too; a multi-line string is left alone, so the callers' check stops),
  and the text is split on `\n` only (a U+2028 inside a TOML string is not a
  line break). `--dry-run __init` prints the plan (`cmd_mode._plan_init`).
  `presets.init` then (1) writes `pyproject.toml` and runs `uv remove --frozen` (no
  resolution) for the old preset's requirements and every declared one the new preset adds in
  another form (`presets._dropped`: a `flet-cli==1.0.0` left in the dev group of a flet project
  with `[preset.flet] version = "1.0.0"` made the resolving `uv add flet==1.0.1` fail), `uv add
  --no-sync [--constraints]` for the new ones (the pins file relative to the root: uv splits a
  `--constraints` value at spaces, section 15.1) and `uv lock`: the only step that needs the network; a resolved `uv.lock` in which a
  package depends on the project itself (`presets._self_dependents`, section 15.1) is refused
  there, before any file of the skeleton is written; (2) renames `src/ tests/
  typings/` into `.pytemplate-init-*` (all or nothing: a locked file fails the rename before
  anything changed), writes every skeleton file (root `pytemplate.toml` included) and chmods
  `pyt`/`pyt.ps1` on POSIX. Any failure or Ctrl+C in (1) or (2) puts every file back
  (`presets._Undo`, prints "every file is back as it was") and is raised; the last step of (2)
  writes the project's own `applied` record (`presets._record`, section 5.8: the copied one,
  the template's or that of the project `new` ran in, must never stand for it). (3) Deletes
  the aside folder and runs `render.apply(force=True)`.
- `presets.check_name_free` (in `new`, `__init`, their dry runs, and `rename`/`apply`, which
  keep its first line) refuses: a name uv refuses (`config.APP_NAME`: a letter first, a letter
  or digit last, PEP 508), a keyword, a standard-library module of any supported Python
  (`presets.shadows_stdlib`), a backend name (cpython, pypy, mypyc), a Windows device name (`WINDOWS_DEVICES`: `aux`,
  `con`, `nul`, `com1`...: the folder cannot exist there and git cannot check it out), a Python
  command (`INTERPRETER_COMMANDS`: `py`, `python`, `python3`, `pyw`, `pythonw`, `pypy3`...: the
  pyz and portable `<name>.cmd` launchers call them by name, and cmd.exe, which looks in the
  current folder first, found `<name>.cmd` itself and restarted it forever), the launcher's
  name (`LAUNCHER_NAMES`: `pyt`, also the command `pyt install` puts on PATH, where the app's
  own console script would take its place; renaming such an app rewrote every `./pyt` of the
  skeleton's text: rename keeps `./pyt` and `.\pyt` of an app named pyt, `rename._LAUNCHER_BEFORE`,
  `test_rename.test_the_launcher_stays_when_an_app_named_pyt_is_renamed`), the
  project's own folders and files (`RESERVED_PACKAGES`: src (root- and src-relative paths would
  read the same: the VS Code matchers, rename), tests, typings, build, dist, assets; plus the
  preset's `src/` entries such as `main`), and every package the project will lock:
  the declared requirements (minus the current preset's own), their tree in `uv.lock`
  (`locked_names`, markers ignored because uv treats the project's own name alike on any
  platform: it refuses the project, or resolves the dependency to the project itself, 15.1; the
  project's own entry excluded) and the preset's pins (`constraints.txt`: the preset's whole
  tested tree, so a raylib project, whose `uv.lock` has no rich, still refuses `new --preset
  script --name mdurl`). When a preset adds packages the lock does not have, uv may resolve
  them against this lock's versions, so every locked name counts too (conservative). Also a
  module one of those packages installs under another name (`presets.IMPORT_NAMES`, read from
  the pinned wheels' RECORD files: pytest's `py`, which pytest imports before the app,
  markdown-it-py's `markdown_it`, raylib's `pyray`, pyyaml's `yaml`, pillow's `PIL` in lower
  case...; `test_import_names_follow_the_installed_packages` checks it against what `.venv`
  installs), and for any other package of those names what the project's environments
  installed (`presets._installed_import_names`: the RECORD of every `*.dist-info` in `.venv*`,
  either layout: beautifulsoup4's `bs4` once `./pyt add` synced it; `new` reads the source
  project's). Without an environment only the pinned packages are mapped (15.2).
  `new` derives the name from the folder with `name_from_folder` (NFKD without the combining
  marks, every run of other characters, letters without an ASCII form included, -> `-`, no
  `-`/`_` at the ends) and checks it before copying, so `./pyt new ../flet --preset
  flet` fails with a hint to use `--name`.
- **[template repo]** Root `src/`, `tests/` and `pytemplate.toml` must equal
  `presets/script/files` rendered with `name = "myapp"` (`test_presets.py` checks it, and
  that every preset ships the same `tests/conftest.py`). Edit the preset, then regenerate the
  root with `./pyt __init script --name myapp --force` (changes no byte when they already
  match: `test_removals` checks it), or mirror the edit byte for byte.
- `copy_template(dest)` (used by `new`): in a git work tree only the files git tracks (`git
  ls-files --cached`, with their working-tree content; its names are read through git's C
  quoting, `presets._git_path`, so a name that is not UTF-8 keeps its bytes: read as text it
  named no file and was left out silently): untracked files, and tracked ones deleted from the
  working tree, are listed as "not copied", ignored ones stay silent, so `.env`, `.idea/`,
  `htmlcov/`, `*.spec` never reach a new project. A maintainer's new file must be `git add`ed before `new` or `selftest --e2e`
  sees it. A symbolic link is copied as the link git tracks (`presets._copy_link`, dangling
  ones too; where Windows refuses to create one, what it points to, with a warning), never as
  a copy of its target. Without git, or when git does not track `.pytemplate/pyt.py` (a copy inside
  another repository, a project never committed), it copies every file, and says so and why
  (`presets._tracked_template`); a git failure other
  than "not a git repository" (dubious ownership...) is a warning first, with git's own command
  to let it read the folder (`presets._git_refused`, `git_refusal_fix`: it said "not a git work
  tree" of a clone another user owns; install refuses with them, 5.9) (git runs with
  `LC_ALL=C`). Both skip (`presets._skipped`) `.git`, `.build`, `dist`, caches, `.flet`,
  `.venv*`, `template-repo` at any depth; `build/`, `.claude/`, `README.md` and `LICENSE` at
  the root; and `.github/workflows/template-*` (template CI files MUST use that prefix; the CI
  image's folder `template-ci-image/` has it too). A
  project made with `new` is another program, not the template (an owner decision): `new`
  (`presets._make_own`) writes its own `README.md` (`presets.project_readme`: name, preset
  description, getting started) and sets `[project] description` to the preset's
  (`_set_description` through `config.set_value`: a multi-line string is replaced whole, a
  missing key is added, an unusual layout is only a warning), and, from the template
  repository only (the `template-repo`
  marker), copies the template's `README.md` and `LICENSE` to `.pytemplate/README.md` (the
  manual of `./pyt`, of that version) and `.pytemplate/LICENSE` (the MIT notice that must
  travel with the copied runner): `presets.TEMPLATE_DOCS`. A project running `new` passes
  those two on as tracked files, and its own root `README.md`/`LICENSE` stay behind. `new`
  then runs the copy's own runner with `__init <preset> --name <n> --force` inside the copy
  (with this run's `-q` or `-v`, and `--no-render`: init renders every generated file itself,
  and `render.auto` only warned about the source's hand-edited ones; under `-q` init's uv calls
  get `--quiet` too), on the new project's `python.cpython` (`presets.preset_python`: the
  skeleton's pytemplate.toml), which `cmd_mode.cmd_new` asks `envs.ensure_python` for before it
  copies anything (the new `uv.lock` is made with it; where uv cannot install it, Android/Termux,
  `new` stops there, exit 3, nothing written; `proc.runner_argv` with that interpreter, so uv's
  cached environment of the copy's runner holds it from the start),
  `git init -b
  main` (the generated CI runs on `main`; git < 2.28: plain `init` + `symbolic-ref HEAD
  refs/heads/main`; no repository inside an existing work tree, where `cmd_mode.cmd_new` then
  warns that the generated CI will not run: `cmd_mode._monorepo_note`, section 13.2; a work tree
  that ignores the folder does not count, `presets.ignored_by_work_tree`, `git check-ignore
  <dest>/pyt` as hooks asks: a home folder kept in git with `*` in its .gitignore left the project
  in no repository at all, and setup then said to `git init` it) and `git
  add --chmod=+x pyt pyt.ps1` (inside an existing work tree only where it has
  `core.filemode = false`, Git for Windows: a later `git add` records a new file as 100644
  there, `_fix_exec_bit` skips untracked files and the hook refused the first commit). When the copy or `__init` fails (a name uv refuses, no network, Ctrl+C) `new`
  removes what it created (the folder and the parents it made, or only the content of the
  empty folder it was given) and says so ("nothing was written" when the copy failed before it
  made anything: it said it had removed a project it never made); a folder with content is
  refused before anything is written, and so are a destination it may not look into (below a
  folder it may not enter, a name too long: "cannot access"), a folder it cannot list and a path
  below a file (`presets.check_destination`, in `cmd_mode.cmd_new` and its dry run too: Path.exists
  raised the PermissionError on Python 3.11-3.13, an internal-error traceback, 3.14 read the folder
  as missing and git's start said "cannot run git", and a path below a file got a bare `[Errno 20]
  Not a directory`). On success it prints one hint (init prints none: it runs in the copy), `cd
  <dest>` and the launcher's `setup` on lines of their own, for the shell of the launcher
  (`presets.next_steps`: the `PYTEMPLATE_LAUNCHER` prefix `ps1:` (PowerShell single quotes;
  `cd -LiteralPath` for a path with `[ ] * ?` or a backtick, which its cd reads as a wildcard
  pattern), then, unless the launcher is `sh:niubash`, `XONSH_VERSION` (a Python string literal:
  xonsh reads quoted arguments so; `cd @(...)` for a path with `$`, which xonsh expands even
  inside quotes) and `NU_VERSION` (a raw single-quoted nushell string, a double-quoted one for a
  path with `'`), which those shells export: `./pyt.cmd setup` behind `cmd` (pyt.cmd serves
  xonsh and nushell on Windows too), `./pyt setup` elsewhere; then cmd `cd /d "..."` (each `%`
  outside the quotes as `^%`, `presets.cmd_path`: cmd expands %NAME% of a typed line inside
  quotes too, and `a%OS%b` led to `aWindows_NTb`) and
  `.\pyt`, else `shlex.quote` and `./pyt setup`). Never a shell function's `pyt setup`
  (`test_the_next_step_runs_a_launcher_never_a_shell_function`). Behind pyt.cmd a shell that
  exports neither variable (a nushell that stops exporting `NU_VERSION`, nushell/nushell#15533)
  gets cmd's syntax: only the hint is wrong. Outside a project (global mode, 5.2) `new` copies the
  installed template: every file (`presets._tracked_template` asks no git: the snapshot has
  none, and may lie in a home folder kept in git) without a note, never the install record
  (`project.INSTALL_RECORD`, `presets._skipped`); its marker makes `_make_own` copy its README
  and LICENSE as from the template repository.
- `init` is internal only: `cli.INTERNAL["__init"]` (`cmd_mode.cmd_init`), reached by `new` and
  by the template maintainer, listed nowhere. `./pyt init` exits 2 with the hint `./pyt
  new DIR --preset P`: a project's preset is chosen when it is created.
- Tested pins: `presets/<p>/constraints.txt` (`name==version`, sorted, generated, never
  hand-edited) lists every package a project of the preset locks (script 24: the template's
  own `uv.lock`; raylib 26; flet 56), the ones the template's `uv.lock` also holds at its
  versions (a package locked at two versions, a fork by platform, cannot be pinned: none so
  far). `init` hands the ones the project does not lock yet to `uv add --constraints`: a one-off
  (nothing in `pyproject.toml` or the lock manifest, `uv lock --check` passes, `./pyt lock
  --upgrade` moves on), so a new project gets the versions CI tested instead of the newest of
  the day, also when `new` runs from a project of another preset (a raylib project has no
  rich), and packages the source project already locks keep their versions. The name check
  reads the whole list. Regenerate after changing a preset's pins or the root `uv.lock`
  (`test_presets.py::test_preset_pins_hold_the_whole_tested_tree` fails, offline, when the file
  is stale): `./pyt new <tmp>/p --preset <p>` from the template (delete the file first to
  take the newest versions of the packages the template's lock lacks), then write
  `presets.constraints_text(p, <tmp>/p/uv.lock)` to `presets.constraints_path(p)` (a
  `python -c` in `.venv` with `.pytemplate` on `sys.path`) and `git add` it. Rejected:
  `exclude-newer` (recorded in the lock: the next `uv lock --check` fails) and `==`
  `constraint-dependencies` in the managed block (permanent: blocks updates, takes the key
  from the user).
- Hard-coded preset names in the runner: `render.ci_workflow` (raylib: apt GL/X11 libs, no
  PyPy on macOS, no macOS at all for `raylib_software`: `render.NO_WHEELS_FOR`), `methods/exe.build` (flet -> `flet pack`), `methods/flet.build` (flet only),
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
  `ProcessPoolExecutor` (compiled code does not release the GIL), in the event loop where no
  process can start (`flet build` for the web, Android, iOS: section 15.1) or the pool cannot be
  made (`_executor`: NotImplementedError without `multiprocessing.synchronize`, OSError where
  named semaphores fail, a read-only or missing `/dev/shm`; every Draw failed there), and the
  button comes back in a `finally`. mypy overrides relax
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
  directly. `"command": "/bin/sh"`, `"args": ["${workspaceFolder}/pyt", ...]` (no exec bit
  needed) and `"windows": {"command": "${workspaceFolder}\\pyt.cmd", "args": [...]}`. In
  `version 2.0.0` per-OS `args` REPLACE the default ones, so both blocks carry the full args.
  VS Code applies the block of the OS where the task runs (the remote OS under WSL/SSH). Every
  task has `options.cwd = ${workspaceFolder}`.
- Catalog (`vscode.catalog`): `run`, `run <b>` per other supported backend, `test` (default
  test task), `test <b>`, `test all` and `check all` (only with more than one backend),
  `check`, `build` (default build task), `report --open` (label `pyt: report`; mypyc only),
  `compile` (mypyc only, hidden), `lint --fix`, `fmt`, `doctor`, `apply`, `setup`, and one task per
  `[tasks]` entry. Labels are `pyt: <args>` (only the catalog's `report --open` hides its
  `--open`: `run --open` is not `pyt: run`; buttons that name the same task, `report` and
  `report --open`, get one task); each task has `detail` (`./pyt <args>  |
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
  PYTEST (crash lines; never the `-ra` skip summary of a green run, `SKIPPED [1] tests/x.py:5:
  reason`: `vscode._NO_SKIP_SUMMARY` starts every pytest matcher, as `tasks.parse_line` skips it;
  a reason such as `ConnectionError: ...` made an error on a file named `SKIPPED [1] tests/x.py`).
  Under mypyc (a task whose scan has both `pytest` and `mypyc`) pytest
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
  `build --method pyz`) gets its own task. Its words are split like `[tasks]` deps
  (`vscode.split_words`: `tasks.split_words`, plain words on unbalanced quotes; a backslash is
  a plain character, so a Windows path arrives as typed), and labels and `detail` quote them
  back only where needed (`vscode.shown`: an empty argument, a blank or a quote; never a
  backslash or a non-ASCII letter, which `shlex.join` quoted), so an argument with a space
  stays one, every label reads back as its arguments and labels stay unique.
  Without the extension the extra keys are ignored.
- `launch.json`: "src/main.py (CPython, interpreted)" first, with no `python` key (VS Code's
  selected interpreter; works under WSL); PyPy (only when supported; per-OS `python`; the
  debugger is unreliable on PyPy); "Run mypyc stage (compiled modules cannot be stepped into)"
  when mypyc is supported (program `.build/mypyc-dev/stage/main.py`, `.venv` python per OS
  through a `windows` block, `preLaunchTask: "pyt: compile"`, `pathMappings` src <->
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
  `\\wsl$` UNC paths do not work with `pyt.cmd`.

### 12.2 LazyVim / Neovim (`editors/nvim.py`, `.pytemplate/nvim/`, `cmd_nvim.py`)

Files:
- `.lazy.lua` (generated from `.pytemplate/templates/nvim/lazy.lua`): lazy.nvim's `local_spec`
  loads the first `.lazy.lua` found upward from Neovim's cwd, through `vim.secure.read` +
  `loadstring`. It MUST stay static (identical bytes in every mode and preset;
  `test_nvim_render.py` checks 6 configs): Neovim trusts it by the sha256 of its raw bytes,
  keyed by its real path, in `stdpath('state')/trust`. Any byte change (CRLF, a BOM, an edit)
  or moving the folder = untrusted again. Hence `.gitattributes` `.lazy.lua text eol=lf`. It is a
  thin loader: `root = vim.fs.root(vim.uv.cwd(), ".lazy.lua")` (the folder of THIS trusted file,
  the one lazy.nvim read, not a nearer `pytemplate.toml`), then `dofile(root ..
  "/.pytemplate/nvim/spec.lua")(root)`. All the logic (extras, the plugin spec, the guarded read
  of lazy.nvim's `spec.modules`) lives in `spec.lua` and the plugin, which trusting `.lazy.lua`
  already trusts (`.pytemplate/nvim/**`), so a fix there needs no re-trust; only a change to the
  loader itself does (`test_lazy_lua_bytes_are_pinned` pins the sha256 in `LAZY_LUA_SHA256`, so
  it is always deliberate; projects already made keep their own copy).
- `spec.lua` (`.pytemplate/nvim/spec.lua`, `dofile`'d with `root`): normalizes `root`, and returns
  `{}` unless it holds `pytemplate.toml` and `.pytemplate/nvim/lua/pytemplate/init.lua` (a folder
  cloned, vendored or added as a submodule inside a trusted project, with its own `pytemplate.toml`
  but no `.lazy.lua`, is never `root`, so its code and its `.venv` tools never run without a trust
  of its own). It also returns `{}`, with one `vim.notify`, when `root` holds a character Neovim
  reads as a `runtimepath` glob (`[ ] { } , \ ` `'` or a `$` it expands; on Windows only `[ , $`:
  Neovim 0.12.5 globs only `[` there and never hands an entry to 'shell', and every project below
  `C:\Users\O'Brien` was refused): the plugin cannot go on the runtimepath from such a path
  (require fails, E79), so the whole integration is skipped and the other LazyVim plugins whose
  `opts` delegate to it keep working. `cmd_nvim.rtp_unsafe_char` names the same characters
  (`RTP_UNSAFE`, `RTP_UNSAFE_WINDOWS`), and `nvim doctor` (a problem line) and `nvim trust`
  (refused) report them (`test_the_runtimepath_rule_is_the_same_in_spec_lua_and_cmd_nvim`: both
  sets through spec.lua with `has('win32')` faked). The `call` delegates are wrapped in `pcall` so a module that fails to load never breaks
  another plugin's config.
- Trusting `.lazy.lua` also trusts `.pytemplate/nvim/**` (`spec.lua` and the plugin), loaded as a
  local plugin
  (`{ dir = root .. "/.pytemplate/nvim", name = "pytemplate.nvim", lazy = false, priority =
  900, main = "pytemplate", opts = { root = root } }`) and never re-hashed. `spec.lua` also
  declares `optional = true` specs whose `opts`/`config` delegate to the plugin: which-key,
  overseer, nvim-lspconfig, nvim-lint, neotest, nvim-dap, nvim-dap-python, venv-selector.
- `.pytemplate/editor.json` (`editors/nvim.editor_data`, schema 1): ASCII data only, relative
  paths only, no comments. Keys: `schema`, `generated`, `name`, `pkg`, `preset`, `gui`,
  `min_python`, `pypy_enabled`, `backend{active, supported}`, `typing{profile, editor, mypy,
  mypy_severity, python_version, basedpyright, basedpyright_node, task_severity}` (`basedpyright`
  = `cmd_dev.BASEDPYRIGHT` and `basedpyright_node` = `cmd_dev.BASEDPYRIGHT_NODE`, the pins of the
  uvx language server; `task_severity` = `editors.nvim.task_severity`, per supported backend
  `{mypy, ruff}` = `error`|`warning` from its profile's `blocking` and ruff `exit_zero`, the
  rule of `vscode.problem_matchers`), `envs{tools, cpython, mypyc, pypy}` (without the `-wsl`
  suffix, which the plugin adds itself),
  `mypyc_stage`, `tasks[{name, help, background}]`, `commands[{name, usage, summary, group}]`
  (from `cli.COMMANDS`), `build{methods, default}`. The Lua side (`init.sanitize`) validates
  every value (whitelists, patterns; `basedpyright` only as `basedpyright==X.Y.Z`,
  `basedpyright_node` only as `nodejs-wheel-binaries==X.Y.Z`) and never
  runs a program named in it. `test_sanitize_keeps_every_generated_value` feeds it the
  editor.json of every test variant and of the three presets: nothing may change (a whitelist
  missing a new profile, backend or command would silently fall back to a default), and
  `test_lua_whitelists_match_the_runner` compares `BACKENDS`, `PROFILES`, `EDITORS` and
  `SEVERITIES` with the runner's. `init.info` re-reads
  it when its mtime/size changes, stripping a leading UTF-8 BOM first (an editor or PS 5.1 may
  add one to the generated file, and `render` leaves it - it hashes without the BOM, 6.2 - so the
  plugin must accept it, else `info()` falls back: typing off, no commands, an unpinned
  basedpyright); it is regenerated only when `./pyt` runs (render-on-save
  of `pytemplate.toml` covers edits made in Neovim).
- Plugin (`.pytemplate/nvim/lua/`, documented in `.pytemplate/nvim/README.md`):
  `pytemplate/init.lua` (root, `info`, environments, uv lookup, `pyt_cmd`, `pyt_env`,
  `refresh`), `pytemplate/tasks.lua` (`META` per command, output parser, pickers, keymaps,
  `:Pyt`, render on save), `pytemplate/integrations.lua` (lspconfig, nvim-lint, neotest,
  overseer, which-key, venv-selector), `pytemplate/dap.lua`, `pytemplate/health.lua`
  (`:checkhealth pytemplate`), `overseer/template/pytemplate.lua` (provider) and
  `overseer/component/pytemplate/refresh.lua`.

Runner contract from Lua (the fourth caller of section 4.1): argv `{<absolute uv>, "run",
"--quiet", "--python=" .. REQUEST, "--python-preference", PREF, "--script",
<root>/.pytemplate/pyt.py, ...}` (`init.pyt_cmd`; REQUEST empty and PREF `only-managed` while the
tools environment's interpreter exists, `M.tool("python")`, else `>=3.11` and `managed`, as the
launchers pick them)
through overseer / `jobstart` with a LIST, env `PYTEMPLATE_CALLER_CWD=<cwd>`, `PYTEMPLATE_LAUNCHER=nvim`,
`PYTEMPLATE_GLOBAL=""`, `UV_PYTHON=""`, `UV_MANAGED_PYTHON=false`, `UV_NO_MANAGED_PYTHON=false`,
`PYTHONHOME=""`, `PYTHONPATH=""` and `UV_WORKING_DIR=.` (`init.pyt_env`; uv and CPython read
those empty values as unset and `false` as an unset flag, and the runner reads only `1` as
global mode, `.` is the job's folder, so the runner starts where the launchers would start it,
as the project's runner). Never a string command (it would go through
`'shell'`, which may be xonsh or niubash) and never `pyt.cmd`/`pyt` unless uv is nowhere
(the launcher prints the install hints; on POSIX it runs as `/bin/sh <root>/pyt`, like the
VS Code tasks and the git hook, so a checkout without the exec bit still gets them). The uv
lookup mirrors the launchers because a GUI, launchd or MSYS2-login Neovim may have a minimal
PATH; on Windows PATH is searched for `uv.exe` exactly (`exepath('uv.exe')`: a `uv.cmd`/`uv.bat`
shim earlier on PATH would run through cmd.exe and parse the arguments again).

LazyVim wiring:
- Extras imported by `.lazy.lua` (only when the config is LazyVim): `lazyvim.plugins.extras`
  `.lang.python`, `.lang.toml`, `.dap.core`, `.test.core`, `.editor.overseer`. The local spec
  is appended AFTER the user's spec, so an extra not already enabled trips LazyVim's
  import-order check: `.lazy.lua` sets `vim.g.lazyvim_check_order = false` only then; the
  permanent fix is `./pyt nvim extras`. Imports are de-duplicated by module name.
- User options (in `lua/config/options.lua`): `vim.g.pytemplate_python_lsp = "pyright"`
  (default basedpyright), `vim.g.pytemplate_prefix` (default `<leader>j`),
  `vim.g.pytemplate_render_on_save = false` (default true).
- `vim.g.lazyvim_python_lsp` set from `.lazy.lua` is too late when `lang.python` is already
  enabled (read at first import): servers are switched by setting `opts.servers.<x>.enabled`
  in an lspconfig `opts` function (runs last). basedpyright by default (no Node.js; `.venv`'s
  `basedpyright-langserver`, else `uv tool run --from <typing.basedpyright> --with
  <typing.basedpyright_node> basedpyright-langserver --stdio` (`integrations.lsp_cmd`) with the
  versions `./pyt check` pins, its Node.js wheel included (an unpinned request re-resolves
  to the newest release whenever uv's index cache expires; a new Node can raise the
  glibc/macOS floor), else Mason); pyright comes from Mason and
  needs Node.js. Pylance exists only in VS
  Code. The server's `cmd_env` clears `PYTHONHOME` and `PYTHONPATH` (empty, unset for CPython;
  vim.lsp merges it over the environment): the uvx basedpyright runs a Python entry point that a
  caller's `PYTHONHOME` kills before it starts, so no Python language server ran at all; harmless
  for a Node or native server. pyright/basedpyright find `.venv` through `pyrightconfig.json`
  `venvPath`/`venv`, so venv-selector's automatic activation of a cached environment is turned
  off (`integrations.venv_selector`). Its uv flow stays: for a buffer with PEP 723 script
  metadata (`.pytemplate/pyt.py`, `tools/mutation_cr.py`, a user's `# /// script` tool) it runs
  `uv sync --script` and activates that environment, which replaces dap-python's
  `resolve_python` for the rest of the session (the debugger keeps `.venv`: the dap item below).
  It cannot be turned off alone: `vim.b.venv_selector_disabled` also removes a user's own
  `$VIRTUAL_ENV` and its PATH entry.
- ruff server from `.venv` with `mason = false` (same version as `./pyt check`). Mason
  prepends its bin dir to PATH after `.lazy.lua` runs, so always use absolute `.venv` paths.
- mypy (nvim-lint, core in LazyVim): `cwd = root` (finds `.mypy.ini`; mypy then prints paths
  relative to it, the only form the parser matches), `--python-version <min_python>
  --python-executable <.venv python>` when PyPy is supported (like `render.mypy_cli_args`),
  severity from `typing.mypy_severity`, disabled with the `off` profile or without `.venv`.
  The linter is built once (at startup or on a refresh), but `.venv` may appear later (`./pyt
  setup` in a terminal, or any `uv run --locked` of run/test/check): `cmd` is a function
  resolved at every run, like `condition`, and the Windows PATH prefix is built from the path
  even before `.venv` exists (`test_mypy_linter_follows_a_venv_created_later`). On Windows
  nvim-lint wraps every linter in `cmd.exe /C`, where an absolute path breaks
  with a space or `& ^ %`: the linter runs the bare name `mypy` with `.venv\Scripts` first on
  PATH, and it appends the buffer's path (and `--python-executable`) root-relative rather than
  absolute (the last element of `args` a function, read at every run for the linted buffer, and
  `append_fname = false`; `args` itself stays a list, nvim-lint's `(string|fun():string)[]`: its
  Windows wrapper `unpack`s it, and a function there stopped every mypy run on Windows. The
  linter's `cwd = root`, so a relative path carries none of the root's own `& ^ %`, which libuv
  passes to cmd.exe unquoted; `test_windows_mypy_linter_uses_root_relative_paths` runs the linter
  through nvim-lint's Windows wrapping, and `test_the_windows_mypy_linter_runs_through_the_real_nvim_lint`
  through the pinned nvim-lint's own `M.lint`, after LazyVim's merge, where `selftest --nvim` left
  its checkout: `test_nvim_render._pinned_plugins`). nvim-lint REPLACES the environment when a
  linter has `env`, so it passes the full environment plus `PYTHONUTF8=1`, minus `VIRTUAL_ENV`,
  `PYTHONHOME` and `PYTHONPATH` (as `proc.base_env` drops them: a `PYTHONHOME` kills the Python
  before it checks anything and the pattern parser then shows no diagnostic, silently).
  nvim-lint only replaces a linter's
  diagnostics when the linter runs, so once mypy no longer runs (`integrations.mypy_available`:
  the `off` profile, no `.venv` mypy) `integrations.forget_mypy` drops them: `init.refresh` for
  every buffer, the linter's `condition` for the buffer it is asked about (after `mode --typing
  off` they stayed until Neovim restarted; `test_mypy_diagnostics_go_once_mypy_is_off`).
- dap: nvim-dap spawns adapters with raw `uv.spawn` (no PATHEXT), so Mason's `.cmd` shims fail
  on Windows. Adapter order (`dap.adapter`): `.venv` python with debugpy (dev group), the tools
  python, Mason's debugpy venv python, an ephemeral `uv run --no-project --with debugpy`
  adapter; `initialize_timeout_sec = 30` (a cold adapter can take more than the default 4 s).
  The adapter's `options.env` is `dap.adapter_env`: Neovim's whole environment as a list of
  `K=V` strings, the adapter's own values over it, without `PYTHONHOME` and `PYTHONPATH` (it runs
  a Python, `-m debugpy.adapter`, that a caller's `PYTHONHOME` would kill before it answers, as
  for every tool the runner starts). nvim-dap hands `options.env` to `uv.spawn` unchanged, and
  luv reads it as that list, which REPLACES the environment: the old map
  `{PYTHONHOME = "", PYTHONPATH = ""}` was an empty list there, so the adapter and every program
  it launched in its own console (debugpy's default `internalConsole`, neotest's debug runs) ran
  with no variable at all, no PATH, HOME or DISPLAY
  (`test_the_debug_adapter_gets_the_whole_environment_but_pythonhome`,
  `test_a_real_debug_session_keeps_the_environment`: the pinned nvim-dap and nvim-dap-python with
  `.venv`'s debugpy).
  debugpy is looked for by listing `lib/python3*` (`init.subdirs`, as the WinGet folders of the uv
  search), never with `vim.fn.glob`, and every path goes through `init.normalize`
  (`vim.fs.normalize` without its `$VAR` expansion): under a project folder named with `[ ]`,
  `{ }` or `$HOME` the glob matched nothing and the adapter fell back to the network, and a
  backquoted part ran as a command through 'shell' (`test_the_venv_debugpy_is_found_in_any_project_folder`).
  The program of a configuration that names no interpreter (`python`, `pythonPath`: launch.json's
  PyPy and mypyc ones do) runs on the cpython runtime env: dap-python's `resolve_python`, and,
  since venv-selector's uv flow replaces that function for the whole session once it saw a PEP
  723 script (F5 then ran `src/main.py` on `.pytemplate/pyt.py`'s script environment:
  ModuleNotFoundError), the adapter's `enrich_config` is wrapped too (`dap.setup`): `.venv`, unless
  the program is such a script itself (`dap.inline_script`, venv-selector's own test: it keeps the
  environment venv-selector gave it) or `$VIRTUAL_ENV`/`$CONDA_PREFIX` is set (they win, as in
  dap-python) (`test_the_debugger_keeps_venv_after_a_pep_723_script_was_opened`,
  `test_the_real_dap_python_keeps_venv_after_venv_selector_switched_it`: the pinned plugins).
  nvim-dap reads `<cwd>/.vscode/launch.json`
  (per-OS blocks lifted, JSONC accepted) and expands `${workspaceFolder}` to the cwd: a
  provider (`dap.launch_configs`) covers a cwd below the root, and feeds `getconfigs` a BOM-free
  copy when a BOM was added to launch.json (`getconfigs` chokes on one, "Error parsing
  launch.json"). At the root nvim-dap's own provider reads launch.json and still chokes on a BOM;
  that path, and mypy reading `.mypy.ini` with a BOM, are out of the plugin's reach.
- neotest-python: always set `python` explicitly (auto-detection globs `*/pyvenv.cfg`, gets
  two lines with `.venv` + `.venv-pypy` and builds a broken path; its `uv run` fallback also
  syncs); `discovery.filter_dir` skips dot-dirs (`.venv*`, `.build`), `dist`, `build`,
  `typings`. neotest cannot pass `-o pythonpath=<stage>` or `PYTEMPLATE_*`, so mypyc/all runs
  go through the `pyt: test` task (which the runner starts, so `proc.base_env` clears
  `PYTHONHOME`/`PYTHONPATH` there); the direct cpython neotest run of a test inherits Neovim's
  environment, and neotest-python exposes no `env`, so a caller's `PYTHONHOME` reaches its
  pytest - out of the plugin's reach, like `.mypy.ini` and nvim-dap's own launch.json provider.
- overseer: the pytemplate provider (one template per command in `editor.json`, `report` and
  `compile` only with mypyc, plus every `[tasks]` entry) replaces the `.vscode/tasks.json` one
  (`disable_template_modules = {"overseer.template.vscode"}`), otherwise labels would be
  duplicated and tasks would go through `pyt.cmd`. overseer runs `"type": "shell"` tasks
  through `'shell'`: another reason to keep VS Code tasks `process`. Commands that can change
  the mode or `editor.json` (`mode`, `setup`, `apply` (the same `tasks.META` entry as
  `setup`), `sync`, `lock`, `add`, `remove`, `render`, `rename`) get the `pytemplate.refresh`
  component (re-read `editor.json`, LSP `didChangeConfiguration`, rebuild the mypy linter, and
  drop mypy's diagnostics once mypy is off);
  `mode`, `setup`, `apply`, `lock` and `rename` also open their output. Every task carries
  `unique` (`tasks.components`): `run` and background tasks with `replace = true` (a new run
  restarts them), the rest with `soft = true`, which disposes the FINISHED previous run of the
  same command (same name) and never stops a running one. overseer keys a task's diagnostics
  by its name and keeps a finished task until `on_complete_dispose` (never for a run nobody
  opened): without it the problems of a fixed file stayed after a clean re-run
  (`test_every_task_replaces_its_previous_run`). Without overseer, tasks run in a terminal
  split.
- Output parser `tasks.parse_line` (overseer `on_output_parse` -> diagnostics + quickfix):
  strips ANSI (CSI, and OSC ended by BEL or by ST `ESC \`: ruff links its rule codes with OSC 8
  in terminals it recognises, `VTE_VERSION`, `WT_SESSION`, iTerm..., and overseer's own cleanup
  keeps them; `test_parser_strips_every_terminal_escape`), honours the `error: `/`warning: `
  prefixes, types mypy's `error:` lines and ruff's findings (a coded rule `F401` or a hyphenated
  name `invalid-syntax`, as the VS Code RUFF matcher accepts) by the task's typing profiles like
  the VS Code matchers (`tasks.severity`: the strictest `typing.task_severity` of the backends
  the task checks, `tasks.task_backends`: its BACKEND argument, `all`, mypyc for
  `compile`/`report`, every supported one for a `[tasks]` entry, else the active one; E/W
  without editor.json data; `test_task_diagnostics_follow_the_typing_profile`), reads basedpyright
  `  path:l:c - sev: msg` and `path:l[:c]: [sev: ]msg` (mypy, ruff concise, pytest crash
  lines); skips notes, `site-packages` and `in <func>` frames, and pytest's `-ra` skip summary
  (`SKIPPED [1] tests/x.py:11: reason`) and indented warnings summary: a path starts with a
  non-blank character, as in `vscode._ANY_FILE` (they became errors on files named `SKIPPED [1]
  tests/x.py` after a green run: `test_parser_ignores_pytest_summary_lines`). Relative paths resolve against
  the root; mypyc prints them relative to its stage (a copy of `src/`: `<pkg>/core/x.py`), so
  a relative path that exists under `src/` but not under the root lands on `src/` (like the VS
  Code MYPYC matcher). A path INTO the stage (`.build[/wsl]/mypyc-{dev,release}/stage/X`,
  relative or absolute: pytest under `test mypyc` imports the stage) lands on `src/X` when that
  file exists (`tasks.absolute`, like the VS Code stage matchers): an edit made in the stage
  copy is overwritten by the next sync (`test_parser_maps_mypyc_stage_paths_to_src`).
- Keymaps under `<leader>j` (which-key group "pyt"; `tasks.KEYS`): `j` pick, `r`/`R` run /
  run on a backend with args, `t`/`T` test / all, `c`/`C` check / all, `b`/`B` build / on a
  backend, `l` lint --fix, `f` fmt, `m` switch backend, `k` `[tasks]` picker, `d` `dev` task,
  `p` mypyc report, `s` sync all, `S` setup, `D` doctor, `w` task list, `x` stop pyt tasks.
  `:Pyt ARGS` (no args = help; quotes group words through `tasks.split_args`, like the
  `R`/`B` prompts: `:Pyt run cpython "a b"` passes `a b` as one argument). Completion
  (`tasks.complete`): command and `[tasks]` names, then `tasks.argument_words` from the
  command's editor.json usage and `tasks.META`: the backends (+ `all`) as the first argument
  of a BACKEND command, a first choice group (`nvim [doctor|trust|...]`), the flags anywhere
  (`--method exe|pyz...` as `--method=exe`...), the names after `help`, nothing after a
  `[tasks]` entry (`test_pyt_completion_follows_the_command`).
  `<leader>j` was chosen because no LazyVim core or extra mapping uses it.

`./pyt nvim [doctor|trust|extras|bootstrap|sync]` (`cmd_nvim.cmd_nvim`):
- Neovim's directories come from one headless query, never hard-coded (`cmd_nvim.headless`:
  `nvim --headless --clean -n -i NONE -c "lua ..." -c qa!`, which prints one `PTNVIM{json}`
  line; the `-c` snippets are one line without double quotes because they cross the Windows
  command line; respects `NVIM_APPNAME` and `XDG_*`). On Windows `XDG_CONFIG_HOME=X` gives
  `X\nvim` and `XDG_DATA_HOME`/`XDG_STATE_HOME=X` give `X\nvim-data`. `cmd_nvim.QUERY_LUA`
  works on ANY Neovim, so an old distro one (Ubuntu 24.04: 0.9.5, Debian 12: 0.7.2) is reported
  as too old by `doctor` and skipped by `selftest --nvim`: the version comes from
  `vim.version()`'s fields (a plain table before 0.10: `tostring` gave `table: 0x...`, exit 3)
  and `stdpath('state')` runs under `pcall` (an error before 0.8: the data dir then)
  (`test_query_reports_an_old_neovim`, which fakes the old API on the Neovim at hand; the real
  trust test skips below 0.9, which brought `vim.secure`).
- `doctor` (default; exit 1 on real problems): Neovim >= 0.11.2 (`MIN_LAZYVIM`), LazyVim
  installed (`Nvim.lazyvim_installed`, which `./pyt doctor`, `sync` and `bootstrap` ask too:
  LazyVim in lazy.nvim's default root `<data>/lazy`, the config's `lazy-lock.json` naming it, or
  the config's Lua naming the `LazyVim/LazyVim` spec or its `lazyvim.plugins` import outside a
  comment, `config_names_lazyvim`; read from the files, never by running the user's config, which
  may install plugins; a `lua/config/lazy.lua` alone passed, and lazy.nvim's own Structured Setup
  has one without LazyVim, while a LazyVim with its own lazy root was refused), no `local_spec =
  false`, the trust of `.lazy.lua`, missing extras in
  `lazyvim.json` (an unreadable one is a note naming the JSON error: LazyVim skips it without a
  word; `missing_extras` reads the module names only, a hand-written `{...}` entry made it raise
  TypeError), tools (`cmd_nvim.TOOLS`; required: git, curl, tar, fd or fdfind (venv-selector, from
  the `lang.python` extra `.lazy.lua` imports, raises an error on the first Python buffer
  without it) and a C compiler (nvim-treesitter builds its parsers; LazyVim lists it among its
  requirements), each with the install command of this OS (`cmd_nvim._install_hint`, chosen
  when doctor runs; on macOS a `/usr/bin` `git` or `python3`/`python` is an xcrun stub that only
  opens the install dialog while `cmd_env._xcode_problem` finds no clang, so it counts as missing
  with `xcode-select --install`, as `c_compiler` treats `/usr/bin/cc`); optional: rg, tree-sitter, python, node), the uv the
  runner runs on (the plugin runs `./pyt` and the uvx basedpyright with the uv it finds in
  the same places, never `uvx`), and ruff, mypy, debugpy in `.venv` (basedpyright optional)
  (`test_nvim_doctor_needs_fd`, `test_nvim_doctor_needs_a_c_compiler`,
  `test_nvim_doctor_says_how_to_install_every_required_tool`).
- Trust DB `<state>/trust`: lines `<sha256|!> <path>` (CRLF on Windows), path = real path
  (backslashes on Windows; `cmd_nvim.same_path`: case-insensitive on Windows, case- and
  Unicode-form-insensitive on macOS, where Neovim's key comes from realpath(3) with the on-disk
  spelling while `os.path.realpath` keeps the typed one, e.g. after `cd ~/projects/mygame` for
  `MyGame`; an exact entry wins), hash over raw bytes. `trust` calls
  `vim.secure.trust({action = "allow", path = ...})` on >= 0.12 (`bufnr` form on 0.11), with
  the file in `$PT_TRUST_FILE`, after creating the state dir (the DB is opened with
  `io.open(..., "w")`), then re-reads the DB to confirm; already trusted -> nothing. Neovim
  0.12 has no "allow" button: the user picks (v)iew, runs `:trust` and restarts (lazy.nvim
  already skipped the file for that session).
- `extras` adds exactly the five extras above to `<config>/lazyvim.json` after a timestamped
  `.bak`, in LazyVim's own format; it refuses (exit 3) when there is no config or no
  `lazyvim.json` yet (start Neovim once). The file is replaced whole (`project.write_whole`: a
  dotfiles link stays a link); a config it may not write (a Nix store, another user's file) is
  exit 3 naming the extras to enable by hand, with no `.bak` left behind (it was a traceback),
  and so is a `lazyvim.json` it may not read (`enable_extras` reads it once, for the backup and
  the JSON: `test_extras_in_a_config_that_cannot_be_read`).
  `bootstrap` clones the LazyVim starter (its newest
  commit, as LazyVim's own install steps do) and deletes its `.git`, only when the config dir
  does not exist (a `.git` it cannot delete: exit 3, delete it by hand). `sync` = `nvim --headless "+Lazy! install" "+lua dofile(vim.env.PT_NVIM_CHECK)"
  +qa` with cwd = ROOT and `NVIM_LOG_FILE` in a temp dir: install only (`Lazy! sync` would also
  update every plugin of the user's config, rewriting their `lazy-lock.json`, and clean the
  plugins its spec does not name). It refuses with exit 3 while `.lazy.lua` is not trusted (the
  trust prompt would hang a headless run, and from any other folder the project's plugins are
  not in the spec). Neovim's exit code proves nothing (0 after a failed clone), so the check
  file (`cmd_nvim.SYNC_CHECK_LUA`, written to the temp dir) asks lazy.nvim for every plugin's
  `_.installed` and writes JSON to `$PT_NVIM_RESULT` (the terminal keeps Neovim's own output):
  a plugin not installed or no report is exit 1, no lazy.nvim (no `:Lazy`) exit 3, a plugin
  with task errors (`has_errors`: a failed build step) only a warning. Headless Neovim ends its
  last message without a line break, so the result starts a new line first (it was glued to it:
  `test_nvim_sync_prints_its_result_on_a_line_of_its_own`).
- `cmd_nvim.doctor(check)` (from `./pyt doctor`): one line, silent without `nvim`, at most
  one headless call. An `nvim` that cannot be executed (another architecture, a truncated
  download, a noexec mount: `cmd_nvim.headless` turns the OSError into a PytError, exit 3 in
  `nvim doctor`) is a `[--]` line there; it ended `./pyt doctor` in a traceback.
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

- `./pyt selftest [pytest args]` (`cli.cmd_selftest`): `python -m pytest -q -p
  no:cacheprovider -c .pytemplate/tests/pytest.ini --rootdir=. .pytemplate/tests [args...]` in
  `.venv`, in the project folder, then `mypy --strict
  --no-incremental --python-version 3.11 --config-file .pytemplate/tests/mypy-runner.ini
  .pytemplate/runner .pytemplate/pyt.py`. Both must pass. Needs `.venv` (`./pyt setup`).
  The suite has pytest settings of its own (`-c`, `addopts = -ra`), as mypy has: pytest looked
  for them itself and read the project's, which are its app's tests' (pyproject.toml
  `[tool.pytest.ini_options]`, pytest.ini, tox.ini or setup.cfg, and the root conftest.py): a
  coverage gate failed selftest with every test passed, a `python_files` collected no runner test
  (`test_selftest_harness.test_plain_selftest_runs_the_suite_with_its_own_pytest_settings`).
  `--rootdir=.` keeps the test ids `.pytemplate/tests/test_x.py::name` (the uv-floor job
  deselects by one); a bare `pytest .pytemplate/tests` finds the same pytest.ini itself, and its
  ids then start at `test_x.py` (its rootdir is `.pytemplate/tests`).
  mypy checks the host platform only: add `--platform linux` / `--platform darwin` by hand to
  check the other branches (template-selftest runs it on all three OSes). The exit code is
  pytest's when it failed, else mypy's (mypy runs either way, but not after pytest's own
  `-h`/`--help`/`--version`: `cli.SELFTEST_INFO_FLAGS`); `--shells`, `--nvim` and `--e2e`
  return their suite's own code. The arguments are ADDED after `.pytemplate/tests`: a file path
  does not narrow the run (pytest still collects the whole folder); select with `-k EXPR`.
- It must pass in every project made with `./pyt new` too (its CLAUDE.md says so; any
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
  (`test_build_methods.skip_when_older_than`: the floor job runs the suite on 3.11). A real
  project has code of its own in src/ and tests/: a test that copies the project and runs
  `__init` there passes `--force` (the copy is no pristine skeleton), and one that reads a plan
  checks what every project has (`[project] name`), the skeleton's docstring only while it is
  still there (7 tests failed in every real project;
  `test_paths.test_the_tests_that_copy_the_project_pass_in_one_with_its_own_code` runs them in a
  copy with code of its own). Its `[tasks]` are its user's too: a test runs only tasks its own
  fixture defines (`test_cli_core.tasks_project`: it ran the preset's deps-only `ci`, which a
  project may rename, delete or give a cmd;
  `test_paths.test_the_task_tests_pass_in_a_project_whose_ci_task_is_its_own`).
- Property-based tests (Hypothesis, the dev group's pin): `.pytemplate/tests/conftest.py` loads
  the profile `pytemplate` (no example database, so a run depends on the code, the profile and
  the seed only; no deadline; a failure prints its replay blob) on top of the profile Hypothesis
  picked itself, `ci` on a CI (derandomized: the same inputs every run), and registers
  `pytemplate-deep` (2000 examples: `./pyt selftest --hypothesis-profile=pytemplate-deep`). They
  cover `config.toml_value` and `config.set_value` (test_config_rules), button labels read back
  (test_vscode), `envs._excludes` against packaging's PEP 440 (test_envs_core),
  `presets.name_from_folder` (test_presets), the rename invariant with made-up names
  (test_rename) and argv through `pyt` and `pyt.ps1` (test_launcher_sh, test_launcher_win). An
  example that starts a process gets fewer examples, `settings.default.max_examples // N` (read
  at collection, after the profile is loaded), and a test that needs a folder per example makes
  it with `tempfile` (Hypothesis refuses a function-scoped fixture without
  `HealthCheck.function_scoped_fixture`). Hypothesis writes `.hypothesis/` where pytest runs
  even without a database (its charmap and the constants of the code under test, the app's
  strings among them): `.gitignore`, `presets.SKIP_ANYWHERE`, `rename.SKIP_DIRS`,
  `mypyc.SKIP_DIRS`, `presets.pristine` and the dry run of `__init`, `e2e.STATE_SKIP_DIRS` and
  the language guard skip it like the other tool caches.
- `.pytemplate/tests/`: `test_runner.py` (config, render, lintc, imports, target keys),
  `test_no_spanish.py`, `test_launcher_sh.py` (static lint of the `pyt` header rules, `-n`
  syntax checks, `__probe` round-trips per shell found; a caller's `set -eu` in 8 shells,
  symlinked subfolders, backslashes in POSIX paths, niubash's in-process run simulated by
  sourcing (no `_pt_` name left on any exit path), `UV_PYTHON` and the runner's Python, a
  caller's `PYTHONHOME`, `PYTHONPATH` and `UV_WORKING_DIR`, the
  Windows-only sh helpers and a fake `reg.exe` in every POSIX shell, the uv search order with
  fake uvs for `pyt` and `pyt.ps1`, the install prompt on a pseudo-terminal; outside any
  project the installed template in every shell, found through XDG_DATA_HOME or HOME, never
  another user's, a stale PYTEMPLATE_GLOBAL never reaching a project, a project's
  `.pytemplate/deploy.py`),
  `test_launcher_win.py` (static rules on every OS, a PowerShell parser check; the pyt.ps1
  behaviour tests run wherever pwsh exists: injection safety of the Core hand-over, `--%`,
  `-X:v`, typed comma lists and array values, pipeline input and raw stdin, `UV_PYTHON` and
  the other cleared variables, ConstrainedLanguage, the x bit, the installed template outside
  projects and `deploy.py` inside old ones; cmd, the registry, the exit code `cmd /c` callers
  get and cmd reading on in what uninstall leaves of the pyt.cmd it runs only on Windows),
  `test_paths.py` (path spellings, WSL detection, colours in a hidden console, dry runs in a
  throwaway copy),
  `test_render_core.py` (`render.apply`/`auto` and `state.json` in a sandbox, the render
  command's exit codes, the managed pyproject parts for every preset and backend set, typing
  profiles, the generated CI for 36 preset/backend combinations through a strict YAML reader and
  `actionlint` when installed, every output clean and hash-seed independent; real taplo and the
  pinned basedpyright when the uv cache has them), `test_shells.py` (quoting per shell family,
  discovery, the launcher and shell lines of doctor, quick real probes), `test_global.py` (global
  mode, 5.2: the commands that run outside a project and the exit-2 hints of the others, help,
  doctor's machine steps, what no child inherits; `new`'s copy leaving the project folder's own
  mode (setgid, another user's folder) and naming a failed copy, its usage errors as `pyt new`;
  real runs of a fake installed template's
  `pyt.py` that must leave it byte for byte unchanged, and `new` from it, with uv faked and, where
  uv reaches the package index, for real: the copy then runs as a project), `test_vscode.py` (also real
  tool output through each task's matchers and a Node `RegExp` cross-check),
  `test_nvim_render.py` (also loads the Lua modules in `nvim --headless --clean`: parser (stage
  paths, terminal escapes, the profile's severities), uv lookup, launcher fallback, sanitize
  round trip, the mypy linter, `tasks.META`, `unique` on every task, `:Pyt` and its
  completion, a Neovim with PYTEMPLATE_GLOBAL=1 running the project's runner as a project; the
  pinned `.lazy.lua` hash), `test_cmd_nvim.py` (`nvim` subcommands with fake
  Neovims, `nvim doctor`'s lines, `nvim sync`'s plugin check (also in a real Neovim with a fake
  lazy.nvim), the query of an old Neovim, the `selftest --nvim` harness: pins, base reuse,
  smoke parsing, tree kill), `test_fixes.py` (regression tests of the
  runner fixes: portable smoke with `lib/`, lazy `{python}`, the pyz `.cmd` wrapper, binary
  preset files, `compile.annotate`, `sync_tree` ns mtimes, portable launcher quoting and
  version probes, unknown arguments, `app.preset`, pinned tools, flet pyproject, wheel
  options), `test_e2e_plan.py` (the pure planning of `e2e.py`, its pin/docs/file-state checks,
  git isolation, host detection, the HOST_GAPS drift guard against the generated CI, and
  template-e2e.yml's triggers and depths), `test_e2e_run.py` (its running parts with the steps
  faked: logs, timeouts that kill the tree, exit codes, the JSON report, SIGTERM/SIGHUP, the
  step kinds on small fake projects), `test_build_methods.py` (argv and
  output discovery of exe, flet pack, Nuitka and flet build with the packager recorded; the
  flet build's pins (flet-desktop pruned by the real uv, offline, on a hand-written lock; the
  web markers, also through `packaging`'s marker evaluation);
  `cmd_build` argument checks; target keys, `install_deps` floors and junk; the pyz layout,
  `pyz-merge` and the real bootstrap run in subprocesses with the cache redirected; one REAL
  host pyz build run with `python -S`, skipped when uv cannot install offline; portable prune,
  launchers, precompile and the runtime smoke with real interpreters), `test_upx.py` (UPX
  flags, candidates per OS, the pinned download with fake archives), `test_presets.py` (preset
  data and skeletons, ruff under every profile, the skeletons' own code with flet and raylib faked, name rules, pins, `copy_template`,
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
  `./pyt init` exits 2 with its hint; `new` and the maintainer route through `__init`, for
  real in throwaway copies),
  `test_config_rules.py` (every schema field and validate rule with a positive and a negative
  case, the encodings, `set_value`/`update_file` on taplo-formatted, CRLF and BOM files and
  commented multi-line arrays, `mode` argument parsing, a failed re-lock or sync putting every
  file back (a fake uv), `new --dry-run` inside another git work tree, and real `mode
  --typing/--editor/--supports` round trips in a throwaway copy that must restore every byte), `test_cli_core.py` (the runner's core: global options,
  dispatch, render-before-command, help, the exit code of every outcome, every command rejecting
  a bogus argument, `proc.run` dry-run/errors/signals/threads, `base_env` per variable, Ctrl+C
  and a SIGTERM/SIGHUP passed on, with real children that trap them, a closed stdout, `[tasks]`
  deps/cycles/placeholders/cwd/env/uv modes/arguments, `check`/`test`/`lint`/`report` semantics
  and their dry runs, the mypyc tests' pythonpath, exit codes through `pyt.py` in a
  throwaway copy, and the restart on `python.cpython`: which commands need it, the second
  runner's command line, folder, environment and exit code, with uv faked, and for real through
  uv on another CPython), `test_envs_core.py` (the section 7 contract, `MIN_UV`, clean,
  sync/add/remove/lock command lines, `ensure_lock`, `python.cpython`'s interpreter (found,
  installed, or why not: `no_download_problem`), exec bits in a throwaway git repository, compiler
  checks, doctor lines and exit code; real uv only offline in `.venv`), `test_hooks.py` (the hook in throwaway
  repositories, git runs it for real; the real ruff/uv command lines against `.venv`),
  `test_install.py` (`./pyt install`/`uninstall` with the runner of a throwaway clone of the
  template and HOME, XDG_DATA_HOME, LOCALAPPDATA and UV_TOOL_BIN_DIR in tmp_path: what the
  installed template holds, a second install swapping it whole, every refusal before a write
  (gathered in one message, in process: `make_plan` with the clone faked; a linked data folder,
  and on Windows a pyt.exe before pyt.cmd), a new bin folder taking the earlier one's launchers
  away, dry runs, uninstall from the clone, a project and the installed template, leftovers, an
  uninstall a file in use stopped (moved aside, or in place with its record last) that the next
  one finishes, `new`
  copying the same files from the installed template (a dotfiles repository around it too),
  the launcher and the installer agreeing on the folder; the swap in process with a failure, a
  Ctrl+C, a SIGTERM or a SIGHUP injected at every step, and a launcher that cannot be replaced;
  the installed template's mode; the lookup rules, PATH and doctor lines (no data folder, the
  launchers left in the recorded bin folder, the execution policies); the pyt.cmd cmd
  runs for the run itself: the stand-in uninstall leaves (where cmd reads on), install refusing
  to replace it; on Windows the bare `pyt` through cmd, Windows PowerShell and pwsh, and `pyt
  uninstall` through the pyt.cmd it removes), `test_apply.py` (`./pyt apply`/`setup` in throwaway projects with a fake uv that edits
  pyproject.toml like `uv add/remove --frozen`: the per-key matrix, `[preset.*]` changes, the
  record (and when it is written: failed steps after the lock, a rename whose lock fails),
  preset detection by every trace, hand-edited name/preset, refusals before any write, the
  PyPy precheck after the lock (restored on failure), dry runs, idempotence, every hook state
  with real git (a monorepo chain too), doctor lines, hints; real `./pyt` runs in a
  copy of the template, and `test_uv_frozen_edits_only_pyproject` checks offline the uv
  behaviour the fake imitates), `test_rename.py` (the skeleton invariant for 3 presets x 7
  pairs x LF/CRLF, random names and round trips, every rewrite rule, scopes, TOML keys and
  module keys, string prefixes and escapes (names of one or two letters), loader arguments,
  encodings and line endings, links and junctions, git states, rollback (a write cut short by
  `ulimit -f` in a child process), the ruff tidy-up, the command in-process and a real run in a
  copy), `test_selftest_harness.py` (the exit codes CI trusts: plain `selftest`
  with pytest and mypy faked, `--shells` with the probes faked but `_run_all` and the table
  real, `--nvim` with Neovim, the base and the smoke runs faked: 0, 1 on any FAIL, 2 usage, 3
  with `--require`), `test_mutation.py` (`selftest --mutation`: the options, the test map and
  its order, junit times, diffs and the lines they change (whatever the user's git settings),
  where no mutant is made, the exception-handler mutants the runner makes (run: the exception
  goes through), the mutants skipped, the verdict of every way pytest ends (colours and
  subtests too) and its detail (one line of 200 characters at most), a fake Cosmic Ray side
  (and one that died, hangs or was closed), the snapshot, the base and its lock (the user's
  own), the workers' environment and copies (links, a submodule, a link that cannot be made), a
  git or a ps that fails, the parser of Python 3.11.3 and older (a NUL byte is a ValueError
  there), real pytest runs over their limit or stopped with their whole process tree (sessions
  of its own too), the threads, deferred signals, the baselines (one that fails, one that never
  ends: their mutants never run; one a stopped run never reached), what a killed run left in
  the base, the report, an error after it, and the exit code; every operator and the pins of
  Cosmic Ray's defects against the real Cosmic Ray, and one real run of a toy module, skipped
  without Cosmic Ray's environment),
  `test_workflows.py` (template repository only: the promises of the
  template-*.yml workflows, the keepalive covering every scheduled one, the gates, `-latest`
  labels, pinned actions, and actionlint on every workflow when installed; the CI image: the
  pins `pins.py` reads from the code, what its tag hashes and that a change moves it (CRLF and
  a BOM do not), its workflow's build, push and container rules, the workflow literals that
  repeat its pins, shellcheck of its scripts, mypy --strict of pins.py, and, inside the image
  only (`CI_IMAGE_INPUTS`), that it holds this checkout's inputs and every tool the suites look
  for; the flet builds: template-flet.yml's triggers, steps, caches and uploads, the exact
  browser pins, and its `check.py`: the skeleton's texts it looks for, the runner's output
  folders, the target edit through the project's own runner, the .apk layout on fake APKs, the
  browser steps on a fake page with a clock of its own, the static server's types, its command
  line and mypy --strict).
- **[template repo]** Language guard `test_no_spanish.py`: skipped unless
  `.pytemplate/template-repo` exists. Scans `git ls-files --cached --others --exclude-standard`
  (so new untracked files count) for accented Spanish letters and a list of Spanish words
  (including the template's former default app name). Add `lang: allow` to a line to allow it
  on purpose.
- Test rules: tests that need a missing tool or shell must skip cleanly (they also run on
  Linux/macOS CI), and so must tests that need the network: the real runs in `test_rename.py`
  and `test_apply.py` re-lock with `uv lock` and SKIP with "needs PyPI" when uv cannot reach
  the index (`rename.needs_pypi`), and so do the real `new` runs (`test_presets`' `network`
  fixture, `test_removals`, `test_global._offline_reason`); everything else in `./pyt selftest` works offline once
  `./pyt setup` has run. Tests that spawn `./pyt` must scrub `UV`, `VIRTUAL_ENV`,
  `UV_PROJECT_ENVIRONMENT`, `UV_PYTHON` and `PYTEMPLATE_*` from the child env (pytest itself
  runs under `uv run`). An environment built from scratch keeps the caller's `LANG`, `LC_ALL`
  and `LC_CTYPE` (`test_launcher_sh._no_uv_env`): in the C locale yash cannot read a project
  path that is not ASCII. Keep them fast: a runner start costs ~0.3 s, and the Windows launcher
  and path tests already take 10-40 s under load.
- `test_runner.py` matches message substrings (`unknown key 'backend.mode'`, `boolean`,
  `is not in backend.supported`, `exact version`, `clashes with`, and the lintc texts `flet`,
  `@cache`, `nested class`, `__main__`; it asserts NO `__file__` finding for the default
  package), and so does `test_mypyc_core.py` (`cannot parse`, `regular (slow) Python class`,
  `relative path`, `dev group`, `does not exist on PyPy`, `matches no module`, `is inside`,
  `mypyc failed (exit code 1)`, `could not run ruff`, `mypy could not check`...): rewording
  those messages means updating the tests in the same commit.
- `./pyt selftest --shells`: section 4.9.
- `./pyt selftest --nvim [PRESET,...] [--keep] [--fresh] [--require] [--timeout S]
  [--dir DIR]` (`nvimtest.selftest`): isolated LazyVim under `--dir` (default `%TEMP%\pt\nvim`,
  `$TMPDIR/pt-nvim-<uid>` elsewhere, made 0700; on POSIX an existing `--dir` another user owns,
  or one every user can write, is refused: `project.check_private_dir`, since code runs from
  it, and checked again once it exists, `project.make_private_dir`, as for `--e2e`): `<dir>/x/{config,data,state,cache}` = the `XDG_*` homes,
  `<dir>/base.json` = the base is complete, `<dir>/p/<preset>` = scratch projects,
  `<dir>/logs/` = one log per step. It stops unless Neovim reports every stdpath inside
  `<dir>/x`, refuses a `--dir` inside the template or one that is a file (exit 2), and only
  wipes a dir carrying its marker `.pytemplate-nvim-test`. One run at a time per `--dir`: `_run`
  holds `project.base_lock` on `<dir>/lock` around the run (a second run is refused, "another run
  is using <dir>", instead of deleting the first's logs or its base as it installs). Its `./pyt` steps get
  `e2e.isolate_git` (the ceiling at the `--dir`'s parent, no global or system git config), as
  those of `--e2e`: with a `--dir` inside the user's work tree `new` skipped `git init` and, with
  `core.filemode = false`, staged the scratch launchers in that repository, where they stayed
  (`test_nvim_pyt_steps_never_touch_a_repository_around_the_dir`); a `--dir` that would hide
  the template's own repository is refused (`e2e.hidden_template_repository`). Neovim keeps the
  user's git config (lazy.nvim clones with it): git's global config is two files, `~/.gitconfig`
  and `$XDG_CONFIG_HOME/git/config`, and `nvim_env` moves `XDG_CONFIG_HOME`, so `nvimtest._run`
  points Neovim's `GIT_CONFIG_GLOBAL` at a `<dir>/gitconfig` that `[include]`s both of the user's
  (`nvimtest.user_git_config`), unless the user set `GIT_CONFIG_GLOBAL` themselves; else the XDG
  file (a proxy, `url.*.insteadOf`, `http.sslCAInfo`) was lost and the clones failed. The include
  paths are quoted values (`nvimtest.git_config_value`): raw, a home folder named with `#` or `;`
  ended the include at a comment and one with `\` made every git call fail
  (`test_nvim_git_config_includes_a_home_whose_name_git_would_read_as_syntax`). Pinned, so a red run is a regression and not upstream drift: the
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
  clone of the starter HEAD, `Lazy! sync`), in a base installed again on every such run (a base
  kept from an earlier one holds the commits of its day, which `record_pins` would report as
  the new pins). A pinned base is made once and reused; `<dir>/base.json` records starter,
  rev, the lock's sha256, the Neovim version and the starter commit the clone held, and a base
  made for other pins or another Neovim is reinstalled (`--fresh` forces it). A base that
  cannot be installed (the clone, a `Lazy!` step, a pin that does not hold) exits 1. Per
  preset: `./pyt new` (name `pt-<preset>`), `./pyt sync cpython` (clean env), `./pyt
  mode --typing strict` (every preset ships `typing.relaxed = off`, which would leave the mypy
  linter untested), trust through the API, `Lazy! install` from the project, and
  `nvim --headless -c "doautocmd UIEnter" -c "luafile .pytemplate/nvim/tests/smoke.lua"` with
  `PT_ROOT=<project>` (timeout 600 s). Every child runs in its own session: a timeout or Ctrl+C
  kills the whole tree (`nvimtest.kill_tree`: SIGTERM to the group, so Neovim can stop its
  jobstart jobs, then SIGKILL after 5 s; `taskkill /T` on Windows), and so do SIGTERM and SIGHUP
  (`e2e.termination_as_interrupt`, as for `--e2e`: they never reached a step's session, and its
  Neovim went on as an orphan writing into `--dir`): `interrupted`, 130, the logs folder
  named (`test_selftest_harness.test_nvim_termination_signal_kills_the_running_step`). Per-preset table with
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
  watchdog; `PT_ROOT` optional. Parse only lines that start with those markers. Every path it
  hands to an Ex command goes through `vim.fn.fnameescape` (`:edit` expands `%`, `#` and `$NAME`:
  E499 under a `--dir` or TEMP holding `%` or `#`) and it lists `tests/`, never globs it
  (`test_cmd_nvim.test_smoke_hands_every_path_to_an_ex_command_escaped`). It runs
  `./pyt help`, `render` and `lint`, and briefly creates `src/<pkg>/_pt_smoke_lint.py` and
  `_pt_smoke_mypy.py`. 20 checks, including the launcher fallback while the scratch project's
  `pyt` has no exec bit (POSIX), stage-relative mypyc paths in the output parser, the mypy diagnostics with the typing
  profile's severity, a debugger stopping at a breakpoint, and the mypyc launch configuration
  (overseer runs its preLaunchTask `pyt: compile`, then a breakpoint in `src/main.py`).
- `./pyt selftest --e2e [PRESET ...] [--backends B,..] [--methods M,..] [--quick|--full]
  [--gui auto|on|off] [--keep] [--reuse] [--json] [--base DIR]` (`e2e.selftest`; `e2e.plan`
  lists the rows of a preset): per preset (default script, raylib, flet) `./pyt new
  <base>/<preset>` from THIS template, then what a user does there. App names `e2escript`
  (named like its package, `e2e.SAME_AS_PACKAGE`: nuitka's `<app>.bin`, the package folder
  next to the executables), `e2e-raylib`, `e2e-flet` (name != package). Every depth:
  - `verify copy` (`do_verify`): nothing `new` must leave out (`template-repo`,
    `template-*.yml`, `.claude`, `.build`, `dist`, `build`, `.venv*`); a git repository with
    `pyt` and `pyt.ps1` at 100755; `uv.lock` at the tested versions (`lock_problems`:
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
    the output must contain the preset's text and, for mypyc, the compiled marker. The pyz and
    wheel smokes take the interpreter of whichever `.venv`/`.venv-pypy` (with or without the
    `-wsl` suffix) the project's OWN runner made (`e2e.runtime_python`): under WSL with a Windows
    checkout the default base is on the Linux file system, so the project made `.venv`, not this
    runner's `.venv-wsl` (and the reverse with `--base` on /mnt/c).
  `--quick`: each backend's `deploy.default` method only. Default: every pair
  `cmd_build.COMPAT` allows but nuitka, `./pyt selftest` in the project, a usage error
  (`build <b> pyz` must exit 2) and a rename round trip (`--dry-run rename`, `rename` to
  `e2e.renamed_app` (the other name shape), `render --check`, `test`, the wheel built and run
  under the new name, a commit through the hook (rename refuses a dirty tree), `rename` back).
  `--full`: + nuitka, a `mode --supports +/-pypy` round trip (back to the preset's active
  backend) and a `[preset.*]` option edit (`e2e.OPTION_EDITS` with `config.set_value`, then
  `./pyt apply`: pyproject.toml and uv.lock must follow, `doctor` passes, a second `apply`
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
  (smoke scratch); default base `%TEMP%\pt\e2e` / `$TMPDIR/pt-e2e-<uid>` (made 0700; on POSIX
  a base another user owns, or one every user can write, is refused: `project.check_private_dir`;
  every step runs code from it, and a shared /tmp/pt-e2e let another user swap a project in
  between two steps; checked again once it exists, `project.make_private_dir`: checked only
  before `mkdir(exist_ok=True)`, a folder another user made in between was taken,
  `test_e2e_plan.test_a_base_another_user_makes_after_the_check_is_refused`; a base it cannot create,
  below a file or in a folder it may not write, is one error line, exit 2, and so is a link whose
  folder is gone, for `--dir` and `--mutation`'s base too: `check_private_dir` stat()ed it, an
  internal-error traceback, `test_e2e_plan.test_a_base_that_is_a_link_to_no_folder_is_one_error_line`); only a base carrying
  `.pytemplate-e2e` is wiped, and a symlinked or junctioned base loses only its link and marker
  (`e2e.rmtree` removes a link as a link and never chmods through one: a passing run left the
  folder it named at 0o200). One run at a time per base: `selftest` holds `project.base_lock` on
  `<base>/lock` around the run and the cleanup (as `--mutation` does), so a second run is refused
  ("another run is using <base>") instead of deleting the first's projects and logs. Steps run with stdin closed and per-step timeouts (`TIMEOUTS`,
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
  `./pyt selftest`): script default 4.5 min, raylib default 4.4 min and `--full` 5.7 min,
  flet `--full` 4.3 min (its exe and nuitka builds failed at once there: the sandbox proxy's
  CA broke the Flet client download), script `--full --methods nuitka,wheel` 14.9 min (5.3
  and 6.5 of them its two nuitka builds).
- `./pyt selftest --mutation [--diff BASE] [--jobs N] [--json]` (`mutation.selftest`): mutation
  testing of the runner (`mutation.SCOPE`: `.pytemplate/runner`) with Cosmic Ray, pinned in the
  PEP 723 header of `.pytemplate/tools/mutation_cr.py` (`cosmic-ray==8.7.0`) and locked next to
  it (`mutation_cr.py.lock`, `uv lock --script`). That script is Cosmic Ray's side
  (`mutation.Driver`: `uv run --locked --script ... serve` on `python.cpython`, one JSON request
  per line, from any worker thread): it lists a module's mutants and makes one with Cosmic Ray's
  own code (`plugins.get_operator`, an operator's `mutation_positions` in a pre-order walk,
  `mutating.mutate_code`); Cosmic Ray's session database, distributors and test runner are not
  used. The runner does the rest:
  - which mutants: the operators of `mutation.OPERATORS` (31 of Cosmic Ray's 213, one change per
    symbol, the one that moves a boundary or turns a test around; never `/` -> `*`, which in the
    runner joins paths), NumberReplacer's +1 only, none in an annotation, in the test or body of
    `if TYPE_CHECKING:` or on a line with `# pragma: no mutate` (`select`, `skipped_spans`:
    parso counts columns in characters, ast in UTF-8 bytes). ExceptionReplacer's mutants are
    the runner's own (`own_mutant`, 15.1): one per exception class an except clause names
    (`handler_classes`: the whole expression, or each element of a tuple; `select` gives the
    mutant the class's span), which becomes `()` (an empty tuple: the handler catches nothing),
    or `*()` in a tuple (no element in its place: Python refuses a tuple inside it). A mutant
    that is no valid Python, the module unchanged, or one Cosmic Ray cannot make is skipped
    (`made`, with the reason in the JSON): pytest would stop at the import, which reads as a
    kill. With `--diff BASE` only the mutants on the lines `git diff BASE` adds or replaces in
    the runner (`changed_lines`: the working tree against BASE, uncommitted changes included,
    and every line of an untracked module; `--relative`, so a project below its repository's
    top works; every option that shapes the output given, whatever the user's git settings:
    prefixes, no colour, no textconv, `--inter-hunk-context=0` (merged hunks counted the lines
    between them as changed); `parse_diff` reads names in git's C quotes, `presets._git_path`,
    and ends a line at git's own line breaks only). The runner holds about 10,600 mutants
    (September 2026).
  - which tests: the test files of `.pytemplate/tests` that import the module (`test_map`:
    `from runner import x`, `from runner.x import y`, `import runner.x`), the likeliest to fail
    soon first (`ordered`: mentions of the module per second of its baseline, `junit_seconds`),
    run with `-x -q -p no:cacheprovider -c .pytemplate/tests/pytest.ini --rootdir=. --color=no
    --hypothesis-seed=0` (the suite's own settings, as `selftest` passes them: the copy's
    pyproject.toml is the project's, and a coverage gate there failed every baseline;
    `--rootdir=.` keeps the JUnit classnames `junit_seconds` reads) and without the user's
    `PYTEST_ADDOPTS`, `PYTEST_PLUGINS` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD` (`mutation.PYTEST_VARIABLES`:
    `-n auto` or `--lf` would change what every run means, and disabling autoload drops Hypothesis's
    plugin, so `--hypothesis-seed=0` would make every run exit 4; a run with no summary line reports
    pytest's own error line, `mutation._why_line`, not the `rootdir:` line). The mutants of a module
    no test file imports are untested.
  - where: N workers (`--jobs`, default half the CPUs, at most 8), each a throwaway copy of the
    project in the scratch base (`make_copy`: the files `git ls-files --cached --others
    --exclude-standard` lists, with their working-tree content, committed into a repository of its
    own; the modules to mutate from the snapshot `list_mutants` takes, which Cosmic Ray reads for
    every mutant too, so the checkout may change meanwhile), with its own `.venv` (`sync_copy`: `uv
    sync --locked --all-groups`, its output captured and shown when it fails, even under `-q`: a
    `--quiet` hides why, `uv -qq` always and one `--quiet` with uv 0.10.12, the oldest the project
    takes, for a stale lock), home, XDG and temp folders (`worker_env`: whatever a mutant makes a
    test write outside tmp_path lands there; `nvimtest.uv_dirs` keeps uv's cache, Pythons and tools
    through their UV_* variables, which the tests' own child environments keep too:
    `test_cli_core.child_env`, the `_uv_dirs` helpers), no bytecode written, and a new mtime for
    every write of a module (`set_mtime`: after the one before and after the clock, so a `.pyc` a
    child wrote never passes for another version of it, not even one of the copy the clock wrote).
  - the baselines: each module's tests first run as they are (`BASELINE_TIMEOUT`, an hour) and
    must pass (a FAIL leaves its mutants not run, and the command exits 1); their time sets each
    mutant's limit, `TIMEOUT_FACTOR` x baseline + `TIMEOUT_EXTRA` (2 x + 60 s). A mutant over it
    is a timeout, which counts as a kill.
  - the verdict (`classify`): a kill needs pytest's own summary line with failed or erroring tests
    (exit 1 or 2; "subtests failed" counts, "xfailed" never), or its KeyboardInterrupt banner with
    exit 2 in a run the suite did not stop (the tests' own interrupt, which the mutant caused: one
    that switched a signal handler off; pytest counts no failure then, and its summary may be "no
    tests ran"); a survivor the summary with none (exit 0); any other end (no summary: a crash, a
    Python that did not start) is an error, never a kill, and keeps its log. Colour codes are read
    through (`pytest_counts`). A run the suite stopped (Ctrl+C, SIGTERM, SIGHUP:
    `e2e.termination_as_interrupt`) proves nothing and is not run, whatever it returned (`Runs.run`
    gives None: stop() may kill it before its own loop sees the stop). The terminal's Ctrl+C never
    reaches a run, which has a session of its own on POSIX and a process group of its own on
    Windows (`NEW_GROUP`, which ignores the console's Ctrl+C): a pytest that the console's Ctrl+C
    ended printed the KeyboardInterrupt banner with exit code 2, a kill when the runner's own
    interrupt came a moment later. A run over its limit or stopped dies with its whole tree
    (`kill_run`: on POSIX also every process below pytest in a session of its own, which a signal
    to pytest's group never reaches, found through /proc or `ps` before the kill: once pytest is
    dead, init owns them).
  - the report (`print_report`; `Report.as_json` with `--json`, on stdout): the counts per file,
    the score (the killed and timed-out share of the judged mutants), every survivor and error
    with its line, operator and change. Exit 0 when every mutant was judged (survivors are the
    report, not a failure); the closing line names how many were untested when any were (a module
    no test file imports), never the false "every mutant judged". 1 on a failed baseline or an error, 2 usage (`--jobs` below 1, an
    empty `--diff`, a BASE that names no commit), 3 without git, uv or Cosmic Ray's environment
    (offline it must be in uv's cache), 130 interrupted (the report of what ran). What stops a
    run (a worker whose `uv sync` failed, Cosmic Ray's side gone: `Report.error`) comes after
    the report of what ran, with its own message and code. A Ctrl+C, SIGTERM or SIGHUP during
    the cleanup or the report waits for it (`deferred_interrupts`) and makes the run interrupted.
  - the scratch base: `mutation.default_base` (`%TEMP%\pt\mut`, `$TMPDIR/pt-mutation-<uid>`,
    0700, `project.check_private_dir`, again once it exists: `make_private_dir`), never inside the project, wiped only with its marker
    `.pytemplate-mutation`, one run at a time (`base_lock` on `<base>/lock`). The workers'
    folders and the snapshot always go; the logs (`<base>/logs`: Cosmic Ray's, each worker's last
    run, the junit times, each failed baseline, each error) stay after an interrupt, a failed
    baseline or an error, and otherwise go, while the lock is still held. The base itself stays
    with its marker and lock file (removed, it could vanish under a second run that just
    prepared it).
  Measured (Linux, `--jobs 3`, September 2026): a module's baseline 1.5 to 3 minutes, a killed
  mutant about 5 s; a survivor costs a whole run of its module's tests, so a pass of the whole
  runner takes a day of CPU or more. `--diff origin/main` on the branch that added the suite
  (mutation.py new, a few lines elsewhere): 531 mutants in 54 minutes.
- `./pyt render --check` (exit 1 when something is outdated or hand-edited) and
  `./pyt doctor`.
- Manual end-to-end: `./pyt new C:\t\p1 --preset <p> --name <n>`, then in the copy
  `./pyt setup`, `test all`, `check all`, `build <b> --method <m>`. Keep the path short.

### 13.2 CI

- `.github/workflows/ci.yml` is generated for every project (`render.ci_workflow` from
  `templates/ci.yml`: placeholders `__HEADER__`, `__MATRIX__`, `__LINUX_DEPS__` (its line must
  exist exactly), `__NAME__`, `__BUILD_BACKEND__`; action majors pinned: bump deliberately;
  `astral-sh/setup-uv` publishes no floating major tags since v8 (`@v10` does not resolve), so
  it is pinned to an exact release, `v10.2.0`, in every workflow; deleting the template
  disables CI generation; a placeholder left behind is a PytError). It triggers on pushes to
  `main` AND `master` (a plain `git init` gives either), pull requests and manual dispatch. Its
  first step is `./pyt render --check` (no environment needed; the generators read no
  platform state, CRLF/BOM checkouts count as up to date): a `pytemplate.toml` edit committed
  without rendering or re-locking (web editor, no hook) fails there instead of being rendered
  silently inside the runner. It never runs `setup`: it `sync`s only the matrix backends of
  each OS (so raylib drops PyPy on macOS; an OS left with no backend gets no matrix row, and so
  does one the preset's package has no wheel for, `render.NO_WHEELS_FOR`: raylib_software on
  macOS, 15.1), then
  `check all`, `test` per backend, a pyz per OS, and `pyz-merge` into one cross-platform
  `.pyz`, uploaded with the `<name>.cmd` wrapper `pyz-merge` writes next to it (a literal
  block `path: |`; `pyz.wrapper_path`). Two moving parts on purpose, explained in its header
  and pinned by `test_ci_workflow_keeps_its_moving_parts_on_purpose`: setup-uv gets no
  `version:`, so it installs the newest uv that satisfies pyproject's `required-version` (a
  pinned uv cannot download Pythons released after it), and the runner labels stay `-latest`
  (GitHub retires a pinned label about six months after a newer image is GA). It runs only
  when the project is the root of its repository (GitHub reads `<top>/.github/workflows/`):
  `new` warns when it creates a project inside another work tree, and `./pyt doctor` notes
  it for a project that sits in one (section 15.2).
- **[template repo]** `template-selftest.yml` (push to `main`, pull requests, weekly and by
  hand; the gate): `./pyt render --check` first, then `setup` and `./pyt selftest` on
  macos and windows-latest (Windows through `pyt.ps1`, `--basetemp` in `RUNNER_TEMP`, git's
  default CRLF checkout), with what the tests look for: MSYS2 with dash
  and uv copied to `~\.local\bin` (the login-shell test); xonsh 0.24.2 and Neovim v0.12.5; the
  UPX `upx.find` finds (the pinned download on Windows). Job `uv-floor` (a bare Linux runner):
  setup-uv with `resolution-strategy: lowest` takes the oldest uv the `required-version`
  accepts, checked against `envs.MIN_UV`, then setup and the suite, minus
  `test_init_round_trip_through_every_preset_is_byte_identical` (uv 0.10.12 writes redundant
  markers into uv.lock after a preset round trip: the same lock, other bytes). Its Linux jobs run
  in the CI image (`template-ci-image.yml`, stage 2): the suite (dash, zsh, ksh, mksh, yash,
  busybox, fish, xonsh, Neovim, actionlint, taplo and the basedpyright pins are in the image),
  `python-floor` (the runner starts on 3.11, `uv run --python 3.11 --script`, and the suite runs
  in process on 3.11, `uv run --no-project --python 3.11` with the locked pytest and hypothesis
  (`--with`) and the suite's own settings, `-c .pytemplate/tests/pytest.ini --rootdir=.`, as
  `./pyt selftest` passes them: the runner code itself on its floor; the tools the tests start
  stay in `.venv`.
  Not on PyPy: uv never starts the runner there, and PyPy only changes harness details (it
  resets an inherited SIG_IGN of SIGINT, no PEP 538 locale coercion)) and `new-project` (`./pyt
  new` of a raylib and a flet project named `Pt-<preset>`, then setup and selftest there). About
  4 min in the image, 4-6 on macOS and 5-6 on Windows (September 2026).
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
- **[template repo]** `template-ci-image.yml` (push to `main`, pull requests, weekly and by
  hand; the gate) and its folder `template-ci-image/`: an Ubuntu 24.04 image (the base pinned by
  digest, apt from `snapshot.ubuntu.com` at one date, `APT_SNAPSHOT`) with what the Linux jobs
  install on a bare runner (the shells, gcc, fd, node, shellcheck; uv, Neovim 0.12.5 and 0.11.2,
  PowerShell and actionlint from their releases, SHA-256 checked, uv outside the folders the
  no-uv tests hide; xonsh) and what they download, in the runner user's default folders (`warm`,
  which runs what those jobs run: CPython 3.14 and 3.11, PyPy, the wheels of the template's lock
  and of a new raylib and flet project, taplo, basedpyright with its Node pin, and the pinned UPX:
  the image has no system upx, the Linux runner image has apt's). `pins.py` holds
  the image's own pins and reads the others from their files by text (`.python-version`,
  `cmd_nvim.MIN_LAZYVIM`, `cmd_dev.BASEDPYRIGHT` and `BASEDPYRIGHT_NODE`, the taplo pin of
  `test_render_core.py`, `upx.VERSION`, the floor of `.pytemplate/pyt.py`). `APT_SNAPSHOT`
  must be later than the base's build date (`system` refuses the other order: the snapshot's
  toolchain needs the base's libc6 at the same version). The tag is the first 16 hex digits of
  the sha256 of `pins.py canonical`: every pin, the image's files and the files that decide its
  downloads (`uv.lock`, `pyproject.toml`, `pytemplate.toml`, `.python-version`, each preset's
  `preset.toml`, `constraints.txt` and `pytemplate.toml`; a BOM and CRLF do not count); the image
  is `ghcr.io/<owner>/<repo>-ci:<tag>`. Not named by the tag: ca-certificates and openssl (from
  the live archive, before the snapshot: the base has no CA certificates and the snapshot is
  https only) and the unpinned dependencies of the pinned tools (xonsh's), taken on build day.
  The `image` job builds it (`build`: the tracked files as
  the context) when the tag is not published, and weekly from scratch (`--no-cache --pull`: red
  with no commit behind it means a pinned download is gone, or an unpinned dependency of a pin
  moved), checks that its `/opt/ci/inputs.txt` equals this checkout's canonical text, prints the
  tool versions, and pushes a tag only when the registry says it is missing ("manifest unknown",
  "denied"; any other answer fails the job, and the push step asks again first; two runs that
  build one new tag at once may both push it, from the same inputs), and only from this
  repository (a fork's pull request gets a token that cannot push). Stage 2 (September 2026):
  its other jobs are the Linux jobs of template-selftest (selftest, python-floor, new-project),
  template-launchers (with `shellcheck -s sh pyt`) and template-nvim (its two pinned rows),
  as GitHub container jobs, whenever the image is usable (published, or pushed by this run); no
  bare Linux job repeats them (`test_workflows.test_linux_jobs_run_in_the_ci_image`).
  `options: --init --user 1001` (the uid of the image's `USER runner`, the runner's uid, which
  owns the checkout; an init reaps orphaned processes), `HOME: /home/runner` (where the caches
  are), and the nvim job's `--dir /tmp/pt-nvim` (`${{ runner.temp }}` is a host path,
  translated only in step inputs and environment values). The package is private when first pushed: the jobs pull
  with the job token (`packages: read`). Stage 2 was taken once every test of a bare Linux job
  also ran in its image job (the image's skips a subset of the bare job's: 70 of the selftest's
  72, 95 of python-floor's 185), those passed, and the image workflow finished no later than the
  bare jobs it replaced (its container start, about 30 s a job, against their apt and tool
  installs). uv-floor, the e2e rows, the nvim canary, template-flet.yml's two builds and every
  Windows and macOS job stay on bare runners. A pull request from another repository that
  changes an input of the image gets no Linux jobs (15.2). Its job `mutation` (pull requests
  only; a measure, not a gate) runs `./pyt selftest --mutation --diff HEAD^1 --jobs 3 --json` in
  the image: HEAD is the merge commit GitHub made for the pull request (checked out with its
  parents, `fetch-depth: 2`, no persisted token), HEAD^1 the base branch it was made on (the
  branch itself may have moved since: its new lines would count as the pull request's). Under
  `timeout -k 5m -s TERM 70m` in a 90-minute job: when the budget ends (timeout's 124) the report
  holds the mutants that ran and decides, as the suite's exit code does when it ends by itself: its
  `failed` key (a failed baseline, a mutant that could not be judged, an error), or a report that
  cannot be read, turns the job red, else a warning (the first run on GitHub ran out of time with
  three failed baselines, and was green); the job is red only when the suite could not judge or
  did not run; `mutation.json` is always uploaded (`mutation-report`). Its workers move HOME, so
  the image's own check reads the image's caches from its user's passwd home
  (`test_workflows.test_ci_image_holds_this_checkout_and_its_tools`: read from HOME it failed
  those baselines). The image's `warm` step makes Cosmic Ray's environment once
  (`mutation_cr.py check`, with the Driver's uv flags), which leaves its wheels in the image's uv
  cache: uv keys a script's environment by the script's path, so the job makes its own from that
  cache, without the network. `pins.py` hashes the script and its lock (`pins.data_files`). A
  pass of the whole runner never runs in CI (a day of CPU or more).
- **[template repo]** `template-launchers.yml` also runs weekly and installs xonsh 0.24.2 on
  pushes and pull requests but the newest xonsh on the schedule (a red scheduled run with no
  commit behind it is upstream drift; the shell versions are logged: xonsh, fish, pwsh,
  busybox, MSYS2 runtime, Cygwin), xonsh on Windows too, and nushell on macOS.
  `template-nvim.yml` pins Neovim v0.12.5 on Windows (the Linux rows, v0.12.5 and v0.11.2 =
  `cmd_nvim.MIN_LAZYVIM`, run in the CI image) for pushes and pull requests, one log artifact
  per row; its canary job (weekly and by hand, Ubuntu, Neovim
  stable) deletes `.pytemplate/nvim/tests/lazy-lock.json` and always uploads the logs with the
  resolved `lazy-lock.json` and `starter-commit.txt`, the next pins after a green run (13.1).
- **[template repo]** `template-launchers.yml` (macOS shells, Windows with
  MSYS2, Cygwin and busybox-w32 (one release, `BUSYBOX: busybox-w64-FRP-<n>-g<commit>.exe`, the
  build scoop, Chocolatey and winget install, checked against its pinned `BUSYBOX_SHA256`
  before it runs; a new release takes its line of frippery.org's `SHA256SUM`. The rolling
  `busybox64u.exe` ran unchecked whatever the server held that day:
  `test_workflows.test_every_downloaded_file_is_checked_against_a_pinned_sha256`; the file is
  kept in the Actions cache, keyed by its name and hash and checked again when restored, 15.1),
  optional WSL job; `selftest --shells` plus user-style
  invocations; a gate job checks the marker file; its Linux job, with shellcheck, in the CI
  image), `template-nvim.yml` (Windows here, Linux in the CI image, Neovim pinned (above),
  `fd` (venv-selector from LazyVim's `lang.python` errors on the first Python buffer without
  it; one release, `FD_VERSION`, whose Windows zip is checked against its pinned `FD_SHA256`
  before it goes on PATH: Chocolatey's floated, `test_the_nvim_workflow_installs_a_pinned_fd`,
  `test_the_fd_step_refuses_a_file_that_is_not_the_pinned_one`), `selftest --nvim --require
  --dir $RUNNER_TEMP/pt-nvim` (the `runner`
  context is not allowed in a job-level `env`, hence the step env), logs on failure or
  cancellation, as for e2e and in the nvim job of template-ci-image.yml
  (`test_workflows.test_logs_of_a_job_that_timed_out_are_uploaded`)),
  `template-e2e.yml` (gate job, then 3 OS x 3 presets: `--quick` on pushes and pull requests
  that touch what `new` copies (`.pytemplate/**`, the launchers, `pyproject.toml`, `uv.lock`,
  `pytemplate.toml`, `.python-version`, `src/**`, `tests/**`, `.gitignore`, `.gitattributes`,
  the workflow), the default depth weekly (Monday cron), `--full` monthly (day-1 cron, named by
  its string in the `MODE` expression), any depth on dispatch; job timeout 120 min, 300 for
  full; Linux gets the raylib libs, `libgl1-mesa-dri` and `xvfb`; the JSON report is always
  uploaded and the logs on failure or cancellation (`failure() || cancelled()`: a job over its
  timeout-minutes is cancelled, not failed, and those are the logs that matter), one artifact
  name per matrix row; `test_e2e_plan.test_e2e_workflow_*` pin the depths, triggers and the
  logs condition). First run on GitHub in
  September 2026 (images ubuntu-24.04, macos-26-arm64, windows-2025-vs2026; uv 0.12, Neovim
  0.12.5).
- **[template repo]** `template-flet.yml` (gate job; pushes to `main` and pull requests that touch
  the runner (`.pytemplate/runner/**`: a flet build runs some 23 of its modules, envs, render,
  mypyc and config among them, not only the flet method), `.pytemplate/pyt.py`, the `pyt`
  launcher, `.python-version`, the flet preset, the root `pyproject.toml`/`uv.lock` or the
  workflow and its folder; weekly and by hand) and its folder
  `template-flet/`: two jobs on bare `ubuntu-latest` runners (the runner image's Android SDK and
  JDKs; Flet installs its own Flutter), each `./pyt new "$RUNNER_TEMP/pt-flet" --preset flet`
  (app `pt-flet`, package `pt_flet`), `./pyt setup`, `check.py target` (`[deploy.flet] target`
  set through the project's own runner, `config.update_file`), `check.py flet` (the Flet version
  uv.lock holds: the cache key) and `./pyt build cpython --method flet`. `web` (45 min): then
  the pinned Playwright's headless Chromium (`browser.txt`; `playwright install` with
  `--with-deps --only-shell chromium`) and `check.py web`: the build served on 127.0.0.1 by a
  plain static server (`check._Site`: JavaScript and wasm types; no cross-origin isolation
  headers, which `flet serve` adds and the app needs none of), then python.js's
  `Python worker initialized` console line (`Python worker init error` fails at once),
  Flutter's semantics tree turned on (`flt-semantics-placeholder`), the skeleton's first status
  `Core: CPython. Press Draw.`, its Draw button and the window title `pt-flet`, a mouse click at
  the button's centre (a tap of its semantics node after 20 s without "Computing...") and the
  status `N iterations in T s (core: CPython)` (`Draw failed` fails at once); the console log
  (with the server's) and a full-page screenshot are uploaded (`flet-web-browser`,
  `if: always()`); the status is set once the core returned the image, which only the
  screenshot shows (unchecked). `android` (75 min): Temurin 17
  (`actions/setup-java`, the javac Flet asks for), disk freed only when the image leaves less than
  30 GB (dotnet, GHC, CodeQL; a cold build needs about 15), then
  `check.py apk` (with the locked `packaging`, `--with packaging==<uv.lock's>`): one `.apk` in
  `dist/pt-flet-cpython-flet-apk/`, every CRC right, the manifest,
  `classes.dex`, `resources.arsc`, `assets/flutter_assets/`, per ABI (`arm64-v8a`,
  `armeabi-v7a`, `x86_64`) `libflutter.so`, `libapp.so`, `libdart_bridge.so` and
  `libpython3.14.so`, `assets/app.zip` with the app's modules (`.py` or `.pyc`, no native
  module), `assets/sitepackages.zip` with flet and a `.dist-info` of every requirement of the
  build project (`.build/flet-build/cpython/pyproject.toml`) whose marker holds on an Android
  device (`check.android_requirements`: an apk without repath, six or httpx passed, and on a
  device `import flet` fails), and in it and `assets/stdlib.zip` at least one
  `.soref` marker whose library every ABI holds (serious_python_android 4.7.1's split); then
  `apksigner verify` and `aapt2 dump badging` of the newest build tools, and the `.apk` is
  uploaded for 3 days (`flet-apk`, `if: always()`). The caches (`~/flutter`, `~/.pub-cache`,
  `~/.flet/cache`, plus Gradle's for Android; about 1.8 and 3.6 GB) are keyed by job, OS, arch
  and the Flet version, restored with `actions/cache/restore` and saved with `actions/cache/save`
  right after a build that passed (a red check keeps a cold build's downloads; 15.1), on `main`
  only: a pull request restores main's (the first run saved both into the pull request's own
  scope, and a copy per branch filled the repository's 10 GB). Pinned by
  `test_workflows.test_flet_workflow_builds_for_the_web_and_android` and the other
  `test_flet_*` tests. First run on GitHub on PR #4 (September 2026, runs 36325942984 and
  36326885602): both jobs passed; the apk build took 6.5 min cold and 3.8 warm (133.7 MB, three
  ABIs of 63 libraries each), the web build 2.2 and 1.2 min (Python started in Chromium in 3-4 s,
  a draw 3-6 s).

### 13.3 Coverage limits

Developed on Windows 11: the Linux/macOS code paths (launcher branches, `.sh` launchers, xvfb,
pyz cache in `HOME`, the nvim harness) are exercised by the CI workflows. Not installed locally, CI only: zsh,
ksh, mksh, yash, fish, Cygwin, busybox-w32, WSL, macOS bash 3.2, and (macOS jobs only)
nushell. Since September 2026 template-selftest runs every Windows-only test (the pyt.cmd
and registry tests included) on CI, and template-nvim runs Neovim 0.11.2 (it
passed once on Linux when the row was added); none of these new jobs had run on GitHub when
they were written. Nor had template-ci-image.yml (stage 1): its seven jobs passed locally,
in a local build of the image, as `docker run --init --user 1001` with the checkout mounted
(September 2026, behind a TLS-intercepting proxy: the image's `ca` secret path runs only in
such a local build, CI passes no secret), with the skip list of the hosted Ubuntu selftest
minus its PyPy test; on GitHub they then ran next to the bare jobs for about 15 hours (13 runs
of the workflow, September 26-27) before stage 2 made them the only Linux jobs. Untested
anywhere so far:
PowerShell 6.x-7.2, a UNC current folder or a project on a share (its long-path names are only
simulated: `test_long_paths_keep_a_network_share_valid`), uv found only in `ProgramFiles` or chocolatey, the
install prompt on Windows (POSIX `pyt` and pwsh
`pyt.ps1` answer it on a pseudo-terminal), Neovim 0.11 on Windows, pyright via Mason, VS
Code itself (buttons, Problems panel: only simulated), the desktop targets of `flet build`
outside Windows (verified by hand there, section 10), an Android app on a device or an emulator
(an owner decision: template-flet.yml only builds the `.apk` and reads it, 13.2; it was never
built locally), an iOS build, the web build served under a subpath (`[tool.flet.web]
base_url`: the check serves it at `/`), a web build in a
browser other than Chromium, bundled PyPy portable builds on CI, Ctrl+C
handling of the nvim harness (its tree kill only simulated; SIGTERM and SIGHUP run for real on
POSIX, as `selftest --e2e`'s do in `test_e2e_run.py`), `[deploy.nuitka]` lto/pgo outside Linux (measured with Nuitka
4.2.2 and gcc 13 only: PGO with MSVC and an ~800-module LTO link are unmeasured). The launcher
changes of
September 2026 were developed on Linux (pwsh 7.6 for `pyt.ps1`; niubash simulated by
sourcing `pyt` in bash, dash, busybox, ksh, mksh and yash): their Windows paths (Windows PowerShell 5.1, `pyt.cmd`, real niubash and MSYS2) run
only with `./pyt selftest` and `selftest --shells` on Windows (template-selftest and
template-launchers; real niubash only on the maintainer's machine). `selftest --mutation` was
developed and run on Linux only (September 2026): on Windows and macOS only its tests run
(template-selftest), which fake or skip what differs there (the lock of `msvcrt`, `ps` instead
of /proc, taskkill, the process group of each test run); its CI job had not run on GitHub when
it was written.

### 13.4 Bug density (rule 1.10)

- Lines = non-blank lines that are not only a comment or a docstring, of the product code:
  `.pytemplate/runner/**/*.py`, `.pytemplate/pyt.py`, `.pytemplate/tools/*.py`, `pyt`,
  `pyt.cmd`, `pyt.ps1`, `.pytemplate/nvim/**/*.lua` (not its `tests/`),
  `.pytemplate/templates/**`, the preset skeletons and tools (`.py`, `.pyi`, `.toml`; not
  `typings/`), `.github/workflows/template-*.yml`, the CI image's `Dockerfile`, `pins.py`,
  `system`, `warm` and `build` (`.github/workflows/template-ci-image/`) and the flet builds'
  `check.py` (`.github/workflows/template-flet/`). Tests (`.pytemplate/tests/`,
  `.pytemplate/nvim/tests/`) are not in the denominator; a test defect counts as a bug only when
  it breaks `./pyt selftest` for a user or hides a product bug.
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
    `./pyt selftest` there): 78 confirmed, 1 per 199 lines. Capture-recapture on the hunt:
    A found 41 of the counted, B 47, both 22: about 87 (Chapman), about 99 with the CI's 12:
    1 per 157 lines. UNACCEPTABLE (worse than 1 per 500, better than 1 per 100). Both teams are
    the same model, so their finds are correlated and the estimate is likely low. Fixed since:
    one branch per area, each fix with a regression test that fails without it, then an
    adversarial review of every branch, whose findings on the fixes themselves (about 40, 2 of
    them critical) were fixed too. All 66 counted and 62 minor defects are fixed but one counted
    notable, only partly: a project inside another git repository keeps a generated CI that
    GitHub never runs (`new` and `doctor` now say so; section 15.2).
  - 2026-09-26, commit e3c3703 (those fixes, and the CI image's stage 1), 18,207 lines: two
    independent teams of 10 hunters again, this time without the verifier and the skeptic (owner
    decision): each finding was reproduced by whoever fixed it, with a regression test that
    fails without the fix. Team A reported 60 findings (6 critical, 32 notable, 22 minor), team B
    71 (6 critical, 33 notable, 32 minor); merged across the teams, 11 critical (one found by
    both: the shared `/tmp` default of `selftest --e2e`), 55 notable and 47 minor distinct
    defects, none rejected. Counted: 66, 1 per 276 lines. Capture-recapture on the counted: A 39,
    B 41, both 14: about 111 (Chapman), 1 per 164 lines. UNACCEPTABLE, as at 87e28f9 (the code
    had grown by 2,700 lines, the CI image and the fixes, and new code is where many finds
    were). Fixed since: the 11 critical ones by hand, one commit each; the other 108 findings,
    the minor ones included, by two fixers (areas 1-5 and 6-10, one branch each), each with its
    regression test, then merged and checked together (`./pyt selftest`, `--shells`,
    `--nvim`, `--e2e --quick`). Measured again by the bug-hunt loop below.
  - The bug-hunt loop (owner request, 2026-09-28): rounds of ONE team of 10 hunters (one area
    each, the whole project as context, no verifier), then 5 fixers in worktrees, who verify each
    finding (reproduce it, or trace it where this container cannot run it; the severity of rule
    1.10), fix it with a regression test that fails without the fix, and one integration (every
    branch reviewed, every critical re-checked, then `./pyt selftest`, mypy on three platforms,
    `--shells`, `--nvim`, `--e2e`). It ends when a round confirms at most 1 counted defect per
    1000 lines. One team per round: its count is a lower bound (no capture-recapture).
    - Round 1, commit ca8134a (after PR 6), 21,891 lines: 74 findings, one reported by two
      hunters: 73 distinct, none rejected. Counted: 43 (7 critical: an in-process launcher run
      took the caller's folder for its own, a hard link passed the launchers' ownership rule, the
      harnesses checked their scratch base before it existed, pyt.cmd probed a drive-root folder
      for uv.exe, apply accepted both names edited onto another package, every build shipped a
      src/ folder it could not list empty, a trusted `.lazy.lua` loaded the plugin of a nested
      untrusted folder; 36 notable), 1 per 509 lines: not at the bar. 30 minor. All 73 fixed.

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
- Expected failures raise `PytError(msg, 2)` (usage/config) or `PytError(msg, 3)`
  (missing requirement), with an actionable hint in the message.
- Processes only through `proc.run/output` or `envs.uv/uv_run`, as argv lists, never
  `shell=True`. `proc.run` defaults to cwd = ROOT and `proc.base_env()`. Long-running
  harnesses (`shells`, `nvimtest`, `e2e`) use `subprocess` directly with stdin closed, output
  to log files and timeouts that kill the process tree; `hooks._run_bytes` too, for raw bytes
  and stdin (section 5.6).
- A project file the user may have edited (pyproject.toml, pytemplate.toml, state.json, the
  generated files, a restored uv.lock) is rewritten with `project.write_whole`, never in place: a
  write cut short (a full disk, a quota) left pyproject.toml truncated mid-block. It keeps the
  file's mode, owner and group (`_give_owner`: root in a dev container left a user's files
  root-owned) and its other hard links; a file with other links, or one whose owner it cannot
  give a new file, is rewritten in place (`_write_in_place`, the old bytes back on failure).
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
   `prog="./pyt x"`; reject unknown arguments (`cmd_dev.only_flags` for flag-only commands)
   and add it to `test_cli_core.MINIMAL` (or, if its arguments belong to another program, to
   `cli.FORWARDS`), and to `test_cli_core.NEVER_RENDER` when `render=False`. `-h`/`--help`
   after it is handled by `cli.dispatch`.
3. `./pyt render` and commit `.pytemplate/editor.json` + `state.json` (section 6.2).
4. Tests in `.pytemplate/tests/`; README commands table; VS Code task catalog
   (`editors/vscode.catalog`, and `vscode.scan` if its output should reach the Problems panel)
   and the Neovim metadata (`tasks.META`: tag, backend picker, parse, refresh) if editors
   should offer it.
5. Outside a project it exits 2 ("needs a project"): add it to `cli.GLOBAL_COMMANDS` only when
   it needs none, and then it must write nothing into the installed template (section 5.2;
   `test_global.test_a_global_run_never_writes_into_the_installed_template`).

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
4. It must pass `./pyt selftest` (`test_presets.py`: ruff-clean skeleton, valid config,
   pins), `./pyt selftest --e2e <p>` and `./pyt selftest --nvim <p>`.

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
  `pyt`, `pyt.ps1` and `.lazy.lua` LF (the last matching line wins); `*.png *.ico *.pyz`
  binary. With `core.autocrlf=true` most working-tree files are CRLF on Windows: that is fine,
  the runner normalises. **[template repo]** `.github/workflows/template-ci-image/.gitattributes`
  (`* text eol=lf`) keeps the CI image's Dockerfile and scripts LF on a Windows checkout.
- `pyt` and `pyt.ps1` are 100755: `cmd_env._fix_exec_bit` repairs both on `setup`, the
  files' own exec bit on POSIX (with or without git: with `core.filemode=true` an index-only fix
  is undone by the next `git add`; a file it may not chmod, another user's in a shared checkout,
  is a warning naming `chmod +x`, and apply goes on) and the git mode (`core.filemode=false` on
  Windows loses it); `presets.new` marks both, `presets.init` chmods both on POSIX.
  `pyt.cmd` stays 100644.
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
  `UV_NO_GROUP`, `UV_NO_SYNC` change the project, environment or groups of `uv run --locked`;
  `UV_MANAGED_PYTHON`/`UV_NO_MANAGED_PYTHON` next to `UV_PYTHON_PREFERENCE` exit 2. Fix:
  `proc.base_env` drops `proc.UV_SELECTION` (5.5). Test:
  `test_cli_core.py::test_base_env_drops_what_would_move_uv_or_python`,
  `test_a_user_uv_no_group_never_reaches_uv`,
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
  the Neovim plugin empties it (uv reads "" as unset), `.pytemplate/pyt.py`
  exits 3 below 3.11 (4.1, 5.2). Test:
  `test_launcher_sh.py::test_launcher_clears_the_callers_uv_python`,
  `test_user_uv_python_older_than_3_11`, `test_entry_refuses_python_older_than_3_11`,
  `test_launcher_win.py::test_ps1_clears_the_callers_uv_python_and_restores_it`. Goes: never.
- **uv has no CPython for some platforms** (LIMITATION): no Android (Termux) nor BSD builds at
  all (uv 0.12.19; `uv python list --only-downloads` is empty there), and a pinned request
  (`.python-version`) under the project's `only-managed` stopped every command, `help`
  included: Termux's own Python 3.13 was never used. Fix: without an environment of the project
  the launchers request `>=3.11` with `--python-preference managed` (a system CPython serves;
  with one, an empty `--python=`, no request, and `only-managed`: uv follows `.python-version` as
  before), `cli._restart` moves the commands that need `python.cpython` onto uv's own CPython of
  that minor (a system one of it counts as another Python: `cli._runs_on_python_cpython`), and
  `envs.no_download_problem` says why they cannot run there; `new` asks for the new project's
  `python.cpython` before it copies anything (`uv lock` needs it: `envs.ensure_python` in
  `cmd_mode.cmd_new`) (4.1, 5.2). Test:
  `test_cli_core.py::test_python_cpython_uv_cannot_install_stops_the_command`,
  `test_a_python_of_the_right_minor_is_not_python_cpython_unless_uv_manages_it`,
  `test_envs_core.py::test_no_download_problem_says_why_and_what_runs_here`,
  `test_doctor_says_where_python_cpython_is`,
  `test_presets.py::test_new_asks_for_python_cpython_before_it_copies_anything`. Goes: never
  (the restart stays; the platforms may come).
- **`uv run --script` reuses a script's cached environment while its interpreter satisfies the
  request** (LIMITATION, `environments-v2`; `--isolated` changes nothing for a script): with a
  plain `>=3.11` the first CPython a machine offered (a distribution's 3.12) stayed the
  runner's for good, and every project command paid a restart; and one a system Python of
  `python.cpython`'s minor built satisfies `.python-version` too. Fix: once the project has an
  environment the launchers make no request, with `only-managed`, and uv makes that environment
  again on the `python.cpython` of `.python-version` (`managed` kept a system 3.14's; measured with
  uv 0.10.12 and 0.12.19) (4.1). Test:
  `test_launcher_sh.py::test_the_runner_starts_on_python_cpython_once_the_project_has_an_environment`,
  `test_runner_runs_on_python_cpython_whatever_the_caller_pins`. Goes: never.
- **uv refuses `UV_MANAGED_PYTHON` and `UV_NO_MANAGED_PYTHON` next to `--python-preference`**
  (LIMITATION: "cannot be used with"), and an empty one ("expected a boolish value"): a caller
  that exported either could not start the runner at all. Fix: the launchers remove both for uv,
  niubash and the plugin pass `false` (4.1). Test:
  `test_launcher_sh.py::test_launcher_ignores_the_callers_python_home_path_and_uv_working_dir`,
  `test_in_process_run_leaves_no_name_behind`,
  `test_launcher_win.py::test_ps1_clears_the_callers_uv_python_and_restores_it`,
  `test_cmd_clears_the_callers_uv_python` (Windows),
  `test_nvim_render.py::test_lua_modules_in_headless_neovim`. Goes: never.
- **`uv python install` writes outside uv's folder** (LIMITATION, uv 0.8+): a `python3.X` link
  in `~/.local/bin` (and a warning that it is not on PATH) and, on Windows, a PEP 514 registry
  entry; uv's automatic downloads write neither. Fix: `envs.ensure_python` passes `--no-bin
  --no-registry` (5.2). Test:
  `test_envs_core.py::test_ensure_python_finds_or_installs_the_version_or_says_why`. Goes:
  never.
- **`uv run --script` hands the runner the caller's `PYTHONHOME`, `PYTHONPATH` and
  `UV_WORKING_DIR`** (LIMITATION): every command died with `Fatal Python error: Failed to import
  encodings module` under a `PYTHONHOME`, a `PYTHONPATH` module could shadow the stdlib the
  runner imports, and `UV_WORKING_DIR` started the runner in another folder (the paths of `new
  DIR` and `pyz-merge` resolved there); uv refuses an empty `UV_WORKING_DIR`. Fix: the
  launchers remove the three for uv; niubash and the Neovim plugin (`init.pyt_env`) pass
  `PYTHONHOME` and `PYTHONPATH` empty (CPython reads "" as unset) and `UV_WORKING_DIR=.` (4.1).
  Test:
  `test_launcher_sh.py::test_launcher_ignores_the_callers_python_home_path_and_uv_working_dir`,
  `test_in_process_run_leaves_no_name_behind`,
  `test_launcher_win.py::test_ps1_clears_the_callers_uv_python_and_restores_it`,
  `test_cmd_clears_the_callers_uv_python` (Windows),
  `test_nvim_render.py::test_lua_modules_in_headless_neovim`. Goes: never.
- **The project is found from the cwd** (LIMITATION): a work folder with its own
  `pyproject.toml` (the `flet build` stage) became the project ("Unable to find lockfile"). Fix:
  `envs.uv_run` adds `--project <ROOT>` (7). Test:
  `test_fixes.py::test_uv_run_pins_the_project_outside_the_root`. Goes: never.
- **`uv sync` is exact for the groups it installs** (LIMITATION): it removed a group added with
  `./pyt add --group G`, and so did the sync `uv remove` runs itself (`./pyt remove idna`
  uninstalled the packages of every non-default group); `--all-groups` enables every group, so
  uv refuses it for a pair of `[tool.uv] conflicts` or a group whose requires-python excludes
  the interpreter. Fix: `envs.sync` passes `--all-groups`, minus a `--no-group` for each group
  `envs.left_out` names; `cmd_env._add_remove` runs `uv add|remove --no-sync`, then
  `envs.sync`, and puts pyproject.toml and uv.lock back when that sync fails or is interrupted
  (uv's own rollback covers only its own sync) (7). Test:
  `test_envs_core.py::test_sync_installs_every_dependency_group`,
  `test_sync_leaves_out_the_groups_uv_cannot_install_there`,
  `test_sync_with_real_uv_installs_what_the_groups_allow`,
  `test_remove_keeps_the_packages_of_every_group`,
  `test_a_failed_sync_puts_pyproject_and_the_lock_back`. Goes: never.
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
  (DEFECT): only a version the project does not have fails ("depends on itself at an
  incompatible version"); in a project named `mdurl` markdown-it-py's `mdurl~=0.1` was
  satisfied by the project (0.1.0): uv.lock recorded `mdurl` with `source = { virtual = "." }`,
  and the real library was missing from the lock, `.venv` and every build (rich's Markdown
  failed), without an error. Up: none
  found. Fix: `presets.check_name_free` refuses every name of the preset's tested tree
  (`constraints.txt`) and of uv.lock; `init` refuses a resolved uv.lock in which a package
  depends on the project (`presets._self_dependents`) and rolls back (11). Test:
  `test_presets.py::test_new_from_a_project_without_the_presets_tree_refuses_its_names`,
  `test_init_refuses_a_lock_that_resolves_a_dependency_to_the_project`, `test_self_dependents`.
  Goes: when uv refuses it (the name check stays: src/<pkg>/ would shadow the library).
- **uv splits a `--constraints` value at every space** (DEFECT): `uv add --constraints "/a
  b/c.txt"` looked for `/a` ("File not found"), so `new` into a folder with a space failed
  (and cleaned up) whenever it passed the tested pins, e.g. `new "../my game" --preset script`
  from a raylib project. Up: astral-sh/uv#12639 (open). Fix: `presets._swap_dependencies`
  passes the pins file relative to the project root, uv's working folder (11). Test:
  `test_presets.py::test_init_passes_the_pins_by_a_path_without_spaces`,
  `test_init_pins_steer_the_resolution`. Goes: when uv takes the value whole (the relative
  path can stay).
- **uv writes `uv.lock` in place** (LIMITATION): a `uv lock` stopped by a full disk or a quota
  left `uv.lock` cut short (invalid TOML) next to the old pyproject.toml, and every `uv run
  --locked` and the next lock failed on it. Fix: `cmd_env.cmd_lock` snapshots both files and puts
  them back when `uv lock` fails (`_snapshot`, `_put_back`, 6.3), as `mode`, `apply` and
  `add`/`remove` do. Test: `test_envs_core.py::test_a_lock_cut_short_puts_uv_lock_back_too`.
  Goes: never.
- **uv writes normalized names** (LIMITATION, PEP 503): `raylib_sdl` became `raylib-sdl`, and a
  verbatim comparison never matched. Fix: `cmd_apply.req_key` (5.8). Test:
  `test_apply.py::test_req_key_normalizes_like_uv`. Goes: never.
- **`uv add` replaces a requirement only with one of the same marker** (LIMITATION: a name may
  have one requirement per marker): `uv add --frozen flet-desktop==1.0.0` appended a second pin
  next to a declared `flet-desktop==1.0.1; sys_platform != 'emscripten'`, the one `uv lock` had no
  solution, and a `[preset.flet] version` change could never be applied; `uv remove` drops every
  requirement of the name, so a package switch lost the marker. Fix:
  `cmd_apply.dependency_changes` adds the requirement with the declared marker
  (`cmd_apply.req_marker`; uv keeps the extras itself), a switch with the replaced package's
  (5.8). Test: `test_apply.py::test_a_marked_requirement_keeps_its_marker`,
  `test_apply_keeps_the_marker_of_an_option_driven_requirement`,
  `test_uv_frozen_replaces_a_marked_requirement_only_with_its_marker` (the real uv, offline).
  Goes: never.
- **`uv pip install --target` leaves build-machine files** (DEFECT for the `.lock`, LIMITATION
  for the rest): a `.lock`, `_virtualenv*`, console-script wrappers whose shebang or `.exe`
  trampoline names this machine's `.venv`, and for a local library (installed for real since
  `--no-editable`) a `direct_url.json` naming its source folder on this machine (PEP 610) plus
  uv's `uv_cache.json`/`uv_build.json` shipped in pyz and portable builds. Up: cf.
  astral-sh/uv#11878 (the `.lock` uv left in a venv; 0.12.19 still leaves an empty one in a
  `--target` folder). Fix: `common.drop_install_junk` (10). Test:
  `test_build_methods.py::test_install_deps_removes_uv_junk_but_keeps_native_tools`,
  `test_install_junk_drops_the_build_machines_path_of_a_local_library`. Goes: the `.lock` part
  when uv removes it; the rest never.
- **A requirements.txt export keeps no index, and uv pip reads no `[tool.uv.sources]`**
  (LIMITATION): a package uv.lock takes from an `explicit = true` index (`{ index = "name" }`: a
  private index, PyTorch's) was looked for on the other indexes by `uv pip install -r`, and every
  pyz and portable build failed ("No solution found"). Fix: `common.export_requirements` also
  exports the lock as pylock.toml (each file's URL), which `common.install_deps` installs (10).
  Test: `test_build_methods.py::test_pyz_and_portable_install_a_package_of_an_explicit_index`.
  Goes: never.
- **`uv export --format pylock.toml` writes a relative path from the project, not from the file**
  (DEFECT, uv 0.10.12 to 0.12.19): `uv pip install -r` reads it from the file's folder, as PEP 751
  says, so exported to `.build/deploy/pylock.toml` a local library (`./pyt add ./libs/x`) named
  `.build/deploy/libs/x` ("Distribution not found"). Up: astral-sh/uv#16299 (open; a draft fix,
  astral-sh/uv-dev#131). Fix: `common._rebase_paths` moves a relative path that names nothing from
  the file's folder and something from the project (a fixed uv's paths stay) (10). Test:
  `test_build_methods.py::test_the_pylock_export_names_local_libraries_from_its_own_folder`,
  `test_the_pylock_rebase_moves_only_what_names_the_project`. Goes: when uv writes them from the
  file's folder (the rebase then moves nothing).
- **Wheels for this machine follow this machine** (LIMITATION): uv took the newest tags the
  build machine allows (manylinux_2_34 on Ubuntu 24.04: the pyz failed on Debian 11), its macOS
  default may move with a uv release, and an sdist built for another OS gives host binaries.
  Fix: `common.host_floor`, `common.UV_PLATFORMS`, `common.MACOS_FLOOR` through
  `MACOSX_DEPLOYMENT_TARGET`, `--only-binary :all:` for other targets but `--no-binary` for the
  packages that publish no wheel (`common.source_only`), kept only when pure
  (`common.install_deps`, 10). Test:
  `test_build_methods.py::test_host_linux_target_gets_the_platform_floor`,
  `test_host_floor_falls_back_to_the_host_wheels`, `test_host_floor_falls_back_for_real_on_uvs_own_words`
  (uv's wording, which the fallback reads),
  `test_macos_targets_pin_the_deployment_target`,
  `test_cross_target_builds_a_package_that_publishes_no_wheel`,
  `test_cross_target_builds_a_pure_sdist_for_real`. Goes: never.
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
- **`uv tool install` takes the newest CPython it has, whatever the wheel's `Requires-Python`**
  (LIMITATION, uv 0.10.12 and 0.12.19): the printed `uv tool install <file>.whl` of a mypyc wheel
  (a cpXY platform wheel) failed with "no wheels with a matching Python version tag" whenever uv's
  newest CPython was another minor (a project on 3.13; the default 3.14 once 3.15 is installed).
  Fix: `wheel.build` prints `uv tool install --python <python.cpython> <file>` for a mypyc wheel,
  whose `requires-python` `wheel._pyproject` pins to `==<python.cpython>.*` (10). Test:
  `test_mypyc_core.py::test_wheel_builds_in_the_locked_tools_env`,
  `test_wheel_pyproject_is_exact_and_ships_the_package_data`. Goes: when uv picks the tool's
  interpreter from the wheel's `Requires-Python` (the printed `--python` can stay).
- **`uv run --with` and `uvx` float** (LIMITATION): an unpinned tool re-resolves to the newest
  release whenever uv's index cache expires (basedpyright's Node.js runtime too). Fix:
  `cmd_dev.BASEDPYRIGHT`, `cmd_dev.BASEDPYRIGHT_NODE`, `methods.nuitka.NUITKA`, and editor.json
  `typing.basedpyright` with its `basedpyright_node` for the plugin's uvx server
  (`integrations.lsp_cmd`: `--from` and `--with`; 7, 12.2). Test:
  `test_cli_core.py::test_tools_are_pinned_exactly`, `test_basedpyright_runs_with_every_pin`,
  `test_nvim_render.py::test_editor_json_is_data_without_machine_paths`,
  `test_lua_modules_in_headless_neovim`. Goes: never (bump the pins deliberately).
- **`uv tool dir --bin` prints a relative `UV_TOOL_BIN_DIR` as it is** (LIMITATION): `./pyt
  install` would have written the launchers below the folder it ran in. Fix: `cmd_install.bin_dir`
  refuses a relative answer (5.9). Test: `test_install.py::test_bin_dir_needs_an_absolute_folder`.
  Goes: never.
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
- **uv 0.10.12 (the floor) says nothing about a stale lock under `--quiet`** (LIMITATION: it
  prints "The lockfile at `uv.lock` needs to be updated" as plain output, which `--quiet` hides;
  0.12.19 prints it as an `error:`; `-qq` hides every error in any uv): `uv --quiet sync
  --locked` exited 1 without a word, and a worker of `selftest --mutation` could not be made with
  no reason given. Fix: `mutation.sync_copy` runs the copies' sync without `--quiet`, its output
  captured and shown when it fails, even under `-q` (13.1). Test:
  `test_mutation.py::test_a_copy_whose_sync_fails_says_why_with_the_oldest_uv` (a fake uv that
  answers as 0.10.12 does), `test_a_copy_whose_sync_fails_says_why_even_under_q` (the real uv:
  the newest, and the oldest in the `uv-floor` job). Goes: never (a captured sync says why with
  any uv).

CPython and its standard library:
- **`subprocess.run` kills the child 0.25 s after Ctrl+C** (LIMITATION, 3.7+): an app's cleanup
  was cut short; under `uv run` the app kept running as an orphan. Up: python/cpython#70130
  (bpo-25942, where the grace period came from). Fix: `proc._wait_through_signals` (5.3). Test:
  `test_cli_core.py::test_ctrl_c_waits_for_a_child_that_cleans_up` and the other
  `test_ctrl_c_*`. Goes: never.
- **`Path.rglob` does not enter symlinked folders** (LIMITATION; `recurse_symlinks` from 3.13):
  a linked `src/assets` reached the mypyc stage empty, and the portable prune removed Tk for a
  `tkinter` import in a linked folder of `src/`. Up: python/cpython#77609. Fix: `mypyc.walk` in
  `mypyc.sync_tree`, `mypyc.compiled_sources` and `common.uses_tkinter` (9, 10). Test:
  `test_mypyc_core.py::test_sync_tree_follows_symlinked_dirs`,
  `test_compiled_sources_follow_a_symlinked_subpackage`,
  `test_build_methods.py::test_portable_keeps_tkinter_imported_in_a_symlinked_src_folder`. Goes:
  maybe once the runner needs 3.13 (the cycle check stays ours).
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
  linter; PyInstaller `--python-option "X utf8"`; `.pytemplate/pyt.py` reconfigures its
  streams (5.5). Test: `test_cli_core.py::test_base_env_drops_what_would_move_uv_or_python`,
  `test_vscode.py::test_launch_configs_run_in_utf8_mode`,
  `test_build_methods.py::test_exe_default_argv_and_output`. Goes: when every supported Python
  is 3.15 or newer.
- **A closed stdout fails at exit** (LIMITATION): `./pyt help | head -1` printed a
  BrokenPipeError traceback. Fix: `cli._output_closed` (quiet exit 141; POSIX only, 15.2). Test:
  `test_cli_core.py::test_a_closed_stdout_is_not_a_runner_bug`. Goes: never.
- **`sys.stdlib_module_names` knows only the running version** (LIMITATION): a name that is a
  module of PyPy 3.11 or of another CPython passed the check (a project named pypyjit could not
  import itself on PyPy: its built-in wins over sys.path). Fix: `presets.STDLIB_OTHER_VERSIONS`
  (removed and new CPython modules, PyPy's own stdlib names and built-ins) in
  `presets.shadows_stdlib` (5.7). Test:
  `test_rename.py::test_stdlib_names_do_not_depend_on_the_runner`,
  `test_presets.py::test_every_pypy_standard_library_module_is_refused` (the pinned PyPy, when
  uv has it). Goes: never (update it per Python release).
- **`shutil.rmtree` error hooks** (LIMITATION): `onerror` is deprecated from 3.12 and `onexc`
  does not exist in 3.11, and a read-only file (git objects on Windows) needs a chmod and a
  retry. Fix: the version switch in `cmd_nvim.remove_tree`, `presets._remove`, `e2e.rmtree`;
  `cmd_env._remove` retries after `cmd_env._make_writable` (7). Test:
  `test_cmd_nvim.py::test_remove_tree_read_only`,
  `test_presets.py::test_remove_deletes_read_only_entries`,
  `test_envs_core.py::test_clean_retries_read_only_contents`. Goes: the switch once the runner
  needs 3.12; the retry never.
- **`shutil.copytree(src, dst)` gives `dst` itself `src`'s mode and times, and
  `tempfile.mkdtemp` makes a 0700 folder** (LIMITATION, both documented): the installed
  template's folder came from mkdtemp, and `pyt new` from it (copytree of that folder) made every
  project folder 0700, took the mode of an empty folder it was given (a setgid 2775 became 700)
  and failed in one the user does not own (EPERM, printed as shutil.Error's list of tuples).
  Fix: `cmd_install._new_folder` (os.mkdir: 0777 minus the umask) and `presets.copy_template`,
  which copies the entries of the root one by one (`_copy_entry`) and names what failed
  (`_raise_copy_errors`) (5.2, 5.9). Test:
  `test_install.py::test_the_installed_template_has_the_mode_of_a_plain_folder`,
  `test_global.py::test_new_outside_a_project_never_changes_the_projects_own_folder`,
  `test_a_copy_that_fails_says_what_failed`. Goes: never.
- **`Path.is_symlink()` is False for a Windows junction** (LIMITATION; `Path.is_junction` from
  3.12): `clean --envs` would have deleted what a junctioned `.venv` points to, and os.walk,
  which stops only where os.path.islink says so, entered a junction in src/: rename rewrote
  the files behind it, outside the project. Fix: `cmd_env._is_link` reads `st_reparse_tag` (7);
  `rename._code_files` and `rename._mentions` prune every link it reports (5.7). Test:
  `test_workarounds.py::test_a_windows_junction_counts_as_a_link`,
  `test_rename.py::test_a_windows_junction_in_src_is_never_followed`. Goes: the
  `st_reparse_tag` read once the runner needs 3.12; the pruning never.
- **The tokenizer changed with PEP 701** (LIMITATION): an f-string is one STRING token on 3.11
  and many tokens from 3.12 (t-strings from 3.14). Fix: `rename` handles both (5.7). Test:
  `test_rename.py::test_fstring_fields_as_one_token`, `test_tokenizer_canary`. Goes: the 3.11
  form once the runner needs 3.12.
- **No processes on WebAssembly, Android and iOS** (LIMITATION, documented: `multiprocessing` and
  the process pools are "not Emscripten, not WASI, not Android, not iOS"): in a `flet build`
  web or mobile app the flet skeleton's Draw handler died on `ProcessPoolExecutor` and left the
  button disabled on "Computing...". Fix: the flet skeleton's `ui/app.py` runs the work in the
  event loop there (its `NO_PROCESSES` platforms, or a pool that raises NotImplementedError)
  and gives the button back in a `finally` (11). Test:
  `test_presets.py::test_flet_skeleton_draws_where_no_process_can_start`,
  `test_flet_skeleton_gives_the_button_back_when_drawing_fails`. Goes: never.
- **No extension module loads from a zip** (LIMITATION, zipimport): Fix: the pyz bootstrap
  (`templates/pyz/__main__.py`) extracts to a per-build cache (10). Test:
  `test_build_methods.py::test_pyz_bootstrap_picks_the_flavour` and the other bootstrap tests.
  Goes: never.
- **`Path.home()` raises for a UID without a passwd entry** (LIMITATION): the pyz crashed in a
  container with a random UID. Fix: the pyz bootstrap falls back to a `tempfile.mkdtemp` folder
  (10). Test: `test_build_methods.py::test_pyz_runs_without_a_usable_cache`. Goes: never.
- **`platform`'s `machine()` names the machine, not the interpreter** (LIMITATION: on Windows
  CPython 3.12+ asks WMI for the native CPU, elsewhere it is the kernel's `uname -m`): an x64
  Python on Windows on ARM labelled its x64 wheels `aarch64`, and a pyz with a `windows-x86_64`
  target refused to run there; a 32-bit Python on a 64-bit kernel took the 64-bit name. Fix:
  `common.host_arch` and the pyz bootstrap's `_arch` read `sysconfig.get_platform()` on Windows
  and the pointer size elsewhere (10). Test:
  `test_build_methods.py::test_pyz_key_names_the_interpreter_not_the_machine`. Goes: never.
- **A missing cwd is blamed on the program** (LIMITATION): subprocess raised FileNotFoundError
  naming the program (POSIX) or NotADirectoryError (Windows). Fix: `proc.run` checks the cwd
  first (5.3). Test: `test_cli_core.py::test_a_bad_working_folder_is_named`. Goes: never.
- **An HTTP body cut short can end like a whole one** (LIMITATION, http.client): a read of a
  response that announced its Content-Length returns an empty chunk when the connection closes
  early (a ragged TLS end reads the same) instead of raising IncompleteRead (kept for
  compatibility, says CPython's own comment), and a chunked one raises IncompleteRead, which is
  no OSError: a Flet client cut short was cached and bundled into every later Nuitka build (the
  app failed at its first start), and a cut UPX download ended in a runner traceback. Fix:
  `nuitka._flet_client_archive` counts the bytes against Content-Length, reads the archive to
  its end (`nuitka.archive_problem`, also for a cached one) and catches HTTPException;
  `upx._download` catches it too and checks the SHA-256 (10). Test:
  `test_workarounds.py::test_a_flet_client_download_cut_short_is_never_cached`,
  `test_a_damaged_cached_flet_client_is_downloaded_again`,
  `test_upx.py::test_download_failures_are_clear_and_leave_nothing`. Goes: never.

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
  dev group of `pyproject.toml`; `./pyt add --cpython-only` (`cmd_env.cmd_add`, 7). Test:
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
  list, a metaclass other than ABCMeta (every Enum), a NamedTuple or a TypedDict make a slow
  Python class; nested classes, classes in functions, t-strings and a module-level `if __name__
  == "__main__"` are unsupported. Fix: the `lintc` rules, `lintc.NATIVE_CLASS_DECORATORS`,
  `lintc.NATIVE_METACLASSES`, `lintc.NON_NATIVE_BASES` (9). Test:
  `test_mypyc_core.py::test_lintc_native_decorators_follow_the_locked_mypyc`,
  `test_lintc_native_metaclasses_follow_the_locked_mypyc`,
  `test_lintc_flags_classes_mypyc_compiles_as_python_classes_for_their_metaclass`,
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
- **mypy splits `mypy_path` on `,` and `:` before it expands variables** (LIMITATION): the
  absolute typings path of the compile-time mypy.ini fell apart in a project folder with a
  comma (or a colon), so mypyc lost the project's stubs (the raylib preset's) and refused the
  skeleton. Fix: `render.mypy_ini` writes it relative to the ini as
  `$MYPY_CONFIG_FILE_DIR/<relative path>` (6.2). Test:
  `test_mypyc_core.py::test_compile_mypy_ini_finds_typings_in_any_project_folder`. Goes: never.

setuptools:
- **An extension is rebuilt only when a source is newer** (LIMITATION): a new `opt_level`, C
  flag or compiler variable kept the old binary (and shipped it). Fix: `mypyc.COMPILED_STAMP`
  and `build_ext --force` (9). Test:
  `test_mypyc_core.py::test_build_forces_a_rebuild_for_every_binary_option`,
  `test_build_forces_a_rebuild_when_the_compiler_environment_changes`. Goes: never.
- **mypyc's incremental cache ignores its own options** (LIMITATION, mypyc 2.3.1): with
  `compile.separate = true` (incremental) an unchanged module's IR and C came from the cache,
  so a new `strip_asserts` (`deploy.optimize`) or `strict_dunder_typing` never reached the
  binary. Fix: a forced build (`mypyc.COMPILED_STAMP`) deletes the profile's `mypy_cache` and
  `c` first (9). Test: `test_mypyc_core.py::test_a_forced_build_starts_without_the_cached_ir_and_c`
  (reproduced with a real compile). Goes: when mypyc keys its cache on those options.
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
  `test_mypyc_core.py::test_compiler_hint_names_the_msvc_tools_of_the_venv_platform`,
  `test_build_compiler_hint_names_the_tools_of_the_venv_platform`. Goes: never.

PyInstaller:
- **UPX only on Windows** (LIMITATION): `configure.get_config` turns UPX off elsewhere (packed
  `.so` files crash). Fix: `exe.size_args` passes `--noupx`, downloads nothing and warns (10).
  Test: `test_build_methods.py::test_exe_size_args_skip_upx_off_windows`. Goes: never.
- **Its UPX step takes no level** (LIMITATION): PyInstaller always adds `--lzma`, and its binary
  cache (global, keyed without the level) reused another level's output. Fix: the level in the
  `UPX` variable (`upx.env_value`), `--clean` (`exe.build`, and `exe._flet_pack` through
  `--pyinstaller-build-args`) (10). Test: `test_upx.py::test_level_flags_and_pyinstaller_env`,
  `test_build_methods.py::test_exe_size_args_use_upx_on_windows`,
  `test_flet_pack_cleans_pyinstallers_cache`. Goes: never.
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
- **Its upx plugin has no exclude option** (LIMITATION): in a standalone build it packs every DLL
  it copies (only `vcruntime140*` is skipped), `python3*.dll` and the files of
  `deploy.upx.exclude` (a DLL that packing breaks) included. Fix: a standalone folder goes
  through `upx.pack_tree` once it is in `dist/`; onefile keeps the plugin, which packs only the
  binary, unless `upx.excluded` names it (`nuitka.build`, 10). Test:
  `test_build_methods.py::test_nuitka_upx_honours_the_excludes`. Goes: never.
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
  at `flet_desktop/app/`, from the same URL (or its `FLET_CLIENT_URL` override, a mirror), for
  the nuitka method and a bundled portable folder (`portable._bundle_flet_client`), with
  `nuitka.flet_client_env` pinning the Linux name it looks for (10). Test:
  `test_apply.py::test_flet_version_change_replaces_the_three_pins`,
  `test_workarounds.py::test_nuitka_bundles_the_flet_client`,
  `test_the_flet_client_follows_flet_client_url`,
  `test_build_methods.py::test_a_bundled_portable_flet_app_carries_its_desktop_client`. Goes:
  never.
- **flet_desktop names the client it looks for from the machine and folder the app runs in**
  (LIMITATION, Flet 1.0.1): on Linux `flet-linux-<distro>[-light]-<arch>.tar.gz`, the distro from
  the user's glibc bracket (`FLET_LINUX_DISTRO` overrides it) and the flavor from
  `FLET_DESKTOP_FLAVOR`, else `[tool.flet] desktop_flavor` of a pyproject.toml in its CURRENT
  folder, else light. A bundled archive of another name is ignored and the client downloaded at
  the first start (offline: the app cannot start). Fix: `nuitka.flet_client_env` reads both
  parts back from the bundled name, and nuitka forces them into the binary
  (`--force-runtime-environment-variable`), a bundled portable folder into its launchers (10).
  Test: `test_workarounds.py::test_nuitka_pins_the_client_name_the_app_looks_for`,
  `test_build_methods.py::test_the_flet_client_env_pins_what_the_archive_was_named_for`. Goes:
  never (flet pack's own exe does not pin it: another glibc bracket downloads its client).
- **flet_desktop hashes a bundled client without its fingerprint, and names the window "flet"**
  (LIMITATION, Flet 1.0.1): it keys its client cache by the archive's SHA-256, read from
  `<archive>.sha256` (`flet pack` writes it) or hashed at every start that cannot keep one next to
  the archive (onefile, a read-only folder); and the taskbar groups the client window as the shared
  `flet` unless `FLET_APP_ID` (Linux) or `FLET_APP_USER_MODEL_ID` (Windows) is set, which flet
  pack's PyInstaller runtime hook does. Fix: `nuitka._fingerprint` writes the sidecar with the
  cached archive, and nuitka and a bundled portable folder ship it; Linux gets `FLET_APP_ID`
  (forced by nuitka, exported by the portable launcher); Windows' ID must be the executable's path
  at runtime, which neither can set: not done (10). Test:
  `test_workarounds.py::test_nuitka_bundles_the_client_fingerprint_flet_pack_writes`,
  `test_nuitka_pins_the_client_name_the_app_looks_for`,
  `test_build_methods.py::test_a_bundled_portable_flet_app_carries_its_desktop_client`. Goes:
  never.
- **flet loads its controls lazily** (LIMITATION): module `__getattr__` + `importlib`, which
  Nuitka cannot follow. Fix: `--include-package=flet --include-package=flet_desktop` in
  `nuitka.build` (10). Test: `test_workarounds.py::test_nuitka_bundles_the_flet_client`. Goes:
  never.
- **`flet pack` options** (LIMITATION): `-y` wipes `<cwd>/build` and the distpath, `--onedir` is
  refused on macOS (always a `.app`), it adds `--noconsole` unless `--debug-console` has a
  value, each `--pyinstaller-build-args` value is one argument, and on Linux it writes a
  desktop entry whose `Exec` is the absolute executable path under `--distpath`. Fix:
  `exe._flet_pack` runs in `.build/flet-pack/<b>` with the final `dist/` folder as
  `--distpath`, never passes `--onedir` on macOS, maps `deploy.exe.console` to
  `--debug-console=true`, passes `--python-option=X utf8` as one item (10). Test:
  `test_build_methods.py::test_flet_pack_macos_is_never_onedir`,
  `test_flet_pack_console_and_utf8` (every `_flet_pack` test checks the cwd),
  `test_flet_pack_desktop_entry_names_the_shipped_executable`. Goes: never.
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
- **`flet build` takes a mobile or web target's binary packages from Flet's own index**
  (LIMITATION, flet-cli 1.0.1 with serious_python 4.7.1): pip runs with `--only-binary :all:`
  and `--extra-index-url https://pypi.flet.dev` (for the web also a local index of the packages
  of its Pyodide release, where numpy and msgpack come from: pypi.flet.dev has no Emscripten
  wheel of them), which hold other releases than PyPI (msgpack 1.1.0 and 1.1.2, where a new flet
  project locks 1.2.2 from PyPI, which has no Android or iOS wheel; Pyodide 314.0.7 has numpy
  2.4.6): the exact pin had no solution and an `apk`, `aab` or `ipa` build failed. Fix:
  `common.unpin_binaries` in `methods.flet.build` (10); a lower bound the project itself keeps
  can still be newer than those hold (`./pyt add numpy` writes `numpy>=<the newest on PyPI>`),
  so `flet.relaxed_message` names it and says to lower it in pyproject.toml. Test:
  `test_build_methods.py::test_flet_build_leaves_mobile_binaries_to_flets_index`,
  `test_flet_relaxed_message_hints_at_a_lower_bound_only_when_one_is_kept`. Goes: never
  (Flet's index and Pyodide follow their own schedule).
- **`flet build` looks for `<work>/<path>/main.py`** (LIMITATION): another `[tool.flet.app]
  path` aborted after installing Flutter. Fix: `methods.flet.build_pyproject` forces
  `methods.flet.STAGE_APP` with a warning (10). Test:
  `test_build_methods.py::test_flet_build_pyproject_points_at_the_staged_app`. Goes: never.
- **`flet build` cleans the packages unless told not to** (LIMITATION): its packages setting of
  `[tool.flet.cleanup]` defaults to true and `--cleanup-packages` has no negative form, so
  `[deploy.flet] cleanup = false` (no flags) still cleaned them. Fix: `methods.flet.build_pyproject` writes `app` and
  `packages` false into the build's `[tool.flet.cleanup]` unless the project sets them (10).
  Test: `test_build_methods.py::test_flet_build_cleanup_false_turns_flets_own_cleanup_off`.
  Goes: never.
- **Flutter needs Developer Mode on Windows (symlinks), and mobile and web targets load no
  extension** (LIMITATION): Fix: `methods.flet._developer_mode` is checked by
  `methods.flet.check_options` before the checks and the payload (also in `--dry-run`); mobile
  and web builds ship the `.py` (10). Test:
  `test_build_methods.py::test_flet_build_needs_developer_mode_on_windows`,
  `test_flet_method_refuses_before_any_work`, `test_flet_build_mobile_and_web_ship_the_py_code`.
  Goes: never.
- **`flet build` reads `sys_platform` and `os_name` markers for the build machine on mobile and
  web targets** (DEFECT, flet-cli 1.0.1 with serious_python 4.7.1): its pip runs on the build
  machine with only `platform.system()` faked for the target (serious_python's pip
  `sitecustomize`, bin/sitecustomize.dart: `Android`, `iOS`, `Emscripten`), and uv writes a
  `platform_system` marker as a `sys_platform` one, the same marker for uv (astral-sh/uv#9949):
  flet's `platform_system != 'Emscripten'` (httpx, oauthlib: flet leaves them out in a browser),
  a user's `platform_system == 'Android'` or `'Linux'`. Every web app carried httpx and its tree,
  1.2 MB of the skeleton's 5.6 MB `app.zip`; an apk lacked its Android-only requirements and got
  the Linux-only ones (a native one failed the build: no Android wheel); from a Windows build
  machine `os_name == 'nt'` held too. Up: none found (cf. the flet 0.85.1 release notes, which
  moved flet's own markers to `platform_system` so that flet build's pip reads them). Fix:
  `methods.flet.target_markers` writes the `sys_platform` markers of a mobile or web build's pins
  as `platform_system` ones (`flet.PLATFORM_SYSTEM`: android, darwin, emscripten, ios, linux,
  win32; another value, `cygwin`, stays), and `os_name` ones too (every such target is POSIX),
  which mean the same on the device (10). A `platform_machine` marker is still the build
  machine's (serious_python runs pip once per ABI with the same requirements): not handled.
  Test: `test_build_methods.py::test_flet_target_markers_say_what_flets_pip_reads` (through
  `packaging`'s marker evaluation, every target from every build machine),
  `test_flet_build_rewrites_the_markers_of_mobile_and_web_pins`. Goes: when flet build's pip
  reads the target's `sys_platform` (or uv keeps such a marker as written).
- **Flutter web draws the app on a canvas** (LIMITATION): the page's DOM holds no text or button
  of the app; Flutter's semantics tree (its accessibility DOM, `flt-semantics-host`) does, once
  a screen reader or a click on its `flt-semantics-placeholder` turns it on. Fix:
  template-flet/check.py (`SEMANTICS_JS`, `TEXTS_JS`, `BUTTON_JS`) turns it on, reads the texts
  and the button's box there, clicks the box with the mouse and, when nothing starts within
  `CLICK_AGAIN` seconds, taps the semantics node itself (13.2). Test:
  `test_workflows.py::test_flet_web_check_drives_the_app` (a fake page). Goes: never.

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
- **raylib_software publishes no macOS wheel, and no sdist** (LIMITATION, raylib-python-cffi:
  6.0.1.0 and every release before it have wheels for Linux i686, x86_64 and aarch64 and for
  Windows only; its README does not say so): `[preset.raylib] package = "raylib_software"`, an
  option the preset offers, locks fine but `./pyt setup` cannot sync on macOS ("marked as
  `--no-build` but has no binary distribution"), and the macOS job of the generated CI failed on
  every push. Up: none found. Fix: `render.NO_WHEELS_FOR` (read by `render.ci_workflow` through
  `_unsupported_oses`) leaves the macOS runner out for it; the README, the preset.toml comment
  and the skeleton's pytemplate.toml say Linux and Windows only (11). Test:
  `test_render_core.py::test_ci_workflow_leaves_out_an_os_the_preset_package_has_no_wheel_for`.
  Goes: when raylib_software ships macOS wheels.

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
  macOS with a warning (`upx.active`, and `exe.size_args` with the same reason);
  `upx.ASSETS` gives Windows arm64 the x64 build (emulated) (10). Test:
  `test_workarounds.py::test_upx_is_off_on_macos_and_windows_arm64_runs_the_x64_build`,
  `test_build_methods.py::test_exe_size_args_say_why_upx_is_off_on_macos`. Goes: per case, when
  UPX supports it.
- **The packagers look for UPX differently** (LIMITATION): PyInstaller wants `<upx-dir>/upx`,
  Nuitka a file named `upx` (any other name: it searches PATH). Fix: `upx.find` hands them an
  absolute, unresolved path, and `upx.locate` refuses a `deploy.upx.path` of another name (10).
  Test: `test_upx.py::test_relative_upx_path_resolves_against_the_project_root`,
  `test_a_upx_path_not_named_upx_is_refused_before_any_work`. Goes: never.

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
- **ruff expands `~`, `$NAME` and `${NAME}` in the paths of a configuration file and in its
  `--config` argument** (LIMITATION, documented for `src`, `extend` and `cache-dir`; ruff 0.16.9
  does it for `--config` too), and reads the relative paths of a `--config` file against its
  cwd (documented): the absolute paths of `.build/cfg/ruff-*.toml`
  under a project folder such as `app$v2` made `check`, the checks of `build` and every commit's
  hook fail ("does not point to a configuration file", "environment variable not found") and
  skipped the rename tidy-up. Fix: `render.ruff_config(relative_to=ROOT)` in
  `cmd_dev._profile_file`, and `cmd_dev.config_arg` for the `--config` of `run_checks`,
  `hooks.check_ruff` and `rename.tidy_before`/`tidy_after`, which all start ruff in ROOT (6.2).
  Test: `test_cli_core.py::test_check_runs_ruff_in_a_project_folder_named_like_a_variable`,
  `test_hooks.py::test_real_ruff_runs_in_a_project_folder_named_like_a_variable`,
  `test_rename.py::test_ruff_tidy_in_a_project_folder_named_like_a_variable`. Goes: never.

Cosmic Ray (selftest --mutation, 13.1):
- **ExceptionReplacer names a class the mutated module never sees, and fails on dotted ones**
  (DEFECT, Cosmic Ray 8.7.0): it writes `CosmicRayTestingException`, which only
  `cosmic_ray.exceptions` defines, in place of the class, so every exception that reaches the
  handler becomes a NameError: a test that expects another exception to go through (or an outer
  handler to catch it) killed a mutant that only switched a handler off. On a dotted class in a
  tuple (`except (OSError, subprocess.TimeoutExpired):`, 45 mutants of the runner) it stops with
  AttributeError, and on a lone dotted class it replaces the dot
  (`subprocessCosmicRayTestingExceptionTimeoutExpired`). Up: none found. Fix:
  `mutation.own_mutant` makes these mutants itself: the class (`mutation.handler_classes`)
  becomes `()`, or `*()` in a tuple, and `mutation.select` gives each class one mutant with its
  span; a mutant Cosmic Ray still fails to make is skipped (`tools/mutation_cr.py` answers
  `cannot`, `mutation.made`). Test: `test_mutation.py::test_an_exception_handler_mutant_catches_nothing`,
  `test_select_gives_each_exception_class_one_mutant_with_its_whole_span`,
  `test_cosmic_rays_defects_that_the_runner_works_around` (a pin: it fails once Cosmic Ray's
  own mutant lets the exception through), `test_a_real_run_kills_what_the_tests_check_and_finds_what_they_miss`.
  Goes: when that pin fails.
- **Some mutants are no valid Python** (DEFECT, Cosmic Ray 8.7.0): Pow_Mul turns the `**` of a
  dict display into `*` (`{**extra, "k": 1}` -> `{*extra, "k": 1}`), AddNot puts `not` before a
  walrus (`if not x := f():`); pytest then stopped at the import, which read as a kill (35 and 4
  mutants of the runner). Up: none found. Fix: `mutation.made` compiles every mutant and skips
  one that does not compile, with the reason (13.1). Test: `test_mutation.py::test_made`,
  `test_cosmic_rays_defects_that_the_runner_works_around`. Goes: never (a cheap check).

VS Code, its extensions, pyright and basedpyright:
- **Shell tasks run in the user's terminal profile** (LIMITATION): xonsh, niubash or MSYS2 as
  the default profile broke `"type": "shell"` tasks. Fix: every task is `"type": "process"`
  running `/bin/sh <root>/pyt` or `pyt.cmd` (12.1). Test:
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
  `vim.secure`): any byte change (CRLF, a BOM, a mode-dependent value) untrusted `.lazy.lua`;
  the path is realpath(3)'s, which on macOS has the on-disk case and Unicode form, where
  Python's realpath keeps the typed ones (a trusted file read as untrusted, and `nvim trust`
  failed with exit 3). Fix: `.lazy.lua` is a static copy (`editors/nvim.py`), `.gitattributes`
  keeps it LF, all logic lives in `.pytemplate/nvim/`; `cmd_nvim.trust_status` compares the
  paths with `cmd_nvim.same_path`, as the volume does (12.2). Test:
  `test_nvim_render.py::test_lazy_lua_is_identical_in_every_mode`,
  `test_lazy_lua_bytes_are_pinned`, `test_gitattributes_keeps_lazy_lua_lf`,
  `test_cmd_nvim.py::test_trust_macos_paths_ignore_case_and_unicode_form`. Goes: never.
- **Neovim reads a 'runtimepath' entry as a file glob** (LIMITATION): `[ ] { }` are wildcards, a
  comma separates entries, a backslash escapes, a backtick is command substitution, a single quote
  sends it through 'shell' and a `$NAME` is expanded (vim.secure expands it too), so in a project
  folder whose path holds one of these the plugin cannot be put on the runtimepath (require fails,
  E79), and because the optional plugins' `opts` delegate to it their config broke too (no LSP, no
  lint, no tasks). On Windows (0.12.5: path.c's `path_has_exp_wildcard` is `*?[` there, and the
  `SPECIAL_WILDCHAR` that sends an entry to 'shell' is defined in `os/unix_defs.h` only) just `[`,
  the comma and `$NAME` do. Up: none found. Fix: `spec.lua` refuses such a root (returns `{}` with
  one `vim.notify`, so the other plugins keep working), with the Windows set there;
  `cmd_nvim.rtp_unsafe_char` names the character and `nvim doctor`/`nvim trust` report it (12.2).
  Test: `test_nvim_render.py::test_lazy_lua_refuses_a_runtimepath_unsafe_root`,
  `test_the_runtimepath_rule_is_the_same_in_spec_lua_and_cmd_nvim`. Goes: never.
- **`vim.secure.trust`** (LIMITATION): the `path` form exists from 0.12 only (0.11 needs a
  buffer), and it writes its database with `io.open(<state>/trust, "w")`, which fails while the
  state folder does not exist. Fix: the trust snippet of `cmd_nvim.trust_file` creates the
  folder and picks the form (12.2). Test: `test_cmd_nvim.py::test_real_nvim_query_and_trust`.
  Goes: the buffer form once 0.12 is the minimum.
- **Headless Neovim** (LIMITATION): a Lua error still exits 0, `confirm()` never returns,
  `VeryLazy` never fires, and an unwritable `NVIM_LOG_FILE` drops `nvim.log` into the cwd. Fix:
  the `PTNVIM{json}` marker of `cmd_nvim.headless`; `nvim sync` asks lazy.nvim afterwards which
  plugins are installed (`cmd_nvim.SYNC_CHECK_LUA`: a failed clone exited 0 and printed "ok
  plugins installed"); trust through the API first (`nvim sync` refuses while `.lazy.lua` is
  untrusted); `doautocmd UIEnter` in `nvimtest`; `NVIM_LOG_FILE` always set (12.2). Test:
  `test_cmd_nvim.py::test_parse_marker_skips_noise`,
  `test_nvim_sync_refuses_an_untrusted_lazy_lua`, `test_nvim_sync_installs_only`,
  `test_nvim_sync_fails_when_a_plugin_is_not_installed`,
  `test_real_nvim_sync_checks_the_plugins`, `test_real_nvim_query_and_trust`. Goes: never.
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
  and removed plugins its spec did not name. Fix: `./pyt nvim sync` runs `Lazy! install`
  (12.2). Test: `test_cmd_nvim.py::test_nvim_sync_installs_only`. Goes: never.
- **The local spec comes after the user's** (LIMITATION): an extra imported from `.lazy.lua`
  trips LazyVim's import-order check. Fix: `.lazy.lua` sets `vim.g.lazyvim_check_order = false`
  only then; `./pyt nvim extras` (12.2). Test:
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
  and on Windows every linter runs through `cmd.exe /C`, where an absolute path breaks on
  a space or `& ^ %` (libuv quotes a Windows argument only when it holds a space, tab or double
  quote, so `C:\dev\R&D\...` reaches cmd.exe bare and is split at `&`). Fix:
  `integrations.mypy_linter` passes the whole environment plus `PYTHONUTF8=1`, minus
  `VIRTUAL_ENV`, `PYTHONHOME` and `PYTHONPATH` (proc.base_env drops the last two: a `PYTHONHOME`
  kills mypy and the pattern parser then shows no diagnostic; the LSP servers clear them too,
  and the debug adapter gets a whole copy without them, see `integrations.lsp` and
  `dap.adapter_env` in 12.2), and on Windows the
  bare `mypy` with `.venv\Scripts` first on PATH plus the buffer's path and `--python-executable`
  as root-relative paths (the linter's `cwd = root`, `under_root`; the buffer's path is a function
  element of the `args` list, which nvim-lint's wrapper unpacks). Test:
  `test_workarounds.py::test_nvim_plugin_workarounds[mypy env]`,
  `test_nvim_render.py::test_mypy_linter_follows_a_venv_created_later` (Windows),
  `test_plugin_python_tools_drop_pythonhome_and_pythonpath`,
  `test_windows_mypy_linter_uses_root_relative_paths`,
  `test_the_windows_mypy_linter_runs_through_the_real_nvim_lint` (the pinned nvim-lint, where
  `selftest --nvim` left it). Goes: never.
- **nvim-dap** (LIMITATION): it spawns adapters with a raw `uv.spawn` (no PATHEXT: Mason's
  `.cmd` shim fails on Windows) and hands it the adapter's `options.env` unchanged, which luv
  reads as a list of `K=V` strings that REPLACES the environment (a map is an empty list: the
  adapter and the programs it launched ran with no variable at all), waits 4 s for `initialize`
  (a cold adapter needs more), and expands `${workspaceFolder}` to Neovim's cwd. Fix:
  `dap.adapter` (Lua) returns an absolute python, `dap.adapter_env` gives the whole environment
  as that list (without `PYTHONHOME` and `PYTHONPATH`), `initialize_timeout_sec = 30` unless the
  configuration sets one, `dap.launch_configs` for a cwd below the root (12.2). Test:
  `test_workarounds.py::test_nvim_plugin_workarounds[dap]`, `[dap adapter]`, `[dap subfolder]`,
  `test_nvim_render.py::test_the_debug_adapter_gets_the_whole_environment_but_pythonhome`,
  `test_a_real_debug_session_keeps_the_environment` (the pinned nvim-dap, where `selftest --nvim`
  left it). Goes: never.
- **neotest-python finds the interpreter by globbing `*/pyvenv.cfg`** (DEFECT): with `.venv` and
  `.venv-pypy` it built a broken path, and its `uv run` fallback syncs the project. Up: none
  found. Fix: `integrations.neotest` sets `python` and `discovery.filter_dir` (12.2). Test:
  `test_workarounds.py::test_nvim_plugin_workarounds[neotest]`. Goes: when neotest-python
  handles several environments.
- **overseer also loads the `.vscode/tasks.json` provider** (LIMITATION): every label twice,
  tasks through `pyt.cmd`, shell tasks through `'shell'`. Fix: `integrations.overseer` adds
  `overseer.template.vscode` to `disable_template_modules` (12.2). Test:
  `test_workarounds.py::test_nvim_plugin_workarounds[overseer]`. Goes: never.
- **venv-selector (LazyVim's `lang.python`) needs `fd`** (LIMITATION, documented): without it it
  raises an error on the first Python buffer, which failed every smoke check that opens one.
  Fix: the CI image holds `fd`, and `template-nvim.yml` installs it on Windows (one release,
  SHA-256 checked) and in the canary (`fdfind` on Ubuntu) (13.2); `./pyt nvim doctor`
  counts a missing fd as a problem, with the install command (`cmd_nvim.TOOLS`, 12.2). Test:
  `test_cmd_nvim.py::test_nvim_doctor_needs_fd`, `test_workflows.py::test_the_nvim_workflow_installs_a_pinned_fd`;
  CI (that workflow's smoke run fails without it). Goes: never.
- **venv-selector's uv flow switches the debugger for the whole session** (LIMITATION, its design:
  one active environment at a time): for every buffer with PEP 723 script metadata (its uv2.lua,
  always on; `.pytemplate/pyt.py` and `tools/mutation_cr.py` have some) it runs `uv sync
  --script` and activates that environment (`venv.update_paths` -> `path.update_python_dap`),
  which replaces dap-python's `resolve_python` and stays after the user goes back to the app: F5
  on launch.json's CPython configuration ran `src/main.py` on the runner's script environment
  (ModuleNotFoundError: rich) until Neovim restarted. `vim.b.venv_selector_disabled`, its only
  switch per buffer, also unsets a user's `$VIRTUAL_ENV`. Fix: `dap.setup` wraps the adapter's
  `enrich_config`: a configuration without `python`/`pythonPath` gets `.venv`, unless its program
  holds such metadata (`dap.inline_script`, its uv2.lua's own test) or `$VIRTUAL_ENV`/
  `$CONDA_PREFIX` is set (12.2). Test:
  `test_nvim_render.py::test_the_debugger_keeps_venv_after_a_pep_723_script_was_opened`,
  `test_the_real_dap_python_keeps_venv_after_venv_selector_switched_it` (the pinned plugins, where
  `selftest --nvim` left them). Goes: never.
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
  linked worktree, a submodule or a `--separate-git-dir` clone, `GIT_DIR` without
  `GIT_WORK_TREE`, which makes the cwd the top (wrong after a hook's `cd apps/a`). Fix:
  `hooks.git_env` makes them absolute when `GIT_DIR` is set and leaves a relative index alone
  otherwise (git reads it from the top); `hooks._found_from` lets git find that `GIT_DIR` from
  the project folder; `hooks.run` removes them for uv and ruff (5.6). Test:
  `test_hooks.py::test_git_env_pins_relative_paths`, `test_install_in_a_linked_worktree`,
  `test_a_hook_that_cds_into_the_project_reads_the_real_index`,
  `test_a_hook_that_cds_into_the_project_finds_the_top_of_a_checkout_whose_git_is_a_file`.
  Goes: never.
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
  the index); `presets._git_init` stages both launchers executable (a new repository, or an
  enclosing one with `core.filemode=false`); `sh <launcher>` in the hook; `/bin/sh
  <root>/pyt` in VS Code tasks and the Neovim fallback (5.6, 11, 12, 14). Test:
  `test_envs_core.py::test_fix_exec_bit_repairs_the_index_and_the_files`,
  `test_presets.py::test_git_init_in_a_monorepo_stages_the_launchers_executable`,
  `test_hooks.py::test_git_runs_the_hook`,
  `test_vscode.py::test_every_task_runs_the_launcher_as_a_process`. Goes: never.
- **CRLF checkouts** (LIMITATION): `* text=auto eol=native` with `core.autocrlf=true` checks
  text out with CRLF on Windows. Fix: generated-file hashes are normalized (`render._norm`),
  comparisons are CRLF-normalized (`presets.pristine`, the pyz parts' app code), the hook reads
  staged files through `git cat-file --filters` (`hooks.staged_blob`), `.gitattributes` pins LF
  for `pyt`, `pyt.ps1`, `.lazy.lua` and CRLF for `*.cmd` (6.2, 14). Test:
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
- **`actions/cache` saves its cache only at the end of a job that passed** (LIMITATION,
  documented): a check that failed after a cold flet build (Flutter, Gradle and Flet's
  downloads, several GB) threw them away, and every red run downloaded them again. Fix:
  template-flet.yml restores with `actions/cache/restore` and saves with `actions/cache/save`
  right after the build step, before the checks (13.2). Test:
  `test_workflows.py::test_flet_workflow_builds_for_the_web_and_android`. Goes: never.
- **A container job runs as the image's USER (root for most images: the runner passes no
  `--user`), with `tail -f /dev/null` as PID 1 and `HOME=/github/home`, and `${{ runner.temp }}`
  is a host path, translated only in step inputs and environment values** (LIMITATION): as
  root, git would see the runner's (uid 1001) checkout as dubious ownership and the
  read-only-folder tests would skip; nothing reaps orphaned processes (the nvim harness counts
  them as alive); the image's caches under `/home/runner` are not found. Fix: the image's
  `system` creates `runner` with uid `pins.CI_UID` (1001) and the Dockerfile ends with `USER
  runner`; the jobs of template-ci-image.yml also pass `options: --init --user 1001` (the uid
  stays right if USER changes) and `HOME: /home/runner`, and its nvim job uses `/tmp/pt-nvim`
  (13.2). Test: `test_workflows.py::test_ci_image_workflow_builds_publishes_and_runs_the_linux_jobs`.
  Goes: never.
- **A GHCR package pushed with the job token is private, and a fork's pull request token cannot
  push** (LIMITATION): Fix: the container jobs pull with the job token (`packages: read`), the
  `image` job pushes only from this repository, and the jobs in the image run only when its tag
  is usable (13.2). Test: `test_workflows.py::test_ci_image_workflow_builds_publishes_and_runs_the_linux_jobs`.
  Goes: never.

Ubuntu and apt (the CI image, 13.2):
- **The ubuntu:24.04 image has no CA certificates, and snapshot.ubuntu.com serves https only**
  (LIMITATION): an apt call with the snapshot failed before the certificates were there. Fix:
  the image's `system` installs ca-certificates from the live archive over http first (apt
  checks the signed Release files), then sets `APT::Snapshot` for every later apt call. Test:
  the image build of template-ci-image.yml. Goes: never.
- **apt downloads as the `_apt` user, and a BuildKit secret is readable by root only** (LIMITATION):
  a builder's CA bundle (a TLS-intercepting proxy) could not be read by apt. Fix: the
  Dockerfile mounts the `ca` secret with `mode=0444`. Test: untested on CI (template-ci-image.yml
  passes no secret); a local build behind such a proxy, September 2026 (13.3). Goes: never.

PowerShell (details: section 4.5):
- **A `.ps1` runs inside the caller's session** (LIMITATION): what it sets stays there, and the
  caller's strict mode and preferences reach it (with `'Stop'`, 7.3+ turned a runner exit code
  into an error and 5.1 turned redirected stderr into a bogus exit 126; 5.1 also printed module
  progress). Fix: `pyt.ps1` restores every variable in `finally`, never assigns `$env:PATH`,
  resets strict mode and the preferences in its own scope. Test:
  `test_launcher_win.py::test_ps1_round_trip_in_a_session`,
  `test_ps1_restores_every_variable_it_sets`. Goes: never.
- **A `param()` block would take `-v`, `-h`, `-q`** (LIMITATION): Fix: `pyt.ps1` has none.
  Test: `test_launcher_win.py::test_ps1_has_no_param_block_and_leaves_path_alone`. Goes: never.
- **`$null` passed to a .NET string parameter becomes `''`** (LIMITATION; with .NET 9, in
  PowerShell 7.5+, `''` no longer removes a variable): `SetEnvironmentVariable($n, $null)` left
  an empty variable. Up: PowerShell/PowerShell#24637 (closed as a duplicate). Fix: `pyt.ps1`
  uses `Remove-Item Env:NAME`. Test:
  `test_launcher_win.py::test_ps1_clears_the_callers_uv_python_and_restores_it`. Goes: never
  (`Remove-Item` stays right).
- **Native arguments before 7.3 (and in `Legacy` mode)** (DEFECT): empty arguments were dropped
  and embedded quotes mangled; 5.1's quote counter ignores backslashes. Up:
  PowerShell/PowerShell#1995 (fixed by 7.3's `$PSNativeCommandArgumentPassing`). Fix:
  `pyt.ps1` pre-quotes every argument (`""` on 5.1, `\"` on 7). Test:
  `test_launcher_win.py::test_ps1_round_trip_in_a_session` (Legacy mode, 5.1 on Windows),
  `test_ps1_hand_over_is_injection_safe`. Goes: when Windows PowerShell 5.1 is dropped.
- **PowerShell 7 rewrites native arguments that are not quoted literals** (DEFECT): splatted
  `@argv` included, it globs on Linux/macOS (`'*'` reached uv as a file list) and expands `~`.
  Up: PowerShell/PowerShell#24178 (the globbing; none found for `~`). Fix: on Core `pyt.ps1`
  rebuilds the call from single-quoted words (`EscapeSingleQuotedStringContent`, typographic
  quotes too) for `Invoke-Expression`. Test:
  `test_launcher_win.py::test_ps1_hand_over_is_injection_safe`; `shells.PS_ARGS` in `selftest
  --shells`. Goes: when splatted arguments pass through verbatim.
- **7.3+ takes any native argument equal to `--%`, quoted or splatted, for the stop-parsing
  token** (LIMITATION): it drops it, then splits and `%VAR%`-expands the rest. Fix: `pyt.ps1`
  switches that call to `Legacy` passing in its own scope. Test:
  `test_launcher_win.py::test_ps1_passes_a_literal_stop_parsing_token`. Goes: never.
- **A script gets a typed list (`a,b`) and an array value (`$files`) alike, as one array**
  (LIMITATION): a native program gets the list as one argument `a,b` and the value's items as
  separate arguments, but the launcher saw the same `object[]`: it split both (`mode
  --supports cpython,mypyc` exited 2), then joined both (`./pyt run $files` gave the app
  `a.py,b.py`). Fix: `pyt.ps1`'s `Get-Typed` reads the text of the call (an
  `ArrayLiteralAst` is a typed list; a forwarded `@args` is read at its caller) through the
  internal `InvocationInfo.ScriptPosition`; another splatted variable passes values, as for a
  native program; what it cannot tell counts as a typed list (4.5).
  Test: `test_launcher_win.py::test_ps1_keeps_typed_comma_lists_whole`,
  `test_ps1_passes_array_values_like_a_native_call`. Goes: never.
- **A typed `-X:v` reaches a script as two elements** (DEFECT): `'-X:'` and `v`. Up:
  PowerShell/PowerShell#6360 (closed for inactivity; 7.6 still splits it). Fix: `pyt.ps1`
  joins them again (limit: `-X: v`; `pwsh -File` and the shebang route split at the colon before
  the script runs, 4.5). Test:
  `test_launcher_win.py::test_ps1_keeps_typed_colon_arguments_whole`. Goes: when fixed upstream.
- **A script that names `$input` gets a redirected stdin re-read as text** (DEFECT): under `pwsh
  -File` and the shebang route, invalid UTF-8 became U+FFFD and CRLF became LF. Up: none found.
  Fix: `pyt.ps1` never names the variable and reads it by name. Test:
  `test_launcher_win.py::test_ps1_never_names_the_pipeline_variable`,
  `test_ps1_forwards_pipeline_input_and_keeps_raw_stdin`. Goes: never.
- **ConstrainedLanguage mode blocks every .NET call** (LIMITATION, AppLocker/WDAC): Fix:
  `pyt.ps1` checks the language mode first: one error, exit 126. Test:
  `test_launcher_win.py::test_ps1_constrained_language_gives_one_clear_error`. Goes: never.
- **Windows PowerShell 5.1 runs no script by default on client Windows** (LIMITATION: execution
  policy `Restricted`; `AllSigned` refuses an unsigned one too): a bare `pyt` there runs the
  installed pyt.ps1 (PowerShell takes it before pyt.cmd), which does not start, and install said
  nothing. Fix: `shells.doctor` reports the policy of each PowerShell (4.5), and install and
  doctor's `pyt install` step note a blocking one with its way out (`cmd_install.policy_notes`,
  `shells.ps_policies`: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or `pyt.cmd`;
  5.9). Test: `test_shells.py::test_doctor_counts_an_execution_policy_only_for_the_powershell_in_use`,
  `test_install.py::test_on_windows_install_and_doctor_note_a_powershell_that_blocks_pyt_ps1`.
  Goes: never.
- **`Get-Command uv` may return an alias, a function or a `uv.cmd`/`uv.ps1` wrapper**
  (LIMITATION): a wrapper parses the arguments again. Fix: `-CommandType Application -All` and
  only a real `uv.exe` on Windows (the plugin: `exepath('uv.exe')`; 4.1). Test:
  `test_launcher_sh.py::test_uv_search_order`,
  `test_nvim_render.py::test_lua_modules_in_headless_neovim`. Goes: never.
- **`[IO.File]::GetUnixFileMode` needs .NET 7 (PowerShell 7.3+)** (LIMITATION): Fix: older
  versions skip the x-bit check of a uv candidate (4.1). Test:
  `test_launcher_win.py::test_ps1_skips_a_uv_without_exec_bit`. Goes: once 7.3 is the minimum.
- **Windows PowerShell 5.1 reads a BOM-less script as ANSI and removes a bare `--`**
  (LIMITATION): Fix: `pyt.ps1` is pure ASCII; no CLI syntax needs `--` (14). Test:
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
  `rem`, and `call` expands `%*` twice. Fix: `pyt.cmd` and the generated `.cmd` files
  (`portable.cmd_launcher`, `pyz._wrapper_cmd`) are ASCII and CRLF, with no blocks, no delayed
  expansion and `%*` only on the uv line. Test:
  `test_launcher_win.py::test_cmd_is_ascii_with_crlf`,
  `test_cmd_has_no_blocks_and_no_delayed_expansion`,
  `test_cmd_forwards_arguments_only_on_the_uv_line`,
  `test_fixes.py::test_pyz_wrapper_is_ascii_crlf_without_blocks`. Goes: never.
- **cmd reads a batch file one line at a time, opening it again by name** (LIMITATION): `pyt
  uninstall` deleted the installed `pyt.cmd` that was running it, and cmd then said "The batch
  file cannot be found." (exit 1; after `pyt install` replaced it, cmd would run what the new
  file holds at the old offset). An `exit /b` right after uv on the uv line reads nothing more,
  but cmd still looked for the file (the same message) and `cmd /c` callers got exit 0 for
  every run. Fix: `pyt.cmd` names itself (`PYTEMPLATE_LAUNCHER_FILE`), `cmd_install.run_by_cmd`
  finds it, uninstall writes `cmd_install.self_deleting` in its place, which ends the batch and
  deletes itself on the line cmd reads next, and install refuses to replace it with other bytes
  (4.4). Test: `test_launcher_win.py::test_cmd_goes_on_reading_what_uninstall_leaves`,
  `test_cmd_takes_the_exit_code_on_the_line_after_uv`,
  `test_install.py::test_the_installed_pyt_runs_from_cmd_powershell_and_pwsh` (Windows),
  `test_uninstall_leaves_a_self_deleting_stand_in_for_the_pyt_cmd_cmd_runs`,
  `test_install_never_replaces_the_pyt_cmd_cmd_runs`. Goes: never.
- **`%~dp0` is wrong when cmd found the file through PATH from a quoted name** (DEFECT): Up:
  none found. Fix: `pyt.cmd` checks `%~dp0.pytemplate\pyt.py`, else walks up from `%CD%`.
  Test: `test_launcher_win.py::test_cmd_walks_up_from_the_current_folder` (Windows). Goes:
  never.
- **cmd re-parses `%*`** (LIMITATION, the BatBadBut class): `% ! " ^` and unquoted `& | < >`
  inside arguments do not survive, whatever quoting a CreateProcess caller applies. Fix: no CLI
  syntax needs them (14); `shells.cmd_quote` for the probes; a `uv = false` task whose program
  is a `.cmd`/`.bat` (npm, found through PATHEXT) refuses an argument cmd.exe would change
  (`tasks._batch_problem`, 6.1). Test: `test_shells.py::test_cmd_quote`,
  `test_cli_core.py::test_a_batch_file_gets_only_arguments_cmd_passes_unchanged`. Goes: never.
- **A quoted registry PATH entry splits `call` arguments** (LIMITATION): Fix: `pyt.cmd` keeps
  the value in a variable and drops the quotes, and its entries never go through `call`
  arguments. Test:
  `test_launcher_win.py::test_cmd_keeps_the_registry_path_out_of_call_arguments`,
  `test_cmd_registry_path_with_quoted_entries` (Windows). Goes: never.
- **In a batch file a `%NAME%` that is not defined expands to nothing, `call`'s second
  expansion included** (LIMITATION, documented; a command line keeps it as it is, and so does
  Windows' own expansion of the registry PATH): `call set` turned the entry
  `%JAVA_HOME%\bin` of an undefined JAVA_HOME into `\bin`, a folder of the drive root any user
  may create, and pyt.cmd ran the uv.exe there. Fix: `pyt.cmd` `:uv_in_list` expands the list
  in a child cmd (`for /f` over its `echo`), hands each entry to `:try_entry` in a variable and
  probes only absolute folders (4.4). Test:
  `test_launcher_win.py::test_cmd_never_takes_uv_from_a_registry_entry_that_is_not_absolute`,
  `test_cmd_registry_path_with_quoted_entries` (Windows). Goes: never.
- **`set "K=v"` in a generated launcher** (LIMITATION): a literal `%` must be written `%%`, and
  non-ASCII text, `"` and line breaks cannot be held. Fix: `portable._cmd_value` (10). Test:
  `test_fixes.py::test_portable_env_values_are_quoted`. Goes: never.
- **A bare name runs the first `<name><ext>` of a folder in PATHEXT order** (LIMITATION: cmd,
  and xonsh, nushell and Python's subprocess, which start PATHEXT files through it): `.COM`,
  `.EXE` and `.BAT` come before `.CMD`, so another tool's `pyt.exe` in uv's tool bin folder
  (`uv tool install python-taint`) ran instead of the installed `pyt.cmd`, and nothing said so.
  Fix: `cmd_install.pathext_shadows`: install refuses before any write, doctor notes it (5.9).
  Test: `test_install.py::test_on_windows_a_pyt_that_runs_before_pyt_cmd_is_refused`. Goes:
  never.

niubash (1.1.4) and WinuxCmd (details: section 4.6):
- **niubash runs `#!/bin/sh` scripts and `sh -c` inside the calling shell** (DEFECT): it ignores
  the shebang, `$0` is the caller's (`$(dirname "$0")` pointed into `%TEMP%`), variables,
  functions, aliases and `cd` leak into the session, and `exit`/`exec` only end the script. Up:
  none found. Fix: `pyt` finds itself through `$BASH_SOURCE`, prefixes every name with `_pt_`
  and unsets it, never changes folder, and runs uv without `exec` with a single exit (4.3); the
  portable `.sh` launcher uses `${BASH_SOURCE:-$0}` (10). Test:
  `test_launcher_sh.py::test_in_process_run_leaves_no_name_behind`, `test_lint`,
  `test_niubash_leaves_nothing_behind` (Windows). Goes: never.
- **niubash's parser** (DEFECT): `exit` is ignored inside `a || exit`, `a && exit` and `{ ...;
  }`; a comment on a `name() {` line is a syntax error; `"...$(cmd "$x")..."` keeps the inner
  quotes. Up: none found. Fix: the `pyt` rules, enforced by `test_launcher_sh.lint` (4.3).
  Test: `test_launcher_sh.py::test_lint`, `test_lint_detects`. Goes: when niubash fixes them.
- **niubash fakes its platform** (LIMITATION): `OSTYPE=msys`, `uname -s` = `MSWindows_NT`,
  `$PWD` like `C:/Users/...`. Fix: `pyt` detects Windows by `OS=Windows_NT` without
  `WSL_DISTRO_NAME`, and its walk-up stops at `C:` and `C:/` (4.3). Test:
  `test_launcher_sh.py::test_windows_helpers_in_posix_shells`. Goes: never.
- **A niubash session keeps stale exports** (LIMITATION): Fix: `project.caller_cwd` trusts
  `PYTEMPLATE_CALLER_CWD` only while it names the process cwd (4.8). Test:
  `test_paths.py::test_stale_caller_cwd_is_ignored`. Goes: never.
- **WinuxCmd's `cygpath.exe` knows no MSYS root** (DEFECT): it turns `/tmp/x` into `\tmp\x`. Up:
  none found. Fix: `project.find_cygpath` takes only a cygpath next to `msys-2.0.dll` or
  `cygwin1.dll`; `pyt` asks the root's `/usr/bin/cygpath` first, accepts only an `X:/...`
  answer and otherwise keeps the POSIX path (4.3, 4.8). Test:
  `test_paths.py::test_cygpath_without_the_msys_runtime_is_ignored`,
  `test_launcher_sh.py::test_winpath_inside_the_msys_root`,
  `test_winpath_asks_the_roots_own_cygpath_first`. Goes: never.

xonsh:
- **xonsh on Windows maps `#!/usr/bin/env bash` to `bash`** (LIMITATION): that can be the WSL
  stub. Fix: `pyt` starts with `#!/bin/sh` (4.3). Test:
  `test_launcher_sh.py::test_shebang_lf_ascii`. Goes: never.
- **xonsh changes the name and default of its subprocess raise-error setting** (LIMITATION):
  0.24 raises `CalledProcessError` from a failing `![...]` unless
  `$XONSH_SUBPROC_CMD_RAISE_ERROR` is off (0.18 did not raise), so a probe that relied on one
  setting's name got exit 1 instead of the child's code. Fix: the xonsh probe of
  `shells.command_text` names no setting and takes the code from `CalledProcessError` too (4.9).
  The template workflows still install xonsh 0.24.2 for pushes and pull requests and
  template-launchers the newest on its weekly run (13.2): a stable signal, and drift that
  shows. Test: `test_shells.py::test_xonsh_probe_exit_code_ignores_raise_settings` (every
  setting forced on), `test_command_text_per_family`. Goes: never.

MSYS2, Cygwin, Git Bash and busybox-w32 (details: section 4.7):
- **frippery.org, busybox-w32's only host, is often unreachable from GitHub's runners**
  (LIMITATION): four downloads in a row timed out and failed template-launchers' Windows job
  (September 2026). Fix: its `windows` job keeps the pinned file in the Actions cache
  (`actions/cache/restore`, key `busybox-w32-<file>-<sha256>`, saved right after it passed the
  SHA-256 check) and checks the restored copy again (13.2). Test:
  `test_workflows.py::test_the_busybox_step_refuses_a_file_that_is_not_the_pinned_one` (a cached
  copy runs without the server; a damaged one fails),
  `test_every_downloaded_file_is_checked_against_a_pinned_sha256`. Goes: never.
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
  `pyt` reads it through `eval`, then `emulate sh`. Test:
  `test_launcher_sh.py::test_posix_shell_runs_the_launcher`. Goes: never.
- **A caller's `set -eu`** (LIMITATION): `sh -eu pyt` and niubash with errexit stopped the
  launcher at the first failing test. Fix: `${v:-}` and `|| :`/`|| _pt_x=` everywhere. Test:
  `test_launcher_sh.py::test_launcher_survives_caller_errexit`. Goes: never.
- **`command -v` prints aliases and functions** (LIMITATION): Fix: `pyt` accepts only an
  answer that is a path (4.1). Test: `test_launcher_sh.py::test_uv_search_order`. Goes: never.
- **`$PWD` is logical** (LIMITATION): below a symlinked folder its `..` is not where the kernel
  found `../pyt`. Fix: `pyt` keeps the relative path unless the folded one holds the
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
- **CreateProcess tries only `<name>.exe` for a bare program name** (LIMITATION, PATHEXT is a
  shell feature): a `uv = false` task running `npm`, `yarn` or `mvn` (`.cmd` files) failed with
  "program not found" on Windows only. Fix: `tasks.run_task` looks the name up on the task's
  PATH with PATHEXT (`tasks._on_windows_path`, not shutil.which, which on Windows searches the
  current folder first: always on Python 3.11); the `.cmd` it finds runs
  through cmd.exe (the entry "cmd re-parses `%*`" below) (6.1). Test:
  `test_cli_core.py::test_a_bare_program_is_found_with_pathext_on_windows`. Goes: never.
- **A command line holds 32767 characters** (LIMITATION): Fix: `hooks.ARG_LIMIT` batches file
  arguments (5.6). Test: `test_hooks.py::test_batches`. Goes: never.
- **A file stays locked after its process ends** (LIMITATION): a just-exited exe, an antivirus
  scan or a DLL still loaded. Fix: `e2e.rmtree` and `e2e._move` (the portable smoke moves the
  folder away and back) retry, `e2e.run_logged` leaves a stdout file a killed tree still holds
  to the base's cleanup; the pyz bootstrap moves an incomplete cache aside (`_discard`) (10,
  13.1). Test: `test_build_methods.py::test_pyz_repairs_an_incomplete_cache`,
  `test_e2e_run.py::test_move_retries_a_locked_folder`,
  `test_e2e_run.py::test_run_logged_timeout_kills_the_whole_tree`. Goes: never.
- **A running program's files cannot be deleted** (LIMITATION): rebuilding an output while the
  previous build ran from it raised PermissionError in the middle of deleting it (half of the
  folder gone, a runner traceback; for nuitka after minutes of work). Fix:
  `common.remove_output` moves the old output aside first (whole or not at all: portable's
  folder with its archives, those already moved come back) before the packager runs and turns
  the error into a clear one (exit 1, 10). The same for the mypyc stage an app still runs from,
  whose extensions setuptools deletes before it copies new ones in (a failed build with the
  compiler hint): `mypyc.set_aside` renames them out of the stage first (9). Test:
  `test_build_methods.py::test_an_output_in_use_is_a_clear_error_before_the_packager_runs`,
  `test_a_previous_output_in_use_is_left_whole`,
  `test_a_previous_portable_output_in_use_is_left_whole_with_its_archives`,
  `test_mypyc_core.py::test_build_on_windows_sets_the_extensions_the_app_holds_aside`. Goes: never.
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
- **A venv is specific to its OS** (LIMITATION, WSL on a Windows checkout): Fix: `.venv*-wsl`
  and `.build/wsl` (`project.ENV_SUFFIX`), WSL read from the kernel and the checkout from its
  mount (`project.detect_wsl`: `project.wsl_kernel`, `project.windows_checkout`; 7). Test:
  `test_envs_core.py::test_runtime_and_tool_environments`, `test_clean_envs_on_the_wsl_side`,
  `test_paths.py::test_windows_checkout_follows_the_mount_of_the_root`,
  `test_wsl_kernel_needs_no_wsl_distro_name`,
  `test_is_wsl_at_import_follows_the_kernel_and_the_mount_of_the_root`,
  `test_nvim_render.py::test_lua_wsl_environments_follow_the_runner`. Goes: never.
- **A console keeps the PATH it started with** (LIMITATION): after `uv tool update-shell`, a
  terminal opened before it still does not find the installed `pyt`. Fix:
  `cmd_install.path_state` also reads the PATH stored in the registry
  (`shells.registry_path_dirs`) and then says to open a new terminal (5.9). Test:
  `test_install.py::test_path_state_reads_the_registry_path_on_windows` (the registry faked).
  Goes: never.
- **`wsl -l -q` prints UTF-16** (LIMITATION): Fix: `shells.wsl_distros` decodes it. Test:
  `test_workarounds.py::test_wsl_distros_are_read_as_utf16`. Goes: never.

macOS:
- **`/usr/bin/cc`, `gcc` and `clang` exist without the developer tools** (LIMITATION, xcrun
  shims that open an install dialog): Fix: `cmd_env._xcode_problem` (the active developer
  folder must hold a clang: `cmd_env.XCODE_CLANG`, the Command Line Tools or an Xcode.app
  toolchain), `cmd_nvim.c_compiler`, and after a failed mypyc build `mypyc_build.xcode_problem`
  (its mirror in the build script, so the missing compiler exits 3) (7, 9, 12.2). Test:
  `test_envs_core.py::test_c_compiler_rejects_macos_xcode_shims`,
  `test_cmd_nvim.py::test_c_compiler_skips_the_macos_shims_without_developer_tools`,
  `test_mypyc_core.py::test_build_script_finds_the_macos_compiler_shims_without_developer_tools`,
  `test_build_script_reads_the_developer_folder_like_doctor`. Goes: never.

### 15.2 Our open issues and fragile points

Behaviour:
- Platforms uv has no CPython builds for (Android/Termux, FreeBSD and the other BSDs) run
  `help`, `doctor`, `install` and `uninstall` only (on any Python 3.11+ there): every other
  command, `new` included, stops with exit 3 and why (`envs.no_download_problem`). By design
  (the owner's choice, September 2026): the environments, `uv.lock` and the builds are made with
  the CPython uv manages. Untested on a real Android or BSD: simulated with an empty
  `UV_PYTHON_DOWNLOADS_JSON_URL` list and a system CPython on PATH. A `.venv` made there by
  hand (`python -m venv .venv`) counts as the project's environment: the launchers then ask uv
  for `python.cpython`, `only-managed`, which uv cannot give, and every command, `help`
  included, stops in uv (delete that `.venv`).
- A project with no environment yet (a fresh clone, after `clean --envs`) starts the runner on
  whatever CPython 3.11+ uv's cached environment of it holds, or finds: each project command
  then pays a `uv python find` and, on another Python, one more Python start (the restart on
  `python.cpython`, about 50 ms on Linux) until `setup` makes `.venv`. The first start after
  that makes uv build the runner's cached environment again when another Python built it (a
  newer uv-managed CPython than `python.cpython`, a system one); on Windows a runner still
  running from it (a long `./pyt run` started before `.venv` existed) keeps its files in use,
  and that start fails in uv (access denied) until the earlier command ends. A runner older than
  this restart (a `deploy.py` project, or `pyt.py` from before September 27, 2026) started by a
  newer installed `pyt` in a project without `.venv` runs its project commands on that Python
  (it has no restart); with `.venv` it runs on `python.cpython`, as before.
- `cmd_dev.split_backend` treats a first argument equal to `cpython`, `pypy` or `mypyc` (and
  `all` for `test`/`check`) as the backend: an app argument with that value must be preceded
  by an explicit backend (`./pyt run cpython mypyc`). By design: the usual fix, `--`, is
  eaten by PowerShell.
- Tasks: a task with `backend = "mypyc"` runs interpreted in `.venv` unless it goes through a
  `deps` entry such as `compile` and runs the stage.
- Global mode (5.2) loads the installed template's `pytemplate.toml` for every command that runs
  there (`new` checks names against its lock, as from the template repository): a copy whose
  configuration no longer loads (edited by hand) stops `new` and `doctor` with that error,
  prefixed with the folder. The runner's bytecode cache of each installed version stays in
  `<user cache>/pytemplate/pycache` (a few hundred KB; nothing prunes it). A project that holds
  an `.pytemplate/installed.json` (copied there by hand) runs in global mode: `new` never copies
  the file.
- Exit 3 is a requirement the runner checks itself. One uv reports missing (an interpreter it
  can neither find nor download: `python.pypy` pinned to a PyPy not installed, with
  `UV_PYTHON_DOWNLOADS=never`; a program a `uv = true` task names that `uv run` cannot spawn)
  ends with uv's own code, 2, after uv's message: the runner streams uv's output and 2 is uv's
  code for its other errors too (the same program with `uv = false` is the runner's exit 3).
  README's exit codes say so (`test_docs.test_the_manual_says_which_code_uv_s_missing_requirements_end_with`).
- Ctrl+C waits for the running child (section 5.3): a child that ignores SIGINT keeps the
  runner waiting (Ctrl+\ ends both; closing the terminal passes SIGHUP on), as with `uv run`.
  Windows: a closed stdout pipe is `OSError` EINVAL there, so `./pyt help | more` quitting
  early still prints a traceback (untested).
- `clean --envs` removes every `.venv*` of this side (WSL on /mnt: only the `-wsl` ones),
  including the environments in use; there is no "unused only" option (`mode` and `apply`
  only print a note about leftovers). On Windows close the editor first (its mypy/ruff servers run from
  `.venv`), or clean fails with exit 1.
- raylib + PyPy on arm64 (macOS and Linux): no PyPy wheel for those platforms (raylib 6.0.1.0
  publishes pp311 wheels for x86_64 only) and `no-build-package`, so `./pyt setup` of a
  raylib project fails to sync `.venv-pypy` on Apple Silicon (confirmed on `macos-latest`:
  "marked as `--no-build` but has no binary distribution") and on Linux arm64 (Raspberry Pi,
  Graviton, Docker on a Mac; simulated with uv's `--python-platform`). The generated `ci.yml`
  syncs only the matrix backends (its Linux runner is x86_64) and `selftest --e2e` switches the
  project off PyPy first (`e2e.HOST_GAPS`); users there run `./pyt mode cpython --supports
  cpython,mypyc`. Possible fix: skip PyPy for raylib on arm64 in `apply`/`new`.
- `./pyt lock` re-locks without applying `[preset.*]`: after a `[preset.raylib] package`
  switch it moves `no-build-package` to the new name and keeps the old dependency until
  `./pyt apply` runs (every mismatch hint names apply). A `[preset.flet] version` edit that
  is not applied yet is missed only by `render.auto`, which compares the managed block, where
  flet leaves no trace: `doctor` and the pre-commit hook report it (`cmd_apply.pending`, which
  `hooks.check_lock` asks too).
- Without a trusted `applied` record (lost: a `state.json` merge conflict whose sides disagree
  on it, resolved with `./pyt render`; a deleted file) apply reads the options applied last
  back from the managed block (`cmd_apply._block_options`: raylib's `no-build-package`) and the
  preset from pyproject.toml's traces (5.8). Two cases stay wrong there: a `./pyt lock` between
  a `[preset.raylib] package` edit and the apply writes the block with the new package first, so
  the switch keeps the old package next to the new one (`./pyt remove raylib-sdl`); and a
  raylib project whose raylib requirement was replaced by hand (`./pyt remove/add`, not
  `[preset.raylib]`) shows no trace of its preset, so apply refuses app.preset = "raylib" as a
  guess that says so and names the way out (`./pyt add raylib==...`, or `[preset.raylib]
  package`). Fix idea: keep one side's record when render rewrites a conflicted state.json.
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
- On Windows the walk-up of the launchers follows the path typed (section 4.1): from a folder
  reached through a junction or a directory symlink into a project (`mklink /J C:\g
  C:\code\game\src`) no parent holds it, and the command finds no project there (`pyt.cmd`'s
  `%CD%`, `pyt.ps1`'s location and the MSYS2/Git Bash `pyt` are logical; resolving the reparse
  point of each folder is not done). cd to the folder it points to. Linux and macOS walk up
  from the physical folder too.
- niubash: the generated portable `.sh` launcher leaks `HERE` and its exported variables into
  the calling session (niubash runs sh scripts in-process; its `_pt_*` helpers are unset). Its
  `cd -P`/`pwd -P`/`CDPATH=''` symlink resolution is verified with dash, bash, zsh, ksh, mksh,
  yash and busybox, not with niubash.
- Portable: a runtime layout change in python-build-standalone or PyPy (bin/python3 missing, the
  stdlib renamed) is only caught by `selftest --e2e`; a bundled PyPy portable build is not run
  on CI.
- Argument limits by design: `pyt.cmd` (and every CreateProcess caller of it) cannot pass
  `% ! " ^ & | < >`; a `uv = false` task running a `.cmd`/`.bat` on Windows refuses the
  arguments cmd.exe would change (6.1); PowerShell drops a bare `--`; xonsh `-c` exits 1 on any failing command
  (the child's real code is in its `CalledProcessError`).
- `uv build` drops a `.gitignore` into `dist/<n>-<b>-wheel/`.
- The installed template (`./pyt install`, 5.9) is a copy: it moves only when `./pyt install`
  runs again in the clone (doctor notes, in the clone, when it is older). On Windows niubash runs
  the installed `pyt` in-process, and whether it keeps that file open, so that an install run
  through it cannot replace it, is not verified (the install would then undo itself and say so;
  `./pyt install` in the clone does not go through it). A launcher of the earlier install that
  install cannot remove from the old bin folder (`_remove_earlier`: a file another user owns) is
  only a warning naming it: the new record names the new folder, so uninstall and doctor never
  look in the old one again; delete it by hand. A project made before the launchers were renamed
  (`.pytemplate/deploy.py`) runs its own old runner, which has no `install` or `uninstall`:
  `pyt uninstall` typed there says "unknown command" (the README says to run it elsewhere).
- A Flet app built as a pyz, a wheel or a portable folder with `runtime = "system"` carries no
  Flet client: its first start downloads ~40 MB from GitHub (`FLET_CLIENT_URL`: a mirror), so it
  needs the network once (README troubleshooting). They run on a Python of the user's machine,
  whatever its OS: one client per OS would weigh 40-90 MB each (a CI pyz merges three). exe,
  nuitka and a bundled portable folder carry it; on Linux the build machine's (glibc bracket and
  flavor), so they need that glibc or newer, as the nuitka binary does anyway.
- A project inside a bigger git repository (supported: `new` skips `git init`, the hook finds
  the repository top) still gets its generated CI at `<project>/.github/workflows/ci.yml`, which
  GitHub never runs (it reads `<top>/.github/workflows/` only), and whose steps expect the
  project at the checkout root. `new` warns when it creates one there
  (`cmd_mode._monorepo_note`), and `./pyt doctor` notes it (`[--]`, never a problem: the
  setup is supported) for a project moved or cloned into a repository later (`hooks.doctor`:
  the repository's top is not the project and the generated `ci.yml` exists); render runs no
  git, so the other commands say nothing. A fix would generate a relocatable workflow (a
  `PROJECT` folder for `defaults.run.working-directory` and the artifact paths) for the user to
  place at the top.
- flet presets: `constraints.txt` gives a new project httpx 0.28.1, but flet 1.0.1 only asks
  for `httpx>=0.28.1`, so `./pyt lock --upgrade` takes httpx 1.x once it is final, and its
  1.0 dev releases drop `AsyncClient`, which `flet.auth` (OAuth; not used by the skeleton)
  needs. Not bounded in the preset on purpose (a direct dependency every flet project would
  have to remove when Flet moves on): an app that uses `flet.auth` runs `./pyt add
  "httpx<1"`.
- Vendored native libraries (`.so`/`.pyd` in `src/`, `git add -f`): the stage, the payloads,
  portable and wheel carry them; PyInstaller/Nuitka only bundle what they detect (a library
  loaded with ctypes needs `[deploy.exe] extra_args = ["--add-binary", ...]`), pyz leaves
  extensions out of `common/`, and a cpython/pypy wheel stays tagged `py3-none-any`.
- `sync_tree` does not detect a case-only rename (`Data.py` -> `data.py`) on a
  case-insensitive file system: the stage keeps the old spelling until `./pyt clean`.
- The name check (`presets.check_name_free`) knows the import names of a dependency the user
  added (not pinned by a preset, so not in `presets.IMPORT_NAMES`) only while an environment
  of the project has it installed (`presets._installed_import_names`): in a clone without
  `.venv`, `./pyt rename bs4` after the dependency beautifulsoup4 still passes (its
  distribution name is refused). `./pyt add` syncs `.venv`, so the usual order is covered.
- **[template repo]** The CI image (13.2) freezes the Linux tool versions of its jobs per tag (uv
  0.12.19, xonsh, noble's shells, git 2.43 and Node 18, PowerShell 7.6.6): the newest ones show
  up in the jobs on bare runners (Windows, macOS, uv-floor, the e2e rows, the nvim canary); its
  Ubuntu stays 24.04 when ubuntu-latest moves. Its
  jobs still reach PyPI (the resolution of `./pyt new`, test_presets' `uv pip compile
  --no-cache`) and GitHub (the LazyVim starter and plugins of `selftest --nvim`), and the pull
  of the image counts toward each job's timeout. A fork's pull request that changes one of its
  inputs gets no Linux jobs at all (stage 2: the image cannot be pushed from it, and no bare job
  repeats them): run them after merging it, or from a branch of this repository. Old tags pile
  up in GHCR: nothing prunes them.
- A pyz built on Windows stores no x bit (Windows files have no Unix mode), so the executables of
  its Linux or macOS targets (`--target`), or of a script in `src/`, are not runnable where it is
  extracted; the generated CI builds each OS's part on that OS, and `pyz-merge` keeps the parts'
  modes (an app file is executable when it is in any part).
- **[template repo]** template-flet.yml (13.2) builds and reads the `.apk`, never runs it (an owner
  decision): a failure on a device (a native module that does not load, a crash at start) is not
  caught. iOS apps and the desktop targets of `flet build` are built in no CI job. The web check
  needs the CDNs the app loads from (jsDelivr for Pyodide, gstatic for CanvasKit and the fonts),
  and both builds need Flet's index (pypi.flet.dev), Flutter's and Gradle's repositories: a red
  run with no commit behind it may be theirs. Its caches (Flutter, pub, Flet, Gradle) take about
  5.4 GB of the repository's 10 GB per Flet version (saved on main only; `~/flutter` twice, once
  per job), of which GitHub evicts the least recently used, and any not used for 7 days: the
  weekly run may then build cold (a few minutes more). Its bare
  `ubuntu-latest` runners move: the Android SDK packages and NDK of the runner image, and an
  Ubuntu release the pinned Playwright cannot install its browser's dependencies on (then bump
  `template-flet/browser.txt`). The `.apk` check reads serious_python_android 4.7.1's layout,
  which a Flet release may change (a red check naming what it misses).
- `selftest --mutation` (13.1) measures, within limits: a test file that reaches a module only
  through a `./pyt` subprocess, without importing it, never runs for its mutants (a survivor may
  be one such a test would kill); the mutants of Windows- or macOS-only code survive where their
  tests skip, and are listed as survivors, not as untested; a test that fails by itself under
  the workers' load (a timing) counts as a kill; the workers' tests get another home, so the
  user's uv.toml and git configuration never reach them (uv's cache, Pythons and tools do, by
  their UV_* variables: a test that drops those starts uv on an empty cache in the worker's
  home, which downloads what it needs, and offline fails the module's baseline; the suite's
  own tests keep them); a process a test started in a session of its own stays when a mutant
  made the test leave it and pytest then ended by itself (the tests' own children end within
  two minutes; `kill_run` only runs for a run over its limit or stopped). A pass of the whole
  runner (a day of CPU or more) is local only: CI tests the lines a pull request changes, within
  70 minutes (13.2).

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
  `lazy-lock.json` until `./pyt nvim extras` is run; `nvim extras` does not detect a
  `vim.g.lazyvim_json` override.
- `selftest --nvim` does not pre-install the treesitter python parser or warm basedpyright:
  on a cold cache those smoke checks SKIP and the first run is slow.
- `spec.lua` reads lazy.nvim's internal `require("lazy.core.config").spec.modules`, now guarded
  (`pcall(require, ...)`, `type(...) == "table"`, else no extras): if lazy.nvim ever drops that
  field the integration loses only the extra imports, never errors. It lives in `spec.lua` (not
  in `.lazy.lua`, which is a static loader), and it names the five extras and the plugins'
  repositories, so a later fix there needs no re-trust (`.pytemplate/nvim/**` is trusted with
  `.lazy.lua`); only editing the loader itself does.
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
  `config._build`, `config._decode`, `config._string_end` (with `config._ScanError`, for
  `_toml_strings`), `presets._norm_name` and, lazily, `cmd_env._is_link` and
  `cmd_apply._other_package`; `cmd_apply` calls the
  private `cmd_env._envs_for`, `_env_dirs`, `_fix_exec_bit`, `cmd_mode._precheck_py311`,
  `rename._plan_pyproject`, `render._holds_python` and `render._read_state`, and
  `cmd_mode._precheck_py311` that same `render._holds_python`; `rename` and
  `cmd_env` import `cmd_apply` lazily (it imports both at module level).
- Which path a `compile.modules` entry names is decided once, by `config.import_path`
  (`config.compiled_paths`): `mypyc.compiled_sources`, the pyright strict list,
  `lintc.relative_file_at_import` and `cmd_apply.reference_problems` all read it (a copy of the
  old folder-wins rule in lintc once turned the `__file__` rule off next to a leftover folder).
- `upx.BUILTIN_EXCLUDE` must keep `flutter_windows.dll`; `nuitka._flet_client_archive` mirrors
  flet_desktop's download URL, its `FLET_CLIENT_URL` override and its `flet_desktop/app/`
  lookup, and `nuitka.archive_problem` the way `ensure_client_cached` extracts the archive.
- `config._check_default_methods` imports `cmd_build.COMPAT` lazily (`cmd_build` imports
  `config`); `config._check_preset_tables` reads `preset.toml` `[options]` itself, like
  `config._presets` mirrors `presets.available`; `render.managed_block` needs `pypy_minor`
  (PyPy's environment) and `min_python` (requires-python) to stay distinct.
- `cmd_mode._config_from_text` and `e2e.preset_info` call the private `config._build`, and
  `e2e.rmtree` lazily `cmd_env._is_link`;
  `cmd_mode._work_tree_top` calls the private `presets._git_env` (the same git environment as
  `presets._git_init`, whose "inside a work tree" rule it mirrors for `new`, an ignoring one
  excepted by the same `presets.ignored_by_work_tree`);
  `nvimtest.selftest` imports `e2e.termination_as_interrupt`, `isolate_git` and
  `hidden_template_repository` lazily (one rule for both harnesses), and `shells.selftest`
  `termination_as_interrupt`; `e2e.selftest` and `nvimtest._run` hold `project.base_lock` around
  the run (`mutation.base_lock` is its own copy, specialised to `--mutation`'s message: the lock
  protocol must stay the same);
  `e2e.flet_build_reason` imports `methods.flet._developer_mode`, and its Flutter size and the
  flet method's docstring follow the manual (`test_docs.test_the_runner_gives_the_manuals_flutter_size`);
  `upx.uses` imports `methods.flet.MOBILE_WEB` lazily (`methods.flet` imports `upx`);
  `cmd_nvim.c_compiler` imports `cmd_env._msvc` and `_xcode_problem` lazily (`cmd_env` imports
  `cmd_nvim`); `presets._record` imports `cmd_apply` lazily (`cmd_apply` imports `presets`);
  `cmd_install._is_link` imports `cmd_env._is_link` lazily (`cmd_env` imports `cmd_install`), and
  `cmd_mode.cmd_new` the private `cli._prog` (the `pyt`/`./pyt` rule of `help`).
- `cmd_apply._block_options` reads the preset.toml `[uv]` templates back from the values
  `render.managed_block` wrote with `presets.uv_extras` (`str.format_map`, reversed by
  `cmd_apply._unformat`): a template with a format spec or conversion is never read back.
- `RULES_RE` / `tasks.parse_line` <-> `ui.error`, `ui.warn`, `str(lintc.Finding)` (5.3).
- `common.ABI_RE`/`abi_tag` <-> the pyz bootstrap's own copies (`templates/pyz/__main__.py` is
  standalone; `test_abi_tags_agree_with_the_bootstrap`); `common.this_libc` <-> its `_libc`
  (`test_the_build_machine_and_the_bootstrap_name_the_c_library_alike`), and the `floor` strings
  `common.platform_floor` writes <-> what its `_meets` reads.
- mypyc internals mirrored by the runner (checked by `test_mypyc_core` against the locked
  mypy): `lintc.NATIVE_CLASS_DECORATORS` <-> mypyc's native decorators;
  `lintc.relative_file_at_import` <-> when mypyc builds no shared lib; `mypyc.remove_stale_extensions`
  <-> mypyc's lib names (`<group>__mypyc`, `<module>__mypyc`). `mypyc.MYPYC_REJECTED`,
  `COMPILER_MISSING` and `C_BUILD_FAILED` <-> `tools/mypyc_build.py`; `mypyc.missing_compiler`
  imports that script in `.venv` and calls its `missing_compiler`
  (`test_missing_compiler_asks_the_venv_as_the_build_script_does`); the spec keys the script
  reads <-> `mypyc.build`; `mypyc_build.XCODE_CLANG`/`xcode_problem` <-> `cmd_env.XCODE_CLANG`/
  `_xcode_problem` (`test_build_script_reads_the_developer_folder_like_doctor`);
  `mypyc_build.extra_cflags`/`compiler_type` <-> the wheel's `SETUP_PY`
  (`test_wheel_setup_py_adds_the_same_flags_as_the_stage`); `mypyc.COMPILER_ENV` <-> the
  variables setuptools' `configure_system` reads.
- `envs.MIN_UV` <-> the presets' `python.pypy` pin and default `python.cpython`, and the newest
  uv flag the runner uses (7); `hooks.launcher_of` <-> `hooks.sh_literal`; `cmd_env._msvc`
  <-> setuptools' `_find_vc2017` component choice (`test_msvc_component_matches_setuptools`)
  and `mypyc.has_compiler_hint(platform)` (the winget `--add` component).
- `editor.json` <-> `cli.COMMANDS` (6.2); `cmd_nvim.EXTRAS` <-> the extras list in
  `.pytemplate/nvim/spec.lua` (`test_lazy_lua_extras_match_cmd_nvim`); `vscode.MYPYC_STAGE` /
  `editor.json` `mypyc_stage` / `vscode._STAGE` (the pytest stage matcher) <->
  `mypyc.profile(cfg, ...).stage` (`test_editor_json_stage_matches_the_runner`); the CI pyz path
  <-> `BuildRequest.out_name` and the merged upload's `.cmd` <-> `pyz.wrapper_path` (10;
  `test_ci_workflow_for_every_preset_and_backend_set`).
- **[template repo]** `template-ci-image/pins.py` reads pins from the code by text:
  `.python-version`, the `MIN_LAZYVIM = (...)` line of `cmd_nvim.py`, `BASEDPYRIGHT = "..."` and
  `BASEDPYRIGHT_NODE = "..."` of `cmd_dev.py`, `VERSION = "..."` of `upx.py`, the one `"taplo==X"` of `test_render_core.py` and
  the `# requires-python` line of `.pytemplate/pyt.py` (`test_ci_image_pins_come_from_the_code`);
  the nvim matrix of template-ci-image.yml (`test_ci_image_workflow_builds_publishes_and_runs_the_linux_jobs`)
  and the xonsh, Neovim and actionlint literals of the other template workflows
  (`test_workflow_literals_follow_the_ci_image_pins`) repeat its pins.
- template-selftest.yml reads `envs.MIN_UV` (`MIN_UV = "..."`) from the code by text and
  deselects `test_init_round_trip_through_every_preset_is_byte_identical` by name in the uv-floor
  job (its id `.pytemplate/tests/test_presets.py::...`, which `cli.cmd_selftest`'s `--rootdir=.`
  keeps); the python-floor job of template-ci-image.yml and the image's `warm` take the pytest and
  hypothesis pins of `uv.lock` (`uv export`). `test_workflows.py` checks the workflow texts they
  rely on.
- `tools/mutation_cr.py` <-> Cosmic Ray 8.7.0's own code, not only its public API
  (`cosmic_ray.ast.ast_nodes`/`get_ast`, `ASTQuery.get_definition_name`, `plugins.get_operator`,
  an operator's `mutation_positions`, `mutating.mutate_code` and the pre-order count of its
  MutationVisitor, which OCCURRENCE must match), and `mutation.OPERATORS` <-> its operator names:
  a new release must pass `test_mutation.test_every_operator_is_one_of_cosmic_rays_and_makes_its_mutant`,
  the pins of `test_cosmic_rays_defects_that_the_runner_works_around` and the real run;
  `mutation.select` <-> where Cosmic Ray's ExceptionReplacer lists its mutants (inside the class
  `handler_classes` gives); `mutation.Driver`'s uv flags <-> the CI image's `warm` (the same
  interpreter and lock: the wheels the job needs are in the image's cache);
  `mutation.pytest_counts` <-> pytest's summary line; `mutation.junit_seconds` <-> the classname
  pytest's JUnit XML gives a test (its file's path from the rootdir, dotted: `Runs.run` passes
  `--rootdir=.`, or the suite's pytest.ini would make `.pytemplate/tests` the rootdir);
  `mutation.parse_diff` calls the
  private `presets._git_path` (git's C quotes).
- `editor.json` `typing.basedpyright` <-> `cmd_dev.BASEDPYRIGHT` and `typing.basedpyright_node`
  <-> `cmd_dev.BASEDPYRIGHT_NODE` (bumping a pin changes a generated file: re-render); the Lua
  whitelists `BACKENDS`, `PROFILES`, `EDITORS`,
  `SEVERITIES` in `nvim/lua/pytemplate/init.lua` <-> the runner's
  (`test_lua_whitelists_match_the_runner`); `nvimtest.LOCK` <-> `cmd_nvim.STARTER_REV` (refresh
  both from one green run, 13.1); `.lazy.lua`'s bytes <-> `test_nvim_render.LAZY_LUA_SHA256`;
  `tasks.META.apply` <-> `tasks.META.setup` (one operation, two names).
- **[template repo]** `template-flet/check.py` <-> the flet skeleton's `ui/app.py` (`SKELETON`,
  `READY`, `DONE`, `BUTTON`: `test_workflows.test_flet_check_follows_the_skeleton`),
  `cmd_build.dist_path` and flet's `-<target>` (`output_dir`:
  `test_flet_check_names_the_runners_output_folders`), `config.update_file` and `ui.PytError`
  (`set_target`), python.js of Flet's web template (`PYTHON_STARTED`, `PYTHON_FAILED`), Flutter's
  semantics DOM (`flt-semantics-*`) and serious_python_android 4.7.1's APK layout (`ABIS`,
  `ABI_LIBS`, `APP_ZIPS`, the `.soref` markers); template-flet.yml's `pt-flet` paths <->
  `presets.name_from_folder` and `BuildRequest.out_name`; `methods.flet.DESKTOP_CLIENT` <-> the
  flet preset's `flet-desktop` requirement.
