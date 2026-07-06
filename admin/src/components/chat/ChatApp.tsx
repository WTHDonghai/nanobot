import React, { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  ChevronRight,
  FileDown,
  GripVertical,
  Loader2,
  MoreHorizontal,
  Pencil,
  SendHorizontal,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  User,
  Zap,
  Headphones,
  Menu,
  X,
} from 'lucide-react';
import { fetchApi, requestHumanHandoff } from '../../services/api';
import {
  ApiEnvelope,
  ChatExperience,
  ChatMessage,
  GuidedQuestionSuggestion,
  MessageFeedback,
  RawSessionListItem,
  SessionArchiveResult,
  SessionContextResult,
  SessionSummary,
} from './types';
import {
  MAX_SESSION_CONTEXT_BUDGET,
  deriveSessionTitleFromMessages,
  formatDuration,
  formatDateTime,
  formatRelativeTime,
  getArchiveIndex,
  getSessionSortTime,
  getSessionId,
  inferIterationCountFromSteps,
  makeWelcomeMessages,
  mapSessionMessages,
  mergeCachedMessageMetadata,
  normalizeGuidedQuestionSuggestions,
  readStoredSessionMessages,
  readStoredSessionTitles,
  unwrapResult,
  shortenSessionId,
  toSingleLine,
  formatShortSessionTime,
  parseIterationFromData,
  renderMessageText,
} from './utils';
import { exportChatSubsetToPdf } from './pdfExport';
import SessionSidebar from './SessionSidebar';
import MarkdownRenderer, { getMarkdownReferenceTarget, MarkdownReferenceTarget } from '../markdown/MarkdownRenderer';
import './ChatApp.css';

const REFERENCE_DRAWER_MIN_WIDTH = 360;
const REFERENCE_DRAWER_MAX_WIDTH = 860;
const REFERENCE_DRAWER_PAGE_GUTTER = 420;
const REFERENCE_EXPORT_CHUNK_MAX_HEIGHT = 1200;
const RESPONSE_STREAM_INTERVAL_MS = 16;
const RESPONSE_STREAM_CHARS_PER_FRAME = 4;

type ReferencePreviewState = {
  href: string;
  uri: string;
  title: string;
  markdown: string;
  loading: boolean;
  error?: string;
};

type ReferencePreviewPayload = {
  title?: string;
  uri?: string;
  markdown?: string;
};

type MessageReferenceItem = {
  id: string;
  label: string;
  subtitle: string;
  target: MarkdownReferenceTarget;
};

type MessageContentSections = {
  body: string;
  references: MessageReferenceItem[];
};

type BotResponseStreamBuffer = {
  displayed: string;
  pending: string;
  timerId: number | null;
  finalText?: string;
};

type FeedbackResponse = {
  session_id: string;
  message_id: string;
  feedback?: MessageFeedback;
};

type SendOptions = {
  text?: string;
  metadata?: Record<string, unknown>;
};

type SessionFeedbackResult = {
  session_id: string;
  feedback?: Record<string, MessageFeedback>;
};

function clampReferenceDrawerWidth(width: number): number {
  const viewportMax = typeof window === 'undefined'
    ? REFERENCE_DRAWER_MAX_WIDTH
    : Math.max(REFERENCE_DRAWER_MIN_WIDTH, window.innerWidth - REFERENCE_DRAWER_PAGE_GUTTER);
  const maxWidth = Math.min(REFERENCE_DRAWER_MAX_WIDTH, viewportMax);
  return Math.min(Math.max(width, REFERENCE_DRAWER_MIN_WIDTH), maxWidth);
}

function decodeReferenceText(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function getReferenceFallbackTitle(uri: string): string {
  const filename = decodeReferenceText(uri.split('/').filter(Boolean).pop() || '');
  return filename.replace(/\.md$/i, '') || '参考文档';
}

function formatReferenceSubtitle(uri: string): string {
  const readablePath = decodeReferenceText(uri.replace(/^viking:\/\/resources\//, ''));
  return readablePath || uri;
}

function cleanReferenceLabel(value: string): string {
  return decodeReferenceText(value)
    .replace(/\\([[\]()])/g, '$1')
    .replace(/[`*_]/g, '')
    .trim();
}

function extractReferenceItems(section: string, serverUrl: string): MessageReferenceItem[] {
  const references: MessageReferenceItem[] = [];
  const seen = new Set<string>();
  const linkPattern = /\[([^\]]+)]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/g;

  for (const match of section.matchAll(linkPattern)) {
    const href = match[2]?.trim() || '';
    const target = getMarkdownReferenceTarget(href, serverUrl);
    if (!target) continue;

    const id = target.uri || href;
    if (seen.has(id)) continue;
    seen.add(id);

    const label = cleanReferenceLabel(match[1] || '') || getReferenceFallbackTitle(target.uri);
    references.push({
      id,
      label,
      subtitle: formatReferenceSubtitle(target.uri),
      target,
    });
  }

  return references;
}

function splitMessageReferenceSection(content: string, serverUrl: string): MessageContentSections {
  if (!content.trim()) return { body: content, references: [] };

  const normalized = content.replace(/\r\n?/g, '\n').trimEnd();
  const headingPattern = /^[ \t]*(?:#{1,6}[ \t]*)?参考文档[ \t]*$/gm;
  const headings = Array.from(normalized.matchAll(headingPattern));

  for (let index = headings.length - 1; index >= 0; index -= 1) {
    const heading = headings[index];
    const headingStart = heading.index ?? 0;
    const headingEnd = headingStart + heading[0].length;
    const section = normalized.slice(headingEnd).replace(/^\n+/, '').trim();
    const references = extractReferenceItems(section, serverUrl);

    if (references.length > 0) {
      return {
        body: normalized.slice(0, headingStart).trimEnd(),
        references,
      };
    }
  }

  return { body: content, references: [] };
}

function buildMarkdownExportChunks(root: HTMLElement, chunkClassName: string): HTMLElement[] {
  const markdownRoot = root.querySelector<HTMLElement>('.markdown-body') || root;
  const children = Array.from(markdownRoot.children) as HTMLElement[];

  if (children.length === 0) {
    return [markdownRoot];
  }

  const chunks: HTMLElement[] = [];
  const createChunk = () => {
    const chunk = document.createElement('div');
    chunk.className = chunkClassName;
    return chunk;
  };
  let currentChunk = createChunk();
  let currentHeight = 0;

  const pushChunk = () => {
    if (currentChunk.children.length > 0) {
      chunks.push(currentChunk);
    }
    currentChunk = createChunk();
    currentHeight = 0;
  };

  children.forEach((child) => {
    const bounds = child.getBoundingClientRect();
    const estimatedHeight = Math.max(bounds.height, child.scrollHeight, child.offsetHeight, 24);
    const containsMedia = Boolean(child.querySelector('img, table, pre'));

    if (
      currentChunk.children.length > 0
      && (containsMedia || currentHeight + estimatedHeight > REFERENCE_EXPORT_CHUNK_MAX_HEIGHT)
    ) {
      pushChunk();
    }

    currentChunk.appendChild(child.cloneNode(true));
    currentHeight += estimatedHeight;

    if (containsMedia) {
      pushChunk();
    }
  });

  if (currentChunk.children.length > 0) {
    chunks.push(currentChunk);
  }

  return chunks.length > 0 ? chunks : [markdownRoot];
}

function buildReferenceExportNodes(root: HTMLElement): HTMLElement[] {
  return buildMarkdownExportChunks(root, 'chat-reference-markdown markdown-body chat-reference-export-chunk');
}

function cloneChatRowShell(row: HTMLElement): HTMLElement {
  const clone = row.cloneNode(true) as HTMLElement;
  clone.querySelectorAll('[data-export-ignore="true"]').forEach((element) => element.remove());

  const bubble = clone.querySelector<HTMLElement>('.chat-bubble');
  if (bubble) {
    bubble.innerHTML = '';
  }

  return clone;
}

function setChatRowBubbleContent(row: HTMLElement, content: Node): HTMLElement {
  const bubble = row.querySelector<HTMLElement>('.chat-bubble');
  if (bubble) {
    bubble.innerHTML = '';
    bubble.appendChild(content);
  }
  return row;
}

function buildChatMessageExportNodes(row: HTMLElement): HTMLElement[] {
  const bubble = row.querySelector<HTMLElement>('.chat-bubble');
  const markdownRoot = bubble?.querySelector<HTMLElement>('.markdown-body') || null;

  if (!bubble || !markdownRoot) {
    return [row];
  }

  const chunks = buildMarkdownExportChunks(markdownRoot, 'markdown-body chat-message-export-chunk');
  if (chunks.length <= 1) {
    return [row];
  }

  const exportRows: HTMLElement[] = [];

  chunks.forEach((chunk) => {
    exportRows.push(setChatRowBubbleContent(cloneChatRowShell(row), chunk));
  });

  const suffix = cloneChatRowShell(row);
  const suffixBubble = suffix.querySelector<HTMLElement>('.chat-bubble');
  const suffixChildren = Array.from(bubble.childNodes).filter((child) => child !== markdownRoot);
  if (suffixBubble && suffixChildren.length > 0) {
    suffixChildren.forEach((child) => suffixBubble.appendChild(child.cloneNode(true)));
    if (suffixBubble.childNodes.length > 0) {
      exportRows.push(suffix);
    }
  }

  return exportRows.length > 0 ? exportRows : [row];
}

const ChatBubbleReferences = ({
  references,
  onOpenReference,
}: {
  references: MessageReferenceItem[];
  onOpenReference: (target: MarkdownReferenceTarget, label: string) => void;
}) => {
  const [open, setOpen] = useState(true);

  if (references.length === 0) return null;

  return (
    <details
      className="chat-bubble-references"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="chat-bubble-references-summary">
        <span>参考文档</span>
        <span className="chat-bubble-reference-count">{references.length} 项</span>
        <ChevronRight size={15} className="chat-bubble-references-chevron" />
      </summary>
      <div className="chat-bubble-reference-list">
        {references.map((reference, index) => (
          <button
            key={reference.id}
            type="button"
            className="chat-bubble-reference-item"
            onClick={() => onOpenReference(reference.target, reference.label)}
            title="在右侧预览参考文档"
          >
            <span className="chat-bubble-reference-index">{index + 1}</span>
            <span className="chat-bubble-reference-text">
              <span className="chat-bubble-reference-title">{reference.label}</span>
              <span className="chat-bubble-reference-subtitle">{reference.subtitle}</span>
            </span>
          </button>
        ))}
      </div>
    </details>
  );
};

const ConfirmModal = ({
  message,
  onConfirm,
  onCancel,
}: {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}) => (
  <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
    <div className="modal">
      <div className="modal-title" style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--danger)' }}>
        <AlertTriangle size={18} /> 操作确认
      </div>
      <div className="modal-body">
        <p style={{ margin: 0, lineHeight: 1.6 }}>{message}</p>
      </div>
      <div className="modal-footer">
        <button className="btn btn-ghost" onClick={onCancel}>取消</button>
        <button className="btn btn-danger" onClick={onConfirm}>确认删除</button>
      </div>
    </div>
  </div>
);

const RenameModal = ({
  value,
  defaultTitle,
  canReset,
  onChange,
  onConfirm,
  onReset,
  onCancel,
}: {
  value: string;
  defaultTitle: string;
  canReset: boolean;
  onChange: (value: string) => void;
  onConfirm: () => void;
  onReset: () => void;
  onCancel: () => void;
}) => (
  <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
    <div className="modal">
      <div className="modal-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Pencil size={18} /> 重命名会话
      </div>
      <div className="modal-body">
        <p className="chat-rename-note">仅保存在当前浏览器，不会写入服务端。</p>
        <div className="form-group" style={{ marginTop: 16 }}>
          <label>会话标题</label>
          <input
            className="input"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={defaultTitle}
            maxLength={200}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                onConfirm();
              }
            }}
          />
        </div>
        <div className="chat-rename-hint">默认标题：{defaultTitle}</div>
      </div>
      <div className="modal-footer chat-rename-footer">
        {canReset && <button className="btn btn-ghost" onClick={onReset}>恢复默认</button>}
        <button className="btn btn-ghost" onClick={onCancel}>取消</button>
        <button className="btn btn-primary" onClick={onConfirm} disabled={!value.trim()}>保存</button>
      </div>
    </div>
  </div>
);

export type ChatAppProps = {
  serverUrl: string;
  apiKey: string;
  accountId: string;
  userId: string;
  role: string;
  experience?: ChatExperience;
  hideUserSelector?: boolean;
};

function summarizeReasoningEvent(data: unknown): string {
  const raw = typeof data === 'string' ? data : String(data ?? '');
  const trimmed = raw.trim();
  if (!trimmed) return '正在规划回答路径...';

  if (trimmed === 'Request received. Preparing context...') {
    return '已接收请求，正在准备上下文...';
  }

  const segments = trimmed
    .split(/\r?\n+/)
    .map((line) => line.replace(/^[-*#\d.\s]+/, '').trim())
    .filter(Boolean)
    .slice(0, 3);

  const summary = (segments.length > 0 ? segments.join(' · ') : trimmed)
    .replace(/\s+/g, ' ')
    .trim();

  if (!summary) return '正在规划回答路径...';
  return summary.length > 120 ? `${summary.slice(0, 117)}...` : summary;
}

function summarizeIterationEvent(data: unknown): string {
  const parsed = parseIterationFromData(data);
  if (parsed) {
    return `第 ${parsed.current} / ${parsed.total} 轮规划中...`;
  }
  return '正在进入下一轮分析...';
}

function extractReferencePreviewMarkdown(raw: string, fallbackTitle: string): { title: string; markdown: string } {
  const content = String(raw || '');
  const looksLikeHtml = /<!doctype html|<html[\s>]/i.test(content);
  if (!looksLikeHtml || typeof DOMParser === 'undefined') {
    return { title: fallbackTitle, markdown: content.trim() };
  }

  const doc = new DOMParser().parseFromString(content, 'text/html');
  const title = doc.querySelector('h1')?.textContent?.trim()
    || doc.title?.trim()
    || fallbackTitle;
  const markdown = doc.querySelector('pre')?.textContent?.trim()
    || doc.body?.textContent?.trim()
    || '';

  return { title, markdown };
}

const ChatApp: React.FC<ChatAppProps> = ({
  serverUrl,
  apiKey,
  accountId,
  userId,
  role,
  experience = 'default',
  hideUserSelector = false,
}) => {
  const isGuestExperience = experience === 'guest';
  const welcomeText = `您好，我是您的AI工作助手，可为您提供：

1）全系统操作指南；

2）常见问题排查。

请直接输入您的问题或指令。`
  const makeInitialMessages = (status = '') => makeWelcomeMessages(status, welcomeText);
  const [messages, setMessages] = useState<ChatMessage[]>(makeInitialMessages());
  const [input, setInput] = useState('');
  const [selectedAccountId, setSelectedAccountId] = useState('');
  const [selectedUserId, setSelectedUserId] = useState('');
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [derivedSessionTitles, setDerivedSessionTitles] = useState<Record<string, string>>({});
  const [renamedSessionTitles, setRenamedSessionTitles] = useState<Record<string, string>>({});
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [activeSessionMeta, setActiveSessionMeta] = useState<SessionSummary | null>(null);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [exportingMessageKey, setExportingMessageKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [sessionListLoading, setSessionListLoading] = useState(false);
  const [sessionReplayLoading, setSessionReplayLoading] = useState(false);
  const [sessionMutating, setSessionMutating] = useState(false);
  const [sessionError, setSessionError] = useState('');
  const [handoffNotice, setHandoffNotice] = useState('');
  const [handoffLoadingKey, setHandoffLoadingKey] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [activeActionMenu, setActiveActionMenu] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<SessionSummary | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  const [referencePreview, setReferencePreview] = useState<ReferencePreviewState | null>(null);
  const [referenceDrawerWidth, setReferenceDrawerWidth] = useState(() => clampReferenceDrawerWidth(440));
  const [referenceExporting, setReferenceExporting] = useState(false);
  const actionMenuRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messageRowRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const referencePreviewContentRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const sessionMessageCacheRef = useRef<Record<string, ChatMessage[]>>({});
  const sessionListRequestRef = useRef(0);
  const replayRequestRef = useRef(0);
  const referencePreviewRequestRef = useRef(0);
  const referenceResizeCleanupRef = useRef<(() => void) | null>(null);
  const responseStreamBuffersRef = useRef<Record<string, BotResponseStreamBuffer>>({});

  const updateBotMessage = (messageKey: string, patch: Partial<ChatMessage>) => {
    setMessages((prev) => prev.map((message) => (
      message.key === messageKey
        ? { ...message, ...patch }
        : message
    )));
  };

  const updateChatMessage = (messageKey: string, patch: Partial<ChatMessage>) => {
    setMessages((prev) => prev.map((message) => (
      message.key === messageKey
        ? { ...message, ...patch }
        : message
    )));
  };

  const mergeFeedbackIntoMessages = (
    sourceMessages: ChatMessage[],
    feedback: Record<string, MessageFeedback> = {},
  ) => sourceMessages.map((message) => {
    if (!message.messageId) return message;
    const item = feedback[message.messageId];
    return item?.value ? { ...message, feedback: item.value } : message;
  });

  const finishBotResponseStream = (messageKey: string) => {
    const buffer = responseStreamBuffersRef.current[messageKey];
    if (!buffer) return;

    if (buffer.timerId !== null) {
      window.clearTimeout(buffer.timerId);
    }
    const text = buffer.finalText ?? `${buffer.displayed}${buffer.pending}`;
    delete responseStreamBuffersRef.current[messageKey];
    updateBotMessage(messageKey, { text, streaming: false });
  };

  const scheduleBotResponseStream = (messageKey: string) => {
    const buffer = responseStreamBuffersRef.current[messageKey];
    if (!buffer || buffer.timerId !== null) return;

    buffer.timerId = window.setTimeout(() => {
      const activeBuffer = responseStreamBuffersRef.current[messageKey];
      if (!activeBuffer) return;

      activeBuffer.timerId = null;
      if (activeBuffer.pending.length === 0) {
        if (activeBuffer.finalText !== undefined) {
          finishBotResponseStream(messageKey);
        }
        return;
      }

      const nextChunk = activeBuffer.pending.slice(0, RESPONSE_STREAM_CHARS_PER_FRAME);
      activeBuffer.pending = activeBuffer.pending.slice(nextChunk.length);
      activeBuffer.displayed += nextChunk;
      updateBotMessage(messageKey, {
        text: activeBuffer.displayed,
        streaming: true,
      });

      scheduleBotResponseStream(messageKey);
    }, RESPONSE_STREAM_INTERVAL_MS);
  };

  const enqueueBotResponseDelta = (
    messageKey: string,
    delta: string,
    createdAt?: string,
  ) => {
    if (!delta) return;

    const existing = responseStreamBuffersRef.current[messageKey];
    const buffer = existing || {
      displayed: '',
      pending: '',
      timerId: null,
    };
    buffer.pending += delta;
    responseStreamBuffersRef.current[messageKey] = buffer;

    updateBotMessage(messageKey, {
      status: 'Bot 回复',
      streaming: true,
      ...(createdAt ? { createdAt } : {}),
    });
    scheduleBotResponseStream(messageKey);
  };

  const completeBotResponseStream = (messageKey: string, finalText?: string) => {
    const buffer = responseStreamBuffersRef.current[messageKey];
    if (!buffer) {
      if (finalText !== undefined) {
        updateBotMessage(messageKey, { text: finalText, streaming: false });
      }
      return;
    }

    buffer.finalText = finalText ?? `${buffer.displayed}${buffer.pending}`;
    if (buffer.pending.length === 0) {
      finishBotResponseStream(messageKey);
    } else {
      scheduleBotResponseStream(messageKey);
    }
  };

  const cancelBotResponseStream = (messageKey: string) => {
    const buffer = responseStreamBuffersRef.current[messageKey];
    if (buffer?.timerId !== null && buffer?.timerId !== undefined) {
      window.clearTimeout(buffer.timerId);
    }
    delete responseStreamBuffersRef.current[messageKey];
  };

  const clearAllBotResponseStreams = () => {
    Object.keys(responseStreamBuffersRef.current).forEach(cancelBotResponseStream);
  };

  useEffect(() => () => {
    clearAllBotResponseStreams();
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;

    const mediaQuery = window.matchMedia('(min-width: 1181px)');
    const closeSidebarOnDesktop = (matches: boolean) => {
      if (matches) {
        setIsSidebarOpen(false);
      }
    };

    closeSidebarOnDesktop(mediaQuery.matches);

    const handleChange = (event: MediaQueryListEvent) => {
      closeSidebarOnDesktop(event.matches);
    };

    mediaQuery.addEventListener('change', handleChange);
    return () => {
      mediaQuery.removeEventListener('change', handleChange);
    };
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;

    const handleResize = () => {
      setReferenceDrawerWidth((width) => clampReferenceDrawerWidth(width));
    };

    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      referenceResizeCleanupRef.current?.();
      referenceResizeCleanupRef.current = null;
      document.body.classList.remove('chat-reference-resizing');
    };
  }, []);

  function scrollToBottom() {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }

  function resetInputHeight() {
    if (inputRef.current) {
      inputRef.current.style.height = 'auto';
      inputRef.current.style.overflowY = 'hidden';
    }
  }

  function resetConversation(status = '') {
    clearAllBotResponseStreams();
    closeReferencePreview();
    persistLastActiveSession(null);
    setSessionId(null);
    setActiveSessionMeta(null);
    setMessages(makeInitialMessages(status));
    setInput('');
    setHandoffNotice('');
    setHandoffLoadingKey(null);
    resetInputHeight();
  }

  function closeReferencePreview() {
    referencePreviewRequestRef.current += 1;
    setReferencePreview(null);
  }

  function beginReferenceDrawerResize(event: React.PointerEvent<HTMLButtonElement>) {
    if (typeof window === 'undefined' || window.innerWidth <= 1180) return;

    event.preventDefault();
    referenceResizeCleanupRef.current?.();

    const startX = event.clientX;
    const startWidth = referenceDrawerWidth;

    document.body.classList.add('chat-reference-resizing');

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const nextWidth = startWidth + startX - moveEvent.clientX;
      setReferenceDrawerWidth(clampReferenceDrawerWidth(nextWidth));
    };
    const stopResize = () => {
      document.body.classList.remove('chat-reference-resizing');
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', stopResize);
      window.removeEventListener('pointercancel', stopResize);
      referenceResizeCleanupRef.current = null;
    };
    referenceResizeCleanupRef.current = stopResize;

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', stopResize, { once: true });
    window.addEventListener('pointercancel', stopResize, { once: true });
  }

  async function openReferencePreview(target: MarkdownReferenceTarget, label = '') {
    const requestId = ++referencePreviewRequestRef.current;
    const fallbackTitle = label.trim() || getReferenceFallbackTitle(target.uri);
    const previewParams = new URLSearchParams({ uri: target.uri });
    if (target.token) previewParams.set('token', target.token);
    const previewPath = `/bot/v1/resources/preview?${previewParams}`;
    setReferencePreview({
      href: target.href,
      uri: target.uri,
      title: fallbackTitle,
      markdown: '',
      loading: true,
    });

    try {
      const payload = await fetchApi<ReferencePreviewPayload | string>(serverUrl, apiKey, previewPath, {
        headers: { Accept: 'application/json' },
      });
      if (referencePreviewRequestRef.current !== requestId) return;

      const parsed = typeof payload === 'string'
        ? extractReferencePreviewMarkdown(payload, fallbackTitle)
        : {
          title: payload.title || fallbackTitle,
          markdown: payload.markdown || '',
        };
      setReferencePreview({
        href: target.href,
        uri: typeof payload === 'string' ? target.uri : (payload.uri || target.uri),
        title: parsed.title || fallbackTitle,
        markdown: parsed.markdown,
        loading: false,
      });
    } catch (err: unknown) {
      if (referencePreviewRequestRef.current !== requestId) return;
      setReferencePreview({
        href: target.href,
        uri: target.uri,
        title: fallbackTitle,
        markdown: '',
        loading: false,
        error: err instanceof Error ? err.message : '参考文档加载失败',
      });
    }
  }

  function getSessionRequestOptions(): { account?: string; user?: string } {
    if (role === 'user') return {};
    return {
      ...(selectedAccountId ? { account: selectedAccountId } : {}),
      ...(selectedUserId ? { user: selectedUserId } : {}),
    };
  }

  function getSessionTitleStorageKey(): string | null {
    const accountScope = role === 'user' ? (accountId || selectedAccountId || 'default') : selectedAccountId;
    const userScope = role === 'user' ? (selectedUserId || userId || '') : selectedUserId;
    if (!serverUrl || !accountScope || !userScope) return null;
    return `ov:bot:session-titles:${serverUrl}:${accountScope}:${userScope}`;
  }

  function getSessionMessageCacheStorageKey(): string | null {
    const accountScope = role === 'user' ? (accountId || selectedAccountId || 'default') : selectedAccountId;
    const userScope = role === 'user' ? (selectedUserId || userId || '') : selectedUserId;
    if (!serverUrl || !accountScope || !userScope) return null;
    return `ov:bot:session-cache:${serverUrl}:${accountScope}:${userScope}`;
  }

  function getDerivedSessionTitleStorageKey(): string | null {
    const accountScope = role === 'user' ? (accountId || selectedAccountId || 'default') : selectedAccountId;
    const userScope = role === 'user' ? (selectedUserId || userId || '') : selectedUserId;
    if (!serverUrl || !accountScope || !userScope) return null;
    return `ov:bot:session-derived-titles:${serverUrl}:${accountScope}:${userScope}`;
  }

  function getLastActiveSessionStorageKey(): string | null {
    const accountScope = role === 'user' ? (accountId || selectedAccountId || 'default') : selectedAccountId;
    const userScope = role === 'user' ? (selectedUserId || userId || '') : selectedUserId;
    if (!serverUrl || !accountScope || !userScope) return null;
    return `ov:bot:last-session:${serverUrl}:${accountScope}:${userScope}`;
  }

  function persistLastActiveSession(nextSessionId: string | null) {
    const storageKey = getLastActiveSessionStorageKey();
    if (!storageKey) return;

    try {
      if (nextSessionId) {
        window.sessionStorage.setItem(storageKey, nextSessionId);
      } else {
        window.sessionStorage.removeItem(storageKey);
      }
    } catch {
      // Ignore browser storage quota and privacy mode errors.
    }
  }

  function readLastActiveSession(): string | null {
    const storageKey = getLastActiveSessionStorageKey();
    if (!storageKey) return null;

    try {
      return window.sessionStorage.getItem(storageKey);
    } catch {
      return null;
    }
  }

  function persistSessionMessageCache(cache: Record<string, ChatMessage[]>) {
    const storageKey = getSessionMessageCacheStorageKey();
    if (!storageKey) return;

    try {
      if (Object.keys(cache).length === 0) {
        window.sessionStorage.removeItem(storageKey);
      } else {
        window.sessionStorage.setItem(storageKey, JSON.stringify(cache));
      }
    } catch {
      // Ignore browser storage quota and privacy mode errors.
    }
  }

  function updateSessionMessageCache(
    updater: (prev: Record<string, ChatMessage[]>) => Record<string, ChatMessage[]>,
  ) {
    const next = updater(sessionMessageCacheRef.current);
    sessionMessageCacheRef.current = next;
    persistSessionMessageCache(next);
  }

  function setCachedSessionMessages(targetSessionId: string, nextMessages: ChatMessage[]) {
    const cacheableMessages = nextMessages.map((message) => ({
      ...message,
      loading: false,
      streaming: false,
    }));
    updateSessionMessageCache((prev) => ({
      ...prev,
      [targetSessionId]: cacheableMessages,
    }));
  }

  function removeCachedSessionMessages(targetSessionId: string) {
    updateSessionMessageCache((prev) => {
      if (!prev[targetSessionId]) return prev;
      const next = { ...prev };
      delete next[targetSessionId];
      return next;
    });
  }

  function rememberSessionTitle(targetSessionId: string, nextTitle: string, overwrite = false) {
    const normalizedTitle = toSingleLine(nextTitle).slice(0, 200);
    if (!normalizedTitle) return;

    setDerivedSessionTitles((prev) => {
      if (!overwrite && prev[targetSessionId]) return prev;
      if (prev[targetSessionId] === normalizedTitle) return prev;
      
      const next = { ...prev, [targetSessionId]: normalizedTitle };
      const storageKey = getDerivedSessionTitleStorageKey();
      try {
        if (storageKey) {
          window.localStorage.setItem(storageKey, JSON.stringify(next));
        }
      } catch {}

      return next;
    });
  }

  function getDefaultSessionTitle(session: SessionSummary): string {
    const cachedTitle = derivedSessionTitles[session.session_id];
    if (cachedTitle) return cachedTitle;

    const fallbackTime = formatShortSessionTime(session.created_at || session.updated_at);
    if (fallbackTime !== '未命名会话') return `${fallbackTime} 的会话`;
    if ((session.message_count ?? 0) === 0) return '空白新会话';
    return '未命名会话';
  }

  function getSessionTitle(session: SessionSummary): string {
    return renamedSessionTitles[session.session_id] || getDefaultSessionTitle(session);
  }

  function getSessionSubtitle(session: SessionSummary): string {
    if (typeof session.message_count === 'number' && session.message_count > 0) {
      return `${session.message_count} 条消息`;
    }
    const timeStr = formatShortSessionTime(session.created_at || session.updated_at);
    return timeStr !== '未命名会话' ? `创建于 ${timeStr}` : '初始化中...';
  }

  function toggleActionMenu(menuId: string) {
    setActiveActionMenu((prev) => prev === menuId ? null : menuId);
  }

  function closeActionMenu() {
    setActiveActionMenu(null);
  }

  function openRenameDialog(targetSession: SessionSummary) {
    closeActionMenu();
    setRenameTarget(targetSession);
    setRenameValue(getSessionTitle(targetSession));
  }

  function updateRenamedSessionTitles(
    updater: (prev: Record<string, string>) => Record<string, string>,
  ) {
    setRenamedSessionTitles((prev) => {
      const next = updater(prev);
      const storageKey = getSessionTitleStorageKey();

      try {
        if (storageKey) {
          if (Object.keys(next).length === 0) {
            window.localStorage.removeItem(storageKey);
          } else {
            window.localStorage.setItem(storageKey, JSON.stringify(next));
          }
        }
      } catch {
        // Ignore local storage errors and keep the in-memory rename state usable.
      }

      return next;
    });
  }

  function closeRenameDialog() {
    setRenameTarget(null);
    setRenameValue('');
  }

  function confirmRenameSession() {
    if (!renameTarget) return;
    const normalizedTitle = toSingleLine(renameValue).slice(0, 200);
    if (!normalizedTitle) return;

    updateRenamedSessionTitles((prev) => ({
      ...prev,
      [renameTarget.session_id]: normalizedTitle,
    }));
    closeRenameDialog();
  }

  function resetRenamedSessionTitle() {
    if (!renameTarget) return;

    updateRenamedSessionTitles((prev) => {
      if (!prev[renameTarget.session_id]) return prev;
      const next = { ...prev };
      delete next[renameTarget.session_id];
      return next;
    });
    closeRenameDialog();
  }

  async function fetchSessionDetail(targetSessionId: string): Promise<SessionSummary> {
    const response = await fetchApi<ApiEnvelope<SessionSummary>>(
      serverUrl,
      apiKey,
      `/api/v1/sessions/${encodeURIComponent(targetSessionId)}`,
      getSessionRequestOptions(),
    );
    return unwrapResult(response, '加载会话详情失败');
  }

  async function loadSessions(preferredSessionId?: string | null): Promise<SessionSummary[]> {
    const ready = role === 'user' ? Boolean(selectedUserId) : Boolean(selectedAccountId && selectedUserId);
    if (!ready) {
      setSessions([]);
      setActiveSessionMeta(null);
      setSessionListLoading(false);
      return [];
    }

    const requestId = ++sessionListRequestRef.current;
    setSessionListLoading(true);

    try {
      const response = await fetchApi<ApiEnvelope<RawSessionListItem[]>>(
        serverUrl,
        apiKey,
        '/api/v1/sessions',
        getSessionRequestOptions(),
      );
      const rawList = unwrapResult(response, '获取会话列表失败');
      const ids = rawList.map(getSessionId).filter(Boolean);
      const details = await Promise.all(
        ids.map(async (id) => {
          try {
            return await fetchSessionDetail(id);
          } catch {
            return { session_id: id };
          }
        }),
      );

      if (sessionListRequestRef.current !== requestId) return [];

      details.sort((left, right) => getSessionSortTime(right) - getSessionSortTime(left));
      setSessions(details);

      const activeId = preferredSessionId === undefined ? sessionId : preferredSessionId;
      if (!activeId) {
        setActiveSessionMeta(null);
      } else {
        const matched = details.find((session) => session.session_id === activeId) || null;
        setActiveSessionMeta(matched);
        if (!matched && sessionId === activeId) {
        resetConversation('之前的服务记录已不可用，请开始新的对话');
        }
      }

      setSessionError('');
      return details;
    } catch (err: unknown) {
      if (sessionListRequestRef.current !== requestId) return [];
      setSessions([]);
      setActiveSessionMeta(null);
      setSessionError(err instanceof Error ? err.message : '获取会话列表失败');
      return [];
    } finally {
      if (sessionListRequestRef.current === requestId) {
        setSessionListLoading(false);
      }
    }
  }

  async function requestNewSession(): Promise<string> {
    const response = await fetchApi<ApiEnvelope<{ session_id: string }>>(
      serverUrl,
      apiKey,
      '/api/v1/sessions',
      { method: 'POST', ...getSessionRequestOptions() },
    );
    return unwrapResult(response, '新建会话失败').session_id;
  }

  async function resolveLatestBotMessageId(
    targetSessionId: string,
    finalText: string,
  ): Promise<string | undefined> {
    try {
      const response = await fetchApi<ApiEnvelope<SessionContextResult>>(
        serverUrl,
        apiKey,
        `/api/v1/sessions/${encodeURIComponent(targetSessionId)}/context?token_budget=${MAX_SESSION_CONTEXT_BUDGET}`,
        getSessionRequestOptions(),
      );
      const context = unwrapResult(response, '同步回复 ID 失败');
      const assistantMessages = (context.messages || [])
        .filter((message) => message.role === 'assistant' && message.id);
      const matched = [...assistantMessages]
        .reverse()
        .find((message) => renderMessageText(message.parts, serverUrl).trim() === finalText.trim());
      return matched?.id || assistantMessages[assistantMessages.length - 1]?.id;
    } catch {
      return undefined;
    }
  }

  async function handleNewSession() {
    if (loading || sessionReplayLoading || sessionMutating) return;

    closeActionMenu();
    setSessionMutating(true);
    try {
      const nextSessionId = await requestNewSession();
      const welcomeMessages = makeInitialMessages('新的服务对话已开始');
      setCachedSessionMessages(nextSessionId, welcomeMessages);
      persistLastActiveSession(nextSessionId);
      setSessionId(nextSessionId);
      setActiveSessionMeta({ session_id: nextSessionId, message_count: 0 });
      setSessionError('');
      setMessages(welcomeMessages);
      setInput('');
      resetInputHeight();
      inputRef.current?.focus();
      await loadSessions(nextSessionId);
    } catch (err: unknown) {
      setSessionError(err instanceof Error ? err.message : '新建会话失败');
    } finally {
      setSessionMutating(false);
    }
  }

  async function handleSelectSession(targetSessionId: string) {
    if (loading || sessionReplayLoading || sessionMutating) return;

    closeActionMenu();
    const requestId = ++replayRequestRef.current;
    const cachedMessages = sessionMessageCacheRef.current[targetSessionId];
    const cachedMeta = sessions.find((session) => session.session_id === targetSessionId) || null;

    if (cachedMessages?.length) {
      persistLastActiveSession(targetSessionId);
      setSessionId(targetSessionId);
      setActiveSessionMeta(cachedMeta || { session_id: targetSessionId });
      setMessages(cachedMessages);
      setInput('');
      resetInputHeight();
      setSessionError('');
    }

    setSessionReplayLoading(true);

    try {
      const detailPromise = fetchSessionDetail(targetSessionId).catch(() => null);
      const feedbackPromise = fetchApi<ApiEnvelope<SessionFeedbackResult>>(
        serverUrl,
        apiKey,
        `/api/v1/sessions/${encodeURIComponent(targetSessionId)}/feedback`,
        getSessionRequestOptions(),
      )
        .then((response) => unwrapResult(response, '加载反馈失败').feedback || {})
        .catch(() => ({} as Record<string, MessageFeedback>));
      const contextResponse = await fetchApi<ApiEnvelope<SessionContextResult>>(
        serverUrl,
        apiKey,
        `/api/v1/sessions/${encodeURIComponent(targetSessionId)}/context?token_budget=${MAX_SESSION_CONTEXT_BUDGET}`,
        getSessionRequestOptions(),
      );
      const context = unwrapResult(contextResponse, '加载会话失败');
      const archiveIds = Array.from(
        new Set(
          [
            context.latest_archive_id || '',
            ...(context.pre_archive_abstracts || []).map((item) => item.archive_id),
          ].filter(Boolean),
        ),
      ).sort((left, right) => getArchiveIndex(left) - getArchiveIndex(right));

      const archives = await Promise.all(
        archiveIds.map(async (archiveId) => {
          try {
            const archiveResponse = await fetchApi<ApiEnvelope<SessionArchiveResult>>(
              serverUrl,
              apiKey,
              `/api/v1/sessions/${encodeURIComponent(targetSessionId)}/archives/${encodeURIComponent(archiveId)}`,
              getSessionRequestOptions(),
            );
            return unwrapResult(archiveResponse, `加载归档 ${archiveId} 失败`);
          } catch {
            return null;
          }
        }),
      );

      if (replayRequestRef.current !== requestId) return;

      const archivedMessages = archives
        .filter((archive): archive is SessionArchiveResult => Boolean(archive))
        .flatMap((archive) => archive.messages || []);
      const mergedMessages = [...archivedMessages, ...(context.messages || [])];
      const detail = await detailPromise;
      const feedback = await feedbackPromise;
      const derivedTitle = deriveSessionTitleFromMessages(mergedMessages);

      if (replayRequestRef.current !== requestId) return;

      rememberSessionTitle(targetSessionId, derivedTitle);
      const nextMessages = mergedMessages.length > 0
        ? mergeFeedbackIntoMessages(
          mergeCachedMessageMetadata(mapSessionMessages(mergedMessages, serverUrl, welcomeText), cachedMessages || []),
          feedback,
        )
        : mergeFeedbackIntoMessages(
          cachedMessages?.length ? cachedMessages : mapSessionMessages(mergedMessages, serverUrl, welcomeText),
          feedback,
        );

      setCachedSessionMessages(targetSessionId, nextMessages);
      persistLastActiveSession(targetSessionId);
      setSessionId(targetSessionId);
      setActiveSessionMeta(detail || { session_id: targetSessionId });
      setMessages(nextMessages);
      setInput('');
      resetInputHeight();
      setSessionError('');
      await loadSessions(targetSessionId);
    } catch (err: unknown) {
      if (replayRequestRef.current !== requestId) return;
      setSessionError(err instanceof Error ? err.message : '加载会话失败');
    } finally {
      if (replayRequestRef.current === requestId) {
        setSessionReplayLoading(false);
      }
    }
  }

  async function confirmDeleteSession() {
    if (!deleteTarget) return;

    setSessionMutating(true);
    try {
      await fetchApi<ApiEnvelope<{ session_id: string }>>(
        serverUrl,
        apiKey,
        `/api/v1/sessions/${encodeURIComponent(deleteTarget.session_id)}`,
        { method: 'DELETE', ...getSessionRequestOptions() },
      );

      const deletedId = deleteTarget.session_id;
      closeActionMenu();
      removeCachedSessionMessages(deletedId);
      setDerivedSessionTitles((prev) => {
        if (!prev[deletedId]) return prev;
        const next = { ...prev };
        delete next[deletedId];

        const storageKey = getDerivedSessionTitleStorageKey();
        try {
          if (storageKey) {
            window.localStorage.setItem(storageKey, JSON.stringify(next));
          }
        } catch {}

        return next;
      });
      updateRenamedSessionTitles((prev) => {
        if (!prev[deletedId]) return prev;
        const next = { ...prev };
        delete next[deletedId];
        return next;
      });
      setDeleteTarget(null);
      if (renameTarget?.session_id === deletedId) {
        closeRenameDialog();
      }
      setSessionError('');

      if (sessionId === deletedId) {
        resetConversation('会话已删除，请开始新的对话');
        await loadSessions(null);
      } else {
        await loadSessions(sessionId);
      }
    } catch (err: unknown) {
      setSessionError(err instanceof Error ? err.message : '删除会话失败');
    } finally {
      setSessionMutating(false);
    }
  }

  async function handleBatchDeleteSessions(batchIds: string[]) {
    if (batchIds.length === 0) return;
    const deletedIds: string[] = [];

    try {
      for (const id of batchIds) {
        try {
          await fetchApi<ApiEnvelope<{ session_id: string }>>(
            serverUrl,
            apiKey,
            `/api/v1/sessions/${encodeURIComponent(id)}`,
            { method: 'DELETE', ...getSessionRequestOptions() },
          );
          deletedIds.push(id);
          removeCachedSessionMessages(id);
        } catch (err: unknown) {
          console.error(`Failed to delete session ${id}:`, err);
        }
      }

      setDerivedSessionTitles((prev) => {
        let changed = false;
        const next = { ...prev };
        deletedIds.forEach(id => {
          if (next[id]) {
            delete next[id];
            changed = true;
          }
        });
        if (changed) {
          const storageKey = getDerivedSessionTitleStorageKey();
          try {
            if (storageKey) window.localStorage.setItem(storageKey, JSON.stringify(next));
          } catch {}
        }
        return changed ? next : prev;
      });

      updateRenamedSessionTitles((prev) => {
        let changed = false;
        const next = { ...prev };
        deletedIds.forEach(id => {
          if (next[id]) {
            delete next[id];
            changed = true;
          }
        });
        return changed ? next : prev;
      });

      setSessionError('');

      if (sessionId && deletedIds.includes(sessionId)) {
        resetConversation('会话已被删除，请开始新的对话');
        await loadSessions(null);
      } else {
        await loadSessions(sessionId);
      }
    } catch (err: unknown) {
      setSessionError(err instanceof Error ? err.message : '批量删除过程中出现错误');
    }
  }

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    if (!activeActionMenu) return undefined;

    function handlePointerDown(event: MouseEvent) {
      if (actionMenuRef.current?.contains(event.target as Node)) return;
      closeActionMenu();
    }

    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [activeActionMenu]);

  useEffect(() => {
    if (!sessionId) return;
    setCachedSessionMessages(sessionId, messages);
  }, [sessionId, messages]);

  useEffect(() => {
    setSelectedAccountId(accountId || 'default');
  }, [accountId]);

  useEffect(() => {
    if (hideUserSelector && userId) {
      setSessionError('');
      setSelectedUserId(userId);
    } else if (role !== 'user') {
      if (userId) {
        setSessionError('');
        setSelectedUserId(userId);
      } else {
        setSelectedUserId('');
        setSessionError('当前登录身份缺少 user_id，暂时无法加载会话。');
      }
    } else if (role === 'user') {
      setSessionError('');
      fetchApi<ApiEnvelope<{ user_id?: string }>>(serverUrl, apiKey, '/api/v1/system/whoami')
        .then((res) => {
          const whoami = unwrapResult(res, '获取当前用户失败');
          if (whoami.user_id) {
            setSessionError('');
            setSelectedUserId(whoami.user_id);
          } else {
            setSelectedUserId('');
            setSessionError('服务端未返回当前用户身份，暂时无法加载会话。');
          }
        })
        .catch(() => {
          setSelectedUserId('');
          setSessionError('获取当前身份失败，请刷新页面或重新登录。');
        });
    } else {
      setSelectedUserId('');
      setSessionError('当前身份未就绪，暂时无法加载会话。');
    }
  }, [serverUrl, apiKey, role, hideUserSelector, userId]);

  useEffect(() => {
    const renamedKey = getSessionTitleStorageKey();
    if (renamedKey) {
      setRenamedSessionTitles(readStoredSessionTitles(renamedKey));
    } else {
      setRenamedSessionTitles({});
    }

    const derivedKey = getDerivedSessionTitleStorageKey();
    if (derivedKey) {
      setDerivedSessionTitles(readStoredSessionTitles(derivedKey));
    } else {
      setDerivedSessionTitles({});
    }
  }, [serverUrl, role, accountId, userId, selectedAccountId, selectedUserId]);

  useEffect(() => {
    const storageKey = getSessionMessageCacheStorageKey();
    sessionMessageCacheRef.current = readStoredSessionMessages(storageKey, serverUrl);
  }, [serverUrl, role, accountId, userId, selectedAccountId, selectedUserId]);

  useEffect(() => {
    setSessions([]);
    setSessionError('');
    closeActionMenu();
    setDeleteTarget(null);
    setRenameTarget(null);
    setRenameValue('');
    setPreviewImage(null);

    if (selectedUserId) {
      const lastActiveSessionId = readLastActiveSession();
      const cachedMessages = lastActiveSessionId ? sessionMessageCacheRef.current[lastActiveSessionId] : undefined;

      if (lastActiveSessionId && cachedMessages?.length) {
        setSessionId(lastActiveSessionId);
        setActiveSessionMeta(null);
        setMessages(cachedMessages);
        setInput('');
        resetInputHeight();
      } else {
        resetConversation('');
      }
    } else {
      resetConversation('正在同步身份与会话上下文...');
    }
  }, [selectedAccountId, selectedUserId]);

  useEffect(() => {
    const ready = role === 'user' ? Boolean(selectedUserId) : Boolean(selectedAccountId && selectedUserId);
    if (!ready) {
      setSessions([]);
      setActiveSessionMeta(null);
      return;
    }

    void loadSessions(sessionId);
  }, [serverUrl, apiKey, role, selectedAccountId, selectedUserId, sessionId]);

  const handleSend = async (options: SendOptions = {}) => {
    const requestedText = options.text ?? input;
    if (!requestedText.trim() || loading || !selectedUserId) return;

    const userMsg = requestedText.trim();
    const userCreatedAt = new Date().toISOString();
    const requestMetadata = options.metadata && Object.keys(options.metadata).length > 0
      ? options.metadata
      : undefined;
    setSessionError('');
    setHandoffNotice('');
    setInput('');
    resetInputHeight();
    setLoading(true);

    const botId = `bot-${Date.now()}`;
    const userMessageKey = `user-${Date.now()}`;
    let activeSessionId = sessionId;
    let streamStartedAt: number | null = null;
    let latestEventTimestamp = userCreatedAt;

    setMessages((prev) => [
      ...prev,
      { key: userMessageKey, role: 'user', text: userMsg, createdAt: userCreatedAt },
      {
        key: botId,
        role: 'bot',
        text: '',
        status: '思考中...',
        loading: true,
        createdAt: userCreatedAt,
        steps: [],
        iterationCount: 0,
      },
    ]);

    try {
      if (!activeSessionId) {
        activeSessionId = await requestNewSession();
        persistLastActiveSession(activeSessionId);
        setSessionId(activeSessionId);
        setActiveSessionMeta({ session_id: activeSessionId, message_count: 0 });
      }

      if (activeSessionId) {
        rememberSessionTitle(activeSessionId, userMsg);
      }

      const url = `${serverUrl}/bot/v1/chat/stream`;
      streamStartedAt = performance.now();
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (apiKey) {
        headers['X-API-Key'] = apiKey;
      }
      const res = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers,
        body: JSON.stringify({
          message: userMsg,
          session_id: activeSessionId,
          user_id: selectedUserId,
          ...(requestMetadata ? { metadata: requestMetadata } : {}),
        }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (!res.body) throw new Error('No response body');

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      let finalContent = '';
      let streamedContent = '';
      let receivedResponseDelta = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;

          try {
            const evt = JSON.parse(line.slice(6));
            if (typeof evt.timestamp === 'string') {
              latestEventTimestamp = evt.timestamp;
            }

            if (evt.event === 'error') {
              let detail = 'Bot 服务异常';
              if (typeof evt.data === 'string') {
                try {
                  const parsed = JSON.parse(evt.data) as { error?: string };
                  detail = parsed.error || evt.data;
                } catch {
                  detail = evt.data;
                }
              }
              throw new Error(detail);
            }

            if (evt.event === 'response_delta') {
              const delta = typeof evt.data === 'string' ? evt.data : JSON.stringify(evt.data);
              receivedResponseDelta = true;
              streamedContent += delta;
              enqueueBotResponseDelta(botId, delta, latestEventTimestamp);
              continue;
            }

            if (evt.event === 'suggestions') {
              const suggestions = normalizeGuidedQuestionSuggestions(evt.data);
              if (suggestions.length > 0) {
                updateBotMessage(botId, { suggestions });
              }
              continue;
            }

            let status = '思考中...';
            let currentIteration: number | undefined;

            if (evt.event === 'reasoning') {
              status = summarizeReasoningEvent(evt.data);
            } else if (evt.event === 'iteration') {
              status = summarizeIterationEvent(evt.data);
              currentIteration = parseIterationFromData(evt.data)?.current;
            } else if (evt.event === 'tool_call') {
              let displayStatus = '正在调用工具...';
              try {
                let name = '';
                let args: Record<string, any> = {};
                
                if (typeof evt.data === 'string') {
                  const match = evt.data.match(/^([a-zA-Z0-9_]+)\((.*)\)$/s);
                  if (match) {
                    name = match[1];
                    try { args = JSON.parse(match[2]); } catch {}
                  }
                }
                if (!name) {
                  name = evt.data?.name || '';
                  const argsStr = evt.data?.arguments || evt.data?.args || '';
                  try { args = typeof argsStr === 'string' && argsStr.startsWith('{') ? JSON.parse(argsStr) : argsStr || {}; } catch {}
                }
                
                if (name.includes('search') || name.includes('检索')) {
                  const query = args.query || args.keyword || args.q || '';
                  displayStatus = query ? `正在搜索: "${query}"` : '正在检索资料库...';
                } else if (name.includes('read_file') || name.includes('读取') || name.includes('read')) {
                  const path = args.path || args.file_path || args.filename || args.uri || '';
                  let filename = path.split('/').pop() || path;
                  if (filename.endsWith('.md')) filename = filename.slice(0, -3);
                  displayStatus = filename ? `正在读取文件: ${filename}` : '正在读取文件...';
                } else if (name.includes('python') || name.includes('run_code')) {
                  displayStatus = '正在运行代码进行计算...';
                } else {
                  displayStatus = name ? `正在执行: ${name}` : '正在调用工具...';
                }
              } catch {}
              status = displayStatus;
            } else if (evt.event === 'tool_result') {
              let displayStatus = '处理结果中...';
              try {
                const dataStr = typeof evt.data === 'string' ? evt.data : JSON.stringify(evt.data);
                if (dataStr.startsWith('FindResult')) {
                  const uriMatches = Array.from(dataStr.matchAll(/uri='viking:\/\/resources\/(.*?)'/g));
                  if (uriMatches.length > 0) {
                    const fileNames = uriMatches.map((m: any) => (m[1].split('/').pop() || '').replace(/\.md$/, '')).filter(Boolean);
                    const uniqueNames = Array.from(new Set(fileNames));
                    if (uniqueNames.length > 0) {
                      displayStatus = `提取到相关资源: ${uniqueNames.slice(0, 2).join('、')}${uniqueNames.length > 2 ? ' 等' : ''}`;
                    } else {
                      displayStatus = `找到 ${uriMatches.length} 份匹配资料`;
                    }
                  } else {
                    displayStatus = '搜索完成，未找到直接相关的资料';
                  }
                } else if (dataStr.length > 20) {
                   displayStatus = '已获取内容，正在分析归纳...';
                }
              } catch {}
              status = displayStatus;
            } else if (evt.event === 'response') {
              status = 'Bot 回复';
            }

            if (evt.event === 'response') {
              finalContent = typeof evt.data === 'string' ? evt.data : JSON.stringify(evt.data);
              if (receivedResponseDelta) {
                if (finalContent.startsWith(streamedContent)) {
                  completeBotResponseStream(botId, finalContent);
                } else {
                  cancelBotResponseStream(botId);
                  updateBotMessage(botId, {
                    text: finalContent,
                    streaming: false,
                    createdAt: latestEventTimestamp,
                  });
                }
              } else if (finalContent) {
                enqueueBotResponseDelta(botId, finalContent, latestEventTimestamp);
                completeBotResponseStream(botId, finalContent);
              }
            }

            setMessages((prev) => prev.map((message) => {
              if (message.key === botId) {
                const currentSteps = message.steps || [];
                const isNewStep = status !== '思考中...' && status !== 'Bot 回复' && currentSteps[currentSteps.length - 1] !== status;
                const nextSteps = isNewStep ? [...currentSteps, status] : currentSteps;
                
                const nextMessage = {
                  ...message,
                  status,
                  createdAt: latestEventTimestamp,
                  steps: nextSteps,
                };
                
                if (currentIteration !== undefined) {
                  nextMessage.iterationCount = Math.max(message.iterationCount ?? 0, currentIteration);
                }
                
                return nextMessage;
              }
              return message;
            }));
          } catch (err) {
            if (err instanceof Error) throw err;
          }
        }
      }

      const elapsedMs = streamStartedAt === null
        ? undefined
        : Math.max(0, Math.round(performance.now() - streamStartedAt));
      if (!finalContent && streamedContent) {
        finalContent = streamedContent;
      }
      if (finalContent) {
        completeBotResponseStream(botId, finalContent);
      }
      const resolvedMessageId = activeSessionId
        ? await resolveLatestBotMessageId(activeSessionId, finalContent)
        : undefined;
      const hasPendingResponseStream = Boolean(responseStreamBuffersRef.current[botId]);
      setMessages((prev) => prev.map((message) => (
        message.key === botId
          ? {
            ...message,
            messageId: resolvedMessageId || message.messageId,
            text: hasPendingResponseStream
              ? message.text
              : finalContent || message.text || '（无回复）',
            status: 'Bot 回复',
            loading: false,
            streaming: hasPendingResponseStream,
            createdAt: latestEventTimestamp,
            elapsedMs,
          }
          : message
      )));
    } catch (err: any) {
      const elapsedMs = streamStartedAt === null
        ? undefined
        : Math.max(0, Math.round(performance.now() - streamStartedAt));
      cancelBotResponseStream(botId);
      setMessages((prev) => prev.map((message) => (
        message.key === botId
          ? {
            ...message,
            text: `错误: ${err.message}`,
            status: 'Error',
            loading: false,
            streaming: false,
            createdAt: latestEventTimestamp,
            elapsedMs,
          }
          : message
      )));
    } finally {
      setLoading(false);
      if (activeSessionId) {
        void loadSessions(activeSessionId);
      }
    }
  };

  const handleGuidedQuestionClick = (messageKey: string, suggestion: GuidedQuestionSuggestion) => {
    if (suggestion.selected) return;

    const metadata: Record<string, unknown> = {
      guided_question_id: suggestion.id,
      guided_source_uris: suggestion.source_uris || [],
    };
    if (suggestion.token) {
      metadata.guided_question_token = suggestion.token;
    }

    updateChatMessage(messageKey, {
      suggestions: messages
        .find((message) => message.key === messageKey)
        ?.suggestions
        ?.map((item) => ({
          ...item,
          selected: item.id === suggestion.id
            || item.canonical_question === suggestion.canonical_question,
        })),
    });

    void handleSend({
      text: suggestion.canonical_question || suggestion.display_text,
      metadata,
    });
  };

  const handleHumanHandoff = async (targetMessage?: ChatMessage) => {
    if (!selectedUserId || handoffLoadingKey) return;

    const fallbackBotMessage = [...messages]
      .reverse()
      .find((message) => message.role === 'bot' && !message.loading && message.key !== 'welcome');
    const latestUserMessage = [...messages]
      .reverse()
      .find((message) => message.role === 'user' && message.text.trim() && message.key !== 'welcome');
    const effectiveBotMessage = targetMessage || fallbackBotMessage;
    const normalizedBotMessage = effectiveBotMessage ? toSingleLine(effectiveBotMessage.text).slice(0, 1000) : '';
    const normalizedUserMessage = latestUserMessage ? toSingleLine(latestUserMessage.text).slice(0, 1000) : '';
    const triggerMessageKey = effectiveBotMessage?.key || 'global-handoff';

    setSessionError('');
    setHandoffNotice('');
    setHandoffLoadingKey(triggerMessageKey);

    try {
      const response = await requestHumanHandoff(serverUrl, apiKey, {
        session_id: sessionId || undefined,
        user_id: selectedUserId,
        reason: 'user_requested_human_handoff',
        summary: normalizedBotMessage || undefined,
        latest_user_message: normalizedUserMessage || undefined,
        latest_assistant_message: normalizedBotMessage || undefined,
        source: isGuestExperience ? 'guest_public_ui' : 'admin_chat_ui',
        metadata: {
          account_id: role === 'user' ? accountId : selectedAccountId,
          ui_role: role,
          ui_experience: experience,
          trigger_message_key: triggerMessageKey,
        },
      });

      const entryUrl = response.entry_url?.trim();
      if (!entryUrl) {
        throw new Error('人工服务入口暂不可用，请稍后重试。');
      }

      setHandoffNotice(response.message || '已为您准备人工服务入口。');
      window.location.assign(entryUrl);
    } catch (err) {
      setSessionError(err instanceof Error ? err.message : '联系人工失败，请稍后重试。');
    } finally {
      setHandoffLoadingKey(null);
    }
  };

  const handleMessageFeedback = async (message: ChatMessage, value: 'up' | 'down') => {
    if (!sessionId || !message.messageId || message.loading || message.streaming) return;

    const previous = message.feedback;
    updateChatMessage(message.key, { feedback: value });
    setSessionError('');

    try {
      const response = await fetchApi<ApiEnvelope<FeedbackResponse>>(
        serverUrl,
        apiKey,
        `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(message.messageId)}/feedback`,
        {
          method: 'PUT',
          body: JSON.stringify({ value }),
          ...getSessionRequestOptions(),
        },
      );
      const result = unwrapResult(response, '反馈提交失败');
      updateChatMessage(message.key, { feedback: result.feedback?.value || value });
      void loadSessions(sessionId);
    } catch (err: unknown) {
      updateChatMessage(message.key, { feedback: previous });
      setSessionError(err instanceof Error ? err.message : '反馈提交失败，请稍后重试');
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void handleSend();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    e.target.style.height = 'auto';
    const scrollHeight = e.target.scrollHeight;
    e.target.style.height = `${Math.min(scrollHeight, 200)}px`;
    e.target.style.overflowY = scrollHeight > 200 ? 'auto' : 'hidden';
  };

  const ready = role === 'user' ? Boolean(selectedUserId) : Boolean(selectedAccountId && selectedUserId);
  const busy = loading || sessionReplayLoading || sessionMutating;
  const notReadyMessage = sessionError
    ? '身份未就绪，请先处理上方错误提示。'
    : '正在为您准备服务记录...';
  const latestBotMessage = [...messages]
    .reverse()
    .find((message) => message.role === 'bot' && !message.loading && message.key !== 'welcome');
  const handoffButtonLoading = handoffLoadingKey === 'global-handoff'
    || handoffLoadingKey === latestBotMessage?.key;
  const currentSessionLabel = '当前服务';
  const currentSessionActionLabel = '当前服务操作';
  const emptySessionTitle = '等待开始';
  const activeSessionTitle = sessionId
    ? (renamedSessionTitles[sessionId] || derivedSessionTitles[sessionId] || (activeSessionMeta ? getSessionTitle(activeSessionMeta) : currentSessionLabel))
    : emptySessionTitle;

  const handleExportPdf = async (message: ChatMessage, index: number) => {
    if (busy || exportingMessageKey) return;

    const exportNodes = messages
      .slice(0, index + 1)
      .filter((item) => item.key !== 'welcome')
      .map((item) => messageRowRefs.current[item.key])
      .filter((node): node is HTMLDivElement => Boolean(node))
      .flatMap((node) => buildChatMessageExportNodes(node));

    if (exportNodes.length === 0) {
      setSessionError('没有可导出的消息内容');
      return;
    }

    const exportTitle = activeSessionTitle === emptySessionTitle
      ? '对话导出'
      : activeSessionTitle;
    const exportTime = formatDateTime(new Date().toISOString());
    const exportStamp = exportTime.replace(/[^\d]/g, '').slice(0, 14) || `${Date.now()}`;

    setExportingMessageKey(message.key);
    setSessionError('');

    try {
      await exportChatSubsetToPdf({
        title: exportTitle,
        exportedAtLabel: exportTime,
        filenameBase: `${exportTitle}-${exportStamp}`,
        messageNodes: exportNodes,
        apiKey,
        captureScale: 1,
        imageQuality: 0.86,
      });
    } catch (err: unknown) {
      setSessionError(err instanceof Error ? `导出 PDF 失败：${err.message}` : '导出 PDF 失败');
    } finally {
      setExportingMessageKey(null);
    }
  };

  const handleExportReferencePdf = async () => {
    if (!referencePreview || referencePreview.loading || referencePreview.error || referenceExporting) return;

    const exportNode = referencePreviewContentRef.current;
    if (!exportNode) {
      setSessionError('没有可导出的参考文档内容');
      return;
    }

    const exportTime = formatDateTime(new Date().toISOString());
    const exportStamp = exportTime.replace(/[^\d]/g, '').slice(0, 14) || `${Date.now()}`;
    const exportTitle = referencePreview.title || '参考文档';

    setReferenceExporting(true);
    setSessionError('');

    try {
      const exportNodes = buildReferenceExportNodes(exportNode);
      await exportChatSubsetToPdf({
        title: exportTitle,
        exportedAtLabel: exportTime,
        filenameBase: `${exportTitle}-${exportStamp}`,
        messageNodes: exportNodes,
        apiKey,
      });
    } catch (err: unknown) {
      setSessionError(err instanceof Error ? `参考文档导出失败：${err.message}` : '参考文档导出失败');
    } finally {
      setReferenceExporting(false);
    }
  };

  return (
    <div className={`chat-layout ${isSidebarOpen ? 'sidebar-open' : ''}`}>
      <div className="mobile-overlay" onClick={() => setIsSidebarOpen(false)} />
      <SessionSidebar
        sessions={sessions}
        sessionId={sessionId}
        ready={ready}
        busy={busy}
        sessionListLoading={sessionListLoading}
        notReadyMessage={notReadyMessage}
        onSelectSession={(id) => { 
          setIsSidebarOpen(false);
          void handleSelectSession(id); 
        }}
        onNewSession={() => {
          setIsSidebarOpen(false);
          handleNewSession();
        }}
        onRenameSession={openRenameDialog}
        onDeleteSession={(session) => { setDeleteTarget(session); }}
        onBatchDelete={handleBatchDeleteSessions}
        getSessionTitle={getSessionTitle}
        getSessionSubtitle={getSessionSubtitle}
      />

      <div className="chat-main-panel">
        <div className="chat-config-bar">
          <button 
            className="chat-mobile-menu-btn" 
            onClick={() => setIsSidebarOpen(true)}
            aria-label="打开会话列表"
          >
            <Menu size={18} />
          </button>
          <div className="chat-current-session">
            <span className="chat-current-label">{currentSessionLabel}</span>
            <span className="chat-current-title" title={activeSessionTitle}>{activeSessionTitle}</span>
            {sessionId && (
              <div
                ref={activeActionMenu === 'current-session' ? actionMenuRef : null}
                className="chat-current-actions"
              >
                <button
                  className={`chat-session-icon-btn chat-current-action-btn ${activeActionMenu === 'current-session' ? 'active' : ''}`}
                  onClick={() => toggleActionMenu('current-session')}
                  disabled={busy || !activeSessionMeta}
                  title={currentSessionActionLabel}
                  aria-label={currentSessionActionLabel}
                >
                  <MoreHorizontal size={14} />
                </button>
                {activeActionMenu === 'current-session' && activeSessionMeta && (
                  <div className="chat-session-menu chat-current-menu">
                    <button className="chat-session-menu-item" onClick={() => openRenameDialog(activeSessionMeta)}>
                      <Pencil size={14} /> 重命名
                    </button>
                    <button className="chat-session-menu-item danger" onClick={() => { closeActionMenu(); setDeleteTarget(activeSessionMeta); }}>
                      <Trash2 size={14} /> 删除
                    </button>
                  </div>
                )}
              </div>
            )}
            {!isGuestExperience && (
              <code title={sessionId || ''}>{sessionId ? shortenSessionId(sessionId) : '待创建'}</code>
            )}
            <span className="chat-current-meta">{messages.filter(m => m.key !== 'welcome').length} 条消息</span>
            <span className="chat-current-meta">
              最近更新 {formatRelativeTime(activeSessionMeta?.updated_at || activeSessionMeta?.created_at)}
            </span>
          </div>
        </div>

        {sessionError && (
          <div className="chat-inline-error">{sessionError}</div>
        )}
        {handoffNotice && (
          <div className="chat-inline-notice">{handoffNotice}</div>
        )}

        <div className="chat-feed-container">
          <div className="chat-messages">
            {(sessionReplayLoading || sessionMutating) && (
              <div className="chat-banner">
                <div className="loader" />
                <span>{sessionReplayLoading ? '正在加载会话...' : '正在更新会话...'}</span>
              </div>
            )}

            {messages.map((message, index) => {
              const iterationCount = message.iterationCount ?? inferIterationCountFromSteps(message.steps);
              const messageSections = message.role === 'bot'
                ? splitMessageReferenceSection(message.text, serverUrl)
                : { body: message.text, references: [] };
              const shouldRenderMessageBody = messageSections.body.trim().length > 0 || messageSections.references.length === 0;
              const showInlineCursor = message.role === 'bot' && Boolean(message.streaming);
              
              // 决定是否在界面上展示思考过程折叠面板的条件：
              // 1. 只有 Bot 回复展示思考过程
              // 2. 至少要有 1 条以上步骤才会展示
              // 3. 在具备步骤的情况下，如果仍在加载、或者步骤数量大于 1、或者带有迭代次数，则展示详细过程
              const isBotWithMessage = message.role === 'bot' && (message.steps?.length ?? 0) > 0;
              const hasComplexProcess = message.loading || (message.steps?.length ?? 0) > 1 || (iterationCount ?? 0) > 0;
              const shouldShowProcess = isBotWithMessage && hasComplexProcess;

              return (
                <div
                  key={message.key}
                  className={`chat-row ${message.role}`}
                  ref={(node) => {
                    if (node) {
                      messageRowRefs.current[message.key] = node;
                    } else {
                      delete messageRowRefs.current[message.key];
                    }
                  }}
                >
                  <div className={`chat-avatar ${message.role === 'user' ? 'user-av' : 'bot-av'}`}>
                    {message.role === 'user' ? <User size={18} /> : <Bot size={18} />}
                  </div>
                  <div className="chat-content-wrap">
                    {shouldShowProcess ? (
                      <div className="chat-status-container">
                        <details className="chat-process-details" open={message.loading}>
                          <summary className="chat-process-summary">
                            <div className="chat-process-summary-left">
                              {message.loading ? <Loader2 size={12} className="chat-status-icon" /> : <Zap size={12} className="chat-status-finished-icon" />}
                              <span>
                                {message.loading
                                  ? message.status
                                  : iterationCount && iterationCount > 0
                                    ? `进行了 ${iterationCount} 轮规划`
                                    : `记录了 ${message.steps?.length ?? 0} 条处理轨迹`}
                              </span>
                            </div>
                            <ChevronRight size={14} className="chat-process-chevron" />
                          </summary>
                          <div className="chat-process-timeline">
                            {message.steps?.map((step, i) => (
                              <div key={i} className="chat-timeline-item">
                                <div className="chat-timeline-dot" />
                                <div className="chat-timeline-text">{step}</div>
                              </div>
                            ))}
                          </div>
                        </details>
                      </div>
                    ) : message.role === 'bot' && message.status && message.status !== 'Bot 回复' ? (
                      <div className="chat-status">
                        {message.loading && <Loader2 size={12} className="chat-status-icon" />}
                        <span>{message.status}</span>
                      </div>
                    ) : null}
                    <div className="chat-bubble">
                      {message.loading && !message.text ? (
                        <div className="chat-stream-placeholder">
                          <span>{message.status || '思考中...'}</span>
                        </div>
                      ) : (
                        <>
                          {shouldRenderMessageBody && (
                            <div className="chat-stream-body">
                              <MarkdownRenderer
                                className="markdown-body"
                                content={messageSections.body}
                                serverUrl={serverUrl}
                                onImageClick={setPreviewImage}
                                onReferenceClick={(target, label) => { void openReferencePreview(target, label); }}
                              />
                              {showInlineCursor && <span className="chat-stream-cursor" aria-hidden="true" />}
                            </div>
                          )}
                          {message.role === 'bot' && (
                            <ChatBubbleReferences
                              references={messageSections.references}
                              onOpenReference={(target, label) => { void openReferencePreview(target, label); }}
                            />
                          )}
                          {message.role === 'bot' && message.suggestions && message.suggestions.length > 0 && (
                            <div className="chat-guided-questions" data-export-ignore="true" aria-label="推荐问题">
                              {message.suggestions.map((suggestion) => (
                                <button
                                  key={suggestion.id || suggestion.canonical_question}
                                  type="button"
                                  className={`chat-guided-question-btn ${suggestion.selected ? 'selected' : ''}`}
                                  onClick={() => handleGuidedQuestionClick(message.key, suggestion)}
                                  disabled={loading || !selectedUserId || suggestion.selected}
                                  title={suggestion.canonical_question}
                                  aria-label={suggestion.selected ? `已选择问题：${suggestion.display_text}` : `发送问题：${suggestion.display_text}`}
                                  aria-pressed={suggestion.selected}
                                >
                                  {suggestion.display_text}
                                </button>
                              ))}
                            </div>
                          )}
                          {message.role === 'bot' && message.key !== 'welcome' && !message.loading && !message.streaming && (
                            <div className="chat-bubble-actions" data-export-ignore="true">
                              <div className="chat-feedback-actions" aria-label="回复反馈">
                                <button
                                  className={`chat-feedback-btn ${message.feedback === 'up' ? 'active positive' : ''}`}
                                  onClick={() => { void handleMessageFeedback(message, 'up'); }}
                                  disabled={busy || !sessionId || !message.messageId}
                                  title="这条回复有帮助"
                                  aria-label="这条回复有帮助"
                                  aria-pressed={message.feedback === 'up'}
                                >
                                  <ThumbsUp size={14} />
                                </button>
                                <button
                                  className={`chat-feedback-btn ${message.feedback === 'down' ? 'active negative' : ''}`}
                                  onClick={() => { void handleMessageFeedback(message, 'down'); }}
                                  disabled={busy || !sessionId || !message.messageId}
                                  title="这条回复没有帮助"
                                  aria-label="这条回复没有帮助"
                                  aria-pressed={message.feedback === 'down'}
                                >
                                  <ThumbsDown size={14} />
                                </button>
                              </div>
                              <button
                                className="chat-export-btn"
                                onClick={() => { void handleExportPdf(message, index); }}
                                disabled={busy || Boolean(exportingMessageKey)}
                                title="导出从顶部到当前回复的 PDF"
                                aria-label="导出当前回复之前的会话为 PDF"
                              >
                                <FileDown size={14} />
                                <span>{exportingMessageKey === message.key ? '导出中...' : '导出 PDF'}</span>
                              </button>
                            </div>
                          )}
                        </>
                      )}
                    </div>
                    {message.role === 'bot' ? (
                      <div className="chat-footer">
                        {(message.createdAt || message.elapsedMs !== undefined) && (
                          <div className="chat-meta">
                            {[message.createdAt ? formatDateTime(message.createdAt) : '', message.elapsedMs !== undefined ? `cost ${formatDuration(message.elapsedMs)}` : '']
                              .filter(Boolean)
                              .join(' · ')}
                          </div>
                        )}
                      </div>
                    ) : (
                      (message.createdAt || message.elapsedMs !== undefined) && (
                        <div className="chat-meta align-right">
                          {[message.createdAt ? formatDateTime(message.createdAt) : '', message.elapsedMs !== undefined ? `cost ${formatDuration(message.elapsedMs)}` : '']
                            .filter(Boolean)
                            .join(' · ')}
                        </div>
                      )
                    )}
                  </div>
                </div>
              );
            })}
            <div ref={messagesEndRef} />
          </div>
        </div>

        <div className="chat-input-wrapper">
          <div className="chat-input-actions">
            <button
              type="button"
              className="chat-transfer-btn chat-transfer-btn-inline"
              title={handoffButtonLoading ? '正在连接人工...' : '联系人工服务'}
              onClick={() => void handleHumanHandoff(latestBotMessage)}
              disabled={Boolean(handoffLoadingKey) || !selectedUserId}
            >
              {handoffButtonLoading ? (
                <Loader2 size={13} className="chat-status-icon" />
              ) : (
                <Headphones size={13} />
              )}
              {handoffButtonLoading ? '正在连接人工...' : '联系人工'}
            </button>
          </div>
          <div className="chat-input-box">
            <textarea
              ref={inputRef}
              value={input}
              onChange={handleInput}
              onKeyDown={handleKeyDown}
              placeholder={selectedUserId ? '请描述您遇到的问题，例如：如何办理入住？' : '正在准备服务，请稍候...'}
              disabled={loading || !selectedUserId}
              rows={1}
            />
            <button className="chat-send-btn" onClick={() => void handleSend()} disabled={loading || !selectedUserId || !input.trim()}>
              {loading ? (
                <div className="loader" style={{ width: 14, height: 14, borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3) rgba(255,255,255,0.3) rgba(255,255,255,0.3) #fff' }} />
              ) : (
                <SendHorizontal size={18} style={{ position: 'relative', left: -1 }} />
              )}
            </button>
          </div>
        </div>
      </div>

      {referencePreview && (
        <aside
          className="chat-reference-drawer"
          aria-label="参考文档预览"
          style={{ '--chat-reference-drawer-width': `${referenceDrawerWidth}px` } as React.CSSProperties}
        >
          <button
            type="button"
            className="chat-reference-resize-handle"
            onPointerDown={beginReferenceDrawerResize}
            title="拖拽调整参考文档宽度"
            aria-label="拖拽调整参考文档宽度"
          >
            <GripVertical size={14} />
          </button>
          <div className="chat-reference-header">
            <div className="chat-reference-heading">
              <span className="chat-reference-kicker">参考文档</span>
              <h2>{referencePreview.title}</h2>
            </div>
            <div className="chat-reference-actions">
              {referenceExporting && <span className="chat-reference-exporting-label">导出中...</span>}
              <button
                type="button"
                className="chat-reference-icon-btn"
                onClick={() => { void handleExportReferencePdf(); }}
                disabled={referencePreview.loading || Boolean(referencePreview.error) || referenceExporting}
                title={referenceExporting ? '正在导出 PDF' : '导出参考文档 PDF'}
                aria-label="导出参考文档 PDF"
              >
                {referenceExporting ? <Loader2 size={16} className="chat-status-icon" /> : <FileDown size={16} />}
              </button>
              <button
                type="button"
                className="chat-reference-icon-btn"
                onClick={closeReferencePreview}
                title="关闭预览"
                aria-label="关闭参考文档预览"
              >
                <X size={16} />
              </button>
            </div>
          </div>
          <div className="chat-reference-uri" title={referencePreview.uri}>{referencePreview.uri}</div>
          <div className="chat-reference-body">
            {referencePreview.loading ? (
              <div className="chat-reference-state">
                <Loader2 size={16} className="chat-status-icon" />
                <span>正在加载参考文档...</span>
              </div>
            ) : referencePreview.error ? (
              <div className="chat-reference-error">{referencePreview.error}</div>
            ) : (
              <div ref={referencePreviewContentRef} className="chat-reference-export-content">
                <MarkdownRenderer
                  className="chat-reference-markdown markdown-body"
                  content={referencePreview.markdown}
                  serverUrl={serverUrl}
                  onImageClick={setPreviewImage}
                  onReferenceClick={(target, label) => { void openReferencePreview(target, label); }}
                />
              </div>
            )}
          </div>
        </aside>
      )}

      {deleteTarget && (
        <ConfirmModal
          message={`确认删除会话 ${deleteTarget.session_id}？此操作不可撤销。`}
          onConfirm={() => void confirmDeleteSession()}
          onCancel={() => setDeleteTarget(null)}
        />
      )}

      {renameTarget && (
        <RenameModal
          value={renameValue}
          defaultTitle={getDefaultSessionTitle(renameTarget)}
          canReset={Boolean(renamedSessionTitles[renameTarget.session_id])}
          onChange={setRenameValue}
          onConfirm={confirmRenameSession}
          onReset={resetRenamedSessionTitle}
          onCancel={closeRenameDialog}
        />
      )}

      {previewImage && (
        <div className="image-preview-overlay" onClick={() => setPreviewImage(null)}>
          <img src={previewImage} alt="Fullscreen preview" onClick={(e) => e.stopPropagation()} />
        </div>
      )}
    </div>
  );
};

export default ChatApp;
