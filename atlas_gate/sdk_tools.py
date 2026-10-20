"""Negotiated SDK capabilities. Native calls never request client execution."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, StrictBool, Field

class SdkTools(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    webSearch: Literal["disabled", "cached", "live"] = "disabled"
    files: StrictBool = False
    imageGeneration: StrictBool = Field(default=False, exclude_if=lambda value: not value)

def enabled(value):
    return bool(value and (value.webSearch != "disabled" or value.files or value.imageGeneration))

def permitted(requested, policy):
    return (not requested.imageGeneration or policy.imageGeneration) and (not requested.files or policy.files) and (requested.webSearch == 'disabled' or
        requested.webSearch == policy.webSearch or (policy.webSearch == 'live' and requested.webSearch == 'cached'))

def supports(capabilities, requested):
    capabilities = capabilities or {}
    return (not requested.imageGeneration or capabilities.get('imageGeneration', {}).get('supported') is True) and (not requested.files or capabilities.get('files', {}).get('supported') is True) and (
        requested.webSearch == 'disabled' or requested.webSearch in capabilities.get('webSearch', {}).get('modes', []))


def defaults(capabilities, policy=None):
    """Enable the node's available SDK tools without an enrollment/UI toggle."""
    capabilities = capabilities or {}
    policy = policy or SdkTools(webSearch='live', files=True, imageGeneration=True)
    modes = capabilities.get('webSearch', {}).get('modes', [])
    mode = next((m for m in ('live', 'cached') if m in modes and
        (policy.webSearch == 'live' or policy.webSearch == m)), 'disabled')
    return SdkTools(webSearch=mode, files=policy.files and capabilities.get('files', {}).get('supported') is True,
        imageGeneration=policy.imageGeneration and capabilities.get('imageGeneration', {}).get('supported') is True)
