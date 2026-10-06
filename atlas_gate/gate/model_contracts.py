"""One model projection for signed settings, discovery and the admin editor."""
from typing import Any, Literal
from pydantic import BaseModel


class ModelContract(BaseModel):
    model: str
    display_name: str
    context_window: int | None
    max_output: int | None
    enabled: bool
    limits_source: Literal['configured'] = 'configured'
    attachments: dict[str, Any] | None = None


class ProfileModel(ModelContract):
    route_id: str


def project_model(model, attachments=None):
    return ModelContract(model=model.model, display_name=model.display_name or model.model,
                         context_window=model.context_window if model.context_window > 0 else None,
                         max_output=model.max_output if model.max_output > 0 else None,
                         enabled=model.enabled, attachments=attachments).model_dump(mode='json')


def published_model(state, org, model):
    """Use the already signed snapshot; a later node refresh cannot change its numbers."""
    for value in state.bodies.get(org.id, {}).get('models', []):
        if value['route_id'] == model.route_id and value['model'] == model.model:
            return ModelContract.model_validate(value).model_dump(mode='json')
    # Disabled/undelivered configured models are public catalog entries, never device permissions.
    return project_model(model)
