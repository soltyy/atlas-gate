"""Выгрузка того же отчёта Gate в Excel без ограничения пагинации."""
from datetime import datetime, timedelta, timezone
from io import BytesIO

from xlsxwriter import Workbook

from .errors import GateError
from .token_usage import GROUP_COLUMNS, usage_report

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
IDENTITY = {
    "org": "Организация", "userName": "Пользователь", "userEmail": "Почта", "userId": "ID пользователя",
    "deviceName": "Устройство", "deviceId": "ID устройства", "model": "Модель", "route": "Маршрут", "kind": "Тип",
}
USER = ("org", "userName", "userEmail", "userId")
MODEL = ("model", "route", "kind")
DIMENSIONS = {
    "device": ("deviceName", "deviceId", "model", "kind"),
    "user": USER,
    "model": MODEL,
    "user_model": (*USER, *MODEL),
}
METRICS = {
    "records": "Записей", "totalTokens": "Все токены", "totalTokensRecords": "Записей с общим расходом",
    "cacheAdjustedTokens": "За вычетом чтения кеша", "cacheAdjustedTokensRecords": "Записей с расходом без чтения",
    "totalInputTokens": "Вход", "totalInputTokensRecords": "Записей со входом",
    "uncachedInputTokens": "Вход вне кеша", "uncachedInputTokensRecords": "Записей со входом вне кеша",
    "cacheReadTokens": "Чтение кеша", "cacheReadTokensRecords": "Записей с чтением кеша",
    "cacheWriteTokens": "Запись кеша", "cacheWriteTokensRecords": "Записей с записью кеша",
    "outputTokens": "Выход", "outputTokensRecords": "Записей с выходом",
    "cacheReadPercent": "Доля чтения кеша", "cacheComparableRecords": "Записей для доли кеша",
    "legacyRecords": "Записей без детализации SDK", "incompleteRecords": "Записей с частичным учётом",
}
MATRIX = ("totalTokens", "cacheAdjustedTokens", "records", "cacheAdjustedTokensRecords")
DETAIL = {
    **{key: IDENTITY[key] for key in (*USER, *MODEL, "deviceName", "deviceId")},
    **{key: METRICS[key] for key in ("totalInputTokens", "uncachedInputTokens",
                                    "cacheReadTokens", "cacheWriteTokens", "outputTokens")},
    "durationMs": "Длительность SDK, мс", "source": "Источник", "status": "Статус",
    "usageIncomplete": "Частичный учёт", "sessionId": "ID сессии", "turnId": "ID хода",
}


def matrix_rows(groups, dimensions, group_keys, periods):
    grouped = {}
    for row in groups:
        key = tuple(row.get(column) for column in group_keys)
        grouped.setdefault(key, {})[row.get("period")] = row
    for values in grouped.values():
        row = next(iter(values.values()))
        totals = []
        for key in MATRIX:
            measured = [item[key] for item in values.values() if item[key] is not None]
            totals.append(sum(measured) if measured else None)
        yield [*(row.get(k) for k in dimensions), *totals,
               *(values.get(period, {}).get(key) for period in periods for key in MATRIX)]


def write_table(sheet, headers, rows, header_format, *, start=0, fixed=1, formats=None):
    sheet.freeze_panes(start + 1, fixed)
    for column, name in enumerate(headers):
        sheet.set_column(column, column, min(38, max(22, len(name) + 2)), (formats or {}).get(column))
    sheet.set_row(start, 32)
    sheet.write_row(start, 0, headers, header_format)
    end = start
    for end, row in enumerate(rows, start=start + 1):
        for column, value in enumerate(row):
            write = sheet.write_string if isinstance(value, str) else sheet.write
            if write(end, column, value) != 0:
                raise GateError(422, "excel_cell_limit", "Данные превышают размер листа Excel. Сузьте период или фильтры.")
    sheet.autofilter(start, 0, end, len(headers) - 1)


def usage_excel(store, params):
    report = usage_report(store, limit=None, **params)
    mode = params["group_by"]
    dimensions = DIMENSIONS[mode]
    periods = [row["period"] for row in report["trend"]]
    headers = [IDENTITY[k] for k in dimensions]
    headers.extend(f"Итого · {METRICS[k]}" for k in MATRIX)
    headers.extend(f"{period} · {METRICS[k]}" for period in periods for k in MATRIX)
    if len(headers) > 16384 or max(len(report["rows"]), len(report["groups"])) >= 1048571:
        raise GateError(422, "excel_sheet_limit", "Размер листа Excel превышен. Выберите более крупный интервал или сузьте диапазон.")
    tz_name = "МСК" if params["timezone"] == "msk" else "UTC"
    zone = timezone(timedelta(hours=3)) if params["timezone"] == "msk" else timezone.utc
    try:
        start, end = [datetime.fromtimestamp(report[k], zone).replace(tzinfo=None) for k in ("since", "until")]
    except (ValueError, OverflowError, OSError) as exc:
        raise GateError(400, "invalid_range", "Диапазон дат за пределами формата Excel.") from exc
    filters = " · ".join(f"{title}: {params[key]}" for key, title in (
        ("org", "Организация"), ("user", "ID пользователя"), ("model", "Модель"),
        ("route", "Маршрут"), ("kind", "Тип"), ("device", "Устройство"),
    ) if params[key])
    output = BytesIO()
    with Workbook(output, {"constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
                           "default_date_format": "yyyy-mm-dd hh:mm:ss"}) as book:
        header = book.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#294771", "text_wrap": True})
        share = book.add_format({"num_format": "0.0%"})
        day = book.add_format({"num_format": "yyyy-mm-dd"})
        matrix = book.add_worksheet("Матрица")
        matrix.write_row(0, 0, ("Расход токенов", tz_name))
        matrix.write_row(1, 0, ("С (включительно)", start))
        matrix.write_row(2, 0, ("До (исключительно)", end))
        matrix.write_row(3, 0, ("Фильтры", filters or "Все"))
        matrix.write(4, 0, "Столбцы показывают интервалы с записями. Пустое значение означает отсутствие записи или измерения.")
        write_table(matrix, headers, matrix_rows(report["groups"], dimensions, GROUP_COLUMNS[mode], periods),
                    header, start=6, fixed=2)
        breakdown = book.add_worksheet("Разбивка")
        fields = (*dimensions, *METRICS)
        columns = {**IDENTITY, **METRICS}

        def breakdown_row(row):
            period = datetime.fromisoformat(row["period"]) if row.get("period") else None
            return [period, *(row.get(k) / 100 if k == "cacheReadPercent" and row.get(k) is not None
                              else row.get(k) for k in fields)]

        write_table(breakdown, [f"Начало интервала ({tz_name})", *(columns[k] for k in fields)],
                    (breakdown_row(row) for row in report["groups"]), header, fixed=2,
                    formats={0: day, 1 + fields.index("cacheReadPercent"): share})
        details = book.add_worksheet("Ходы и запросы")
        write_table(details, [f"Время ({tz_name})", *DETAIL.values()], (
            [datetime.fromtimestamp(row["ts"], zone).replace(tzinfo=None), *(row.get(k) for k in DETAIL)]
            for row in report["rows"]
        ), header, fixed=2)
    return output.getvalue()
