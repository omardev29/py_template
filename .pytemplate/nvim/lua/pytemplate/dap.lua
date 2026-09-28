-- Debugging: nvim-dap + nvim-dap-python with the configurations of the generated
-- .vscode/launch.json (nvim-dap reads <cwd>/.vscode/launch.json by itself).
local pt = require("pytemplate")
local M = {}
local uv = vim.uv or vim.loop

local function has_debugpy(python)
  if not python then
    return false
  end
  local venv = vim.fs.dirname(vim.fs.dirname(pt.normalize(python)))
  -- lib/python3.X listed, never globbed: a project path with [ ], { }, $ or backquotes
  local libs = pt.is_win and { venv .. "/Lib" } or pt.subdirs(venv .. "/lib", "python3")
  for _, lib in ipairs(libs) do
    if uv.fs_stat(lib .. "/site-packages/debugpy/__init__.py") then
      return true
    end
  end
  return false
end

---Interpreter that runs debugpy.adapter and where it comes from. nvim-dap spawns adapters with
---a raw uv.spawn, which on Windows only finds .exe/.com: always an absolute python.exe (Mason's
---"debugpy-adapter" shim is a .cmd there).
function M.adapter()
  local py = pt.python("cpython")
  if has_debugpy(py) then
    return py, ".venv" -- debugpy is in the dev group: pinned by uv.lock
  end
  local tools = pt.tool("python")
  if tools ~= py and has_debugpy(tools) then
    return tools, ".venv"
  end
  local mason = vim.fn.stdpath("data") .. "/mason/packages/debugpy/venv/" .. (pt.is_win and "Scripts/python.exe" or "bin/python")
  if uv.fs_stat(mason) then
    return pt.native(mason), "mason"
  end
  if pt.uv() then
    return nil, "uv" -- ephemeral: uv run --no-project --with debugpy (never syncs the project)
  end
  return nil, "none"
end

---The argv an adapter would run (used by :checkhealth and the smoke test).
function M.adapter_cmd()
  local py, source = M.adapter()
  if py then
    return { py, "-m", "debugpy.adapter" }, source
  end
  if source == "uv" then
    return { pt.uv(), "run", "--no-project", "--quiet", "--with", "debugpy", "python", "-m", "debugpy.adapter" }, source
  end
  return nil, source
end

function M.setup()
  local dp = require("dap-python")
  local py, source = M.adapter()
  dp.setup(py or "python")
  dp.test_runner = "pytest"
  -- The program runs on the CPython runtime env (.venv) unless the
  -- configuration names its own python (launch.json's PyPy one). $VIRTUAL_ENV still wins.
  dp.resolve_python = function()
    return pt.python("cpython")
  end
  local dap = require("dap")
  local orig = dap.adapters.python
  dap.adapters.python = function(cb, config)
    orig(function(adapter)
      if adapter.type == "executable" then
        if not py and source == "uv" then
          local cmd = M.adapter_cmd()
          adapter.command = cmd[1]
          adapter.args = vim.list_slice(cmd, 2)
        end
        -- a cold `python -m debugpy.adapter` (or uv resolving debugpy) can take more than
        -- nvim-dap's default 4 s to answer `initialize`, mostly on Windows
        local options = adapter.options or {}
        if options.initialize_timeout_sec == nil then
          options.initialize_timeout_sec = 30
        end
        -- the adapter runs a Python (`-m debugpy.adapter`): a caller's PYTHONHOME kills it before
        -- it answers, a PYTHONPATH can shadow a stdlib module, as for every tool the runner starts
        -- (proc.base_env). nvim-dap merges options.env over the environment; "" is unset for CPython.
        options.env = vim.tbl_extend("force", options.env or {}, { PYTHONHOME = "", PYTHONPATH = "" })
        adapter.options = options
      end
      cb(adapter)
    end, config)
  end
  dap.adapters.debugpy = dap.adapters.python
end

---Expand ${workspaceFolder} to the root: nvim-dap uses Neovim's cwd for it.
local function expand(v, root)
  if type(v) == "string" then
    return (v:gsub("%${workspaceFolder}", function()
      return root
    end))
  end
  if type(v) == "table" then
    local r = {}
    for k, x in pairs(v) do
      r[k] = expand(x, root)
    end
    return r
  end
  return v
end

---launch.json configurations when Neovim's cwd is a subdirectory of the project (at the root,
---nvim-dap's own "dap.launch.json" provider already reads them).
function M.launch_configs()
  local root = pt.root()
  if not root or pt.same_path(uv.cwd(), root) then
    return {}
  end
  local launch = root .. "/.vscode/launch.json"
  -- getconfigs reads the file itself and chokes on a UTF-8 BOM (an editor, or PS 5.1, added to
  -- the generated file, which render then leaves - it hashes without the BOM): give it a BOM-free
  -- copy when there is one. (At the root, nvim-dap's own provider reads launch.json and still
  -- chokes on a BOM; that path is out of the plugin's reach.)
  local fd = io.open(launch, "rb")
  if fd then
    local raw = fd:read("*a") or ""
    fd:close()
    if raw:sub(1, 3) == "\239\187\191" then
      local tmp = vim.fn.tempname()
      local out = io.open(tmp, "wb")
      if out then
        out:write(raw:sub(4))
        out:close()
        launch = tmp
      end
    end
  end
  local ok, cfgs = pcall(require("dap.ext.vscode").getconfigs, launch)
  if not ok or type(cfgs) ~= "table" then
    return {}
  end
  return vim.tbl_map(function(c)
    return expand(c, root)
  end, cfgs)
end

function M.providers()
  require("dap").providers.configs["pytemplate"] = M.launch_configs
end

return M
