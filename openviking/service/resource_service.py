# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Resource Service for OpenViking.

Provides resource management operations: add_resource, add_skill, wait_processed.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlparse

from openviking.server.identity import RequestContext
from openviking.service.knowledge_document_registry import (
    DOCUMENT_PROCESSING_STATUS_FAILED,
    DOCUMENT_PROCESSING_STATUS_PROCESSING,
    DOCUMENT_PROCESSING_STATUS_READY,
    DocumentMovePlan,
    FolderRenamePlan,
    KnowledgeDocumentRegistry,
)
from openviking.storage.expr import PathScope
from openviking.storage import VikingDBManager
from openviking.storage.queuefs import get_queue_manager
from openviking.storage.viking_fs import VikingFS
from openviking.telemetry import get_current_telemetry
from openviking.telemetry.resource_summary import (
    build_queue_status_payload,
    record_resource_wait_metrics,
    register_wait_telemetry,
    unregister_wait_telemetry,
)
from openviking.utils import parse_code_hosting_url
from openviking.utils.resource_processor import ResourceProcessor
from openviking.utils.skill_processor import SkillProcessor
from openviking_cli.exceptions import (
    ConflictError,
    DeadlineExceededError,
    InvalidArgumentError,
    NotInitializedError,
    NotFoundError,
)
from openviking_cli.utils import get_logger
from openviking_cli.utils.uri import VikingURI

if TYPE_CHECKING:
    from openviking.resource.watch_manager import WatchManager
    from openviking.resource.watch_scheduler import WatchScheduler

logger = get_logger(__name__)


class ResourceService:
    """Resource management service."""

    def __init__(
        self,
        vikingdb: Optional[VikingDBManager] = None,
        viking_fs: Optional[VikingFS] = None,
        resource_processor: Optional[ResourceProcessor] = None,
        skill_processor: Optional[SkillProcessor] = None,
        watch_scheduler: Optional["WatchScheduler"] = None,
    ):
        self._vikingdb = vikingdb
        self._viking_fs = viking_fs
        self._resource_processor = resource_processor
        self._skill_processor = skill_processor
        self._watch_scheduler = watch_scheduler
        self._document_registry: Optional[KnowledgeDocumentRegistry] = None
        self._document_lock = asyncio.Lock()

    def set_dependencies(
        self,
        vikingdb: VikingDBManager,
        viking_fs: VikingFS,
        resource_processor: ResourceProcessor,
        skill_processor: SkillProcessor,
        watch_scheduler: Optional["WatchScheduler"] = None,
        workspace_path: Optional[str] = None,
    ) -> None:
        """Set dependencies (for deferred initialization)."""
        self._vikingdb = vikingdb
        self._viking_fs = viking_fs
        self._resource_processor = resource_processor
        self._skill_processor = skill_processor
        self._watch_scheduler = watch_scheduler
        if workspace_path:
            self._document_registry = KnowledgeDocumentRegistry(workspace_path)

    def _get_watch_manager(self) -> Optional["WatchManager"]:
        if not self._watch_scheduler:
            return None
        return self._watch_scheduler.watch_manager

    def _sanitize_watch_processor_kwargs(self, processor_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        sanitized: Dict[str, Any] = {}
        for key, value in processor_kwargs.items():
            try:
                json.dumps(value, ensure_ascii=False)
            except TypeError:
                continue
            sanitized[key] = value
        return sanitized

    def _ensure_initialized(self) -> None:
        """Ensure all dependencies are initialized."""
        if not self._resource_processor:
            raise NotInitializedError("ResourceProcessor")
        if not self._skill_processor:
            raise NotInitializedError("SkillProcessor")
        if not self._viking_fs:
            raise NotInitializedError("VikingFS")

    def _derive_default_resource_uri(
        self,
        *,
        path: str,
        source_ref: Optional[str],
        folder_path: str,
    ) -> str:
        """Derive a stable resource URI from virtual folders and the source name."""
        segments: List[str] = []
        for part in str(folder_path or "").replace("\\", "/").split("/"):
            segment = part.strip()
            if not segment or segment == ".":
                continue
            if segment == "..":
                raise InvalidArgumentError("folder_path cannot contain '..'.")
            segments.append(VikingURI.sanitize_segment(segment))

        reference = source_ref or path
        repo_slug = parse_code_hosting_url(reference) or parse_code_hosting_url(path)
        if repo_slug:
            segments.extend(
                VikingURI.sanitize_segment(part)
                for part in repo_slug.split("/")
                if part
            )
            return VikingURI.build("resources", *segments)

        parsed = urlparse(reference)
        if reference.startswith("git@"):
            name = Path(reference.split(":", 1)[-1]).name
        elif parsed.scheme:
            name = Path(parsed.path or "").name or parsed.netloc or reference
        else:
            name = Path(reference).name or reference

        source_path = Path(path)
        is_dir = source_path.exists() and source_path.is_dir()
        leaf_name = name if is_dir else (Path(name).stem or name)
        segments.append(VikingURI.sanitize_segment(leaf_name))
        return VikingURI.build("resources", *segments)

    async def add_resource(
        self,
        path: str,
        ctx: RequestContext,
        to: Optional[str] = None,
        parent: Optional[str] = None,
        folder_path: Optional[str] = None,
        reason: str = "",
        instruction: str = "",
        wait: bool = False,
        timeout: Optional[float] = None,
        build_index: bool = True,
        summarize: bool = False,
        watch_interval: float = 0,
        skip_watch_management: bool = False,
        allow_local_path_resolution: bool = True,
        register_document: bool = True,
        source_ref: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Add resource to OpenViking (only supports resources scope).

        Args:
            path: Resource path (local file or URL)
            to: Target URI (e.g., "viking://resources/my_resource")
            parent: Parent URI under which the resource will be stored
            reason: Reason for adding the resource
            instruction: Processing instruction for semantic extraction
            wait: Whether to wait for semantic extraction and vectorization to complete
            timeout: Wait timeout in seconds
            build_index: Whether to build vector index immediately (default: True)
            summarize: Whether to generate summary (default: False)
            watch_interval: Watch interval in minutes for automatic resource monitoring.
                - watch_interval > 0: Creates or updates a watch task. The resource will be
                  automatically re-processed at the specified interval by the scheduler.
                - watch_interval = 0: No watch task is created. If a watch task exists for
                  this resource, it will be cancelled (deactivated).
                - watch_interval < 0: Same as watch_interval = 0, cancels any existing watch task.
                Default is 0 (no monitoring).

                Note: If the target URI already has an active watch task, a ConflictError will be
                raised. You must first cancel the existing watch (set watch_interval <= 0) before
                creating a new one.
            skip_watch_management: If True, skip watch task management (used by scheduler to
                avoid recursive watch task creation during scheduled execution)
            **kwargs: Extra options forwarded to the parser chain

        Returns:
            Processing result containing 'root_uri' and other metadata

        Raises:
            ConflictError: If the target URI already has an active watch task
            InvalidArgumentError: If the URI scope is not 'resources'
        """
        self._ensure_initialized()
        request_start = time.perf_counter()
        telemetry = get_current_telemetry()
        telemetry_id = register_wait_telemetry(wait)
        watch_manager = self._get_watch_manager()
        effective_to = to
        if effective_to is None and parent is None and folder_path is not None:
            effective_to = self._derive_default_resource_uri(
                path=path,
                source_ref=source_ref,
                folder_path=folder_path,
            )
        watch_enabled = bool(
            watch_manager and effective_to and not skip_watch_management and watch_interval > 0
        )

        telemetry.set("resource.flags.wait", wait)
        telemetry.set("resource.flags.build_index", build_index)
        telemetry.set("resource.flags.summarize", summarize)
        telemetry.set("resource.flags.watch_enabled", watch_enabled)

        try:
            registered_document: Dict[str, Any] = {}

            async def _register_document_after_finalize(finalize_result: Dict[str, Any]) -> Dict[str, Any]:
                nonlocal registered_document
                if not register_document or finalize_result.get("status") != "success":
                    return {}
                processing_requested = bool(finalize_result.get("processing_requested"))
                processing_status = (
                    DOCUMENT_PROCESSING_STATUS_PROCESSING
                    if processing_requested
                    else DOCUMENT_PROCESSING_STATUS_READY
                )
                registered_document = await self._register_knowledge_document(
                    ctx=ctx,
                    path=path,
                    source_ref=source_ref,
                    folder_path=folder_path,
                    result=finalize_result,
                    reason=reason,
                    instruction=instruction,
                    processing_status=processing_status,
                )
                return registered_document

            # add_resource only supports resources scope
            if effective_to and effective_to.startswith("viking://"):
                parsed = VikingURI(effective_to)
                if parsed.scope != "resources":
                    raise InvalidArgumentError(
                        f"add_resource only supports resources scope, use dedicated interface to add {parsed.scope} content"
                    )
            if parent and parent.startswith("viking://"):
                parsed = VikingURI(parent)
                if parsed.scope != "resources":
                    raise InvalidArgumentError(
                        f"add_resource only supports resources scope, use dedicated interface to add {parsed.scope} content"
                    )
            if watch_manager and not skip_watch_management and watch_interval > 0 and not effective_to:
                raise InvalidArgumentError(
                    "watch_interval > 0 requires 'to' to be specified (target URI to watch)"
                )

            result = await self._resource_processor.process_resource(
                path=path,
                ctx=ctx,
                reason=reason,
                instruction=instruction,
                scope="resources",
                to=effective_to,
                parent=parent,
                build_index=build_index,
                summarize=summarize,
                allow_local_path_resolution=allow_local_path_resolution,
                post_finalize_hook=_register_document_after_finalize,
                **kwargs,
            )

            if registered_document:
                result["document_id"] = registered_document["document_id"]
                result["knowledge_document"] = registered_document

            if result.get("document_id"):
                if result.get("processing_requested") and not result.get("processing_enqueued"):
                    await self._update_document_processing_status(
                        ctx=ctx,
                        document_id=result["document_id"],
                        processing_status=DOCUMENT_PROCESSING_STATUS_FAILED,
                        processing_error=result.get("processing_error") or "文档处理任务提交失败",
                    )
                elif not result.get("processing_requested"):
                    await self._update_document_processing_status(
                        ctx=ctx,
                        document_id=result["document_id"],
                        processing_status=DOCUMENT_PROCESSING_STATUS_READY,
                    )

            if wait:
                qm = get_queue_manager()
                wait_start = time.perf_counter()
                try:
                    with telemetry.measure("resource.wait"):
                        status = await qm.wait_complete(timeout=timeout)
                except TimeoutError as exc:
                    telemetry.set_error(
                        "resource_service.wait_complete",
                        "DEADLINE_EXCEEDED",
                        str(exc),
                    )
                    raise DeadlineExceededError("queue processing", timeout) from exc
                queue_wait_duration_ms = round((time.perf_counter() - wait_start) * 1000, 3)
                result["queue_status"] = build_queue_status_payload(status)
                record_resource_wait_metrics(
                    telemetry_id=telemetry_id,
                    queue_status=status,
                    root_uri=result.get("root_uri"),
                )
                telemetry.set("queue.wait.duration_ms", queue_wait_duration_ms)
                if result.get("document_id") and result.get("processing_requested"):
                    current_document = await self._get_document_record(
                        ctx=ctx,
                        document_id=result["document_id"],
                    )
                    if current_document and current_document.processing_status != DOCUMENT_PROCESSING_STATUS_FAILED:
                        await self._update_document_processing_status(
                            ctx=ctx,
                            document_id=result["document_id"],
                            processing_status=DOCUMENT_PROCESSING_STATUS_READY,
                        )
            if watch_manager and effective_to and not skip_watch_management:
                with telemetry.measure("resource.watch"):
                    if watch_interval > 0:
                        try:
                            processor_kwargs = self._sanitize_watch_processor_kwargs(kwargs)
                            await self._handle_watch_task_creation(
                                path=path,
                                to_uri=effective_to,
                                parent_uri=parent,
                                reason=reason,
                                instruction=instruction,
                                watch_interval=watch_interval,
                                build_index=build_index,
                                summarize=summarize,
                                processor_kwargs=processor_kwargs,
                                ctx=ctx,
                            )
                        except ConflictError:
                            raise
                        except Exception as e:
                            logger.warning(
                                f"[ResourceService] Failed to create watch task for {effective_to}: {e}"
                            )
                    else:
                        try:
                            await self._handle_watch_task_cancellation(
                                to_uri=effective_to,
                                ctx=ctx,
                            )
                        except Exception as e:
                            logger.warning(
                                f"[ResourceService] Failed to cancel watch task for {effective_to}: {e}"
                            )
            return result
        except Exception as exc:
            telemetry.set_error(
                "resource_service.add_resource",
                type(exc).__name__,
                str(exc),
            )
            raise
        finally:
            telemetry.set(
                "resource.request.duration_ms",
                round((time.perf_counter() - request_start) * 1000, 3),
            )
            unregister_wait_telemetry(telemetry_id)

    async def list_documents(self, ctx: RequestContext) -> List[Dict[str, Any]]:
        """List user-managed knowledge documents for the current account."""
        self._ensure_initialized()
        if not self._document_registry:
            return []

        async with self._document_lock:
            records = await asyncio.to_thread(
                self._document_registry.list_documents,
                ctx.account_id,
            )
        return [record.to_public_dict() for record in records]

    async def list_folders(self, ctx: RequestContext) -> List[Dict[str, Any]]:
        """List user-managed virtual folders for the current account."""
        self._ensure_initialized()
        if not self._document_registry:
            return []

        async with self._document_lock:
            records = await asyncio.to_thread(
                self._document_registry.list_folders,
                ctx.account_id,
            )
        return [record.to_public_dict() for record in records]

    async def create_folder(
        self,
        *,
        name: str,
        parent_path: str,
        ctx: RequestContext,
    ) -> Dict[str, Any]:
        """Create a virtual folder for organizing knowledge documents."""
        self._ensure_initialized()
        if not self._document_registry:
            raise NotFoundError(ctx.account_id, "knowledge document registry")

        async with self._document_lock:
            record = await asyncio.to_thread(
                lambda: self._document_registry.create_folder(
                    account_id=ctx.account_id,
                    name=name,
                    parent_path=parent_path,
                )
            )
        return record.to_public_dict()

    async def delete_document(self, document_id: str, ctx: RequestContext) -> Dict[str, Any]:
        """Delete a knowledge document and its synchronized resource tree."""
        self._ensure_initialized()
        if not self._document_registry:
            raise NotFoundError(document_id, "knowledge document")

        async with self._document_lock:
            record = await asyncio.to_thread(
                self._document_registry.get_document,
                ctx.account_id,
                document_id,
            )
        if not record:
            raise NotFoundError(document_id, "knowledge document")

        if record.resource_root_uri and self._viking_fs:
            await self._viking_fs.rm(record.resource_root_uri, recursive=True, ctx=ctx)

        async with self._document_lock:
            await asyncio.to_thread(
                self._document_registry.delete_document,
                ctx.account_id,
                document_id,
            )

        return {
            "document_id": document_id,
            "display_name": record.display_name,
            "resource_root_uri": record.resource_root_uri,
        }

    async def delete_folder(self, folder_id: str, ctx: RequestContext) -> Dict[str, Any]:
        """Delete an empty virtual folder."""
        self._ensure_initialized()
        if not self._document_registry:
            raise NotFoundError(folder_id, "knowledge folder")

        async with self._document_lock:
            record = await asyncio.to_thread(
                self._document_registry.get_folder,
                ctx.account_id,
                folder_id,
            )
        if not record:
            raise NotFoundError(folder_id, "knowledge folder")

        async with self._document_lock:
            await asyncio.to_thread(
                self._document_registry.delete_folder,
                ctx.account_id,
                folder_id,
            )

        return {
            "folder_id": folder_id,
            "name": record.name,
            "path": record.path,
        }

    async def move_document(
        self,
        *,
        document_id: str,
        target_folder_path: str,
        ctx: RequestContext,
    ) -> Dict[str, Any]:
        """Move a knowledge document to another virtual folder."""
        self._ensure_initialized()
        if not self._document_registry:
            raise NotFoundError(document_id, "knowledge document")

        async with self._document_lock:
            plan = await asyncio.to_thread(
                self._document_registry.plan_move_document,
                account_id=ctx.account_id,
                document_id=document_id,
                target_folder_path=target_folder_path,
            )
            moved_resource = await self._move_resource_if_needed(
                old_uri=plan.source_document.resource_root_uri,
                new_uri=plan.updated_document.resource_root_uri,
                ctx=ctx,
            )
            try:
                updated_record = await asyncio.to_thread(
                    self._document_registry.apply_document_move,
                    plan=plan,
                )
            except Exception:
                await self._rollback_resource_move(
                    moved_resource=moved_resource,
                    old_uri=plan.source_document.resource_root_uri,
                    new_uri=plan.updated_document.resource_root_uri,
                    ctx=ctx,
                )
                raise

        return {
            **updated_record.to_public_dict(),
            "previous_folder_path": plan.source_document.folder_path,
            "previous_resource_root_uri": plan.source_document.resource_root_uri,
        }

    async def rename_folder(
        self,
        *,
        folder_id: str,
        new_name: str,
        ctx: RequestContext,
    ) -> Dict[str, Any]:
        """Rename a virtual folder and synchronize descendant resource URIs."""
        self._ensure_initialized()
        if not self._document_registry:
            raise NotFoundError(folder_id, "knowledge folder")

        async with self._document_lock:
            plan = await asyncio.to_thread(
                self._document_registry.plan_rename_folder,
                account_id=ctx.account_id,
                folder_id=folder_id,
                new_name=new_name,
            )
            moved_resource = await self._move_resource_if_needed(
                old_uri=plan.old_resource_prefix,
                new_uri=plan.new_resource_prefix,
                ctx=ctx,
            )
            try:
                renamed_folder = await asyncio.to_thread(
                    self._document_registry.apply_folder_rename,
                    plan=plan,
                )
            except Exception:
                await self._rollback_resource_move(
                    moved_resource=moved_resource,
                    old_uri=plan.old_resource_prefix,
                    new_uri=plan.new_resource_prefix,
                    ctx=ctx,
                )
                raise

        return {
            **renamed_folder.to_public_dict(),
            "previous_path": plan.source_folder.path,
            "moved_document_count": len(plan.updated_documents),
            "moved_folder_count": max(len(plan.updated_folders) - 1, 0),
        }

    async def cleanup_orphan_resource_vectors(
        self,
        *,
        ctx: RequestContext,
        dry_run: bool = True,
        batch_size: int = 200,
        preview_limit: int = 50,
    ) -> Dict[str, Any]:
        """Scan resource vectors and optionally delete URIs whose files no longer exist."""
        self._ensure_initialized()
        if batch_size <= 0:
            raise InvalidArgumentError("batch_size must be greater than 0.")
        if preview_limit < 0:
            raise InvalidArgumentError("preview_limit cannot be negative.")
        if not self._viking_fs:
            raise NotInitializedError("VikingFS")

        vector_store = self._viking_fs._get_vector_store()
        if not vector_store:
            return {
                "dry_run": dry_run,
                "checked_vector_record_count": 0,
                "checked_resource_uri_count": 0,
                "orphan_uri_count": 0,
                "orphan_vector_record_count": 0,
                "deleted_uri_count": 0,
                "deleted_vector_record_count": 0,
                "orphan_uris": [],
                "orphan_uris_truncated": False,
            }

        cursor: Optional[str] = None
        checked_vector_record_count = 0
        resource_uri_counts: Dict[str, int] = {}

        while True:
            records, cursor = await vector_store.scroll(
                filter=PathScope("uri", "viking://resources", depth=-1),
                limit=batch_size,
                cursor=cursor,
                output_fields=["uri"],
                ctx=ctx,
            )
            checked_vector_record_count += len(records)
            for record in records:
                uri = str(record.get("uri") or "")
                if not uri:
                    continue
                if uri != "viking://resources" and not uri.startswith("viking://resources/"):
                    continue
                resource_uri_counts[uri] = resource_uri_counts.get(uri, 0) + 1

            if not cursor:
                break

        orphan_uris: List[str] = []
        orphan_vector_record_count = 0
        for uri in sorted(resource_uri_counts):
            if await self._viking_fs.exists(uri, ctx=ctx):
                continue
            orphan_uris.append(uri)
            orphan_vector_record_count += resource_uri_counts[uri]

        deleted_uri_count = 0
        deleted_vector_record_count = 0
        if orphan_uris and not dry_run:
            await vector_store.delete_uris(ctx, orphan_uris)
            deleted_uri_count = len(orphan_uris)
            deleted_vector_record_count = orphan_vector_record_count

        preview = orphan_uris[:preview_limit] if preview_limit else []
        return {
            "dry_run": dry_run,
            "checked_vector_record_count": checked_vector_record_count,
            "checked_resource_uri_count": len(resource_uri_counts),
            "orphan_uri_count": len(orphan_uris),
            "orphan_vector_record_count": orphan_vector_record_count,
            "deleted_uri_count": deleted_uri_count,
            "deleted_vector_record_count": deleted_vector_record_count,
            "orphan_uris": preview,
            "orphan_uris_truncated": len(orphan_uris) > len(preview),
        }

    async def _move_resource_if_needed(
        self,
        *,
        old_uri: str,
        new_uri: str,
        ctx: RequestContext,
    ) -> bool:
        if not self._viking_fs or old_uri == new_uri:
            return False

        old_exists = await self._viking_fs.exists(old_uri, ctx=ctx)
        if not old_exists:
            return False

        if await self._viking_fs.exists(new_uri, ctx=ctx):
            raise ConflictError(
                "Target resource URI already exists.",
                resource=new_uri,
            )

        await self._viking_fs.mv(old_uri, new_uri, ctx=ctx)
        return True

    async def _rollback_resource_move(
        self,
        *,
        moved_resource: bool,
        old_uri: str,
        new_uri: str,
        ctx: RequestContext,
    ) -> None:
        if not moved_resource or not self._viking_fs:
            return
        try:
            await self._viking_fs.mv(new_uri, old_uri, ctx=ctx)
        except Exception as rollback_exc:
            logger.error(
                "[ResourceService] Failed to rollback resource move %s -> %s: %s",
                new_uri,
                old_uri,
                rollback_exc,
            )

    async def _register_knowledge_document(
        self,
        *,
        ctx: RequestContext,
        path: str,
        source_ref: Optional[str],
        folder_path: Optional[str],
        result: Dict[str, Any],
        reason: str,
        instruction: str,
        processing_status: str,
    ) -> Dict[str, Any]:
        if not self._document_registry:
            return {}

        async with self._document_lock:
            record = await asyncio.to_thread(
                lambda: self._document_registry.upsert_document(
                    account_id=ctx.account_id,
                    source_path=path,
                    source_ref=source_ref,
                    resource_root_uri=str(result["root_uri"]),
                    folder_path=folder_path,
                    source_format=result.get("source_format"),
                    reason=reason,
                    instruction=instruction,
                    processing_status=processing_status,
                    meta=result.get("meta") or {},
                )
            )
        return record.to_public_dict()

    async def _update_document_processing_status(
        self,
        *,
        ctx: RequestContext,
        document_id: str,
        processing_status: str,
        processing_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self._document_registry:
            return {}

        async with self._document_lock:
            record = await asyncio.to_thread(
                self._document_registry.update_document_processing_status,
                ctx.account_id,
                document_id,
                processing_status=processing_status,
                processing_error=processing_error,
            )
        return record.to_public_dict() if record else {}

    async def _get_document_record(
        self,
        *,
        ctx: RequestContext,
        document_id: str,
    ):
        if not self._document_registry:
            return None

        async with self._document_lock:
            return await asyncio.to_thread(
                self._document_registry.get_document,
                ctx.account_id,
                document_id,
            )

    async def _handle_watch_task_creation(
        self,
        path: str,
        to_uri: str,
        parent_uri: Optional[str],
        reason: str,
        instruction: str,
        watch_interval: float,
        build_index: bool,
        summarize: bool,
        processor_kwargs: Dict[str, Any],
        ctx: RequestContext,
    ) -> None:
        """Handle creation or update of watch task.

        Args:
            path: Resource path to monitor
            to_uri: Target URI
            parent_uri: Parent URI
            reason: Reason for monitoring
            instruction: Monitoring instruction
            watch_interval: Monitoring interval in minutes
            ctx: Request context with user identity

        Raises:
            ConflictError: If target URI is already used by another active task
        """
        watch_manager = self._get_watch_manager()
        if not watch_manager:
            return

        existing_task = await watch_manager.get_task_by_uri(
            to_uri=to_uri,
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
            role=ctx.role.value,
            agent_id=ctx.user.agent_id,
        )
        if existing_task:
            if existing_task.is_active:
                raise ConflictError(
                    f"Target URI '{to_uri}' is already being monitored by task {existing_task.task_id}. "
                    f"Please cancel the existing task first.",
                    resource=to_uri,
                )
            await watch_manager.update_task(
                task_id=existing_task.task_id,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
                role=ctx.role.value,
                agent_id=ctx.user.agent_id,
                path=path,
                to_uri=to_uri,
                parent_uri=parent_uri,
                reason=reason,
                instruction=instruction,
                watch_interval=watch_interval,
                build_index=build_index,
                summarize=summarize,
                processor_kwargs=processor_kwargs,
                is_active=True,
            )
            logger.info(
                f"[ResourceService] Reactivated and updated watch task {existing_task.task_id} for {to_uri}"
            )
        else:
            task = await watch_manager.create_task(
                path=path,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
                agent_id=ctx.user.agent_id,
                original_role=ctx.role.value,
                to_uri=to_uri,
                parent_uri=parent_uri,
                reason=reason,
                instruction=instruction,
                watch_interval=watch_interval,
                build_index=build_index,
                summarize=summarize,
                processor_kwargs=processor_kwargs,
            )
            logger.info(f"[ResourceService] Created watch task {task.task_id} for {to_uri}")

    async def _handle_watch_task_cancellation(self, to_uri: str, ctx: RequestContext) -> None:
        """Handle cancellation of watch task.

        Args:
            to_uri: Target URI to cancel watch for
            ctx: Request context with user identity
        """
        watch_manager = self._get_watch_manager()
        if not watch_manager:
            return

        existing_task = await watch_manager.get_task_by_uri(
            to_uri=to_uri,
            account_id=ctx.account_id,
            user_id=ctx.user.user_id,
            role=ctx.role.value,
            agent_id=ctx.user.agent_id,
        )
        if existing_task:
            await watch_manager.update_task(
                task_id=existing_task.task_id,
                account_id=ctx.account_id,
                user_id=ctx.user.user_id,
                role=ctx.role.value,
                agent_id=ctx.user.agent_id,
                is_active=False,
            )
            logger.info(
                f"[ResourceService] Deactivated watch task {existing_task.task_id} for {to_uri}"
            )

    async def add_skill(
        self,
        data: Any,
        ctx: RequestContext,
        wait: bool = False,
        timeout: Optional[float] = None,
        allow_local_path_resolution: bool = True,
    ) -> Dict[str, Any]:
        """Add skill to OpenViking.

        Args:
            data: Skill data (directory path, file path, string, or dict)
            wait: Whether to wait for vectorization to complete
            timeout: Wait timeout in seconds

        Returns:
            Processing result
        """
        self._ensure_initialized()

        result = await self._skill_processor.process_skill(
            data=data,
            viking_fs=self._viking_fs,
            ctx=ctx,
            allow_local_path_resolution=allow_local_path_resolution,
        )

        if wait:
            qm = get_queue_manager()
            wait_start = time.perf_counter()
            try:
                status = await qm.wait_complete(timeout=timeout)
            except TimeoutError as exc:
                get_current_telemetry().set_error(
                    "resource_service.wait_complete",
                    "DEADLINE_EXCEEDED",
                    str(exc),
                )
                raise DeadlineExceededError("queue processing", timeout) from exc
            get_current_telemetry().set(
                "queue.wait.duration_ms",
                round((time.perf_counter() - wait_start) * 1000, 3),
            )
            result["queue_status"] = build_queue_status_payload(status)

        return result

    async def build_index(
        self, resource_uris: List[str], ctx: RequestContext, **kwargs
    ) -> Dict[str, Any]:
        """Manually trigger index building.

        Args:
            resource_uris: List of resource URIs to index.
            ctx: Request context.

        Returns:
            Processing result
        """
        self._ensure_initialized()
        return await self._resource_processor.build_index(resource_uris, ctx, **kwargs)

    async def summarize(
        self, resource_uris: List[str], ctx: RequestContext, **kwargs
    ) -> Dict[str, Any]:
        """Manually trigger summarization.

        Args:
            resource_uris: List of resource URIs to summarize.
            ctx: Request context.

        Returns:
            Processing result
        """
        self._ensure_initialized()
        return await self._resource_processor.summarize(resource_uris, ctx, **kwargs)

    async def wait_processed(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Wait for all queued processing to complete.

        Args:
            timeout: Wait timeout in seconds

        Returns:
            Queue status
        """
        qm = get_queue_manager()
        try:
            status = await qm.wait_complete(timeout=timeout)
        except TimeoutError as exc:
            raise DeadlineExceededError("queue processing", timeout) from exc
        return {
            name: {
                "processed": s.processed,
                "error_count": s.error_count,
                "errors": [{"message": e.message} for e in s.errors],
            }
            for name, s in status.items()
        }
