-- ./deploy as editor tasks: task definitions (overseer, or a terminal split without it), the
-- output parser, pickers, the <leader>j keymaps, :Deploy and render-on-save.
local pt = require("pytemplate")
local M = {}

-- What the editor knows about each command. The command LIST comes from editor.json (cli.py's
-- COMMANDS); unknown commands still get a generic task. Fields:
--   tag: overseer tag   backend: "optional" | "all" (optional, plus `all`)   method: --method
--   parse: diagnostics from the output   refresh: reload the editor state after it succeeds
--   needs: only offered when that backend is supported   show: open the output on start
M.META = {
  run = { tag = "RUN", backend = "optional" },
  test = { tag = "TEST", backend = "all", parse = true },
  check = { tag = "BUILD", backend = "all", parse = true },
  lint = { parse = true },
  fmt = {},
  compile = { tag = "BUILD", needs = "mypyc", parse = true },
  report = { needs = "mypyc", parse = true },
  build = { tag = "BUILD", backend = "optional", method = true, parse = true },
  mode = { refresh = true, show = true },
  setup = { refresh = true, show = true },
  apply = { refresh = true, show = true },
  sync = { backend = "all", refresh = true },
  lock = { refresh = true, show = true },
  add = { refresh = true },
  remove = { refresh = true },
  render = { refresh = true },
  -- rename rewrites editor.json (name, pkg), uv.lock and the sources: refresh, and show what changed
  rename = { refresh = true, show = true },
  clean = { tag = "CLEAN" },
  doctor = { show = true },
  help = { show = true },
  tasks = { show = true },
  nvim = { show = true },
  ["shell-setup"] = { show = true },
  selftest = { show = true },
}

-- Used when editor.json is missing or unreadable (./deploy render fixes it).
local FALLBACK_COMMANDS = { "run", "test", "check", "lint", "fmt", "build", "mode", "setup", "sync", "render", "doctor", "help" }

---The ./deploy commands the editor offers: {name, usage, summary} (report/compile need mypyc).
function M.commands()
  local info = pt.info()
  local out = {}
  local cmds = info.commands
  if #cmds == 0 then
    cmds = vim.tbl_map(function(n)
      return { name = n, usage = "", summary = "" }
    end, FALLBACK_COMMANDS)
  end
  for _, c in ipairs(cmds) do
    local meta = M.META[c.name] or {}
    if not meta.needs or vim.tbl_contains(info.backend.supported, meta.needs) then
      out[#out + 1] = c
    end
  end
  return out
end

-- --- output parser ------------------------------------------------------------------------------

local SEV = { error = "E", warning = "W", note = "N", information = "I" }

-- CSI (colours), then OSC (ruff's OSC 8 links in terminals it knows) ended by BEL or by ST
-- (ESC \): a payload never holds ESC or BEL, so one sequence never swallows the text after it.
local function strip(line)
  return (line:gsub("\27%[[%d;?]*[%a@]", ""):gsub("\27%][^\7\27]*\7", ""):gsub("\27%][^\7\27]*\27\\", ""):gsub("\r", ""))
end

-- The mypyc stage (.build[/wsl]/mypyc-{dev,release}/stage/, relative or absolute) is a throwaway
-- copy of src/ (pytest under `test mypyc` imports it): the rest of such a path, or nil.
local STAGE = { "^(.-)%.build/mypyc%-%l+/stage/(.+)$", "^(.-)%.build/wsl/mypyc%-%l+/stage/(.+)$" }
local function in_stage(file)
  for _, pattern in ipairs(STAGE) do
    local prefix, rest = file:match(pattern)
    if prefix and (prefix == "" or prefix:sub(-1) == "/") then
      return rest
    end
  end
end

local function absolute(file)
  file = file:gsub("\\", "/")
  local root = pt.root()
  local staged = root and in_stage(file)
  if staged then
    -- land on the src/ file: an edit made in the stage copy is overwritten by the next sync
    local src = vim.fs.normalize(root .. "/src/" .. staged)
    if vim.uv.fs_stat(src) then
      return src
    end
  end
  if file:match("^%a:/") or file:sub(1, 1) == "/" then
    return vim.fs.normalize(file)
  end
  if not root then
    return file
  end
  local path = vim.fs.normalize(root .. "/" .. file)
  -- mypyc runs in its stage (a copy of src/) and prints stage-relative paths: map them to src/
  if not vim.uv.fs_stat(path) then
    local src = vim.fs.normalize(root .. "/src/" .. file)
    if vim.uv.fs_stat(src) then
      return src
    end
  end
  return path
end

---Parse one output line of ./deploy into a quickfix item, or nil. Understands
--- mypy       src/pkg/x.py:12: error: Incompatible types  [assignment]   (and :12:5:)
--- ruff       src\pkg\x.py:3:8: F401 [*] `os` imported but unused           (concise format)
--- pytest     tests/test_x.py:14: AssertionError                             (not "in func" frames)
--- mypyc      pkg/core/x.py:7: error: <message>                              (relative to its stage)
--- runner     warning: src/pkg/core/x.py:7: <message>                        (runner warnings/errors)
--- basedpyright  C:\p\src\x.py:3:5 - error: <message>
---Relative paths are relative to the project root (the runner runs the tools there); mypyc's,
---relative to its stage (a copy of src/), resolve to src/ when the root has no such file; paths
---into the stage itself (pytest under mypyc: .build/mypyc-dev/stage/pkg/x.py) land on src/ too.
function M.parse_line(line)
  line = strip(line)
  local forced
  local rest = line:match("^warning: (.*)$")
  if rest then
    forced, line = "W", rest
  else
    rest = line:match("^error: (.*)$")
    if rest then
      forced, line = "E", rest
    end
  end
  local file, lnum, col, sev, msg = line:match("^%s+(%S.-%.pyi?):(%d+):(%d+) %- (%a+): (.*)$")
  if file then
    local k = SEV[sev:lower()] or "E"
    if k == "N" then
      return nil
    end
    return { filename = absolute(file), lnum = tonumber(lnum), col = tonumber(col), text = msg, type = k }
  end
  file, lnum, col, rest = line:match("^(%a?:?[^:]+%.pyi?):(%d+):(%d*):?%s*(.*)$")
  if not file or file:find("site-packages", 1, true) or rest:match("^in ") or rest == "" then
    return nil
  end
  sev, msg = rest:match("^(%a+): (.*)$")
  local k = SEV[(sev or ""):lower()]
  if k == "N" then
    return nil
  end
  local kind = forced or k or (rest:match("^%u+%d+") and "W" or "E")
  return {
    filename = absolute(file),
    lnum = tonumber(lnum),
    col = tonumber(col) or 1,
    text = k and msg or rest,
    type = kind,
  }
end

-- --- task definitions ---------------------------------------------------------------------------

---overseer components for a ./deploy task.
function M.components(name, o)
  local info = pt.info()
  local meta = M.META[name] or {}
  o = vim.tbl_extend("keep", o or {}, meta)
  local c = {}
  if o.parse then
    c[#c + 1] = { "on_output_parse", parser = M.parse_line, relative_file_root = pt.root() }
    c[#c + 1] = { "on_result_diagnostics", remove_on_restart = true }
    c[#c + 1] = { "on_result_diagnostics_quickfix", open = false }
  end
  -- console apps and long-running tasks show their output; GUI apps only when they fail
  local show = o.show or o.background or (name == "run" and not info.gui)
  c[#c + 1] = { "open_output", direction = "dock", on_start = show and "always" or "never", on_complete = "failure" }
  if name == "run" or o.background then
    c[#c + 1] = { "unique", replace = true } -- a new run restarts the app or the dev server
  else
    -- a re-run disposes the finished previous run of the same command (tasks are compared by
    -- name): its diagnostics share the namespace and would otherwise stay until overseer
    -- disposes it (never for a run nobody opened). soft: a running one is never stopped.
    c[#c + 1] = { "unique", soft = true }
  end
  if o.refresh then
    c[#c + 1] = "pytemplate.refresh"
  end
  c[#c + 1] = "default"
  return c
end

---The pytemplate.toml [tasks] entry called `name`, if any.
function M.project_task(name)
  for _, t in ipairs(pt.info().tasks) do
    if t.name == name then
      return t
    end
  end
end

---overseer task definition for `./deploy ARGS...`.
function M.definition(args, o)
  o = vim.tbl_extend("force", {}, o or {})
  local recipe = M.project_task(args[1])
  if recipe then
    if o.background == nil then
      o.background = recipe.background
    end
    if o.parse == nil then
      o.parse = true -- recipes such as `ci` run ruff, mypy and pytest
    end
  end
  local meta = M.META[args[1]] or {}
  local parse = o.parse or meta.parse
  return {
    name = "deploy " .. table.concat(args, " "),
    cmd = pt.deploy_cmd(args),
    cwd = pt.caller_cwd(),
    env = pt.deploy_env(parse and { RUFF_OUTPUT_FORMAT = "concise" } or nil),
    components = M.components(args[1], o),
    metadata = { pytemplate = true, args = vim.deepcopy(args) },
  }
end

---Run `./deploy ARGS...` as an overseer task, or in a terminal split without overseer.
function M.run(args, o)
  if not pt.root() then
    return vim.notify("pytemplate: not inside a pytemplate project", vim.log.levels.WARN)
  end
  local d = M.definition(args, o)
  local ok, overseer = pcall(require, "overseer")
  if not ok then
    vim.cmd("botright 15new")
    return vim.fn.jobstart(d.cmd, { term = true, cwd = d.cwd, env = d.env })
  end
  local task = overseer.new_task(d)
  task:start()
  return task
end

---Split a typed argument string (double or single quotes group words).
function M.split_args(s)
  local out, cur, q = {}, nil, nil
  for ch in s:gmatch(".") do
    if q then
      if ch == q then
        q = nil
      else
        cur = cur .. ch
      end
    elseif ch == '"' or ch == "'" then
      q, cur = ch, cur or ""
    elseif ch:match("%s") then
      if cur then
        out[#out + 1], cur = cur, nil
      end
    else
      cur = (cur or "") .. ch
    end
  end
  if cur then
    out[#out + 1] = cur
  end
  return out
end

-- --- pickers ----------------------------------------------------------------------------------

function M.pick_backend(prompt, with_all, cb)
  local info = pt.info()
  local items = vim.deepcopy(info.backend.supported)
  if with_all and #items > 1 then
    items[#items + 1] = "all"
  end
  vim.ui.select(items, {
    prompt = prompt,
    format_item = function(b)
      return (b == info.backend.active and "* " or "  ") .. b
    end,
  }, function(b)
    if b then
      cb(b)
    end
  end)
end

function M.mode()
  M.pick_backend("Active backend", false, function(b)
    M.run({ "mode", b })
  end)
end

function M.with_args(cmd)
  M.pick_backend("./deploy " .. cmd, cmd == "test" or cmd == "check", function(b)
    vim.ui.input({ prompt = ("./deploy %s %s "):format(cmd, b) }, function(s)
      if s then
        M.run(vim.list_extend({ cmd, b }, M.split_args(s)))
      end
    end)
  end)
end

function M.pick_task()
  local tasks = pt.info().tasks
  if #tasks == 0 then
    return vim.notify("pytemplate: no [tasks] in pytemplate.toml")
  end
  vim.ui.select(tasks, {
    prompt = "pytemplate.toml [tasks]",
    format_item = function(t)
      return ("%-12s %s"):format(t.name, t.help)
    end,
  }, function(t)
    if t then
      M.run({ t.name })
    end
  end)
end

function M.task(name)
  if M.project_task(name) then
    return M.run({ name })
  end
  vim.notify(("pytemplate: preset '%s' has no '%s' task"):format(pt.info().preset, name))
end

function M.report()
  if not vim.tbl_contains(pt.info().backend.supported, "mypyc") then
    return vim.notify("pytemplate: mypyc is not in backend.supported")
  end
  M.run({ "report", "--open" })
end

function M.stop()
  local ok, overseer = pcall(require, "overseer")
  if not ok then
    return
  end
  for _, t in ipairs(overseer.list_tasks({ status = "RUNNING" })) do
    if t.metadata and t.metadata.pytemplate then
      t:stop()
    end
  end
end

function M.pick()
  if pcall(require, "overseer") then
    vim.cmd("OverseerRun")
  else
    local names = vim.tbl_map(function(c)
      return c.name
    end, M.commands())
    vim.list_extend(names, vim.tbl_map(function(t)
      return t.name
    end, pt.info().tasks))
    vim.ui.select(names, { prompt = "./deploy" }, function(n)
      if n then
        M.run({ n })
      end
    end)
  end
end

---Completion for :Deploy.
function M.complete(lead, line)
  local info = pt.info()
  local words = {}
  local nargs = #vim.split(vim.trim(line), "%s+") - (line:match("%s$") and 0 or 1)
  if nargs <= 1 then
    for _, c in ipairs(M.commands()) do
      words[#words + 1] = c.name
    end
    for _, t in ipairs(info.tasks) do
      words[#words + 1] = t.name
    end
  else
    vim.list_extend(words, info.backend.supported)
    words[#words + 1] = "all"
    for _, m in ipairs(info.build.methods) do
      words[#words + 1] = "--method=" .. m
    end
  end
  return vim.tbl_filter(function(w)
    return vim.startswith(w, lead)
  end, words)
end

-- --- keymaps, :Deploy, render on save ---------------------------------------------------------

M.KEYS = {
  { "j", M.pick, "Pick a deploy task" },
  { "r", { "run" }, "Run" },
  { "R", function() M.with_args("run") end, "Run on backend..." },
  { "t", { "test" }, "Test" },
  { "T", { "test", "all" }, "Test all backends" },
  { "c", { "check" }, "Check" },
  { "C", { "check", "all" }, "Check all backends" },
  { "b", { "build" }, "Build" },
  { "B", function() M.with_args("build") end, "Build on backend..." },
  { "l", { "lint", "--fix" }, "Lint --fix" },
  { "f", { "fmt" }, "Format" },
  { "m", M.mode, "Switch backend" },
  { "k", M.pick_task, "Project tasks" },
  { "d", function() M.task("dev") end, "Dev (hot reload)" },
  { "p", M.report, "mypyc report" },
  { "s", { "sync", "all" }, "Sync envs" },
  { "S", { "setup" }, "Setup" },
  { "D", { "doctor" }, "Doctor" },
  { "w", "<cmd>OverseerToggle!<cr>", "Task list" },
  { "x", M.stop, "Stop deploy tasks" },
}

function M.setup(cfg)
  local prefix = cfg.prefix or "<leader>j"
  for _, k in ipairs(M.KEYS) do
    local rhs = k[2]
    if type(rhs) == "table" then
      local args = rhs
      rhs = function()
        M.run(args)
      end
    end
    vim.keymap.set("n", prefix .. k[1], rhs, { desc = k[3] })
  end
  vim.api.nvim_create_user_command("Deploy", function(a)
    -- quotes group words ("a b" is one argument), like the <leader>jR prompt
    local args = M.split_args(a.args)
    M.run(#args > 0 and args or { "help" }, { show = true })
  end, { nargs = "*", complete = M.complete, desc = "./deploy ARGS (through uv, never 'shell')" })
  local group = vim.api.nvim_create_augroup("pytemplate", { clear = true })
  if cfg.render_on_save then
    vim.api.nvim_create_autocmd("BufWritePost", {
      group = group,
      pattern = "pytemplate.toml",
      desc = "pytemplate: ./deploy render",
      callback = function(ev)
        if pt.same_path(vim.fs.dirname(vim.fs.normalize(ev.match)), pt.root()) then
          M.run({ "render" })
        end
      end,
    })
  end
end

return M
