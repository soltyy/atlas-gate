from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from atlas_gate.gate.usage_excel import CONTENT_TYPE
from .test_discovery import configured, enroll
from .test_token_usage import record

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


async def test_usage_excel_filters_full_matrix_without_pagination_and_never_writes_formulas(configured):
    http, state, _ = configured
    credentials = await enroll(http)
    device = credentials["device_id"]
    state.store._exec("UPDATE devices SET user_name=? WHERE device_id=?", ("{=1+1}", device))
    user = state.store._one("SELECT org,user_id FROM devices WHERE device_id=?", (device,))
    for index, cached in enumerate((40, 0)):
        state.store.usage_add(org=user["org"], user_id=user["user_id"], device_id=device, kind="gateway",
                              model="model", route="api", prompt_tokens=100, completion_tokens=10,
                              cached_tokens=cached, model_call=f"api-{index}")
    record(state.store, "unrelated", {"input_tokens": 10, "output_tokens": 5})
    filters = dict(org=user["org"], user=user["user_id"], model="model", group_by="user_model",
                   bucket="day", timezone="msk", limit=1)
    path = "/harness/admin/usage.xlsx"
    assert (await http.get(path)).status_code == 401
    assert (await http.get(path, headers={"X-Atlas-Token": credentials["device_token"]})).status_code == 401
    admin = {"X-Atlas-Token": "test-admin-secret"}
    page = (await http.get("/harness/admin/usage", params=filters, headers=admin)).json()
    assert len(page["rows"]) == 1 and page["summary"]["totalTokens"] == 220
    assert page["summary"]["cacheAdjustedTokens"] == 70
    response = await http.get(path, params={**filters, "before": 1}, headers=admin)
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    assert 'attachment; filename="atlas-usage.xlsx"' == response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    with ZipFile(BytesIO(response.content)) as book:
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        assert [x.get("name") for x in workbook.findall("x:sheets/x:sheet", NS)] == [
            "Матрица", "Разбивка", "Ходы и запросы"]
        matrix, breakdown, details = [ET.fromstring(book.read(f"xl/worksheets/sheet{i}.xml")) for i in (1, 2, 3)]
    assert matrix.find(".//x:row[@r='8']/x:c[@r='B8']/x:is/x:t", NS).text == "{=1+1}"
    assert matrix.find(".//x:c[@r='H8']/x:v", NS).text == "220"
    assert matrix.find(".//x:c[@r='I8']/x:v", NS).text == "70"
    assert len(breakdown.findall("x:sheetData/x:row", NS)) == 2
    assert len(details.findall("x:sheetData/x:row", NS)) == 3
    assert details.find(".//x:c[@r='M2']", NS) is None  # legacy zero with no cache proof
    assert details.find(".//x:c[@r='M3']/x:v", NS).text == "40"
    assert all(sheet.find(".//x:f", NS) is None for sheet in (matrix, breakdown, details))
