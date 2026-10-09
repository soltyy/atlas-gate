"""Сохранённые измерения SDK: неизвестное значение отличается от нуля."""
from __future__ import annotations

import json

METRICS = ("totalInputTokens", "uncachedInputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens")


def count(value):
    return value if type(value) is int and value >= 0 else None


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
    return {
        "source": "router-sdk", "uncachedInputTokens": uncached,
        "cacheReadTokens": read, "cacheWriteTokens": write,
        "totalInputTokens": sum((uncached, read, write)) if None not in (uncached, read, write) else None,
        "outputTokens": count(usage.get("output_tokens")),
        "durationMs": count(result.get("durationMs")),
        "status": "success" if result.get("ok") is True else "failed" if result.get("ok") is False else "unknown",
        "modelUsage": models,
    }


def usage_report(store, *, since, until, limit=50, before=None, **filters):
    conditions, values = ["u.ts >= ?", "u.ts < ?"], [since, until]
    for name, column in (("org", "org"), ("device", "device_id"), ("model", "model"), ("route", "route"), ("kind", "kind")):
        if filters.get(name):
            conditions.append(f"u.{column} = ?")
            values.append(filters[name])
    select = """SELECT u.rowid AS id, u.ts, u.org, u.device_id AS deviceId,
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
        json_extract(a.payload,'$.modelUsage') AS modelUsage
        FROM usage u LEFT JOIN devices d ON u.device_id = d.device_id
        LEFT JOIN agent_usage_details a ON u.kind = 'agent' AND u.session_id = a.session_id AND u.model_call = a.turn_id
        WHERE """ + " AND ".join(conditions)
    aggregate = "COUNT(*) AS records, SUM(source = 'legacy') AS legacyRecords, " + ", ".join(
        f"SUM({name}) AS {name}, COUNT({name}) AS {name}Records" for name in METRICS)
    aggregate += """, COUNT(CASE WHEN totalInputTokens IS NOT NULL AND cacheReadTokens IS NOT NULL THEN 1 END) AS cacheComparableRecords,
        SUM(CASE WHEN cacheReadTokens IS NOT NULL THEN totalInputTokens END) AS cacheComparableInput,
        SUM(CASE WHEN totalInputTokens IS NOT NULL THEN cacheReadTokens END) AS cacheComparableRead"""
    with store._lock:
        db = store._db
        db.execute("BEGIN")
        try:
            summary = dict(db.execute(f"SELECT {aggregate} FROM ({select})", values).fetchone())
            groups = [dict(row) for row in db.execute(f"SELECT deviceId, deviceName, platform, model, kind, {aggregate} FROM ({select}) GROUP BY deviceId, model, kind ORDER BY totalInputTokens DESC", values)]
            page_values = list(values)
            page = f"SELECT * FROM ({select})"
            if before is not None:
                page += " WHERE id < ?"
                page_values.append(before)
            page += " ORDER BY id DESC LIMIT ?"
            page_values.append(limit + 1)
            rows = [dict(row) for row in db.execute(page, page_values)]
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    for item in [summary, *groups]:
        denominator = item.pop("cacheComparableInput")
        numerator = item.pop("cacheComparableRead")
        item["cacheReadPercent"] = 100 * numerator / denominator if denominator and numerator is not None else None
    next_before = rows[limit - 1]["id"] if len(rows) > limit else None
    for row in rows[:limit]:
        row["modelUsage"] = json.loads(row["modelUsage"]) if row["modelUsage"] else {}
    return {"since": since, "until": until, "summary": summary, "groups": groups, "rows": rows[:limit], "nextBefore": next_before}
