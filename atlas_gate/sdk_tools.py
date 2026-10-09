"""Negotiated SDK capabilities. Native calls never request client execution."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, StrictBool

class SdkTools(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    webSearch: Literal["disabled", "cached", "live"] = "disabled"
    files: StrictBool = False

def enabled(value):
    return bool(value and (value.webSearch != "disabled" or value.files))

def permitted(requested, policy):
    return (not requested.files or policy.files) and (requested.webSearch == 'disabled' or
        requested.webSearch == policy.webSearch or (policy.webSearch == 'live' and requested.webSearch == 'cached'))

def supports(capabilities, requested):
    capabilities = capabilities or {}
    return (not requested.files or capabilities.get('files', {}).get('supported') is True) and (
        requested.webSearch == 'disabled' or requested.webSearch in capabilities.get('webSearch', {}).get('modes', []))


def defaults(capabilities, policy=None):
    """Enable the node's available SDK tools without an enrollment/UI toggle."""
    capabilities = capabilities or {}
    policy = policy or SdkTools(webSearch='live', files=True)
    modes = capabilities.get('webSearch', {}).get('modes', [])
    mode = next((m for m in ('live', 'cached') if m in modes and
        (policy.webSearch == 'live' or policy.webSearch == m)), 'disabled')
    return SdkTools(webSearch=mode, files=policy.files and capabilities.get('files', {}).get('supported') is True)
