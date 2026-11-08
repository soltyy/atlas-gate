"""Сохранённые измерения SDK: неизвестное значение отличается от нуля."""
from __future__ import annotations

import json

METRICS = ("totalInputTokens", "uncachedInputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens")
GROUP_COLUMNS = {
    "device": ("deviceId", "model", "kind"),
    "user": ("org", "userId"),
    "model": ("model", "route", "kind"),
    "user_model": ("org", "userId", "model", "route", "kind"),
}
PERIOD_MODIFIERS = {
    "day": "",
    "week": ", 'weekday 0', '-6 days'",
    "month": ", 'start of month'",
}
TIMEZONE_OFFSETS = {"msk": "+3 hours", "utc": "+0 hours"}


def count(value):
    return value if type(value) is int and value >= 0 else None


def accountable_terminal(event):
    if event.get('type') == 'result':
        return True
    usage = event.get('usage')
    return (event.get('type') == 'error' and event.get('usageIncomplete') is True
            and event.get('usageSource') == 'completed_sdk_results' and isinstance(usage, dict)
            and any(count(usage.get(key)) is not None for key in
                    ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')))


def sdk_usage(result):
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    uncached = count(usage.get("input_tokens"))
    read = count(usage.get("cache_read_input_tokens"))
    write = count(usage.get("cache_creation_input_tokens"))
    breakdown = result.get("modelUsage")
    models = {}
    if isinstance(breakdown, dict):
        for model, values in breakdown.items():
            if isinstance(values, dict):
                models[model] = {key: count(values.get(key)) for key in
                    ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")}
    detail = {
        "source": "router-sdk", "uncachedInputTokens": uncached,
        "cacheReadTokens": read, "cacheWriteTokens": write,
        "totalInputTokens": sum((uncached, read, write)) if None not in (uncached, read, write) else None,
        "outputTokens": count(usage.get("output_tokens")),
        "durationMs": count(result.get("durationMs")),
        "status": "success" if result.get("ok") is True else "failed" if result.get("ok") is False else "unknown",
        "modelUsage": models,
    }
    if result.get('usageIncomplete') is True:
        detail['usageIncomplete'] = True
    if result.get('usageSource') == 'completed_sdk_results':
        detail['usageSource'] = 'completed_sdk_results'
    return detail


def usage_report(store, *, since, until, limit=50, before=None, group_by="device",
                 bucket=None, timezone="msk", **filters):
    conditions, values = ["u.ts >= ?", "u.ts < ?"], [since, until]
    for name, column in (("org", "org"), ("user", "user_id"), ("device", "device_id"),
                         ("model", "model"), ("route", "route"), ("kind", "kind")):
        if filters.get(name):
            conditions.append(f"u.{column} = ?")
            values.append(filters[name])
    select = """SELECT u.rowid AS id, u.ts, u.org, u.user_id AS userId, u.device_id AS deviceId,
        d.device_name AS deviceName, d.platform, u.model, u.route, u.kind,
        u.session_id AS sessionId, u.model_call AS turnId,
        CASE WHEN a.payload IS NOT NULL THEN json_extract(a.payload,'$.totalInputTokens') ELSE u.prompt_tokens END AS totalInputTokens,
        json_extract(a.payload,'$.uncachedInputTokens') AS uncachedInputTokens,
        CASE WHEN a.payload IS NOT NULL THEN json_extract(a.payload,'$.cacheReadTokens') WHEN u.cached_tokens > 0 THEN u.cached_tokens ELSE NULL END AS cacheReadTokens,
        json_extract(a.payload,'$.cacheWriteTokens') AS cacheWriteTokens,
        u.completion_tokens AS outputTokens,
        json_extract(a.payload,'$.durationMs') AS durationMs,
        COALESCE(json_extract(a.payload,'$.source'),'legacy') AS source,
        COALESCE(json_extract(a.payload,'$.status'),'unknown') AS status,
        json_extract(a.payload,'$.usageIncomplete') AS usageIncomplete,
        json_extract(a.payload,'$.usageSource') AS usageSource,
        json_extract(a.payload,'$.modelUsage') AS modelUsage, p.decision AS pricingDecision
        FROM usage u LEFT JOIN devices d ON u.device_id = d.device_id
        LEFT JOIN agent_usage_details a ON u.kind = 'agent' AND u.session_id = a.session_id AND u.model_call = a.turn_id
        LEFT JOIN gateway_usage_pricing p ON u.rowid = p.usage_id
        WHERE """ + " AND ".join(conditions)
    aggregate = "COUNT(*) AS records, SUM(source = 'legacy') AS legacyRecords, COUNT(CASE WHEN usageIncomplete=1 THEN 1 END) AS incompleteRecords, " + ", ".join(
        f"SUM({name}) AS {name}, COUNT({name}) AS {name}Records" for name in METRICS)
    aggregate += """, COUNT(CASE WHEN totalInputTokens IS NOT NULL AND cacheReadTokens IS NOT NULL THEN 1 END) AS cacheComparableRecords,
        SUM(CASE WHEN cacheReadTokens IS NOT NULL THEN totalInputTokens END) AS cacheComparableInput,
        SUM(CASE WHEN totalInputTokens IS NOT NULL THEN cacheReadTokens END) AS cacheComparableRead"""
    adjusted = "CASE WHEN cacheReadTokens BETWEEN 0 AND totalInputTokens THEN totalInputTokens - cacheReadTokens + outputTokens END"
    aggregate += f""", SUM(totalInputTokens + outputTokens) AS totalTokens,
        COUNT(totalInputTokens + outputTokens) AS totalTokensRecords,
        SUM({adjusted}) AS cacheAdjustedTokens, COUNT({adjusted}) AS cacheAdjustedTokensRecords"""
    period = None
    if bucket:
        offset = TIMEZONE_OFFSETS[timezone]
        modifiers = PERIOD_MODIFIERS[bucket]
        period = f"date(ts, 'unixepoch', '{offset}'{modifiers})"
    columns = GROUP_COLUMNS[group_by]
    group_keys = ", ".join(("period", *columns) if period else columns)
    group_fields = ", ".join(("deviceId", "deviceName", "platform", "model", "kind")
                             if group_by == "device" else columns)
    if period:
        group_fields = f"{period} AS period, {group_fields}"
    order = "period DESC, totalInputTokens DESC" if period else "totalInputTokens DESC"
    with store._lock:
        db = store._db
        db.execute("BEGIN")
        try:
            summary = dict(db.execute(f"SELECT {aggregate} FROM ({select})", values).fetchone())
            groups = [dict(row) for row in db.execute(
                f"SELECT {group_fields}, {aggregate} FROM ({select}) GROUP BY {group_keys} ORDER BY {order}", values)]
            trend = [dict(row) for row in db.execute(
                f"SELECT {period} AS period, {aggregate} FROM ({select}) GROUP BY period ORDER BY period", values
            )] if period else []
            if group_by != "device":
                labels = {(row["org"], row["user_id"]): (row["user_name"], row["user_email"])
                          for row in db.execute("SELECT org,user_id,user_name,user_email FROM devices ORDER BY created_at,device_id")}
                dimensions = db.execute("""SELECT DISTINCT org, user_id, model, route FROM usage
                    WHERE ts >= ? AND ts < ?""", (since, until)).fetchall()
            page_values = list(values)
            page = f"SELECT * FROM ({select})"
            if before is not None:
                page += " WHERE id < ?"
                page_values.append(before)
            page += " ORDER BY id DESC"
            if limit is not None:
                page += " LIMIT ?"
                page_values.append(limit + 1)
            rows = [dict(row) for row in db.execute(page, page_values)]
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    for item in [summary, *groups, *trend]:
        denominator = item.pop("cacheComparableInput")
        numerator = item.pop("cacheComparableRead")
        item["cacheReadPercent"] = 100 * numerator / denominator if denominator and numerator is not None else None
    next_before = rows[limit - 1]["id"] if limit is not None and len(rows) > limit else None
    for row in rows[:limit]:
        if row['usageIncomplete'] is not None:
            row['usageIncomplete'] = bool(row['usageIncomplete'])
        row["modelUsage"] = json.loads(row["modelUsage"]) if row["modelUsage"] else {}
        row['pricingDecision'] = json.loads(row['pricingDecision']) if row['pricingDecision'] else None
    report = {"since": since, "until": until, "summary": summary, "groups": groups,
              "rows": rows[:limit], "nextBefore": next_before, "trend": trend}
    if group_by != "device":
        for row in [*groups, *rows[:limit]]:
            if "userId" in row:
                row["userName"], row["userEmail"] = labels.get((row["org"], row["userId"]), (None, None))
        people = sorted({(row["org"], row["user_id"]) for row in dimensions})
        report["options"] = {
            "users": [{"org": org, "userId": user, "userName": labels.get((org, user), (None, None))[0],
                       "userEmail": labels.get((org, user), (None, None))[1]} for org, user in people],
            "models": sorted({row["model"] for row in dimensions if row["model"]}),
            "routes": sorted({row["route"] for row in dimensions if row["route"]}),
        }
    return report
