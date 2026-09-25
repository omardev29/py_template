-- :checkhealth pytemplate
local M = {}
local uv = vim.uv or vim.loop

local function has(mod)
  return (pcall(require, mod))
end

local function lazy_plugin(name)
  local ok, cfg = pcall(require, "lazy.core.config")
  return ok and cfg.plugins[name] ~= nil
end

function M.check()
  local h = vim.health
  local pt = require("pytemplate")

  h.start("pytemplate: project")
  local root = pt.root()
  if not root then
    h.warn("Neovim's cwd is not inside a pytemplate project (no pytemplate.toml upward)", {
      "Start Neovim from the project folder: .lazy.lua is only loaded from the cwd upward",
    })
    return
  end
  h.ok("root: " .. pt.native(root))
  if not pt.same_path(uv.cwd(), root) then
    h.info("cwd is a subdirectory of the root: launch.json is read through the pytemplate provider")
  end
  if vim.fn.has("nvim-0.11.2") == 0 then
    h.warn("Neovim " .. tostring(vim.version()) .. ": LazyVim needs 0.11.2 or newer")
  end
  local info = pt.info()
  if info.schema ~= 1 then
    h.warn("cannot read .pytemplate/editor.json", { "Run ./deploy render (any ./deploy command does it)" })
  else
    h.ok(
      ("preset %s, backend %s (supported: %s), typing %s, editor %s"):format(
        info.preset,
        info.backend.active,
        table.concat(info.backend.supported, ", "),
        info.typing.profile,
        info.typing.editor
      )
    )
  end

  h.start("pytemplate: runner")
  local uvbin = pt.uv()
  if uvbin then
    h.ok("uv: " .. uvbin)
  else
    h.warn("uv not found: tasks fall back to the launcher, which prints how to install it", {
      pt.is_win and "winget install --id=astral-sh.uv -e   (or: scoop install main/uv)" or "curl -LsSf https://astral.sh/uv/install.sh | sh",
    })
  end
  h.ok("./deploy runs as: " .. table.concat(pt.deploy_cmd({ "ARGS" }), " "))
  h.info("'shell' is not used (" .. vim.o.shell .. "): ./deploy always runs as an argv list")

  h.start("pytemplate: environments")
  local py = pt.python("cpython")
  if py then
    h.ok("CPython env: " .. py)
  else
    h.warn("no " .. info.envs.cpython .. " yet", { "Run ./deploy setup (or ./deploy sync cpython), then restart Neovim" })
  end
  if info.backend.active == "pypy" or vim.tbl_contains(info.backend.supported, "pypy") then
    local pypy = pt.python("pypy")
    if pypy then
      h.ok("PyPy env: " .. pypy)
    else
      h.info("no " .. info.envs.pypy .. " yet (./deploy sync pypy)")
    end
  end
  for _, tool in ipairs({ "ruff", "mypy" }) do
    local exe = pt.tool(tool)
    if exe then
      h.ok(tool .. ": " .. exe)
    else
      h.warn(tool .. " not in " .. info.envs.tools, { "Run ./deploy setup" })
    end
  end
  local mypy_on = info.typing.mypy and info.typing.profile ~= "off"
  h.info("mypy diagnostics: " .. (mypy_on and ("on (profile " .. info.typing.profile .. ", errors as " .. info.typing.mypy_severity.error .. ")") or "off (typing profile off)"))

  h.start("pytemplate: language server")
  local lsp = pt.lsp_name()
  local cmd, source = require("pytemplate.integrations").lsp_cmd(lsp)
  if cmd then
    h.ok(lsp .. " from " .. source .. ": " .. table.concat(cmd, " "))
  elseif lsp == "pyright" then
    h.info("pyright from Mason (it needs Node.js); basedpyright needs none: remove vim.g.pytemplate_python_lsp")
  else
    h.info(lsp .. " from Mason")
  end
  if info.typing.editor == "pylance" then
    h.info("typing.editor = pylance is VS Code only; Neovim uses " .. lsp .. " with the same pyrightconfig.json")
  end

  h.start("pytemplate: debugger")
  local acmd, asource = require("pytemplate.dap").adapter_cmd()
  if acmd then
    h.ok("debugpy adapter (" .. asource .. "): " .. table.concat(acmd, " "))
  else
    h.warn("no debugpy: run ./deploy setup (debugpy is in the dev group) or :MasonInstall debugpy")
  end

  h.start("pytemplate: plugins")
  local plugins = {
    { "overseer.nvim", "overseer", "tasks (LazyVim extra editor.overseer)" },
    { "nvim-dap", "dap", "debugging (extra dap.core)" },
    { "nvim-dap-python", "dap-python", "Python debugging (extra lang.python)" },
    { "neotest", "neotest", "tests (extra test.core)" },
    { "neotest-python", "neotest-python", "pytest adapter (extra lang.python)" },
    { "nvim-lint", "lint", "mypy diagnostics" },
    { "nvim-lspconfig", "lspconfig", "language servers" },
  }
  for _, p in ipairs(plugins) do
    if lazy_plugin(p[1]) or has(p[2]) then
      h.ok(p[1] .. ": " .. p[3])
    else
      h.info(p[1] .. " is not installed: no " .. p[3])
    end
  end
end

return M
