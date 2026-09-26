-- opts functions for the LazyVim plugins the project configures (called from .lazy.lua). They run
-- after LazyVim's own and the user's specs (the local spec is last), so they get the last word.
local pt = require("pytemplate")
local M = {}

local function tbl(x)
  return type(x) == "table" and x or {}
end

local function server(opts, name)
  opts.servers = tbl(opts.servers)
  if type(opts.servers[name]) ~= "table" then
    opts.servers[name] = {}
  end
  return opts.servers[name]
end

-- --- which-key ---------------------------------------------------------------------------------

function M.which_key(_, opts)
  opts.spec = tbl(opts.spec)
  local prefix = type(vim.g.pytemplate_prefix) == "string" and vim.g.pytemplate_prefix or "<leader>j"
  table.insert(opts.spec, { prefix, group = "deploy", mode = "n" })
end

-- --- overseer ----------------------------------------------------------------------------------

---Our template provider (lua/overseer/template/pytemplate.lua) replaces the tasks.json one: same
---tasks without duplicate labels, typed parameters, the [tasks] entries, and no deploy.cmd.
function M.overseer(_, opts)
  opts.disable_template_modules = tbl(opts.disable_template_modules)
  if not vim.tbl_contains(opts.disable_template_modules, "overseer.template.vscode") then
    table.insert(opts.disable_template_modules, "overseer.template.vscode")
  end
end

-- --- language servers --------------------------------------------------------------------------

---argv of the Python language server and where it comes from; nil = LazyVim's default (Mason).
function M.lsp_cmd(name)
  local exe = pt.tool(name .. "-langserver")
  if exe then
    return { exe, "--stdio" }, ".venv"
  end
  local uvbin = pt.uv()
  if name == "basedpyright" and uvbin then
    -- uvx: a cached, isolated basedpyright (bundles its own Node.js); never touches the project.
    -- The versions ./deploy check pins (editor.json), basedpyright's Node.js wheel included:
    -- an unpinned request re-resolves to the newest (a new Node can raise the glibc/macOS floor).
    local typing = pt.info().typing
    local argv = { uvbin, "tool", "run", "--from", typing.basedpyright or "basedpyright" }
    if typing.basedpyright_node then
      vim.list_extend(argv, { "--with", typing.basedpyright_node })
    end
    return vim.list_extend(argv, { "basedpyright-langserver", "--stdio" }), "uvx"
  end
  return nil, "mason"
end

function M.lsp(_, opts)
  local want = pt.lsp_name()
  local other = want == "pyright" and "basedpyright" or "pyright"
  local s = server(opts, want)
  s.enabled = true
  server(opts, other).enabled = false
  local cmd = M.lsp_cmd(want)
  if cmd then
    s.cmd, s.mason = cmd, false
  end
  -- ruff from .venv: the version pinned in uv.lock, the same one ./deploy check runs
  local ruff = server(opts, "ruff")
  ruff.enabled = true
  local exe = pt.tool("ruff")
  if exe then
    ruff.cmd, ruff.mason = { exe, "server" }, false
  end
  server(opts, "ruff_lsp").enabled = false
end

-- --- mypy (nvim-lint) ---------------------------------------------------------------------------

local S = vim.diagnostic.severity
local SEVERITY = { Error = S.ERROR, Warning = S.WARN, Information = S.INFO, Hint = S.HINT }

---mypy arguments, mirroring ./deploy check (render.mypy_cli_args) with nvim-lint's output format.
function M.mypy_args()
  local info = pt.info()
  local args = {
    "--show-column-numbers",
    "--show-error-end",
    "--hide-error-context",
    "--no-color-output",
    "--no-error-summary",
    "--no-pretty",
  }
  local python = pt.venv_exe(info.envs.tools, "python")
  if info.pypy_enabled and info.typing.python_version and python then
    vim.list_extend(args, { "--python-version", info.typing.python_version, "--python-executable", python })
  end
  return args
end

---The environment of the mypy linter: nvim-lint REPLACES the environment when a linter has `env`.
local function mypy_env()
  local env = vim.fn.environ()
  env.PYTHONUTF8 = "1" -- like the runner (proc.base_env)
  env.VIRTUAL_ENV = nil
  -- the path even before .venv exists: the linter is built once, ./deploy setup may come later
  local mypy = pt.venv_exe(pt.info().envs.tools, "mypy")
  if pt.is_win and mypy then
    -- nvim-lint runs `cmd.exe /C <cmd> ...` on Windows: a quoted absolute path breaks cmd's
    -- quote rules (spaces, & ^ %), so run the bare name with .venv\Scripts first on PATH.
    env.PATH = vim.fs.dirname(mypy):gsub("/", "\\") .. ";" .. (env.PATH or env.Path or "")
    env.Path = nil
  end
  return env
end

function M.mypy_enabled(filename)
  local info = pt.info()
  return info.typing.mypy and info.typing.profile ~= "off" and pt.tool("mypy") ~= nil and pt.in_root(filename)
end

function M.mypy_linter()
  local base = require("lint.linters.mypy")
  local info = pt.info()
  local on_error = SEVERITY[info.typing.mypy_severity.error] or S.ERROR
  local on_note = SEVERITY[info.typing.mypy_severity.note] or S.INFO
  return {
    -- resolved at every run, like `condition`: .venv may appear after the linter was built
    cmd = function()
      return pt.is_win and "mypy" or (pt.tool("mypy") or "mypy")
    end,
    args = M.mypy_args(),
    stdin = false,
    append_fname = true,
    stream = "both",
    ignore_exitcode = true,
    cwd = pt.root(), -- finds .mypy.ini and prints paths relative to it (the parser needs that)
    env = mypy_env(),
    condition = function(ctx)
      return M.mypy_enabled(ctx.filename)
    end,
    parser = function(output, bufnr, cwd)
      local out = base.parser(output, bufnr, cwd)
      for _, d in ipairs(out) do
        if d.severity == S.ERROR then
          d.severity = on_error
        elseif d.severity == S.HINT then -- mypy notes
          d.severity = on_note
        end
      end
      return out
    end,
  }
end

function M.lint(_, opts)
  opts.linters_by_ft = tbl(opts.linters_by_ft)
  local py = vim.deepcopy(tbl(opts.linters_by_ft.python))
  if not vim.tbl_contains(py, "mypy") then
    py[#py + 1] = "mypy"
  end
  opts.linters_by_ft.python = py
  opts.linters = tbl(opts.linters)
  -- A complete definition: LazyVim deep-merges it over nvim-lint's mypy (lists are replaced).
  opts.linters.mypy = M.mypy_linter()
end

-- --- neotest ------------------------------------------------------------------------------------

local SKIP_DIRS = { dist = true, build = true, typings = true, __pycache__ = true, node_modules = true }

function M.neotest(_, opts)
  opts.adapters = tbl(opts.adapters)
  if vim.islist(opts.adapters) and #opts.adapters > 0 then
    return -- a list of adapter objects: not ours to rewrite
  end
  opts.adapters["neotest-python"] = vim.tbl_deep_extend("force", tbl(opts.adapters["neotest-python"]), {
    runner = "pytest",
    dap = { justMyCode = false },
    -- explicit: with .venv and .venv-pypy, neotest-python's pyvenv.cfg glob builds a broken path,
    -- and its `uv run` fallback would sync the environment
    python = function()
      return { pt.test_python() or "python" }
    end,
  })
  opts.discovery = tbl(opts.discovery)
  local prev = opts.discovery.filter_dir
  opts.discovery.filter_dir = function(name, rel, root)
    if name:sub(1, 1) == "." or SKIP_DIRS[name] then
      return false
    end
    return prev == nil or prev(name, rel, root)
  end
end

-- --- venv-selector ------------------------------------------------------------------------------

---pyright/basedpyright find .venv through pyrightconfig.json (venvPath/venv): no automatic switch.
function M.venv_selector(_, opts)
  opts.options = tbl(opts.options)
  opts.options.cached_venv_automatic_activation = false
end

return M
