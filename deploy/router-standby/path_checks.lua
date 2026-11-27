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

-- The v1 wire contract has exactly three safe ASCII scalars. Reject unknown,
-- duplicate, escaped or malformed fields instead of partially matching a JSON body.
local function identity(router, body)
    if type(body) ~= "string" or #body > 2048 then return "untrusted" end
    local rest = body:match("^%s*{%s*(.-)%s*}%s*$")
    local fields = {}
    for index = 1, 3 do
        if not rest then return "untrusted" end
        local key, value
        key, rest = rest:match('^"(%a+)"%s*:%s*(.*)$')
        if not key or fields[key] then return "untrusted" end
        if key == "protocolVersion" then
            value, rest = rest:match("^(1)%s*(.*)$")
        elseif key == "nodeId" or key == "bootId" then
            value, rest = rest:match('^"([%w_-]+)"%s*(.*)$')
        end
        if not value then return "untrusted" end
        fields[key] = value
        if index < 3 then rest = rest:match("^,%s*(.*)$") end
    end
    if rest ~= "" then return "untrusted" end
    if fields.nodeId ~= router.id or fields.bootId ~= router.boot then return "mismatch" end
    return "verified"
end

local function fetch_identity(router, path)
    local response = core.httpclient():get{
        url = "http://127.0.0.1:" .. path.port .. "/v1/node/identity",
        headers = {["X-Atlas-Token"] = {router.token}, ["X-Atlas-Node"] = {router.id},
                   ["X-Atlas-Boot"] = {router.boot}},
        timeout = timeout,
    }
    if response.status == 200 then return identity(router, response.body) end
    if response.status == 409 then return "mismatch" end
    if response.status == 0 or response.status == 502 or response.status == 503 or response.status == 504 then
        return "unavailable"
    end
    return "untrusted"
end

local function check(router, path, generation)
    local ok, outcome = pcall(fetch_identity, router, path)
    if not ok then outcome = "unavailable" end
    -- Only identity evidence can retire or re-admit a boot. A newer transport
    -- failure cannot hide a late 409 from the original pair.
    if outcome == "mismatch" and generation >= path.evidence then
        path.foreign = true
        path.evidence = generation
    elseif outcome == "verified" and generation == router.generation then
        path.foreign = false
        path.evidence = generation
    end
    if generation >= path.applied and (generation == router.generation or outcome ~= "verified") then
        path.applied = generation
        path.ready = outcome == "verified"
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
        local path = {name=direction, applied=0, evidence=0, port=assert(os.getenv(prefix .. direction:upper() .. "_PORT"))}
        router[direction] = path
        table.insert(router.paths, path)
    end
    routers[name] = router
    core.register_task(monitor, router)
end

-- Called only when a NEW connection is assigned. Existing streams stay put.
core.register_fetches("path_ready", function(_, name, path)
    local router = routers[name]
    return router[path].ready == true and not router[path].foreign and not router.direct.foreign
end)
