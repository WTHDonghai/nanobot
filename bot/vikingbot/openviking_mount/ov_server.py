import asyncio
import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

import openviking as ov
from vikingbot.config.loader import load_config
from vikingbot.openviking_mount.user_apikey_manager import UserApiKeyManager
from vikingbot.utils.helpers import get_images_path

viking_resource_prefix = "viking://resources/"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff"}
READABLE_TEXT_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd", ".txt"}
WORD_IMAGE_PLACEHOLDER_RE = re.compile(r"!\[([^\]]*)\]\(ov-asset://([^)]+)\)")


class VikingClient:
    def __init__(self, agent_id: Optional[str] = None):
        config = load_config()
        openviking_config = config.ov_server
        self.openviking_config = openviking_config
        self.ov_path = config.ov_data_path
        if openviking_config.mode == "local":
            self.client = ov.AsyncHTTPClient(url=openviking_config.server_url)
            self.agent_id = "default"
            self.account_id = "default"
            self.admin_user_id = "default"
            self._apikey_manager = None
        else:
            if agent_id and "#" in agent_id:
                agent_id = agent_id.split("#", 1)[0]
            self.client = ov.AsyncHTTPClient(
                url=openviking_config.server_url,
                api_key=openviking_config.root_api_key,
                account=openviking_config.account_id,
                user=openviking_config.admin_user_id,
                agent_id=agent_id,
            )
            self.agent_id = agent_id
            self.account_id = openviking_config.account_id
            self.admin_user_id = openviking_config.admin_user_id
            self._apikey_manager = None
            if self.ov_path:
                self._apikey_manager = UserApiKeyManager(
                    ov_path=self.ov_path,
                    server_url=openviking_config.server_url,
                    account_id=openviking_config.account_id,
                )
        self.mode = openviking_config.mode

    async def _initialize(self):
        """Initialize the client (must be called after construction)"""
        await self.client.initialize()

        # 检查并初始化 admin_user_id（如果配置了）
        if self.mode == "remote" and self.admin_user_id:
            user_exists = await self._check_user_exists(self.admin_user_id)
            if not user_exists:
                await self._initialize_user(self.admin_user_id, role="admin")

    @classmethod
    async def create(cls, agent_id: Optional[str] = None):
        """Factory method to create and initialize a VikingClient instance.

        Args:
            agent_id: The agent ID to use
        """
        instance = cls(agent_id)
        await instance._initialize()
        return instance

    def _matched_context_to_dict(self, matched_context: Any) -> Dict[str, Any]:
        """将 MatchedContext 对象转换为字典"""
        return {
            "uri": getattr(matched_context, "uri", ""),
            "context_type": str(getattr(matched_context, "context_type", "")),
            "is_leaf": getattr(matched_context, "is_leaf", False),
            "abstract": getattr(matched_context, "abstract", ""),
            "overview": getattr(matched_context, "overview", None),
            "category": getattr(matched_context, "category", ""),
            "score": getattr(matched_context, "score", 0.0),
            "match_reason": getattr(matched_context, "match_reason", ""),
            "relations": [
                self._relation_to_dict(r) for r in getattr(matched_context, "relations", [])
            ],
        }

    def _relation_to_dict(self, relation: Any) -> Dict[str, Any]:
        """将 Relation 对象转换为字典"""
        return {
            "from_uri": getattr(relation, "from_uri", ""),
            "to_uri": getattr(relation, "to_uri", ""),
            "relation_type": getattr(relation, "relation_type", ""),
            "reason": getattr(relation, "reason", ""),
        }

    def get_agent_space_name(self, user_id: str) -> str:
        return hashlib.md5(f"{user_id}:{self.agent_id}".encode()).hexdigest()[:12]

    async def find(self, query: str, target_uri: Optional[str] = None):
        """搜索资源"""
        if target_uri:
            return await self.client.find(query, target_uri=target_uri)
        return await self.client.find(query)

    async def add_resource(self, local_path: str, desc: str) -> Optional[Dict[str, Any]]:
        """添加资源到 Viking"""
        result = await self.client.add_resource(path=local_path, reason=desc)
        return result

    async def list_resources(
        self, path: Optional[str] = None, recursive: bool = False
    ) -> List[Dict[str, Any]]:
        """列出资源"""
        if path is None or path == "":
            path = viking_resource_prefix
        entries = await self.client.ls(path, recursive=recursive)
        return entries

    async def read_content(self, uri: str, level: str = "abstract") -> str:
        """读取内容

        Args:
            uri: Viking URI
            level: 读取级别 ("abstract" - L0摘要, "overview" - L1概览, "read" - L2完整内容)
        """
        try:
            if level == "abstract":
                return await self.client.abstract(uri)
            elif level == "overview":
                return await self.client.overview(uri)
            elif level == "read":
                return await self.client.read(uri)
            else:
                raise ValueError(f"Unsupported level: {level}")
        except FileNotFoundError:
            return ""
        except Exception as e:
            logger.warning(f"Failed to read content from {uri}: {e}")
            return ""

    async def stat(self, uri: str) -> Dict[str, Any]:
        """Return filesystem metadata for a Viking URI."""
        try:
            return await self.client.stat(uri)
        except Exception as e:
            logger.warning(f"Failed to stat {uri}: {e}")
            return {}

    async def download_content(self, uri: str) -> bytes:
        """Download raw file bytes for images and other binary resources."""
        http_client = getattr(self.client, "_http", None)
        if http_client is None:
            return b""

        try:
            response = await http_client.get("/api/v1/content/download", params={"uri": uri})
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.warning(f"Failed to download content from {uri}: {e}")
            return b""

    async def export_related_images_for_send(self, uri: str, max_images: int = 4) -> list[str]:
        """Export nearby resource images into send:// references for bot channels."""
        image_uris = await self._find_related_image_uris(uri, max_images=max_images)
        return await self._export_image_uris_for_send(image_uris)

    async def export_uri_for_send(self, uri: str, max_images: int = 4) -> list[str]:
        """Export one image URI or an image directory to send:// references."""
        stat = await self.stat(uri)
        if not stat:
            return []

        normalized_uri = uri.rstrip("/")
        if stat.get("isDir"):
            try:
                entries = await self.list_resources(path=normalized_uri, recursive=True)
            except Exception as e:
                logger.warning(f"Failed to list image directory {normalized_uri}: {e}")
                return []

            image_uris = [
                entry["uri"]
                for entry in entries
                if not entry.get("isDir") and Path(entry.get("name", "")).suffix.lower() in IMAGE_EXTENSIONS
            ]
            image_uris.sort(key=self._image_sort_key)
            return await self._export_image_uris_for_send(image_uris[:max_images])

        if Path(normalized_uri).suffix.lower() in IMAGE_EXTENSIONS:
            return await self._export_image_uris_for_send([normalized_uri])

        return []

    async def resolve_read_uri(self, uri: str) -> tuple[Optional[str], list[str]]:
        """Resolve a level='read' target to a concrete text leaf when possible."""
        stat = await self.stat(uri)
        if not stat:
            return None, []

        normalized_uri = uri.rstrip("/")
        if not stat.get("isDir") or normalized_uri.endswith("/_images") or normalized_uri.endswith(
            "_images"
        ):
            return normalized_uri, []

        try:
            entries = await self.list_resources(path=normalized_uri, recursive=True)
        except Exception as e:
            logger.warning(f"Failed to list read candidates under {normalized_uri}: {e}")
            return None, []

        candidates = sorted(
            [
                entry["uri"]
                for entry in entries
                if self._is_readable_text_entry(entry)
            ],
            key=self._text_read_sort_key,
        )
        if not candidates:
            return None, []

        dir_name = self._uri_name(normalized_uri)
        same_name_candidates = [
            candidate for candidate in candidates if Path(self._uri_name(candidate)).stem == dir_name
        ]
        if len(same_name_candidates) == 1:
            return same_name_candidates[0], candidates
        if len(candidates) == 1:
            return candidates[0], candidates
        return None, candidates

    async def materialize_inline_image_refs(self, content: str, source_uri: str) -> str:
        """Replace Word inline asset placeholders with sendable image references."""
        if "ov-asset://" not in content:
            return content

        rendered = content
        replacement_cache: dict[str, str] = {}

        for match in WORD_IMAGE_PLACEHOLDER_RE.finditer(content):
            alt_text = match.group(1)
            asset_name = match.group(2)
            placeholder = match.group(0)

            if asset_name not in replacement_cache:
                resolved_uri = await self._resolve_image_asset_uri(source_uri, asset_name)
                if not resolved_uri:
                    raise ValueError(
                        f"Unable to resolve inline image asset '{asset_name}' from {source_uri}"
                    )

                exported = await self._export_image_uris_for_send([resolved_uri])
                if not exported:
                    raise ValueError(
                        f"Unable to export inline image asset '{resolved_uri}' for send"
                    )

                send_match = re.search(r"!\[[^\]]*\]\((send://[^)\s]+)\)", exported[0])
                if not send_match:
                    raise ValueError(
                        f"Inline image asset '{resolved_uri}' did not produce a send:// reference"
                    )
                replacement_cache[asset_name] = f"![{alt_text}]({send_match.group(1)})"

            rendered = rendered.replace(placeholder, replacement_cache[asset_name], 1)

        return rendered

    async def _export_image_uris_for_send(self, image_uris: list[str]) -> list[str]:
        """Persist image URIs into bot send:// staging files."""
        if not image_uris:
            return []

        images_dir = get_images_path()
        exported_refs: list[str] = []

        for idx, image_uri in enumerate(image_uris, start=1):
            image_bytes = await self.download_content(image_uri)
            if not image_bytes:
                continue

            suffix = Path(image_uri).suffix.lower()
            if suffix not in IMAGE_EXTENSIONS:
                suffix = ".png"
            filename = f"{uuid.uuid4().hex}{suffix}"
            image_path = images_dir / filename
            image_path.write_bytes(image_bytes)

            label = Path(image_uri).stem or f"image_{idx}"
            exported_refs.append(f"![{label}](send://{filename})")

        return exported_refs

    async def read_user_profile(self, user_id: str) -> str:
        """读取用户 profile。

        首先检查用户是否存在，如不存在则初始化用户并返回空字符串。
        用户存在时，再查询 profile 信息。

        Args:
            user_id: 用户ID

        Returns:
            str: 用户 profile 内容，如果用户不存在或查询失败返回空字符串
        """
        # Step 1: 检查用户是否存在
        user_exists = await self._check_user_exists(user_id)

        # Step 2: 如果用户不存在，初始化用户并直接返回
        if not user_exists:
            await self._initialize_user(user_id)
            return ""

        # Step 3: 用户存在，查询 profile
        uri = f"viking://user/{user_id}/memories/profile.md"
        result = await self.read_content(uri=uri, level="read")
        return result

    async def search(self, query: str, target_uri: Optional[str] = "") -> Dict[str, Any]:
        # session = self.client.session()

        result = await self.client.search(query, target_uri=target_uri)

        # 将 FindResult 对象转换为 JSON map
        return {
            "memories": [self._matched_context_to_dict(m) for m in result.memories]
            if hasattr(result, "memories")
            else [],
            "resources": [self._matched_context_to_dict(r) for r in result.resources]
            if hasattr(result, "resources")
            else [],
            "skills": [self._matched_context_to_dict(s) for s in result.skills]
            if hasattr(result, "skills")
            else [],
            "total": getattr(result, "total", len(getattr(result, "resources", []))),
            "query": query,
            "target_uri": target_uri,
        }

    async def search_user_memory(self, query: str, user_id: str) -> list[Any]:
        user_exists = await self._check_user_exists(user_id)
        if not user_exists:
            return []
        uri_user_memory = f"viking://user/{user_id}/memories/"
        result = await self.client.search(query, target_uri=uri_user_memory)
        return (
            [self._matched_context_to_dict(m) for m in result.memories]
            if hasattr(result, "memories")
            else []
        )

    async def _check_user_exists(self, user_id: str) -> bool:
        """检查用户是否存在于账户中。

        Args:
            user_id: 用户ID

        Returns:
            bool: 用户是否存在
        """
        if self.mode == "local":
            return True
        try:
            res = await self.client.admin_list_users(self.account_id)
            if not res or len(res) == 0:
                return False
            return any(user.get("user_id") == user_id for user in res)
        except Exception as e:
            logger.warning(f"Failed to check user existence: {e}")
            return False

    async def _find_related_image_uris(self, uri: str, max_images: int = 4) -> list[str]:
        """Find extracted document images near a content URI."""
        current_dir = await self._resolve_start_directory(uri)
        checked_dirs: set[str] = set()

        for _ in range(4):
            if not current_dir or current_dir in checked_dirs:
                break
            checked_dirs.add(current_dir)

            images_dir = f"{current_dir.rstrip('/')}/_images"
            try:
                stat = await self.client.stat(images_dir)
            except Exception:
                stat = {}

            if stat.get("isDir"):
                try:
                    entries = await self.list_resources(path=images_dir, recursive=False)
                except Exception as e:
                    logger.warning(f"Failed to list image directory {images_dir}: {e}")
                    entries = []

                image_uris = [
                    entry["uri"]
                    for entry in entries
                    if not entry.get("isDir") and Path(entry.get("name", "")).suffix.lower() in IMAGE_EXTENSIONS
                ]
                image_uris.sort(key=self._image_sort_key)
                return image_uris[:max_images]

            parent_dir = self._parent_uri(current_dir)
            if not parent_dir or parent_dir == current_dir:
                break
            current_dir = parent_dir

        return []

    async def _resolve_image_asset_uri(self, source_uri: str, asset_name: str) -> Optional[str]:
        """Resolve an inline Word asset placeholder to the nearest extracted image URI."""
        current_dir = await self._resolve_start_directory(source_uri)
        checked_dirs: set[str] = set()

        for _ in range(6):
            if not current_dir or current_dir in checked_dirs:
                break
            checked_dirs.add(current_dir)

            candidate = f"{current_dir.rstrip('/')}/_images/{asset_name}"
            stat = await self.stat(candidate)
            if stat and not stat.get("isDir"):
                return candidate

            parent_dir = self._parent_uri(current_dir)
            if not parent_dir or parent_dir == current_dir:
                break
            current_dir = parent_dir

        return None

    async def _resolve_start_directory(self, uri: str) -> str:
        """Resolve the best starting directory for sibling image lookup."""
        try:
            stat = await self.client.stat(uri)
        except Exception:
            stat = {}

        if stat.get("isDir"):
            return uri.rstrip("/")
        return self._parent_uri(uri)

    @staticmethod
    def _parent_uri(uri: str) -> str:
        normalized = uri.rstrip("/")
        if "://" not in normalized:
            return normalized

        scheme, path = normalized.split("://", 1)
        parts = [segment for segment in path.split("/") if segment]
        if len(parts) <= 1:
            return f"{scheme}://{parts[0]}" if parts else f"{scheme}://"
        return f"{scheme}://{'/'.join(parts[:-1])}"

    @staticmethod
    def _image_sort_key(uri: str) -> tuple[int, str]:
        name = Path(uri).name.lower()
        match = re.search(r"(\d+)", name)
        number = int(match.group(1)) if match else 10**9
        return number, name

    @staticmethod
    def _uri_name(uri: str) -> str:
        return uri.rstrip("/").rsplit("/", 1)[-1]

    @classmethod
    def _is_readable_text_entry(cls, entry: Dict[str, Any]) -> bool:
        if entry.get("isDir"):
            return False

        uri = str(entry.get("uri", "")).rstrip("/")
        name = str(entry.get("name", "") or cls._uri_name(uri))
        if not uri or name.startswith(".") or "/_images/" in uri:
            return False

        return Path(name).suffix.lower() in READABLE_TEXT_EXTENSIONS

    @classmethod
    def _text_read_sort_key(cls, uri: str) -> tuple[int, str]:
        normalized = uri.rstrip("/")
        path = normalized.split("://", 1)[-1]
        return path.count("/"), cls._uri_name(normalized)

    async def _initialize_user(self, user_id: str, role: str = "user") -> bool:
        """初始化用户。

        Args:
            user_id: 用户ID

        Returns:
            bool: 初始化是否成功
        """
        if self.mode == "local":
            return True
        try:
            result = await self.client.admin_register_user(
                account_id=self.account_id, user_id=user_id, role=role
            )

            # Save the API key if returned and we're in remote mode with a valid apikey manager
            if self._apikey_manager and isinstance(result, dict):
                api_key = result.get("user_key")
                if api_key:
                    self._apikey_manager.set_apikey(user_id, api_key)

            return True
        except Exception as e:
            if "User already exists" in str(e):
                return True
            logger.warning(f"Failed to initialize user {user_id}: {e}")
            return False

    async def _get_or_create_user_apikey(self, user_id: str) -> Optional[str]:
        """获取或创建用户的 API key。

        优先从本地 json 文件获取，如果本地没有则：
        1. 删除用户（如果存在）
        2. 重新创建用户
        3. 保存新的 API key

        Args:
            user_id: 用户ID

        Returns:
            API key 或 None（如果获取失败）
        """
        if not self._apikey_manager:
            return None

        # Step 1: Check local storage first
        api_key = self._apikey_manager.get_apikey(user_id)
        if api_key:
            return api_key

        try:
            # 2a. Remove user if exists
            user_exists = await self._check_user_exists(user_id)
            if user_exists:
                await self.client.admin_remove_user(self.account_id, user_id)
            # 2b. Recreate user - this will save API key in _initialize_user
            success = await self._initialize_user(user_id)
            if not success:
                logger.warning(f"Failed to recreate user {user_id}")
                return None

            # 2c. Get API key from local storage (it was saved by _initialize_user)
            api_key = self._apikey_manager.get_apikey(user_id)
            if api_key:
                return api_key
            else:
                return None

        except Exception as e:
            logger.error(f"Error getting or creating API key for user {user_id}: {e}")
            return None

    async def search_memory(
        self, query: str, user_id: str, limit: int = 10
    ) -> dict[str, list[Any]]:
        """通过上下文消息，检索viking 的user、Agent memory。

        首先检查用户是否存在，如不存在则初始化用户并返回空结果。
        用户存在时，再进行记忆检索。
        """
        # Step 1: 检查用户是否存在
        user_exists = await self._check_user_exists(user_id)

        # Step 2: 如果用户不存在，初始化用户并直接返回
        if not user_exists:
            await self._initialize_user(user_id)
            return {
                "user_memory": [],
                "agent_memory": [],
            }
        # Step 3: 用户存在，查询记忆
        uri_user_memory = f"viking://user/{user_id}/memories/"
        user_memory = await self.client.find(
            query=query,
            target_uri=uri_user_memory,
            limit=limit,
        )
        agent_space_name = self.get_agent_space_name(user_id)
        uri_agent_memory = f"viking://agent/{agent_space_name}/memories/"
        agent_memory = await self.client.find(
            query=query,
            target_uri=uri_agent_memory,
            limit=limit,
        )
        return {
            "user_memory": user_memory.memories if hasattr(user_memory, "memories") else [],
            "agent_memory": agent_memory.memories if hasattr(agent_memory, "memories") else [],
        }

    async def grep(self, uri: str, pattern: str, case_insensitive: bool = False) -> Dict[str, Any]:
        """通过模式（正则表达式）搜索内容"""
        return await self.client.grep(
            uri, pattern, case_insensitive=case_insensitive, node_limit=10
        )

    async def glob(self, pattern: str, uri: Optional[str] = None) -> Dict[str, Any]:
        """通过 glob 模式匹配文件"""
        return await self.client.glob(pattern, uri=uri)

    async def commit(self, session_id: str, messages: list[dict[str, Any]], user_id: str = None):
        """提交会话"""
        import re
        import uuid

        from openviking.message.part import TextPart, ToolPart

        user_exists = await self._check_user_exists(user_id)
        if not user_exists:
            success = await self._initialize_user(user_id)
            if not success:
                return {"error": "Failed to initialize user"}

        # For remote mode, try to get user's API key and create a dedicated client
        client = self.client
        start = time.time()
        if (
            self.mode == "remote"
            and user_id
            and user_id != self.admin_user_id
            and self._apikey_manager
        ):
            user_api_key = await self._get_or_create_user_apikey(user_id)
            if user_api_key:
                # Create a new HTTP client with user's API key
                client = ov.AsyncHTTPClient(
                    url=self.openviking_config.server_url,
                    api_key=user_api_key,
                    agent_id=self.agent_id,
                )
                await client.initialize()

        create_res = await client.create_session()
        session_id = create_res["session_id"]
        session = client.session(session_id)

        for message in messages:
            role = message.get("role")
            content = message.get("content")
            tools_used = message.get("tools_used") or []

            parts: list[Any] = []

            if content:
                parts.append(TextPart(text=content))

            for tool_info in tools_used:
                tool_name = tool_info.get("tool_name", "")
                if not tool_name:
                    continue

                tool_id = f"{tool_name}_{uuid.uuid4().hex[:8]}"
                tool_input = None
                try:
                    import json

                    args_str = tool_info.get("args", "{}")
                    tool_input = json.loads(args_str) if args_str else {}
                except Exception:
                    tool_input = {"raw_args": tool_info.get("args", "")}

                result_str = str(tool_info.get("result", ""))

                skill_uri = ""
                if tool_name == "read_file" and result_str:
                    match = re.search(r"^---\s*\nname:\s*(.+?)\s*\n", result_str, re.MULTILINE)
                    if match:
                        skill_name = match.group(1).strip()
                        skill_uri = f"viking://agent/skills/{skill_name}"

                execute_success = tool_info.get("execute_success", True)
                tool_status = "completed" if execute_success else "error"
                parts.append(
                    ToolPart(
                        tool_id=tool_id,
                        tool_name=tool_name,
                        tool_uri=f"viking://session/{session_id}/tools/{tool_id}",
                        tool_input=tool_input,
                        tool_output=result_str[:2000],
                        tool_status=tool_status,
                        skill_uri=skill_uri,
                        duration_ms=float(tool_info.get("duration", 0.0)),
                        prompt_tokens=tool_info.get("input_token"),
                        completion_tokens=tool_info.get("output_token"),
                    )
                )

            if not parts:
                continue
            await session.add_message(role=role, parts=parts)

        result = await session.commit_async()
        if client is not self.client:
            await client.close()
        logger.info(f"time spent: {time.time() - start}")
        logger.debug(f"Message add ed to OpenViking session {session_id}, user: {user_id}")
        return {"success": result["status"]}

    async def close(self):
        """关闭客户端"""
        await self.client.close()


async def main_test():
    client = await VikingClient.create(agent_id="shared")
    # res = client.list_resources()
    # res = await client.search("头有点疼", target_uri="viking://user/memories/")
    # res = await client.get_viking_memory_context("123", current_message="头疼", history=[])
    res = await client.search_memory("你好", "user_1")
    # res = await client.list_resources("viking://resources/")
    # res = await client.read_content("viking://user/memories/profile.md", level="read")
    # res = await client.add_resource("https://github.com/volcengine/OpenViking", "ov代码")
    # res = await client.grep("viking://resources/", "viking", True)
    # res = await client.commit(
    #     session_id="99999",
    #     messages=[{"role": "user", "content": "你好"}],
    #     user_id="1010101010",
    # )
    # res = await client.commit("1234", [{"role": "user", "content": "帮我搜索 Python asyncio 教程"}
    #                                    ,{"role": "assistant", "content": "我来帮你r搜索 Python asyncio 相关的教程。"}])
    print(res)

    await client.close()
    print("处理完成！")


async def account_test():
    client = ov.AsyncHTTPClient(url="http://localhost:1933", api_key="test")
    await client.initialize()

    # res = await client.admin_list_users("eval")
    # res = await client.admin_remove_user("default", "")
    # res = await client.admin_remove_user("default", "admin")
    # res = await client.admin_list_accounts()
    # res = await client.admin_create_account("eval", "default")
    res = await client.admin_register_user("default", "test_root", "root")
    print(res)


if __name__ == "__main__":
    asyncio.run(main_test())
    # asyncio.run(account_test())
