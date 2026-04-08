# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Storage layer interfaces and implementations."""

from importlib import import_module
from typing import Any, Dict

__all__ = [
    # Exceptions
    "StorageException",
    "CollectionNotFoundError",
    "RecordNotFoundError",
    "DuplicateKeyError",
    "ConnectionError",
    "SchemaError",
    # Backend
    "VikingVectorIndexBackend",
    "VikingDBManager",
    "VikingDBManagerProxy",
    # QueueFS
    "QueueManager",
    "init_queue_manager",
    "get_queue_manager",
    # VikingFS
    "VikingFS",
    "init_viking_fs",
    "get_viking_fs",
    # Observers
    "BaseObserver",
    "QueueObserver",
]

_MODULE_BY_EXPORT: Dict[str, str] = {
    "StorageException": "openviking.storage.errors",
    "CollectionNotFoundError": "openviking.storage.errors",
    "RecordNotFoundError": "openviking.storage.errors",
    "DuplicateKeyError": "openviking.storage.errors",
    "ConnectionError": "openviking.storage.errors",
    "SchemaError": "openviking.storage.errors",
    "VikingVectorIndexBackend": "openviking.storage.viking_vector_index_backend",
    "VikingDBManager": "openviking.storage.vikingdb_manager",
    "VikingDBManagerProxy": "openviking.storage.vikingdb_manager",
    "QueueManager": "openviking.storage.queuefs.queue_manager",
    "init_queue_manager": "openviking.storage.queuefs.queue_manager",
    "get_queue_manager": "openviking.storage.queuefs.queue_manager",
    "VikingFS": "openviking.storage.viking_fs",
    "init_viking_fs": "openviking.storage.viking_fs",
    "get_viking_fs": "openviking.storage.viking_fs",
    "BaseObserver": "openviking.storage.observers",
    "QueueObserver": "openviking.storage.observers",
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
