# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from importlib import import_module
from typing import Any, Dict

__all__ = [
    "QueueManager",
    "get_queue_manager",
    "init_queue_manager",
    "NamedQueue",
    "QueueStatus",
    "QueueError",
    "EmbeddingQueue",
    "EmbeddingMsg",
    "EmbeddingTaskTracker",
    "SemanticQueue",
    "SemanticDagExecutor",
    "SemanticMsg",
    "SemanticProcessor",
]

_MODULE_BY_EXPORT: Dict[str, str] = {
    "QueueManager": "openviking.storage.queuefs.queue_manager",
    "get_queue_manager": "openviking.storage.queuefs.queue_manager",
    "init_queue_manager": "openviking.storage.queuefs.queue_manager",
    "NamedQueue": "openviking.storage.queuefs.named_queue",
    "QueueStatus": "openviking.storage.queuefs.named_queue",
    "QueueError": "openviking.storage.queuefs.named_queue",
    "EmbeddingQueue": "openviking.storage.queuefs.embedding_queue",
    "EmbeddingMsg": "openviking.storage.queuefs.embedding_msg",
    "EmbeddingTaskTracker": "openviking.storage.queuefs.embedding_tracker",
    "SemanticQueue": "openviking.storage.queuefs.semantic_queue",
    "SemanticDagExecutor": "openviking.storage.queuefs.semantic_dag",
    "SemanticMsg": "openviking.storage.queuefs.semantic_msg",
    "SemanticProcessor": "openviking.storage.queuefs.semantic_processor",
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
