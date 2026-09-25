-- pytemplate.nvim: LazyVim integration of the ./deploy runner (loaded by the project's .lazy.lua).
local M = {}

M.config = { root = nil }

function M.root()
  M.config.root = M.config.root or vim.fs.root(vim.uv.cwd(), "pytemplate.toml")
  return M.config.root
end

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", M.config, opts or {})
end

return M
