-- Trusted Lua 5.4 hook factory. Only plain, bounded copies cross the worker boundary.
function(source, check, wanted, paused, sentinel, count)
    local info, getlocal, getupvalue = debug.getinfo, debug.getlocal, debug.getupvalue
    local function text(value, available)
        local prefix = string.sub(value, 1, math.min(16384, math.max(available, 0)))
        local count, bad = utf8.len(prefix)
        if not count then
            local parts = {}
            while bad do
                table.insert(parts, prefix:sub(1, bad - 1))
                if #prefix < #value and #prefix - bad < 3 then prefix = ""; break end
                table.insert(parts, "?")
                prefix = prefix:sub(bad + 1)
                count, bad = utf8.len(prefix)
            end
            prefix = table.concat(parts) .. prefix
        end
        local boundary = utf8.offset(prefix, 4097)
        if boundary then prefix = string.sub(prefix, 1, boundary - 1) end
        return prefix, #prefix < #value
    end

    local function snapshot()
        local frames, budget, bytes, seen, refs = {}, 100, 65536, {}, 0
        local function entry(name, value, depth)
            budget = budget - 1
            local kind = type(value)
            local label, cut = text(name, bytes)
            bytes = bytes - #label
            local item = {name=label, type=kind, truncated=cut}
            if kind == "table" then
                if seen[value] then item.reference_id = seen[value]; return item end
                refs = refs + 1
                seen[value] = "table-" .. refs
                item.reference_id = seen[value]
                item.value = {}
                for key, child in next, value do
                    if budget <= 0 or depth >= 5 or bytes <= 0 then item.truncated=true; break end
                    local kt = type(key)
                    local label = (kt == "string" and key) or ((kt == "number" or kt == "boolean") and tostring(key)) or ("<"..kt..">")
                    table.insert(item.value, entry(label, child, depth+1))
                end
            elseif kind == "string" then
                local content, shortened = text(value, bytes)
                item.value, item.truncated = content, item.truncated or shortened
                bytes = bytes - #item.value
            elseif kind == "number" then
                item.value = (value == value and math.abs(value) ~= math.huge) and value or tostring(value)
            elseif kind == "boolean" then item.value=value
            elseif kind ~= "nil" then item.value="<"..kind..">" end
            return item
        end
        for level=2,258 do
            local f=info(level,"Slf")
            if not f then break end
            if f.source == source then
                local frame={line=f.currentline, variables={}, truncated=false}
                table.insert(frames,frame)
                local function add(name,value)
                    if budget <= 0 or bytes <= 0 then frame.truncated=true; return false end
                    local item=entry(name,value,0)
                    table.insert(frame.variables,item)
                    return true
                end
                for i=1,255 do
                    local name,value=getlocal(level,i)
                    if not name then break end
                    if not name:match("^%(") and not add(name,value) then break end
                end
                for i=1,255 do
                    local name,value=getupvalue(f.func,i)
                    if not name then break end
                    if name == "_ENV" then
                        if not add("args",rawget(value,"args")) then break end
                    elseif not add(name,value) then break end
                end
            end
        end
        return frames
    end
    local function hook(event,line)
        if check(event,line) then error(sentinel,0) end
        if event ~= "line" or info(2,"S").source ~= source then return end
        local depth=1
        for level=3,258 do
            local f=info(level,"S")
            if not f then break end
            if f.source == source then depth=depth+1 end
        end
        local reason=wanted(line,depth)
        if reason and paused(reason,snapshot()) then error(sentinel,0) end
    end
    debug.sethook(hook,"crl",count)
    return snapshot
end
