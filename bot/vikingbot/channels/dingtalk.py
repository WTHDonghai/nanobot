"""DingTalk/DingDing channel implementation using Stream Mode."""

import asyncio
import json
import re
import time
import unicodedata
from typing import Any

import httpx
from loguru import logger

from vikingbot.bus.events import OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.base import BaseChannel
from vikingbot.config.schema import DingTalkChannelConfig

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
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
MARKDOWN_DECORATION_PATTERN = re.compile(r"(\*\*|__|\*|_|~~|`)(.+?)\1")
MARKDOWN_TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)")
FLOW_LINE_PATTERN = re.compile(r"[→←↑↓]|->|<-|=>|<=|↔")


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

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through DingTalk."""
        if await super().send(msg):
            return

        # Only send normal response messages, skip thinking/tool_call/etc.
        if not msg.is_normal_message:
            return

        token = await self._get_access_token()
        if not token:
            return

        try:
            rendered_content = await self._replace_inline_images(msg.content, token)
            payloads = self._build_message_payloads(rendered_content)
            if not payloads:
                logger.warning("DingTalk rendered content is empty, skipping send")
                return

            # oToMessages/batchSend: sends to individual users (private chat)
            # https://open.dingtalk.com/document/orgapp/robot-batch-send-messages
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"

            headers = {"x-acs-dingtalk-access-token": token}

            if not self._http:
                logger.warning("DingTalk HTTP client not initialized, cannot send")
                return

            for index, payload in enumerate(payloads, start=1):
                data = {
                    "robotCode": self.config.client_id,
                    "userIds": [msg.session_key.chat_id],  # chat_id is the user's staffId
                    **payload,
                }

                resp = await self._http.post(url, json=data, headers=headers)
                if resp.status_code != 200:
                    logger.exception(
                        f"DingTalk send failed on chunk {index}/{len(payloads)}: {resp.text}"
                    )
                    break

            else:
                logger.debug(
                    f"DingTalk message sent to {msg.session_key.chat_id} in {len(payloads)} chunk(s)"
                )
        except Exception as e:
            logger.exception(f"Error sending DingTalk message: {e}")

    async def _on_message(self, content: str, sender_id: str, sender_name: str) -> None:
        """Handle incoming message (called by NanobotDingTalkHandler).

        Delegates to BaseChannel._handle_message() which enforces allow_from
        permission checks before publishing to the bus.
        """
        try:
            logger.info(f"DingTalk inbound: {content} from {sender_name}")
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender_id,  # For private chat, chat_id == sender_id
                content=str(content),
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                },
            )
        except Exception as e:
            logger.exception(f"Error publishing DingTalk message: {e}")
