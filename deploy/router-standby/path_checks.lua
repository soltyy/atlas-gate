-- Each pair shares one HAProxy Lua state. A probe never receives client data.
local routers = {}

local function milliseconds(value)
    local number, unit = value:match("^(%d+)(%a*)$")
    local factor = ({ms=1, s=1000, m=60000, h=3600000})[unit == "" and "ms" or unit]
    assert(number and factor and tonumber(number) > 0, "invalid path check interval")
    return tonumber(number) * factor
end

local interval = milliseconds(os.getenv("ROUTER_PATH_CHECK_INTERVAL") or "30s")
local timeout = milliseconds(os.getenv("ROUTER_PATH_CHECK_TIMEOUT") or "5s")

local function fetch_identity(router, path)
    local response = core.httpclient():get{
        url = "http://127.0.0.1:" .. path.port .. "/v1/node/identity",
        headers = {["X-Atlas-Token"] = {router.token}, ["X-Atlas-Node"] = {router.id},
                   ["X-Atlas-Boot"] = {router.boot}},
        timeout = timeout,
    }
    if response.status == 200 then
        local body = response.body
        if type(body) == "string" and #body <= 2048 then return "response", body end
        return "untrusted"
    end
    if response.status == 0 or response.status == 502 or response.status == 503 or response.status == 504 then
        return "unavailable"
    end
    return "untrusted"
end

local function check(router, path, generation)
    local ok, outcome, body = pcall(fetch_identity, router, path)
    if not ok then outcome = "unavailable" end
    if generation == router.generation then
        -- An invalid/unauthorized response retires prior identity evidence.
        -- An HTTP transport failure carries no new evidence about the remote boot.
        path.current = nil
        if outcome == "response" then
            path.current = body
            path.seen = body
        elseif outcome == "untrusted" then
            path.seen = "{}"
        end
        if path.outcome ~= outcome then
            core.Info("identity check " .. router.name .. "/" .. path.name .. ": " .. outcome)
            path.outcome = outcome
        end
    end
    router.results:push({generation=generation, outcome=outcome})
end

local function pair(router, allow_recheck)
    router.generation = router.generation + 1
    local generation = router.generation
    for _, path in ipairs(router.paths) do
        core.register_task(check, router, path, generation)
    end
    local pending = #router.paths
    while pending > 0 do
        local result = router.results:pop_wait()
        if result.generation == generation then
            if allow_recheck and result.outcome == "unavailable" then
                return true
            end
            pending = pending - 1
        end
    end
    return false
end

local function monitor(router)
    while true do
        if pair(router, true) then pair(router, false) end
        core.msleep(interval)
    end
end

for _, name in ipairs({"codex", "claude"}) do
    local prefix = name:upper() .. "_"
    local router = {name=name, id=assert(os.getenv(prefix .. "NODE_ID")),
                    boot=assert(os.getenv(prefix .. "BOOT_ID")), token=assert(os.getenv(prefix .. "CHECK_TOKEN")),
                    paths={}, results=core.queue(), generation=0}
    for _, direction in ipairs({"direct", "standby"}) do
        local path = {name=direction, port=assert(os.getenv(prefix .. direction:upper() .. "_PORT"))}
        router[direction] = path
        table.insert(router.paths, path)
    end
    routers[name] = router
    core.register_task(monitor, router)
end

-- HAProxy evaluates identity JSON with its native parser when assigning a new
-- connection. A rejected direct identity also fences the previously enrolled FI.
core.register_fetches("path_identity", function(_, router, path, field)
    return routers[router][path][field]
end)
