"""DingTalk/DingDing channel implementation using Stream Mode."""

import asyncio
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
from loguru import logger

from vikingbot.bus.events import OutboundEventType, OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.base import BaseChannel
from vikingbot.config.schema import DingTalkChannelConfig, SessionKey

try:
    from dingtalk_stream import (
        AckMessage,
        CallbackHandler,
        CallbackMessage,
        Credential,
        DingTalkStreamClient,
    )
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    # Fallback so class definitions don't crash at module level
    CallbackHandler = object  # type: ignore[assignment,misc]
    CallbackMessage = None  # type: ignore[assignment,misc]
    AckMessage = None  # type: ignore[assignment,misc]
    ChatbotMessage = None  # type: ignore[assignment,misc]


DINGTALK_TEXT_SOFT_LIMIT = 420
DINGTALK_MARKDOWN_TITLE = "XR Support Reply"
DINGTALK_MARKDOWN_TITLE_LIMIT = 100
DINGTALK_INTERACTIVE_CARD_TEMPLATE_ID = "StandardCard"
DINGTALK_INTERACTIVE_CARD_SEND_URL = (
    "https://api.dingtalk.com/v1.0/im/v1.0/robot/interactiveCards/send"
)
DINGTALK_INTERACTIVE_CARD_UPDATE_URL = "https://api.dingtalk.com/v1.0/im/robots/interactiveCards"
DINGTALK_PLACEHOLDER_TEXT = "消息卡片生成中..."
DINGTALK_CARD_LOGO = "@lALPDfJ6V_FPDmvNAfTNAfQ"
DINGTALK_PROGRESS_STATUSES = (
    "检索中",
    "提取中",
    "整理中",
    "生成中",
    "刷新中",
)
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
MARKDOWN_DECORATION_PATTERN = re.compile(r"(\*\*|__|\*|_|~~|`)(.+?)\1")
MARKDOWN_TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)")
FLOW_LINE_PATTERN = re.compile(r"[→←↑↓]|->|<-|=>|<=|↔")


@dataclass
class DingTalkCardState:
    """Track a live DingTalk interactive card while a reply is being generated."""

    session_key: str
    chat_id: str
    single_chat_receiver: str
    card_biz_id: str
    process_query_key: str | None = None
    last_card_data: str | None = None
    status_history: list[str] = field(default_factory=list)
    last_tool_name: str | None = None


class NanobotDingTalkHandler(CallbackHandler):
    """
    Standard DingTalk Stream SDK Callback Handler.
    Parses incoming messages and forwards them to the Nanobot channel.
    """

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """Process incoming stream message."""
        try:
            # Parse using SDK's ChatbotMessage for robust handling
            chatbot_msg = ChatbotMessage.from_dict(message.data)

            # Extract text content; fall back to raw dict if SDK object is empty
            content = ""
            if chatbot_msg.text:
                content = chatbot_msg.text.content.strip()
            if not content:
                content = message.data.get("text", {}).get("content", "").strip()

            if not content:
                logger.warning(
                    f"Received empty or unsupported message type: {chatbot_msg.message_type}"
                )
                return AckMessage.STATUS_OK, "OK"

            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
            sender_name = chatbot_msg.sender_nick or "Unknown"

            logger.info(f"Received DingTalk message from {sender_name} ({sender_id}): {content}")

            # Forward to Nanobot via _on_message (non-blocking).
            # Store reference to prevent GC before task completes.
            task = asyncio.create_task(self.channel._on_message(content, sender_id, sender_name))
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.exception(f"Error processing DingTalk message: {e}")
            # Return OK to avoid retry loop from DingTalk server
            return AckMessage.STATUS_OK, "Error"


class DingTalkChannel(BaseChannel):
    """
    DingTalk channel using Stream Mode.

    Uses WebSocket to receive events via `dingtalk-stream` SDK.
    Uses direct HTTP API to send messages (SDK is mainly for receiving).

    Note: Currently only supports private (1:1) chat. Group messages are
    received but replies are sent back as private messages to the sender.
    """

    name = "dingtalk"

    def __init__(self, config: DingTalkChannelConfig, bus: MessageBus, **kwargs):
        super().__init__(config, bus, **kwargs)
        self.config: DingTalkChannelConfig = config
        self._client: Any = None
        self._http: httpx.AsyncClient | None = None

        # Access Token management for sending messages
        self._access_token: str | None = None
        self._token_expiry: float = 0

        # Hold references to background tasks to prevent GC
        self._background_tasks: set[asyncio.Task] = set()
        self._active_cards_by_session: dict[str, DingTalkCardState] = {}
        self._active_cards_by_biz_id: dict[str, DingTalkCardState] = {}

    async def start(self) -> None:
        """Start the DingTalk bot with Stream Mode."""
        try:
            if not DINGTALK_AVAILABLE:
                logger.exception(
                    "DingTalk Stream SDK not installed. Install with: uv pip install 'openviking[bot-dingtalk]' (or uv pip install -e \".[bot-dingtalk]\" for local dev)"
                )
                return

            if not self.config.client_id or not self.config.client_secret:
                logger.exception("DingTalk client_id and client_secret not configured")
                return

            self._running = True
            self._http = httpx.AsyncClient()

            logger.info(
                f"Initializing DingTalk Stream Client with Client ID: {self.config.client_id}..."
            )
            credential = Credential(self.config.client_id, self.config.client_secret)
            self._client = DingTalkStreamClient(credential)

            # Register standard handler
            handler = NanobotDingTalkHandler(self)
            self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

            logger.info("DingTalk bot started with Stream Mode")

            # Reconnect loop: restart stream if SDK exits or crashes
            while self._running:
                try:
                    await self._client.start()
                except Exception as e:
                    logger.warning(f"DingTalk stream error: {e}")
                if self._running:
                    logger.info("Reconnecting DingTalk stream in 5 seconds...")
                    await asyncio.sleep(5)

        except Exception as e:
            logger.exception(f"Failed to start DingTalk channel: {e}")

    async def stop(self) -> None:
        """Stop the DingTalk bot."""
        self._running = False
        # Close the shared HTTP client
        if self._http:
            await self._http.aclose()
            self._http = None
        # Cancel outstanding background tasks
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()
        self._active_cards_by_session.clear()
        self._active_cards_by_biz_id.clear()

    async def _get_access_token(self) -> str | None:
        """Get or refresh Access Token."""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot refresh token")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            # Expire 60s early to be safe
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception as e:
            logger.exception(f"Failed to get DingTalk access token: {e}")
            return None

    async def _upload_image_to_dingtalk(
        self, image_data: bytes, token: str, filename: str = "image.png"
    ) -> str:
        """Upload an image to DingTalk and return its media_id."""
        if not self._http:
            raise RuntimeError("DingTalk HTTP client not initialized")

        url = "https://oapi.dingtalk.com/media/upload"
        response = await self._http.post(
            url,
            params={"access_token": token, "type": "image"},
            files={"media": (filename, image_data, "application/octet-stream")},
        )
        response.raise_for_status()
        result = response.json()
        media_id = result.get("media_id") or result.get("mediaId")
        if not media_id:
            raise ValueError(f"Unexpected DingTalk media upload response: {result}")
        return media_id

    async def _replace_inline_images(self, content: str, token: str) -> str:
        """Replace send:// and Markdown image references with DingTalk media IDs."""
        markdown_pattern = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
        standalone_pattern = re.compile(r"(send://[^\s)]+\.(?:png|jpe?g|gif|bmp|webp|svg|tiff))")

        async def _replace_markdown(match: re.Match[str]) -> str:
            alt_text = match.group(1)
            image_ref = match.group(2)

            if not (
                image_ref.startswith("send://")
                or image_ref.startswith("data:")
                or image_ref.startswith("http://")
                or image_ref.startswith("https://")
            ):
                return match.group(0)

            try:
                is_content, parsed = await self._parse_data_uri(image_ref)
                if is_content or not isinstance(parsed, bytes):
                    raise ValueError(
                        f"DingTalk inline image ref {image_ref[:120]} did not resolve to image bytes"
                    )
                filename = image_ref.rsplit("/", 1)[-1] or "image.png"
                media_id = await self._upload_image_to_dingtalk(parsed, token, filename=filename)
                return f"![{alt_text}]({media_id})"
            except Exception as e:
                logger.exception(f"Failed to upload DingTalk markdown image {image_ref[:120]}: {e}")
                raise

        async def _replace_standalone(match: re.Match[str]) -> str:
            image_ref = match.group(1)
            try:
                is_content, parsed = await self._parse_data_uri(image_ref)
                if is_content or not isinstance(parsed, bytes):
                    raise ValueError(
                        f"DingTalk image ref {image_ref[:120]} did not resolve to image bytes"
                    )
                filename = image_ref.rsplit("/", 1)[-1] or "image.png"
                media_id = await self._upload_image_to_dingtalk(parsed, token, filename=filename)
                alt_text = filename.rsplit(".", 1)[0] or "image"
                return f"![{alt_text}]({media_id})"
            except Exception as e:
                logger.exception(f"Failed to upload DingTalk image {image_ref[:120]}: {e}")
                raise

        rendered = content
        for match in list(markdown_pattern.finditer(content)):
            rendered = rendered.replace(match.group(0), await _replace_markdown(match), 1)
        for match in list(standalone_pattern.finditer(rendered)):
            rendered = rendered.replace(match.group(1), await _replace_standalone(match), 1)

        return rendered

    @staticmethod
    def _make_single_chat_receiver(user_id: str) -> str:
        """Build the singleChatReceiver payload expected by DingTalk interactive cards."""
        return json.dumps({"userId": user_id}, ensure_ascii=False)

    @staticmethod
    def _extract_tool_name(tool_call_content: str) -> str:
        """Extract the tool name from a tool-call event payload."""
        name = tool_call_content.split("(", 1)[0].strip()
        return name or "unknown_tool"

    def _session_cache_key(self, session_key: SessionKey) -> str:
        """Build a stable cache key for the current session."""
        return session_key.safe_name()

    def _remember_card_state(self, state: DingTalkCardState) -> None:
        """Store a live card state for later updates."""
        self._active_cards_by_session[state.session_key] = state
        self._active_cards_by_biz_id[state.card_biz_id] = state

    def _forget_card_state(self, state: DingTalkCardState) -> None:
        """Drop a live card state once the reply has completed."""
        cached = self._active_cards_by_session.get(state.session_key)
        if cached and cached.card_biz_id == state.card_biz_id:
            self._active_cards_by_session.pop(state.session_key, None)
        self._active_cards_by_biz_id.pop(state.card_biz_id, None)

    def _get_card_state_from_message(self, msg: OutboundMessage) -> DingTalkCardState | None:
        """Resolve the live card state for an outbound event."""
        metadata = msg.metadata or {}
        card_biz_id = metadata.get("card_biz_id") or metadata.get("message_id")
        if card_biz_id:
            state = self._active_cards_by_biz_id.get(str(card_biz_id))
            if state:
                return state
        return self._active_cards_by_session.get(self._session_cache_key(msg.session_key))

    def _append_card_status(self, state: DingTalkCardState, status: str) -> None:
        """Append a progress status to the live card, deduplicating adjacent repeats."""
        status = status.strip()
        if not status:
            return
        if state.status_history and state.status_history[-1] == status:
            return
        state.status_history.append(status)
        if len(state.status_history) > 6:
            state.status_history = state.status_history[-6:]

    def _build_loading_progress_text(self, state: DingTalkCardState) -> str:
        """Render the in-progress status list shown on the loading card."""
        if not state.status_history:
            return ""
        return "\n".join(f"- {item}" for item in state.status_history)

    @staticmethod
    def _truncate_status_snippet(text: str, limit: int = 28) -> str:
        """Compress noisy model/tool text into a short user-facing status line."""
        collapsed = re.sub(r"\s+", " ", text or "").strip()
        if len(collapsed) <= limit:
            return collapsed
        return f"{collapsed[:limit].rstrip()}..."

    def _friendly_tool_status(self, tool_name: str, *, completed: bool = False) -> str:
        """Map internal tool names to user-facing progress text."""
        normalized = (tool_name or "").strip().lower()
        if normalized in {"openviking_search", "search", "ov_search_context"}:
            return "知识库检索完成" if completed else "检索知识库"
        if normalized in {"openviking_read", "read", "ov_file_read"}:
            return "文档读取完成" if completed else "读取文档"
        if "web" in normalized or "search" in normalized:
            return "联网搜索完成" if completed else "联网搜索"
        if "image" in normalized:
            return "图片处理完成" if completed else "处理图片"
        if normalized in {"shell", "exec_command"}:
            return "命令执行完成" if completed else "执行命令"
        if "python" in normalized or "code" in normalized:
            return "代码运行完成" if completed else "运行代码"
        if not normalized:
            return "处理中" if not completed else "处理完成"
        return f"{tool_name} 完成" if completed else f"调用 {tool_name}"

    def _status_from_event(self, msg: OutboundMessage) -> str | None:
        """Map agent-side events to user-facing DingTalk status text."""
        if msg.event_type == OutboundEventType.ITERATION:
            return "继续处理"
        if msg.event_type == OutboundEventType.REASONING:
            snippet = self._truncate_status_snippet(msg.content, limit=18)
            return f"分析中：{snippet}" if snippet else "分析中"
        if msg.event_type == OutboundEventType.TOOL_CALL:
            return self._friendly_tool_status(self._extract_tool_name(msg.content))
        if msg.event_type == OutboundEventType.TOOL_RESULT:
            state = self._get_card_state_from_message(msg)
            if state and state.last_tool_name:
                return f"{self._friendly_tool_status(state.last_tool_name, completed=True)}，整理中"
            return "结果已返回，整理中"
        return None

    def _build_interactive_card_data(self, content: str, *, status: str | None = None) -> str:
        """Build the StandardCard payload used for send/update interactive-card calls."""
        use_markdown = self._contains_inline_image(content)
        normalized_content = content if use_markdown else self._normalize_text_for_sample_text(content)
        content_chunks = self._split_dingtalk_content(normalized_content) if normalized_content else []
        body_text = "\n\n".join(content_chunks).strip()
        contents: list[dict[str, Any]] = []
        status_text = (status or "").strip()
        if status_text:
            contents.append({"type": "text", "text": status_text, "id": "status_text"})

        if body_text:
            if status_text:
                contents.append({"type": "divider", "id": "status_divider"})
            contents.append(
                {
                    "type": "markdown",
                    "text": body_text,
                    "id": "content_markdown",
                }
            )

        if not contents:
            contents.append({"type": "markdown", "text": DINGTALK_PLACEHOLDER_TEXT, "id": "content_markdown"})

        card = {
            "config": {
                "autoLayout": True,
                "enableForward": True,
            },
            "header": {
                "title": {
                    "type": "text",
                    "text": DINGTALK_MARKDOWN_TITLE[:DINGTALK_MARKDOWN_TITLE_LIMIT],
                },
                "logo": DINGTALK_CARD_LOGO,
            },
            "contents": contents,
        }
        return json.dumps(card, ensure_ascii=False)

    async def _send_interactive_card(
        self, token: str, state: DingTalkCardState, card_data: str
    ) -> bool:
        """Send a new StandardCard message for a DingTalk single chat."""
        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot send interactive card")
            return False

        headers = {"x-acs-dingtalk-access-token": token}
        data = {
            "cardTemplateId": DINGTALK_INTERACTIVE_CARD_TEMPLATE_ID,
            "singleChatReceiver": state.single_chat_receiver,
            "cardBizId": state.card_biz_id,
            "robotCode": self.config.client_id,
            "cardData": card_data,
            "pullStrategy": False,
        }

        resp = await self._http.post(
            DINGTALK_INTERACTIVE_CARD_SEND_URL,
            json=data,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.exception(f"DingTalk interactive-card send failed: {resp.text}")
            return False

        response_data = resp.json() if resp.content else {}
        state.process_query_key = response_data.get("processQueryKey")
        state.last_card_data = card_data
        return True

    async def _update_interactive_card(
        self, token: str, state: DingTalkCardState, card_data: str
    ) -> bool:
        """Update an existing StandardCard message by cardBizId."""
        if state.last_card_data == card_data:
            return True
        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot update interactive card")
            return False

        headers = {"x-acs-dingtalk-access-token": token}
        data = {
            "cardBizId": state.card_biz_id,
            "cardData": card_data,
        }

        resp = await self._http.put(
            DINGTALK_INTERACTIVE_CARD_UPDATE_URL,
            json=data,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.exception(f"DingTalk interactive-card update failed: {resp.text}")
            return False

        response_data = resp.json() if resp.content else {}
        state.process_query_key = response_data.get("processQueryKey") or state.process_query_key
        state.last_card_data = card_data
        return True

    async def _send_loading_card(self, session_key: SessionKey, chat_id: str) -> DingTalkCardState | None:
        """Send the initial loading card so the user sees work start immediately."""
        token = await self._get_access_token()
        if not token:
            return None

        state = DingTalkCardState(
            session_key=self._session_cache_key(session_key),
            chat_id=chat_id,
            single_chat_receiver=self._make_single_chat_receiver(chat_id),
            card_biz_id=str(uuid4()),
        )
        card_data = self._build_interactive_card_data(
            self._build_loading_progress_text(state),
            status=DINGTALK_PLACEHOLDER_TEXT,
        )
        if not await self._send_interactive_card(token, state, card_data):
            return None

        self._remember_card_state(state)
        return state

    async def _refresh_loading_card(self, state: DingTalkCardState, status: str) -> None:
        """Update the existing loading card with the newest progress status."""
        token = await self._get_access_token()
        if not token:
            return

        self._append_card_status(state, status)
        card_data = self._build_interactive_card_data(
            self._build_loading_progress_text(state),
            status=DINGTALK_PLACEHOLDER_TEXT,
        )
        await self._update_interactive_card(token, state, card_data)

    async def _send_interactive_final_response(
        self,
        token: str,
        chat_id: str,
        rendered_content: str,
        state: DingTalkCardState | None = None,
    ) -> bool:
        """Send the final content, preferring to update the existing loading card."""
        card_data = self._build_interactive_card_data(rendered_content, status=None)

        if state and await self._update_interactive_card(token, state, card_data):
            return True

        fresh_state = DingTalkCardState(
            session_key=f"final::{chat_id}",
            chat_id=chat_id,
            single_chat_receiver=self._make_single_chat_receiver(chat_id),
            card_biz_id=str(uuid4()),
        )
        return await self._send_interactive_card(token, fresh_state, card_data)

    async def _send_legacy_message(self, token: str, rendered_content: str, chat_id: str) -> bool:
        """Fallback to the previous plain-text / markdown bot message flow."""
        payloads = self._build_message_payloads(rendered_content)
        if not payloads:
            logger.warning("DingTalk rendered content is empty, skipping send")
            return False

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot send")
            return False

        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
        headers = {"x-acs-dingtalk-access-token": token}

        for index, payload in enumerate(payloads, start=1):
            data = {
                "robotCode": self.config.client_id,
                "userIds": [chat_id],
                **payload,
            }
            resp = await self._http.post(url, json=data, headers=headers)
            if resp.status_code != 200:
                logger.exception(
                    f"DingTalk legacy send failed on chunk {index}/{len(payloads)}: {resp.text}"
                )
                return False

        logger.debug(f"DingTalk legacy message sent to {chat_id} in {len(payloads)} chunk(s)")
        return True

    def _contains_inline_image(self, content: str) -> bool:
        """Return whether the content contains inline images that require markdown rendering."""
        return bool(MARKDOWN_IMAGE_PATTERN.search(content.strip()))

    def _display_units(self, text: str) -> int:
        """Approximate how much vertical space a chunk will take in DingTalk."""
        units = 0
        for char in text:
            if char == "\n":
                units += 2
                continue
            units += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        return units

    def _normalize_text_for_sample_text(self, content: str) -> str:
        """Degrade simple markdown to plain text for better DingTalk mobile rendering."""
        text = content.replace("\r\n", "\n").strip()
        if not text:
            return ""

        normalized_blocks: list[str] = []
        for raw_block in re.split(r"\n\s*\n", text):
            block = raw_block.strip()
            if not block:
                continue
            table_block = self._convert_markdown_table_block(block)
            if table_block:
                normalized_blocks.append(table_block)
                continue

            cleaned = MARKDOWN_LINK_PATTERN.sub(r"\1 (\2)", block)
            cleaned = MARKDOWN_DECORATION_PATTERN.sub(r"\2", cleaned)
            cleaned = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", cleaned)
            cleaned = re.sub(r"(?m)^\s*>\s?", "", cleaned)
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            normalized_blocks.append(cleaned.strip())

        return "\n\n".join(block for block in normalized_blocks if block).strip()

    @staticmethod
    def _split_table_row(line: str) -> list[str]:
        """Split a markdown table row into cells."""
        stripped = line.strip().strip("|")
        if not stripped:
            return []
        return [cell.strip() for cell in stripped.split("|")]

    def _convert_markdown_table_block(self, block: str) -> str | None:
        """Convert a markdown table block into a DingTalk-friendly bullet list."""
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "|" not in lines[0]:
            return None
        if not MARKDOWN_TABLE_SEPARATOR_PATTERN.match(lines[1]):
            return None

        headers = self._split_table_row(lines[0])
        if not headers:
            return None

        bullet_lines: list[str] = []
        for line in lines[2:]:
            if "|" not in line:
                return None
            row = self._split_table_row(line)
            if not row:
                continue
            cells = row + [""] * max(0, len(headers) - len(row))
            pairs = [
                f"{header}: {cells[index]}"
                for index, header in enumerate(headers)
                if header and index < len(cells) and cells[index]
            ]
            if pairs:
                bullet_lines.append(f"- {'；'.join(pairs)}")

        return "\n".join(bullet_lines).strip() or None

    def _classify_text_block(self, block: str) -> str:
        """Classify a block so we can split it without breaking its structure."""
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            return "empty"
        if all(LIST_ITEM_PATTERN.match(line) for line in lines):
            return "list"
        if len(lines) >= 2 and any(FLOW_LINE_PATTERN.search(line) for line in lines):
            return "flow"
        return "paragraph"

    def _split_list_block(self, block: str, max_chars: int) -> list[str]:
        """Split a list block by list item, keeping each item intact when possible."""
        items = [line.strip() for line in block.splitlines() if line.strip()]
        chunks: list[str] = []
        current = ""
        for item in items:
            if self._display_units(item) > max_chars:
                oversized_items = self._split_long_line(item, max_chars)
            else:
                oversized_items = [item]
            for piece in oversized_items:
                candidate = piece if not current else f"{current}\n{piece}"
                if self._display_units(candidate) <= max_chars:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = piece
        if current:
            chunks.append(current)
        return chunks

    def _split_flow_block(self, block: str, max_chars: int) -> list[str]:
        """Split a compact flow/diagram block by lines instead of by sentences."""
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        chunks: list[str] = []
        current = ""
        for line in lines:
            line_chunks = [line] if self._display_units(line) <= max_chars else self._split_long_line(line, max_chars)
            for piece in line_chunks:
                candidate = piece if not current else f"{current}\n{piece}"
                if self._display_units(candidate) <= max_chars:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = piece
        if current:
            chunks.append(current)
        return chunks

    def _split_text_block(self, block: str, max_chars: int) -> list[str]:
        """Split a normalized block while preserving structure as much as possible."""
        if self._display_units(block) <= max_chars:
            return [block]

        block_type = self._classify_text_block(block)
        if block_type == "list":
            return self._split_list_block(block, max_chars)
        if block_type == "flow":
            return self._split_flow_block(block, max_chars)
        return self._split_paragraph(block, max_chars)

    def _hard_wrap_text(self, text: str, max_chars: int) -> list[str]:
        """Fallback splitter for text without natural breakpoints."""
        chunks: list[str] = []
        current = ""
        for char in text:
            candidate = f"{current}{char}"
            if current and self._display_units(candidate) > max_chars:
                chunks.append(current)
                current = char
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    def _split_long_line(self, line: str, max_chars: int) -> list[str]:
        """Split a long line by sentence punctuation before falling back to hard wraps."""
        text = line.strip()
        if self._display_units(text) <= max_chars:
            return [text]

        sentences = [fragment for fragment in re.split(r"(?<=[。！？!?；;])", text) if fragment]
        if len(sentences) <= 1:
            return self._hard_wrap_text(text, max_chars)

        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            fragment = sentence.strip()
            if not fragment:
                continue
            candidate = fragment if not current else f"{current}{fragment}"
            if self._display_units(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
            if self._display_units(fragment) <= max_chars:
                current = fragment
            else:
                chunks.extend(self._hard_wrap_text(fragment, max_chars))
                current = ""
        if current:
            chunks.append(current)
        return chunks

    def _split_paragraph(self, paragraph: str, max_chars: int) -> list[str]:
        """Split a paragraph while preserving line breaks when possible."""
        text = paragraph.strip()
        if self._display_units(text) <= max_chars:
            return [text]

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if len(lines) <= 1:
            return self._split_long_line(text, max_chars)

        chunks: list[str] = []
        current = ""
        for line in lines:
            for piece in self._split_long_line(line, max_chars):
                candidate = piece if not current else f"{current}\n{piece}"
                if self._display_units(candidate) <= max_chars:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = piece
        if current:
            chunks.append(current)
        return chunks

    def _split_dingtalk_content(self, content: str, max_chars: int = DINGTALK_TEXT_SOFT_LIMIT) -> list[str]:
        """Split a DingTalk reply into chunks that stay within the template's safe length."""
        text = content.replace("\r\n", "\n").strip()
        if not text:
            return []

        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
        chunks: list[str] = []
        current = ""

        for paragraph in paragraphs:
            for piece in self._split_text_block(paragraph, max_chars):
                candidate = piece if not current else f"{current}\n\n{piece}"
                if self._display_units(candidate) <= max_chars:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = piece

        if current:
            chunks.append(current)

        return chunks or self._hard_wrap_text(text, max_chars)

    def _build_message_payloads(self, content: str) -> list[dict[str, str]]:
        """Build one or more DingTalk payload fragments for a reply."""
        use_markdown = self._contains_inline_image(content)
        normalized_content = content if use_markdown else self._normalize_text_for_sample_text(content)
        chunks = self._split_dingtalk_content(normalized_content)
        if not chunks:
            return []

        total = len(chunks)
        payloads: list[dict[str, str]] = []

        for index, chunk in enumerate(chunks, start=1):
            if use_markdown:
                title = DINGTALK_MARKDOWN_TITLE
                if total > 1:
                    title = f"{title} ({index}/{total})"
                payloads.append(
                    {
                        "msgKey": "sampleMarkdown",
                        "msgParam": json.dumps(
                            {
                                "text": chunk,
                                "title": title[:DINGTALK_MARKDOWN_TITLE_LIMIT],
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            else:
                payloads.append(
                    {
                        "msgKey": "sampleText",
                        "msgParam": json.dumps({"content": chunk}, ensure_ascii=False),
                    }
                )

        return payloads

    async def handle_processing_tick(self, message_id: str, tick_count: int) -> None:
        """Refresh the live card while the backend is still processing."""
        state = self._active_cards_by_biz_id.get(message_id)
        if not state:
            return

        if state.last_tool_name:
            status = f"{self._friendly_tool_status(state.last_tool_name)}，耗时较长，请稍候"
        else:
            status = DINGTALK_PROGRESS_STATUSES[tick_count % len(DINGTALK_PROGRESS_STATUSES)]
        await self._refresh_loading_card(state, status)

    async def send(self, msg: OutboundMessage) -> None:
        """Send or update a DingTalk reply card."""
        if await super().send(msg):
            return

        state = self._get_card_state_from_message(msg)

        if not msg.is_normal_message:
            if not state:
                return
            if msg.event_type == OutboundEventType.TOOL_CALL:
                state.last_tool_name = self._extract_tool_name(msg.content)
            status = self._status_from_event(msg)
            if not status:
                return
            await self._refresh_loading_card(state, status)
            return

        token = await self._get_access_token()
        if not token:
            return

        try:
            rendered_content = await self._replace_inline_images(msg.content, token)
            final_sent = await self._send_interactive_final_response(
                token=token,
                chat_id=msg.session_key.chat_id,
                rendered_content=rendered_content,
                state=state,
            )
            if not final_sent:
                await self._send_legacy_message(token, rendered_content, msg.session_key.chat_id)
        except Exception as e:
            logger.exception(f"Error sending DingTalk message: {e}")
            if state:
                error_card = self._build_interactive_card_data("消息生成失败，请稍后重试。")
                await self._update_interactive_card(token, state, error_card)
        finally:
            if state:
                state.last_tool_name = None
                self._forget_card_state(state)

    async def _on_message(self, content: str, sender_id: str, sender_name: str) -> None:
        """Handle incoming message (called by NanobotDingTalkHandler).

        Delegates to BaseChannel._handle_message() which enforces allow_from
        permission checks before publishing to the bus.
        """
        try:
            logger.info(f"DingTalk inbound: {content} from {sender_name}")
            session_key = SessionKey(
                type=str(getattr(self.channel_type, "value", self.channel_type)),
                channel_id=self.channel_id,
                chat_id=sender_id,
            )
            card_state = None
            if self.is_allowed(sender_id):
                card_state = await self._send_loading_card(session_key, sender_id)

            metadata = {
                "sender_name": sender_name,
                "platform": "dingtalk",
            }
            if card_state:
                metadata["message_id"] = card_state.card_biz_id
                metadata["card_biz_id"] = card_state.card_biz_id

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender_id,  # For private chat, chat_id == sender_id
                content=str(content),
                metadata=metadata,
            )
        except Exception as e:
            logger.exception(f"Error publishing DingTalk message: {e}")
