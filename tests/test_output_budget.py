"""Device schema must retain an explicit optional budget; defaults untouched."""

import pytest
from pydantic import ValidationError

from atlas_gate.gate.schemas import GateAgentSessionCreate
from atlas_gate.schemas import SessionCreate


@pytest.mark.parametrize("schema", [SessionCreate, GateAgentSessionCreate])
def test_optional_forwarding(schema):
    assert "maxOutputTokens" not in schema().model_dump(exclude_none=True)
    assert (
        schema(maxOutputTokens=128).model_dump(exclude_none=True)["maxOutputTokens"]
        == 128
    )


@pytest.mark.parametrize("value", [0, -1, True, 128.5, "128"])
@pytest.mark.parametrize("schema", [SessionCreate, GateAgentSessionCreate])
def test_invalid_budget(schema, value):
    with pytest.raises(ValidationError):
        schema(maxOutputTokens=value)
