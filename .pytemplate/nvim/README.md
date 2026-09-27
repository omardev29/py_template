# pytemplate.nvim (LazyVim integration)

The project-local plugin that `.lazy.lua` loads when Neovim starts inside a pytemplate project.
It needs no Python tooling setup in Neovim: every tool comes from the project's own environments.

## First time

Requires Neovim 0.11.2 or newer with LazyVim (`./pyt nvim doctor` checks both;
`./pyt nvim bootstrap` installs the LazyVim starter when you have no Neovim config yet), plus
git, curl, tar, `fd` (the venv-selector of LazyVim's `lang.python` extra raises an error without
it) and a C compiler (nvim-treesitter builds its parsers): `nvim doctor` names what is missing
and how to install it.

1. `./pyt setup` (creates `.venv` with ruff, mypy and debugpy, pinned by `uv.lock`).
2. Trust `.lazy.lua` once per clone: `./pyt nvim trust`, or open Neovim in the project, pick
   (v)iew and run `:trust`, then restart Neovim. The file never changes with the mode, so the
   trust survives `./pyt mode ...`.
3. `./pyt nvim sync` installs the plugins the project adds (`Lazy! install`: your other
   plugins are neither updated nor removed; it needs the trust of step 2). Starting Neovim in
   the project does the same.
4. Start Neovim from the project folder (lazy.nvim only reads `.lazy.lua` from the cwd upward).

`.lazy.lua` imports the LazyVim extras `lang.python`, `lang.toml`, `dap.core`, `test.core` and
`editor.overseer`. `./pyt nvim extras` enables them in your own `lazyvim.json`, which avoids
LazyVim's import-order warning and keeps their plugins installed when you work elsewhere.

## What you get

| Area | Behaviour |
|---|---|
| Language server | basedpyright (no Node.js): `.venv`, else `uvx` with the versions `./pyt check` pins, its Node.js wheel included (`typing.basedpyright` and `typing.basedpyright_node` in `.pytemplate/editor.json`), else Mason. It reads the generated `pyrightconfig.json` (typing profile, `.venv`, `typings/` stubs). `vim.g.pytemplate_python_lsp = "pyright"` switches to pyright (Mason, needs Node.js). |
| ruff | The language server from `.venv`: the same version as `./pyt check`. |
| mypy | nvim-lint runs `.venv`'s mypy from the project root with `.mypy.ini`; off with the `off` typing profile, the default on cpython and pypy (`typing.relaxed = "off"`): `./pyt mode --typing warn` (or `strict`) turns it on; errors shown with the profile's severity (warn: warnings). With PyPy supported it checks the 3.11 syntax like `./pyt check`. |
| Tasks | overseer templates `pyt: <command>` for every `./pyt` command and every `pytemplate.toml` `[tasks]` entry (they replace the `tasks.json` ones). Output of check/lint/test/build becomes diagnostics and quickfix items. |
| Debugging | nvim-dap with the generated `.vscode/launch.json`; the adapter is `.venv`'s debugpy (else Mason's, else an ephemeral `uv run --with debugpy`). |
| Tests | neotest runs pytest with the active backend's interpreter (`.venv`, `.venv-pypy`); mypyc and "all backends" runs go through `pyt: test`. |
| Health | `:checkhealth pytemplate` |

`./pyt` always runs as an argument list,
`uv run --quiet --python=REQUEST --python-preference managed --script .pytemplate/pyt.py ARGS`,
where REQUEST is empty (uv then follows `.python-version`, `python.cpython`) once the project
has its `.venv`, else `>=3.11`, as the launchers pick it: Neovim's `'shell'` is never used, so
xonsh, niubash or PowerShell as `'shell'` do not matter. Like the launchers, the plugin keeps
your `UV_PYTHON`, `UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON`, `PYTHONHOME`, `PYTHONPATH`,
`UV_WORKING_DIR` and `PYTEMPLATE_GLOBAL` away from it (the runner starts where the launchers start it, in Neovim's
folder, as the project's runner: never in the global mode of the installed `pyt`).
Only when uv is nowhere does it run the launcher (`/bin/sh pyt`, or `pyt.cmd` on
Windows), which prints how to install uv.

## Keymaps (`<leader>j`, which-key group "pyt")

| Key | Action | Key | Action |
|---|---|---|---|
| `j` | pick a task (`:OverseerRun`) | `m` | switch the active backend |
| `r` / `R` | run / run on a backend with args | `k` | `[tasks]` picker |
| `t` / `T` | test / test all backends | `d` | `dev` task (flet hot reload) |
| `c` / `C` | check / check all backends | `p` | mypyc report |
| `b` / `B` | build / build on a backend | `s` / `S` | sync all / setup |
| `l` / `f` | lint --fix / format | `D` | doctor |
| `w` | task list | `x` | stop running pyt tasks |

`:Pyt ARGS` runs any command or task, with completion; quotes group words
(`:Pyt run cpython "a b"` passes `a b` as one argument). Saving `pytemplate.toml` runs
`./pyt render`, and commands that change the mode (or `rename`) refresh the editor state.

## Options (set them in `lua/config/options.lua`)

```lua
vim.g.pytemplate_python_lsp = "pyright"   -- default "basedpyright"
vim.g.pytemplate_prefix = "<leader>j"     -- keymap prefix
vim.g.pytemplate_render_on_save = false   -- default true
```

## Files

- `lua/pytemplate/init.lua`: project root, `.pytemplate/editor.json` (validated data), environments, uv lookup, the runner argv.
- `lua/pytemplate/tasks.lua`: task definitions, output parser, pickers, keymaps, `:Pyt`.
- `lua/pytemplate/integrations.lua`: opts for nvim-lspconfig, nvim-lint, neotest, overseer, which-key, venv-selector.
- `lua/pytemplate/dap.lua`: nvim-dap-python setup and the launch.json provider for subdirectories.
- `lua/pytemplate/health.lua`: `:checkhealth pytemplate`.
- `lua/overseer/template/pytemplate.lua`, `lua/overseer/component/pytemplate/refresh.lua`: overseer provider and component.
- `tests/smoke.lua`: headless smoke test in a real LazyVim (`./pyt selftest --nvim`).
- `tests/lazy-lock.json`: the plugin commits `./pyt selftest --nvim` pins (with the LazyVim
  starter commit `cmd_nvim.STARTER_REV`); your own Neovim keeps its own `lazy-lock.json`.
