# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Service layer for OpenViking.

Provides business logic decoupled from transport layer,
enabling reuse across HTTP Server and CLI.
"""

from importlib import import_module
from typing import Any, Dict

__all__ = [
    "OpenVikingService",
    "ComponentStatus",
    "DebugService",
    "SystemStatus",
    "FSService",
    "RelationService",
    "PackService",
    "SearchService",
    "ResourceService",
    "SessionService",
]

_MODULE_BY_EXPORT: Dict[str, str] = {
    "OpenVikingService": "openviking.service.core",
    "ComponentStatus": "openviking.service.debug_service",
    "DebugService": "openviking.service.debug_service",
    "SystemStatus": "openviking.service.debug_service",
    "FSService": "openviking.service.fs_service",
    "RelationService": "openviking.service.relation_service",
    "PackService": "openviking.service.pack_service",
    "SearchService": "openviking.service.search_service",
    "ResourceService": "openviking.service.resource_service",
    "SessionService": "openviking.service.session_service",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_BY_EXPORT.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
