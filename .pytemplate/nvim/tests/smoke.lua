-- Headless smoke test of the Neovim integration, run by `./deploy selftest --nvim` inside an
-- isolated LazyVim with the project as cwd. Prints one "ok"/"FAIL" line per check and exits
-- with 0 (all passed) or 1.
local ok, pt = pcall(require, "pytemplate")
local passed = ok and pt.root() ~= nil
io.stdout:write((passed and "ok   " or "FAIL ") .. "pytemplate.nvim loaded from .lazy.lua\n")
vim.cmd(passed and "qa!" or "cq!")
