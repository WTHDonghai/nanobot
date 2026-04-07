# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Resource endpoints for OpenViking HTTP Server."""

import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, ConfigDict, model_validator

from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.local_input_guard import (
    require_remote_resource_source,
    resolve_uploaded_temp_file_id,
)
from openviking.server.models import Response
from openviking.server.telemetry import run_operation
from openviking.telemetry import TelemetryRequest
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.utils.config.open_viking_config import get_openviking_config

router = APIRouter(prefix="/api/v1", tags=["resources"])


class AddResourceRequest(BaseModel):
    """Request model for add_resource.

    Attributes:
        path: Remote resource source such as an HTTP(S) URL or repository URL.
            Either path or temp_file_id must be provided.
        temp_file_id: Temporary upload id returned by /api/v1/resources/temp_upload.
            Either path or temp_file_id must be provided.
        to: Target URI for the resource (e.g., "viking://resources/my_resource").
            If not specified, an auto-generated URI will be used.
        parent: Parent URI under which the resource will be stored.
            Cannot be used together with 'to'.
        folder_path: Virtual folder path used by the admin UI to organize original documents.
            This does not change the internal resource processing tree.
        reason: Reason for adding the resource. Used for documentation and monitoring.
        instruction: Processing instruction for semantic extraction.
            Provides hints for how the resource should be processed.
        wait: Whether to wait for semantic extraction and vectorization to complete.
            Default is False (async processing).
        timeout: Timeout in seconds when wait=True. None means no timeout.
        strict: Whether to use strict mode for processing. Default is True.
        ignore_dirs: Comma-separated list of directory names to ignore during parsing.
        include: Glob pattern for files to include during parsing.
        exclude: Glob pattern for files to exclude during parsing.
        directly_upload_media: Whether to directly upload media files. Default is True.
        preserve_structure: Whether to preserve directory structure when adding directories.
        watch_interval: Watch interval in minutes for automatic resource monitoring.
            - watch_interval > 0: Creates or updates a watch task. The resource will be
              automatically re-processed at the specified interval.
            - watch_interval = 0: No watch task is created. If a watch task exists for
              this resource, it will be cancelled (deactivated).
            - watch_interval < 0: Same as watch_interval = 0, cancels any existing watch task.
            Default is 0 (no monitoring).

            Note: If the target URI already has an active watch task, a ConflictError will be
            raised. You must first cancel the existing watch (set watch_interval <= 0) before
            creating a new one.
    """

    model_config = ConfigDict(extra="forbid")

    path: Optional[str] = None
    temp_file_id: Optional[str] = None
    to: Optional[str] = None
    parent: Optional[str] = None
    folder_path: Optional[str] = None
    reason: str = ""
    instruction: str = ""
    wait: bool = False
    timeout: Optional[float] = None
    strict: bool = True
    ignore_dirs: Optional[str] = None
    include: Optional[str] = None
    exclude: Optional[str] = None
    directly_upload_media: bool = True
    preserve_structure: Optional[bool] = None
    telemetry: TelemetryRequest = False
    watch_interval: float = 0

    @model_validator(mode="after")
    def check_path_or_temp_file_id(self):
        if not self.path and not self.temp_file_id:
            raise ValueError("Either 'path' or 'temp_file_id' must be provided")
        return self


class AddSkillRequest(BaseModel):
    """Request model for add_skill.

    Attributes:
        data: Inline skill content or structured skill data. HTTP requests do not treat
            string values as host filesystem paths.
        temp_file_id: Temporary upload id returned by /api/v1/resources/temp_upload.
        wait: Whether to wait for skill processing to complete.
        timeout: Timeout in seconds when wait=True.
    """

    model_config = ConfigDict(extra="forbid")

    data: Any = None
    temp_file_id: Optional[str] = None
    wait: bool = False
    timeout: Optional[float] = None
    telemetry: TelemetryRequest = False

    @model_validator(mode="after")
    def check_data_or_temp_file_id(self):
        if self.data is None and not self.temp_file_id:
            raise ValueError("Either 'data' or 'temp_file_id' must be provided")
        return self


class CreateKnowledgeFolderRequest(BaseModel):
    """Request model for creating a virtual knowledge folder."""

    model_config = ConfigDict(extra="forbid")

    name: str
    parent_path: str = ""


def _cleanup_temp_files(temp_dir: Path, max_age_hours: int = 1):
    """Clean up temporary files older than max_age_hours."""
    if not temp_dir.exists():
        return

    now = time.time()
    max_age_seconds = max_age_hours * 3600

    for file_path in temp_dir.iterdir():
        file_age = now - file_path.stat().st_mtime
        if file_age <= max_age_seconds:
            continue
        if file_path.is_dir():
            shutil.rmtree(file_path, ignore_errors=True)
        elif file_path.is_file():
            file_path.unlink(missing_ok=True)


@router.post("/resources/temp_upload")
async def temp_upload(
    file: UploadFile = File(...),
    telemetry: bool = Form(False),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Upload a temporary file for add_resource or import_ovpack."""

    async def _upload() -> dict[str, str]:
        config = get_openviking_config()
        temp_dir = config.storage.get_upload_temp_dir()

        # Clean up old temporary files
        _cleanup_temp_files(temp_dir)

        # Save the uploaded file
        original_filename = Path(file.filename or "upload.bin").name
        if not original_filename or original_filename in {".", ".."}:
            file_ext = Path(file.filename or "").suffix or ".bin"
            original_filename = f"upload{file_ext}"

        temp_entry_id = f"upload_{uuid.uuid4().hex}"
        temp_entry_dir = temp_dir / temp_entry_id
        temp_entry_dir.mkdir(parents=True, exist_ok=True)
        temp_file_path = temp_entry_dir / original_filename

        with open(temp_file_path, "wb") as f:
            f.write(await file.read())

        return {
            "temp_file_id": temp_entry_id,
            "original_filename": original_filename,
        }

    execution = await run_operation(
        operation="resources.temp_upload",
        telemetry=telemetry,
        fn=_upload,
    )
    return Response(
        status="ok",
        result=execution.result,
        telemetry=execution.telemetry,
    ).model_dump(exclude_none=True)


@router.get("/knowledge-documents")
async def list_knowledge_documents(
    _ctx: RequestContext = Depends(get_request_context),
):
    """List user-managed knowledge documents."""
    service = get_service()
    result = await service.resources.list_documents(_ctx)
    return Response(status="ok", result=result).model_dump(exclude_none=True)


@router.get("/knowledge-folders")
async def list_knowledge_folders(
    _ctx: RequestContext = Depends(get_request_context),
):
    """List user-managed virtual folders."""
    service = get_service()
    result = await service.resources.list_folders(_ctx)
    return Response(status="ok", result=result).model_dump(exclude_none=True)


@router.post("/knowledge-folders")
async def create_knowledge_folder(
    request: CreateKnowledgeFolderRequest,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Create a virtual folder for organizing knowledge documents."""
    service = get_service()
    result = await service.resources.create_folder(
        name=request.name,
        parent_path=request.parent_path,
        ctx=_ctx,
    )
    return Response(status="ok", result=result).model_dump(exclude_none=True)


@router.delete("/knowledge-documents/{document_id}")
async def delete_knowledge_document(
    document_id: str,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Delete a knowledge document and its synchronized resource tree."""
    service = get_service()
    result = await service.resources.delete_document(document_id, _ctx)
    return Response(status="ok", result=result).model_dump(exclude_none=True)


@router.delete("/knowledge-folders/{folder_id}")
async def delete_knowledge_folder(
    folder_id: str,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Delete an empty virtual folder."""
    service = get_service()
    result = await service.resources.delete_folder(folder_id, _ctx)
    return Response(status="ok", result=result).model_dump(exclude_none=True)


@router.post("/resources")
async def add_resource(
    request: AddResourceRequest,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Add resource to OpenViking."""
    service = get_service()
    if request.to and request.parent:
        raise InvalidArgumentError("Cannot specify both 'to' and 'parent' at the same time.")

    upload_temp_dir = get_openviking_config().storage.get_upload_temp_dir()
    path = request.path
    source_ref = request.path
    allow_local_path_resolution = False
    if request.temp_file_id:
        path = resolve_uploaded_temp_file_id(request.temp_file_id, upload_temp_dir)
        source_ref = Path(path).name
        allow_local_path_resolution = True
    elif path is not None:
        path = require_remote_resource_source(path)
    if path is None:
        raise InvalidArgumentError("Either 'path' or 'temp_file_id' must be provided.")

    kwargs = {
        "strict": request.strict,
        "ignore_dirs": request.ignore_dirs,
        "include": request.include,
        "exclude": request.exclude,
        "directly_upload_media": request.directly_upload_media,
        "watch_interval": request.watch_interval,
    }
    if request.preserve_structure is not None:
        kwargs["preserve_structure"] = request.preserve_structure

    execution = await run_operation(
        operation="resources.add_resource",
        telemetry=request.telemetry,
        fn=lambda: service.resources.add_resource(
            path=path,
            ctx=_ctx,
            to=request.to,
            parent=request.parent,
            folder_path=request.folder_path,
            reason=request.reason,
            instruction=request.instruction,
            wait=request.wait,
            timeout=request.timeout,
            allow_local_path_resolution=allow_local_path_resolution,
            source_ref=source_ref,
            **kwargs,
        ),
    )
    return Response(
        status="ok",
        result=execution.result,
        telemetry=execution.telemetry,
    ).model_dump(exclude_none=True)


@router.post("/skills")
async def add_skill(
    request: AddSkillRequest,
    _ctx: RequestContext = Depends(get_request_context),
):
    """Add skill to OpenViking."""
    service = get_service()
    upload_temp_dir = get_openviking_config().storage.get_upload_temp_dir()
    data = request.data
    allow_local_path_resolution = False
    if request.temp_file_id:
        data = resolve_uploaded_temp_file_id(request.temp_file_id, upload_temp_dir)
        allow_local_path_resolution = True

    execution = await run_operation(
        operation="resources.add_skill",
        telemetry=request.telemetry,
        fn=lambda: service.resources.add_skill(
            data=data,
            ctx=_ctx,
            wait=request.wait,
            timeout=request.timeout,
            allow_local_path_resolution=allow_local_path_resolution,
        ),
    )
    return Response(
        status="ok",
        result=execution.result,
        telemetry=execution.telemetry,
    ).model_dump(exclude_none=True)
