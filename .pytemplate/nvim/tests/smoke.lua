-- Headless smoke test of pytemplate.nvim inside a real LazyVim (use an isolated one: XDG_* or
-- NVIM_APPNAME), with the project as the cwd and its .lazy.lua trusted. `./pyt selftest --nvim`
-- runs it like this (VeryLazy only fires on UIEnter, which headless Neovim never sends):
--   nvim --headless -c "doautocmd UIEnter" -c "luafile .pytemplate/nvim/tests/smoke.lua"
-- PT_ROOT (optional): the project root the caller expects. Output on stdout, one line per check:
--   "ok   NAME"  |  "FAIL NAME" followed by the error, indented  |  "SKIP NAME (reason)"
-- and last "DONE <number of checks>". Each result starts on a fresh line: stdout is shared with
-- anything that leaks there (a pty, a banner), and text without a newline would hide it.
-- Exit code 0 when nothing failed (:qa!), 1 otherwise (:cq!). SKIP is only used for things that
-- need a network install (a language server via uvx/Mason, a treesitter parser) or a missing
-- C compiler (the mypyc debug configuration).
local uv = vim.uv or vim.loop
local failed, total = 0, 0

local function emit(line)
  io.stdout:write(line, "\n")
  io.stdout:flush()
end

local function result(line)
  emit("\n" .. line)
end

local Skip = {}
local function skip(reason)
  error(setmetatable({ reason = reason }, Skip), 0)
end

local function check(name, fn)
  total = total + 1
  local ok, err = xpcall(fn, function(e)
    if getmetatable(e) == Skip then
      return e
    end
    return debug.traceback(tostring(e), 2)
  end)
  if ok then
    result("ok   " .. name)
  elseif getmetatable(err) == Skip then
    result("SKIP " .. name .. " (" .. err.reason .. ")")
  else
    failed = failed + 1
    result("FAIL " .. name)
    for _, line in ipairs(vim.split(tostring(err), "\n", { plain = true })) do
      emit("     " .. line)
    end
  end
  return ok
end

local function wait(ms, cond, what)
  if not vim.wait(ms, cond, 50) then
    error(("timeout after %d ms: %s"):format(ms, what), 2)
  end
end

local function finish()
  result("DONE " .. total) -- the caller counts the result lines it parsed against this
  vim.cmd(failed == 0 and "qa!" or "cq!")
end

-- A hung check must not hang the caller.
vim.defer_fn(function()
  result("FAIL watchdog (the smoke test took more than 20 minutes)")
  vim.cmd("cq!")
end, 20 * 60 * 1000)

local pt, tasks, integ, root, info
local function supported(b)
  return vim.tbl_contains(info.backend.supported, b)
end
local function has_component(def, name)
  for _, c in ipairs(def.components) do
    if c == name or (type(c) == "table" and c[1] == name) then
      return c
    end
  end
end
local function run_task(args, o)
  local d = tasks.definition(args, o)
  d.strategy = { "jobstart", use_terminal = false } -- a PTY in headless Neovim loses the output
  local task = require("overseer").new_task(d)
  task:start()
  wait(300000, function()
    return task:is_complete()
  end, "task " .. d.name)
  return task
end
local function task_text(task)
  local buf = task:get_bufnr()
  return buf and vim.api.nvim_buf_is_valid(buf) and table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), "\n") or ""
end

-- --- the plugin is there ------------------------------------------------------------------------

local loaded = check("pytemplate.nvim loaded by .lazy.lua", function()
  local plugin = require("lazy.core.config").plugins["pytemplate.nvim"]
  assert(plugin, "pytemplate.nvim is not in the lazy.nvim spec: is .lazy.lua trusted? (./pyt nvim trust)")
  assert(plugin._.loaded, "pytemplate.nvim is in the spec but not loaded")
  pt = require("pytemplate")
  tasks = require("pytemplate.tasks")
  integ = require("pytemplate.integrations")
  root = assert(pt.root(), "no project root from the cwd " .. tostring(uv.cwd()))
  info = pt.info()
  if vim.env.PT_ROOT and vim.env.PT_ROOT ~= "" then
    assert(pt.same_path(root, vim.env.PT_ROOT), root .. " is not PT_ROOT " .. vim.env.PT_ROOT)
  end
  assert(pt.same_path(plugin.dir, root .. "/.pytemplate/nvim"), "plugin dir " .. plugin.dir)
end)
if not loaded then
  return finish()
end

check("LazyVim extras imported", function()
  local modules = require("lazy.core.config").spec.modules
  if not vim.tbl_contains(modules, "lazyvim.plugins") then
    skip("not a LazyVim config")
  end
  for _, m in ipairs({ "lang.python", "lang.toml", "dap.core", "test.core", "editor.overseer" }) do
    assert(vim.tbl_contains(modules, "lazyvim.plugins.extras." .. m), m .. " not imported")
  end
end)

check("editor.json read and validated", function()
  info = pt.info()
  assert(info.schema == 1, ".pytemplate/editor.json missing or invalid")
  assert(supported(info.backend.active), "active backend not supported")
  assert(uv.fs_stat(root .. "/src/" .. info.pkg), "no src/" .. info.pkg)
  local names = vim.tbl_map(function(c)
    return c.name
  end, info.commands)
  for _, n in ipairs({ "run", "test", "check", "build", "mode", "setup", "render", "doctor" }) do
    assert(vim.tbl_contains(names, n), "command missing: " .. n)
  end
  assert(info.envs.tools == ".venv" and info.envs.pypy == ".venv-pypy", vim.inspect(info.envs))
  -- invalid values never get through
  local bad = pt.sanitize({
    schema = 1,
    backend = { active = "rm -rf", supported = { "cpython", "calc.exe" } },
    envs = { tools = "C:/Windows/System32", cpython = "../x" },
    mypyc_stage = "../../etc",
    tasks = { { name = "x; y" } },
    typing = { profile = "evil", mypy_severity = { error = "Fatal" } },
  })
  assert(vim.deep_equal(bad.backend, { active = "cpython", supported = { "cpython" } }), vim.inspect(bad.backend))
  assert(bad.envs.tools == ".venv" and bad.envs.cpython == ".venv" and bad.mypyc_stage == ".build/mypyc-dev/stage")
  assert(#bad.tasks == 0 and bad.typing.profile == "off" and bad.typing.mypy_severity.error == "Error")
end)

-- --- the runner ---------------------------------------------------------------------------------

check("pyt argv never uses 'shell'", function()
  local cmd = pt.pyt_cmd({ "help" })
  assert(pt.uv(), "uv not found by the plugin")
  assert(cmd[1] == pt.uv() and vim.fn.executable(cmd[1]) == 1, vim.inspect(cmd))
  assert(cmd[2] == "run" and cmd[4] == "--script" and cmd[#cmd] == "help", vim.inspect(cmd))
  local saved = { vim.o.shell, vim.o.shellcmdflag }
  local ok, err = pcall(function()
    for _, sh in ipairs({ "xonsh", "niu", "/nonexistent/sh" }) do
      vim.o.shell, vim.o.shellcmdflag = sh, "-c"
      local r = vim.system(cmd, { cwd = pt.caller_cwd(), env = pt.pyt_env(), text = true }):wait(120000)
      assert(r.code == 0 and r.stdout:find("Development:", 1, true), sh .. ": exit " .. tostring(r.code) .. "\n" .. (r.stderr or ""))
    end
    -- jobstart, what overseer uses
    local code, out = nil, {}
    local id = vim.fn.jobstart(cmd, {
      cwd = pt.caller_cwd(),
      env = pt.pyt_env(),
      stdout_buffered = true,
      on_stdout = function(_, data)
        out = data
      end,
      on_exit = function(_, c)
        code = c
      end,
    })
    assert(id > 0, "jobstart failed: " .. id)
    wait(120000, function()
      return code ~= nil
    end, "jobstart")
    assert(code == 0 and table.concat(out, "\n"):find("Development:", 1, true), "jobstart exit " .. tostring(code))
  end)
  vim.o.shell, vim.o.shellcmdflag = saved[1], saved[2]
  assert(ok, err)
end)

check("launcher fallback without uv", function()
  local saved = pt._uv
  pt._uv = false
  local cmd = pt.pyt_cmd({ "help" })
  pt._uv = saved
  -- POSIX: through /bin/sh, like the VS Code tasks and the git hook (no exec bit needed)
  assert(#cmd == (pt.is_win and 2 or 3) and cmd[#cmd - 1] == pt.launcher(), vim.inspect(cmd))
  assert(cmd[#cmd - 1]:match(pt.is_win and "pyt%.cmd$" or "/pyt$"), vim.inspect(cmd))
  assert(pt.is_win or cmd[1] == "/bin/sh", vim.inspect(cmd))
  local mode = not pt.is_win and uv.fs_stat(pt.launcher()).mode % 4096 or nil -- permission bits
  if mode then
    uv.fs_chmod(pt.launcher(), 420) -- 0644: a checkout that lost the exec bit
  end
  local ok, r = pcall(function()
    return vim.system(cmd, { cwd = pt.caller_cwd(), env = pt.pyt_env(), text = true }):wait(120000)
  end)
  if mode then
    uv.fs_chmod(pt.launcher(), mode)
  end
  assert(ok, r)
  assert(r.code == 0 and r.stdout:find("Development:", 1, true), "launcher exit " .. tostring(r.code) .. "\n" .. (r.stderr or ""))
end)

-- --- overseer -----------------------------------------------------------------------------------

check("overseer templates: every command and [tasks] entry, no duplicates", function()
  local list
  require("overseer.template").list({ dir = root }, function(t)
    list = t
  end)
  wait(15000, function()
    return list ~= nil
  end, "overseer template list")
  local seen = {}
  for _, t in ipairs(list) do
    assert(not seen[t.name], "duplicate template: " .. t.name)
    assert(t.module ~= "vscode", "tasks.json template still enabled: " .. t.name)
    seen[t.name] = t
  end
  for _, c in ipairs(info.commands) do
    local needs = (tasks.META[c.name] or {}).needs
    local want = not needs or supported(needs)
    assert((seen["pyt: " .. c.name] ~= nil) == want, "pyt: " .. c.name .. (want and " missing" or " should be hidden"))
  end
  for _, t in ipairs(info.tasks) do
    assert(seen["pyt: " .. t.name], "task missing: " .. t.name)
  end
  assert((seen["pyt: report"] ~= nil) == supported("mypyc"), "report must exist only with mypyc")
  local d = seen["pyt: test"].builder({ backend = "all", args = { "-x" } })
  assert(vim.deep_equal(vim.list_slice(d.cmd, #d.cmd - 2), { "test", "all", "-x" }), vim.inspect(d.cmd))
  local b = seen["pyt: build"].builder({ method = "pyz" })
  assert(vim.deep_equal(vim.list_slice(b.cmd, #b.cmd - 2), { "build", "--method", "pyz" }), vim.inspect(b.cmd))
end)

check("task definitions (" .. info.preset .. ")", function()
  local d = tasks.definition({ "test", "cpython" })
  assert(type(d.cmd) == "table" and d.cmd[#d.cmd] == "cpython" and d.cmd[#d.cmd - 1] == "test", vim.inspect(d.cmd))
  assert(d.env.RUFF_OUTPUT_FORMAT == "concise" and d.env.PYTEMPLATE_LAUNCHER == "nvim", vim.inspect(d.env))
  assert(pt.same_path(d.env.PYTEMPLATE_CALLER_CWD, d.cwd) and pt.in_root(d.cwd), vim.inspect(d))
  assert(has_component(d, "on_output_parse") and has_component(d, "default"))
  local run = tasks.definition({ "run" })
  local open = has_component(run, "open_output")
  assert(open.on_start == (info.gui and "never" or "always"), "run output on start: " .. open.on_start)
  assert(has_component(run, "unique"), "run must be unique")
  assert(has_component(tasks.definition({ "mode", "cpython" }), "pytemplate.refresh"), "mode must refresh")
  for _, t in ipairs(info.tasks) do
    local td = tasks.definition({ t.name }) -- [tasks] entries: background from editor.json, parsed output
    assert(has_component(td, "on_output_parse"), t.name .. " output is not parsed")
    if t.background then
      assert(has_component(td, "unique") and has_component(td, "open_output").on_start == "always", t.name)
    end
  end
  if info.preset == "flet" then
    local dev = vim.tbl_filter(function(t)
      return t.name == "dev"
    end, info.tasks)[1]
    assert(dev and dev.background, "flet: [tasks.dev] must be a background task")
  elseif info.preset == "raylib" then
    local names = vim.tbl_map(function(t)
      return t.name
    end, info.tasks)
    assert(vim.tbl_contains(names, "stubs") and vim.tbl_contains(names, "bunnymark"), vim.inspect(names))
  end
end)

check("output parser", function()
  local p = tasks.parse_line
  local m = p("src/a/x.py:12: error: Incompatible types in assignment  [assignment]")
  assert(m and m.type == "E" and m.lnum == 12 and pt.same_path(m.filename, root .. "/src/a/x.py"), vim.inspect(m))
  m = p("src\\a\\x.py:3:8: F401 [*] `os` imported but unused")
  assert(m and m.type == "W" and m.col == 8 and m.text:find("F401", 1, true), vim.inspect(m))
  m = p("C:\\p\\x.py:1:1: E501 Line too long")
  assert(m and m.lnum == 1 and m.filename:match("^C:/p/x%.py$"), vim.inspect(m))
  m = p("tests/test_x.py:14: AssertionError")
  assert(m and m.type == "E" and m.lnum == 14, vim.inspect(m))
  m = p("\27[33mwarning: \27[0msrc/a/core/x.py:7: mypyc: generator in compiled code")
  assert(m and m.type == "W" and m.lnum == 7, vim.inspect(m))
  m = p("  C:\\p\\src\\x.py:3:5 - error: Type of \"y\" is unknown")
  assert(m and m.type == "E" and m.col == 5, vim.inspect(m))
  -- mypyc prints paths relative to its stage (a copy of src/): they land on src/
  local core = "/src/" .. info.pkg .. "/core/__init__.py"
  m = p(info.pkg .. "/core/__init__.py:2: error: Incompatible types in assignment  [assignment]")
  assert(m and m.type == "E" and pt.same_path(m.filename, root .. core), vim.inspect(m))
  m = p("src/" .. info.pkg .. "/core/__init__.py:2: error: x  [misc]")
  assert(m and pt.same_path(m.filename, root .. core), vim.inspect(m))
  for _, line in ipairs({
    "src/x.py:5: note: See https://mypy.rtfd.io",
    "Found 3 errors in 1 file (checked 4 source files)",
    "src/x.py:30: in helper",
    "C:\\u\\.venv\\Lib\\site-packages\\x.py:3: error: boom",
    "$ uv run --locked ruff check src tests",
  }) do
    assert(p(line) == nil, "should be ignored: " .. line)
  end
end)

check("a pyt task runs to SUCCESS", function()
  local task = run_task({ "help" })
  assert(task.status == "SUCCESS", task.status .. " (exit " .. tostring(task.exit_code) .. ")\n" .. task_text(task))
  assert(task_text(task):find("Development:", 1, true), "no help text in the task output")
end)

check("./pyt render task, then the editor refresh", function()
  local task = run_task({ "render" })
  assert(task.status == "SUCCESS", task.status .. "\n" .. task_text(task))
  local ok, err = pcall(pt.refresh)
  assert(ok, err)
  assert(pt.info().schema == 1, "editor.json unreadable after render")
end)

check("task output becomes diagnostics (./pyt lint)", function()
  local file = root .. "/src/" .. info.pkg .. "/_pt_smoke_lint.py"
  vim.fn.writefile({ "value = undefined_name_smoke" }, file)
  local ok, err = pcall(function()
    local task = run_task({ "lint" })
    assert(task.status == "FAILURE", "ruff should fail on an undefined name: " .. task.status .. "\n" .. task_text(task))
    local found
    for _, d in ipairs((task.result or {}).diagnostics or {}) do
      if pt.same_path(d.filename, file) and d.lnum == 1 and tostring(d.text):find("F821", 1, true) then
        found = d
      end
    end
    assert(found, "no F821 diagnostic for " .. file .. "\n" .. vim.inspect((task.result or {}).diagnostics) .. "\n" .. task_text(task))
  end)
  os.remove(file)
  assert(ok, err)
end)

-- --- editor integration -------------------------------------------------------------------------

check("keymaps, :Pyt, completion, render on save", function()
  local prefix = pt.config.prefix
  for _, k in ipairs(tasks.KEYS) do
    local m = vim.fn.maparg(prefix .. k[1], "n", false, true)
    assert(m.desc == k[3], "keymap " .. prefix .. k[1] .. ": " .. vim.inspect(m))
  end
  assert(vim.fn.exists(":Pyt") == 2, ":Pyt missing")
  assert(vim.tbl_contains(vim.fn.getcompletion("Pyt ", "cmdline"), "run"), "no command completion")
  assert(vim.tbl_contains(vim.fn.getcompletion("Pyt test ", "cmdline"), "all"), "no backend completion")
  assert(#vim.api.nvim_get_autocmds({ group = "pytemplate", event = "BufWritePost" }) == 1, "no render-on-save autocmd")
  local wk = require("lazy.core.config").plugins["which-key.nvim"]
  if wk then
    local spec = require("lazy.core.plugin").values(wk, "opts", false).spec or {}
    local group = vim.tbl_filter(function(s)
      return type(s) == "table" and s[1] == prefix and s.group == "pyt"
    end, spec)
    assert(#group == 1, "which-key group 'pyt' missing")
  end
end)

check("mypy diagnostics (profile " .. info.typing.profile .. ")", function()
  require("lazy").load({ plugins = { "nvim-lint" } })
  local lint = require("lint")
  assert(vim.tbl_contains(lint.linters_by_ft.python or {}, "mypy"), "mypy not in linters_by_ft.python")
  local linter = lint.linters.mypy
  assert(type(linter) == "table" and pt.same_path(linter.cwd, root), "mypy linter cwd is not the root")
  local enabled = integ.mypy_enabled(root .. "/src/main.py")
  if not (info.typing.mypy and info.typing.profile ~= "off") then
    assert(not enabled, "mypy must be off with the profile " .. info.typing.profile)
    return
  end
  assert(enabled, "mypy is not enabled (is .venv set up?)")
  local file = root .. "/src/" .. info.pkg .. "/_pt_smoke_mypy.py"
  vim.fn.writefile({ 'value: int = "text"' }, file)
  local ok, err = pcall(function()
    vim.cmd.edit(file)
    local buf = vim.api.nvim_get_current_buf()
    -- LazyVim lints on BufReadPost after a 100 ms debounce, and a new run of a linter cancels the
    -- running one (on Windows only its cmd.exe wrapper): let that run start and end first
    vim.wait(500)
    wait(120000, function()
      return #lint.get_running(buf) == 0
    end, "the automatic lint run")
    lint.try_lint("mypy")
    local ns = lint.get_namespace("mypy")
    if not vim.wait(180000, function()
      return #vim.diagnostic.get(buf, { namespace = ns }) > 0
    end, 50) then
      -- the same command by hand: its exit code, time and output say why nothing came
      local cmd = type(linter.cmd) == "function" and linter.cmd() or linter.cmd
      local argv = vim.list_extend({ cmd }, vim.deepcopy(linter.args or {}))
      argv[#argv + 1] = file
      if pt.is_win then
        argv = vim.list_extend({ "cmd.exe", "/C" }, argv)
      end
      local started = uv.hrtime()
      local r = vim.system(argv, { cwd = linter.cwd, env = linter.env, clear_env = linter.env ~= nil, text = true }):wait(120000)
      error(("no mypy diagnostics after 180 s (linters still running: %s); by hand: exit %s after %.1f s\n%s%s"):format(
        table.concat(lint.get_running(buf), ", "), tostring(r.code), (uv.hrtime() - started) / 1e9, r.stdout or "", r.stderr or ""))
    end
    local d = vim.diagnostic.get(buf, { namespace = ns })[1]
    assert(d.message:find("Incompatible types", 1, true), d.message)
    local S = vim.diagnostic.severity
    local want = ({ Error = S.ERROR, Warning = S.WARN, Information = S.INFO, Hint = S.HINT })[info.typing.mypy_severity.error]
    assert(d.severity == want, "severity " .. d.severity .. " instead of " .. want)
  end)
  pcall(vim.cmd, "bwipeout! " .. vim.fn.fnameescape(file))
  os.remove(file)
  assert(ok, err)
end)

local function lsp_file()
  local candidates = { "/src/" .. info.pkg .. "/app.py", "/src/" .. info.pkg .. "/ui/app.py", "/src/main.py" }
  for _, c in ipairs(candidates) do
    if uv.fs_stat(root .. c) then
      return root .. c
    end
  end
end

check("ruff language server from .venv", function()
  local ruff = assert(pt.tool("ruff"), "no ruff in .venv (./pyt setup)")
  vim.cmd.edit(lsp_file())
  local buf = vim.api.nvim_get_current_buf()
  wait(90000, function()
    return #vim.lsp.get_clients({ bufnr = buf, name = "ruff" }) > 0
  end, "ruff attaching")
  local client = vim.lsp.get_clients({ bufnr = buf, name = "ruff" })[1]
  assert(pt.same_path(client.config.cmd[1], ruff), "ruff from " .. vim.inspect(client.config.cmd))
end)

check("python language server (" .. pt.lsp_name() .. ")", function()
  local want = pt.lsp_name()
  local other = want == "pyright" and "basedpyright" or "pyright"
  if vim.lsp.is_enabled then
    assert(vim.lsp.is_enabled(want) and not vim.lsp.is_enabled(other), "enabled servers are wrong")
  end
  local cmd, source = integ.lsp_cmd(want)
  vim.cmd.edit(lsp_file())
  local buf = vim.api.nvim_get_current_buf()
  local attached = vim.wait(150000, function()
    return #vim.lsp.get_clients({ bufnr = buf, name = want }) > 0
  end, 200)
  if not attached then
    skip(want .. " did not start within 150 s (from " .. source .. ": it may still be downloading)")
  end
  local client = vim.lsp.get_clients({ bufnr = buf, name = want })[1]
  if cmd then
    assert(vim.deep_equal(client.config.cmd, cmd), vim.inspect(client.config.cmd))
  end
  local params = { textDocument = vim.lsp.util.make_text_document_params(buf) }
  local res = client:request_sync("textDocument/documentSymbol", params, 120000, buf)
  if not res or res.err then
    skip(want .. " did not answer: " .. vim.inspect(res))
  end
  vim.wait(8000, function()
    return false
  end)
  for _, d in ipairs(vim.diagnostic.get(buf)) do
    assert(not d.message:find("could not be resolved", 1, true), "unresolved import (.venv not found?): " .. d.message)
  end
end)

check("debug adapter and launch.json", function()
  require("lazy").load({ plugins = { "nvim-dap", "nvim-dap-python" } })
  local dap = require("dap")
  local adapter
  dap.adapters.debugpy(function(a)
    adapter = a
  end, { type = "debugpy", request = "launch" })
  assert(adapter and adapter.type == "executable", vim.inspect(adapter))
  assert(vim.fn.executable(adapter.command) == 1, "adapter command not executable: " .. tostring(adapter.command))
  if pt.is_win then -- nvim-dap's raw uv.spawn only finds .exe/.com on Windows
    assert(adapter.command:lower():match("%.exe$") and adapter.command:match("^%a:"), adapter.command)
  end
  local cfgs = require("dap.ext.vscode").getconfigs(root .. "/.vscode/launch.json")
  assert(#cfgs >= 2 and cfgs[1].type == "debugpy", vim.inspect(cfgs))
  -- nvim-dap lifts only this OS's block; the other OSes' blocks stay (and are ignored)
  local os_key = pt.is_win and "windows" or (vim.fn.has("mac") == 1 and "osx" or "linux")
  for _, c in ipairs(cfgs) do
    assert(not c[os_key], "OS block not lifted: " .. c.name)
  end
  local provider = dap.providers.configs.pytemplate
  assert(provider and #provider(0) == 0, "the pytemplate provider must stay empty at the root")
  vim.cmd.cd(vim.fn.fnameescape(root .. "/src"))
  local ok, sub = pcall(provider, 0)
  vim.cmd.cd(vim.fn.fnameescape(root))
  assert(ok and #sub == #cfgs, "provider from src/: " .. vim.inspect(sub))
  assert(sub[1].program and pt.same_path(sub[1].program, root .. "/src/main.py"), vim.inspect(sub[1]))
end)

check("debugger stops at a breakpoint (launch.json)", function()
  local dap = require("dap")
  local main = root .. "/src/main.py"
  vim.cmd.edit(main)
  local line = vim.fn.search([[^if __name__ == "__main__":]], "nw")
  assert(line > 0, "no __main__ guard in src/main.py")
  require("dap.breakpoints").set({}, vim.api.nvim_get_current_buf(), line)
  local stopped
  dap.listeners.after.event_stopped.pt_smoke = function(_, body)
    stopped = body
  end
  local config = vim.deepcopy(require("dap.ext.vscode").getconfigs(root .. "/.vscode/launch.json")[1])
  config.console = "internalConsole"
  local ok, err = pcall(function()
    dap.run(config)
    wait(120000, function()
      return stopped ~= nil
    end, "breakpoint hit")
    assert(stopped.reason == "breakpoint", vim.inspect(stopped))
    wait(30000, function()
      return dap.session() ~= nil and dap.session().current_frame ~= nil
    end, "stack trace")
    local frame = dap.session().current_frame
    assert(frame.line == line, "stopped at line " .. tostring(frame.line) .. " instead of " .. line)
  end)
  dap.listeners.after.event_stopped.pt_smoke = nil
  if dap.session() then
    dap.disconnect({ terminateDebuggee = true })
    vim.wait(30000, function()
      return dap.session() == nil
    end, 100)
  end
  require("dap.breakpoints").clear()
  assert(ok, err)
end)

local function has_c_compiler()
  for _, cc in ipairs({ vim.env.CC or "cc", "cc", "gcc", "clang", "cl" }) do
    if vim.fn.executable(cc) == 1 then
      return true
    end
  end
  local x86 = vim.env["ProgramFiles(x86)"]
  return pt.is_win and x86 ~= nil and uv.fs_stat(x86 .. "/Microsoft Visual Studio/Installer/vswhere.exe") ~= nil
end

-- The mypyc configuration: overseer runs its preLaunchTask "pyt: compile" (our provider, the
-- only one defining it), then debugpy runs the stage's main.py with src <-> stage pathMappings.
check("mypyc launch config: pyt: compile, then a breakpoint in src/main.py", function()
  if not supported("mypyc") then
    skip("mypyc is not in backend.supported")
  end
  if not has_c_compiler() then
    skip("no C compiler: mypyc cannot build the stage")
  end
  local dap = require("dap")
  local config
  for _, c in ipairs(require("dap.ext.vscode").getconfigs(root .. "/.vscode/launch.json")) do
    if c.name:find("mypyc", 1, true) then
      config = vim.deepcopy(c)
    end
  end
  assert(config, "no mypyc configuration in launch.json")
  assert(config.preLaunchTask == "pyt: compile", vim.inspect(config.preLaunchTask))
  config.console = "internalConsole"
  local main = root .. "/src/main.py"
  vim.cmd.edit(main)
  local line = vim.fn.search([[^if __name__ == "__main__":]], "nw")
  assert(line > 0, "no __main__ guard in src/main.py")
  require("dap.breakpoints").set({}, vim.api.nvim_get_current_buf(), line)
  local stopped
  dap.listeners.after.event_stopped.pt_smoke_mypyc = function(_, body)
    stopped = body
  end
  local ok, err = pcall(function()
    dap.run(config)
    wait(420000, function()
      return stopped ~= nil
    end, "pyt: compile, then the breakpoint")
    assert(stopped.reason == "breakpoint", vim.inspect(stopped))
    wait(30000, function()
      return dap.session() ~= nil and dap.session().current_frame ~= nil
    end, "stack trace")
    local frame = dap.session().current_frame
    assert(frame.line == line, "stopped at line " .. tostring(frame.line) .. " instead of " .. line)
    assert(uv.fs_stat(root .. "/" .. info.mypyc_stage .. "/main.py"), "no stage: the preLaunchTask did not compile")
  end)
  dap.listeners.after.event_stopped.pt_smoke_mypyc = nil
  if dap.session() then
    dap.disconnect({ terminateDebuggee = true })
    vim.wait(30000, function()
      return dap.session() == nil
    end, 100)
  end
  require("dap.breakpoints").clear()
  assert(ok, err)
end)

local function has_parser(lang)
  local ok, res = pcall(vim.treesitter.language.add, lang)
  return ok and res ~= nil and res ~= false
end

check("neotest: discovery skips .venv, tests pass", function()
  if not has_parser("python") then
    local ok, ts = pcall(require, "nvim-treesitter")
    if ok and type(ts.install) == "function" then
      local job = ts.install({ "python" })
      if type(job) == "table" and job.wait then
        pcall(job.wait, job, 300000)
      end
    end
    if not has_parser("python") then
      skip("no treesitter parser for python")
    end
  end
  local nt = require("neotest")
  local file = vim.fn.glob(root .. "/tests/test_*.py", true, true)[1]
  assert(file, "no tests/test_*.py")
  vim.cmd.edit(file)
  local buf = vim.api.nvim_get_current_buf()
  nt.run.run(file) -- the neotest client starts (and discovers) on first use
  local id
  wait(180000, function()
    for _, x in ipairs(nt.state.adapter_ids()) do
      if x:find("neotest-python", 1, true) then
        id = x
      end
    end
    return id ~= nil and nt.state.positions(id, { buffer = buf }) ~= nil
  end, "neotest discovery")
  wait(300000, function()
    local c = nt.state.status_counts(id, { buffer = buf })
    return c ~= nil and c.running == 0 and (c.passed + c.failed + c.skipped) > 0
  end, "neotest run")
  local c = nt.state.status_counts(id, { buffer = buf })
  assert(c.failed == 0 and c.passed > 0, vim.inspect(c))
  local files = 0
  for _, pos in nt.state.positions(id):iter() do
    local rel = vim.fs.normalize(pos.path):sub(#vim.fs.normalize(root) + 2)
    assert(not rel:match("^%.") and not rel:find("/%.venv"), "discovered inside a hidden folder: " .. pos.path)
    files = files + (pos.type == "file" and 1 or 0)
  end
  assert(files > 0, "no test files discovered")
end)

check(":checkhealth pytemplate has no ERROR", function()
  vim.cmd("checkhealth pytemplate")
  local lines = vim.api.nvim_buf_get_lines(0, 0, -1, false)
  local text = table.concat(lines, "\n")
  assert(text:find("pytemplate", 1, true) and text:find("OK", 1, true), text)
  assert(not text:find("ERROR", 1, true), text)
end)

finish()
