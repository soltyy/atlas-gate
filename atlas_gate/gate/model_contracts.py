"""One model projection for signed settings, discovery and the admin editor."""
from typing import Any, Literal
from pydantic import BaseModel


class ModelContract(BaseModel):
    model: str
    display_name: str
    context_window: int | None
    max_output: int | None
    enabled: bool
    limits_source: Literal['configured', 'router-sdk', 'unknown'] = 'configured'
    context_source: str = 'configured'
    context_full_window: int | None = None
    context_semantics: Literal['effective', 'model-full'] = 'model-full'
    context_status: Literal['configured', 'current', 'stale', 'unavailable', 'unknown'] = 'configured'
    attachments: dict[str, Any] | None = None
    sdkTools: dict[str, Any] | None = None
    output_policy: Literal['sdk-default', 'harness-budget'] | None = None
    output_budget: int | None = None


class ProfileModel(ModelContract):
    route_id: str


def project_model(model, attachments=None, kind="gateway"):
    budget = model.max_output if model.max_output > 0 else 4096
    budget = min(budget, 393216, model.context_window if model.context_window > 0 else 393216)
    return ModelContract(model=model.model, display_name=model.display_name or model.model,
                         context_window=model.context_window if model.context_window > 0 else None,
                         max_output=model.max_output if model.max_output > 0 else None,
                         enabled=model.enabled, attachments=attachments,
                         context_semantics='effective' if kind == 'router-agent' else 'model-full',
                         output_policy='sdk-default' if kind == 'router-agent' else 'harness-budget',
                         output_budget=None if kind == 'router-agent' else budget).model_dump(mode='json')


def published_model(state, org, model):
    """Use the already signed snapshot; a later node refresh cannot change its numbers."""
    for value in state.bodies.get(org.id, {}).get('models', []):
        if value['route_id'] == model.route_id and value['model'] == model.model:
            return ModelContract.model_validate(value).model_dump(mode='json')
    # Disabled/undelivered configured models are public catalog entries, never device permissions.
    route = org.route(model.route_id)
    fields = project_model(model, kind=route.kind if route else 'gateway')
    if route and route.kind == 'router-agent':
        fields.update(state.context_windows.project(org.id,route.id,model.model))
    return fields
