-- spec.lua: the body of the project's .lazy.lua, kept in the plugin folder so a fix here needs no
-- new trust (trusting .lazy.lua already trusts .pytemplate/nvim/**). It is dofile'd by .lazy.lua
-- and returns lazy.nvim's local spec for `root`, the folder of the trusted .lazy.lua that ran it.
return function(root)
  root = vim.fs.normalize(root, { expand_env = false })
  -- A real pytemplate project the user trusted: this same folder must hold the config and the
  -- plugin. A folder cloned, vendored, unzipped or added as a submodule inside a trusted project,
  -- with a pytemplate.toml but no .lazy.lua of its own, is never `root` (.lazy.lua took the trusted
  -- file's folder), so its plugin code and its .venv tools are never run without a trust of its own.
  if not vim.uv.fs_stat(root .. "/pytemplate.toml") or not vim.uv.fs_stat(root .. "/.pytemplate/nvim/lua/pytemplate/init.lua") then
    return {}
  end
  -- Neovim reads a 'runtimepath' entry as a file glob: [ ] { } are wildcards, a comma separates
  -- entries, a backslash escapes, a backtick is command substitution, a single quote makes it go
  -- through 'shell' and a $NAME is expanded. The plugin cannot be put on the runtimepath from such
  -- a path (require fails, or E79), which would also break the LazyVim plugins whose opts call into
  -- it. Skip the integration with one message so those plugins keep working; the fix is to move the
  -- project to a plain path (./pyt nvim doctor and trust name the character too).
  local bad = root:find("[%[%]{},\\`'$]")
  if bad then
    vim.schedule(function()
      vim.notify(
        "pytemplate: the ./pyt integration is off: the project path holds `" .. root:sub(bad, bad)
          .. "`, which Neovim cannot put on 'runtimepath'. Move the project to a path without [ ] { } , \\ ` ' or $.",
        vim.log.levels.WARN
      )
    end)
    return {}
  end

  -- Delegates to the plugin; they run when the target plugin loads, never while parsing specs.
  -- Wrapped in pcall so a module that fails to load never breaks another plugin's config.
  local function call(mod, fn)
    return function(...)
      local ok, m = pcall(require, "pytemplate." .. mod)
      if ok and type(m) == "table" and type(m[fn]) == "function" then
        return m[fn](...)
      end
    end
  end

  local spec = {}
  -- LazyVim extras the integration uses. Those already enabled in :LazyExtras were imported
  -- earlier (lazy.nvim skips duplicate imports); any imported here lands after the user's
  -- `plugins`, which trips LazyVim's import order check: silence it only in that case.
  -- `./pyt nvim extras` enables them globally, which is the permanent fix. The read of
  -- lazy.nvim's internal spec state is guarded: without it, no extras (never an error).
  local ok_cfg, cfg = pcall(require, "lazy.core.config")
  local modules = (ok_cfg and type(cfg) == "table" and type(cfg.spec) == "table" and cfg.spec.modules) or {}
  if type(modules) == "table" and vim.tbl_contains(modules, "lazyvim.plugins") then
    for _, extra in ipairs({
      "lazyvim.plugins.extras.lang.python",
      "lazyvim.plugins.extras.lang.toml",
      "lazyvim.plugins.extras.dap.core",
      "lazyvim.plugins.extras.test.core",
      "lazyvim.plugins.extras.editor.overseer",
    }) do
      if not vim.tbl_contains(modules, extra) then
        vim.g.lazyvim_check_order = false
      end
      spec[#spec + 1] = { import = extra }
    end
  end

  vim.list_extend(spec, {
    {
      dir = root .. "/.pytemplate/nvim",
      name = "pytemplate.nvim",
      lazy = false,
      priority = 900, -- before any plugin whose opts below call into it
      main = "pytemplate",
      opts = { root = root },
    },
    { "folke/which-key.nvim", optional = true, opts = call("integrations", "which_key") },
    { "stevearc/overseer.nvim", optional = true, opts = call("integrations", "overseer") },
    { "neovim/nvim-lspconfig", optional = true, opts = call("integrations", "lsp") },
    { "mfussenegger/nvim-lint", optional = true, opts = call("integrations", "lint") },
    { "nvim-neotest/neotest", optional = true, opts = call("integrations", "neotest") },
    { "mfussenegger/nvim-dap", optional = true, opts = call("dap", "providers") },
    { "mfussenegger/nvim-dap-python", optional = true, config = call("dap", "setup") },
    { "linux-cultist/venv-selector.nvim", optional = true, opts = call("integrations", "venv_selector") },
  })
  return spec
end
