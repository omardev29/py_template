-- overseer component "pytemplate.refresh": after a ./deploy command that can change the mode
-- (mode, setup, sync, lock, render...) succeeds, reload the editor state.
---@type overseer.ComponentFileDefinition
return {
  desc = "pytemplate: reload the editor state after ./deploy changed the mode",
  editable = false,
  params = {},
  constructor = function()
    return {
      on_complete = function(_, _, status)
        if status == "SUCCESS" then
          vim.schedule(require("pytemplate").refresh)
        end
      end,
    }
  end,
}
