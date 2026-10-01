-- overseer template provider (found on 'runtimepath'): one "pyt: X" template per ./pyt
-- command in .pytemplate/editor.json and per pytemplate.toml [tasks] entry. It replaces the
-- tasks.json provider in pytemplate projects (see pytemplate.integrations.overseer).
local ARGS = {
  type = "list",
  subtype = { type = "string" },
  delimiter = " ",
  optional = true,
  order = 3,
  desc = "Extra arguments",
}

-- Order in :OverseerRun: the daily commands first, then the project's [tasks].
local RANK = { Development = 1, Distribution = 3, Environment = 4, Mode = 5 }
local TASKS_RANK = 2

-- The templates of the plugin's one root (the folder of the trusted .lazy.lua), whatever the
-- search: overseer builds it from the current buffer (its folder, else Neovim's cwd), and a buffer
-- outside the project (a stdlib module reached by go-to-definition or by stepping into it) left
-- :OverseerRun without a pyt template and a launch configuration's preLaunchTask ("pyt: compile")
-- unfound, so F5 did nothing. The tasks run in the root whatever the buffer (tasks.definition),
-- as the <leader>j keymaps and :Pyt do; the search never names another root.
local function generator()
  local pt = require("pytemplate")
  local root = pt.root()
  if not root then
    return "not in a pytemplate project"
  end
  local tasks = require("pytemplate.tasks")
  local TAG = require("overseer.constants").TAG
  local info = pt.info()
  local out = {}
  for _, cmd in ipairs(tasks.commands()) do
    local meta = tasks.META[cmd.name] or {}
    local params = {}
    if meta.backend then
      local choices = vim.deepcopy(info.backend.supported)
      if meta.backend == "all" and #choices > 1 then
        choices[#choices + 1] = "all"
      end
      params.backend = {
        type = "enum",
        choices = choices,
        optional = true,
        order = 1,
        desc = "Backend (empty: the active one, " .. info.backend.active .. ")",
      }
    end
    if meta.method and #info.build.methods > 0 then
      params.method = { type = "enum", choices = info.build.methods, optional = true, order = 2, desc = "Build method" }
    end
    -- usage "DIR ...", "PKG...", "PRESET ...": the first argument is required
    local required = cmd.usage ~= "" and not cmd.usage:match("^%[")
    params.args = vim.tbl_extend("force", ARGS, { optional = not required, desc = cmd.usage ~= "" and cmd.usage or ARGS.desc })
    out[#out + 1] = {
      name = "pyt: " .. cmd.name,
      desc = cmd.summary ~= "" and cmd.summary or nil,
      tags = meta.tag and { TAG[meta.tag] } or nil,
      params = params,
      rank = RANK[cmd.group] or 6,
      builder = function(p)
        local args = { cmd.name }
        if p.backend then
          args[#args + 1] = p.backend
        end
        if p.method then
          vim.list_extend(args, { "--method", p.method })
        end
        vim.list_extend(args, p.args or {})
        return tasks.definition(args)
      end,
    }
  end
  for _, t in ipairs(info.tasks) do
    out[#out + 1] = {
      name = "pyt: " .. t.name,
      desc = t.help ~= "" and t.help or nil,
      tags = t.background and { TAG.RUN } or nil,
      params = { args = ARGS },
      rank = TASKS_RANK,
      builder = function(p)
        return tasks.definition(vim.list_extend({ t.name }, p.args or {}))
      end,
    }
  end
  for i, t in ipairs(out) do
    t.index = i
  end
  table.sort(out, function(a, b)
    if a.rank ~= b.rank then
      return a.rank < b.rank
    end
    return a.index < b.index
  end)
  for _, t in ipairs(out) do
    t.rank, t.index = nil, nil
  end
  return out
end

return { generator = generator }
