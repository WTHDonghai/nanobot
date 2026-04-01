import React, { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  CheckSquare,
  ChevronRight,
  History,
  Loader2,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  SendHorizontal,
  Square,
  Trash2,
  User,
  Zap,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { fetchApi } from '../../services/api';
import './ChatApp.css';

type ApiEnvelope<T> = {
  status?: string;
  result?: T;
  error?: {
    message?: string;
  };
};

type UserOption = {
  user_id: string;
};

type RawSessionListItem = {
  session_id?: string;
} | string;

type SessionSummary = {
  session_id: string;
  created_at?: string;
  updated_at?: string;
  message_count?: number;
  pending_tokens?: number;
  commit_count?: number;
  last_commit_at?: string;
};

type SessionContextPart = {
  type?: string;
  text?: string;
  abstract?: string;
  context_type?: string;
  tool_name?: string;
  tool_status?: string;
};

type SessionContextMessage = {
  id?: string;
  role: 'user' | 'assistant';
  parts?: SessionContextPart[];
  created_at?: string;
};

type SessionContextResult = {
  latest_archive_id?: string;
  pre_archive_abstracts?: Array<{ archive_id: string; abstract?: string }>;
  messages?: SessionContextMessage[];
};

type SessionArchiveResult = {
  archive_id: string;
  messages?: SessionContextMessage[];
};

type ChatMessage = {
  key: string;
  role: 'user' | 'bot';
  text: string;
  status?: string;
  loading?: boolean;
  createdAt?: string;
  steps?: string[];
};

const WELCOME_TEXT = '你好！我是 XMS 技术支持专员。有什么可以帮助你？';
const MAX_SESSION_CONTEXT_BUDGET = 100_000_000;

const makeWelcomeMessages = (status = ''): ChatMessage[] => [
  { key: 'welcome', role: 'bot', text: WELCOME_TEXT, status },
];

const unwrapResult = <T,>(response: ApiEnvelope<T>, fallbackMessage: string): T => {
  if (!response || response.status === 'error' || response.result === undefined) {
    throw new Error(response?.error?.message || fallbackMessage);
  }
  return response.result;
};

const getSessionId = (item: RawSessionListItem): string => {
  if (typeof item === 'string') return item;
  return item.session_id || '';
};

const getSessionSortTime = (session: SessionSummary): number => {
  const value = session.updated_at || session.created_at;
  if (!value) return 0;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? 0 : time;
};

const getArchiveIndex = (archiveId: string): number => {
  const match = archiveId.match(/archive_(\d+)/);
  return match ? Number(match[1]) : 0;
};

const formatDateTime = (value?: string): string => {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
};

const formatRelativeTime = (value?: string): string => {
  if (!value) return '时间未知';
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return value;
  const diff = Date.now() - timestamp;
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return formatDateTime(value);
};

const toSingleLine = (value: string): string => value.replace(/\s+/g, ' ').trim();

const shortenSessionId = (value: string): string => {
  if (value.length <= 22) return value;
  return `${value.slice(0, 8)}...${value.slice(-6)}`;
};

const formatShortSessionTime = (value?: string): string => {
  if (!value) return '未命名会话';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '未命名会话';
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).replace(',', ' ');
};

const getSessionGroupLabel = (value?: string): string => {
  if (!value) return '更早';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '更早';

  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfTarget = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const diffDays = Math.floor((startOfToday - startOfTarget) / 86400000);

  if (diffDays <= 0) return '今天';
  if (diffDays === 1) return '昨天';
  if (diffDays < 7) return '近 7 天';
  return '更早';
};

const getPrimaryTextFromMessage = (message: SessionContextMessage): string => {
  const textParts = (message.parts || [])
    .filter((part) => part.type === 'text' && part.text?.trim())
    .map((part) => part.text!.trim());
  return toSingleLine(textParts.join(' '));
};

const deriveSessionTitleFromMessages = (sessionMessages: SessionContextMessage[]): string => {
  const candidate = sessionMessages.find((message) => message.role === 'user' && getPrimaryTextFromMessage(message))
    || sessionMessages.find((message) => getPrimaryTextFromMessage(message));

  if (!candidate) return '';
  return getPrimaryTextFromMessage(candidate).slice(0, 200);
};

const renderMessageText = (parts: SessionContextPart[] = []): string => {
  const textBlocks = parts
    .filter((part) => part.type === 'text' && part.text?.trim())
    .map((part) => part.text!.trim());
  const contextLines = parts
    .filter((part) => part.type === 'context' && part.abstract?.trim())
    .map((part, index) => `${index + 1}. [${part.context_type || 'context'}] ${part.abstract!.trim()}`);
  const toolLines = parts
    .filter((part) => part.type === 'tool')
    .map((part, index) => `${index + 1}. ${part.tool_name || 'tool'} (${part.tool_status || 'done'})`);

  const sections: string[] = [];
  if (textBlocks.length > 0) sections.push(textBlocks.join('\n\n'));
  if (contextLines.length > 0) sections.push(['**关联上下文**', ...contextLines].join('\n'));
  if (toolLines.length > 0) sections.push(['**工具调用**', ...toolLines].join('\n'));

  return sections.join('\n\n').trim() || '（空消息）';
};

const mapSessionMessages = (sessionMessages: SessionContextMessage[]): ChatMessage[] => {
  if (sessionMessages.length === 0) {
    return makeWelcomeMessages('该会话暂无消息，可以继续提问');
  }

  return sessionMessages.map((message, index) => ({
    key: message.id || `${message.role}-${index}`,
    role: message.role === 'assistant' ? 'bot' : 'user',
    text: renderMessageText(message.parts),
    createdAt: message.created_at,
  }));
};

const readStoredSessionTitles = (storageKey: string | null): Record<string, string> => {
  if (!storageKey) return {};

  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return {};

    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return Object.entries(parsed).reduce<Record<string, string>>((acc, [key, value]) => {
      if (typeof value !== 'string') return acc;
      const normalizedValue = toSingleLine(value).slice(0, 200);
      if (!normalizedValue) return acc;
      acc[key] = normalizedValue;
      return acc;
    }, {});
  } catch {
    return {};
  }
};

const readStoredSessionMessages = (storageKey: string | null): Record<string, ChatMessage[]> => {
  if (!storageKey) return {};

  try {
    const raw = window.sessionStorage.getItem(storageKey);
    if (!raw) return {};

    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return Object.entries(parsed).reduce<Record<string, ChatMessage[]>>((acc, [sessionId, value]) => {
      if (!Array.isArray(value)) return acc;

      const messages = value.reduce<ChatMessage[]>((items, entry, index) => {
        if (!entry || typeof entry !== 'object') return items;

        const record = entry as Record<string, unknown>;
        const role = record.role === 'user' || record.role === 'bot' ? record.role : null;
        const text = typeof record.text === 'string' ? record.text : null;
        if (!role || text === null) return items;

        items.push({
          key: typeof record.key === 'string' ? record.key : `${sessionId}-${index}`,
          role,
          text,
          status: typeof record.status === 'string' ? record.status : undefined,
          loading: typeof record.loading === 'boolean' ? record.loading : undefined,
          createdAt: typeof record.createdAt === 'string' ? record.createdAt : undefined,
        });
        return items;
      }, []);

      if (messages.length > 0) acc[sessionId] = messages;
      return acc;
    }, {});
  } catch {
    return {};
  }
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
  hideUserSelector?: boolean;
};

const ChatApp: React.FC<ChatAppProps> = ({
  serverUrl,
  apiKey,
  accountId,
  userId,
  role,
  hideUserSelector = false,
}) => {
  const [messages, setMessages] = useState<ChatMessage[]>(makeWelcomeMessages());
  const [input, setInput] = useState('');
  const [users, setUsers] = useState<UserOption[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState('');
  const [selectedUserId, setSelectedUserId] = useState('');
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [derivedSessionTitles, setDerivedSessionTitles] = useState<Record<string, string>>({});
  const [renamedSessionTitles, setRenamedSessionTitles] = useState<Record<string, string>>({});
  const [sessionQuery, setSessionQuery] = useState('');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [activeSessionMeta, setActiveSessionMeta] = useState<SessionSummary | null>(null);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [sessionListLoading, setSessionListLoading] = useState(false);
  const [sessionReplayLoading, setSessionReplayLoading] = useState(false);
  const [sessionMutating, setSessionMutating] = useState(false);
  const [sessionError, setSessionError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [activeActionMenu, setActiveActionMenu] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<SessionSummary | null>(null);
  const [renameValue, setRenameValue] = useState('');
  
  // Multi-select state
  const [isSelectMode, setIsSelectMode] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(new Set());
  const [isBatchDeleting, setIsBatchDeleting] = useState(false);
  const actionMenuRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const sessionMessageCacheRef = useRef<Record<string, ChatMessage[]>>({});
  const sessionListRequestRef = useRef(0);
  const replayRequestRef = useRef(0);

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
    persistLastActiveSession(null);
    setSessionId(null);
    setActiveSessionMeta(null);
    setMessages(makeWelcomeMessages(status));
    setInput('');
    resetInputHeight();
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
    updateSessionMessageCache((prev) => ({
      ...prev,
      [targetSessionId]: nextMessages,
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
    return shortenSessionId(session.session_id);
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
          resetConversation('当前会话已不存在，请重新开始');
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

  async function handleNewSession() {
    if (loading || sessionReplayLoading || sessionMutating) return;

    closeActionMenu();
    setSessionMutating(true);
    try {
      const nextSessionId = await requestNewSession();
      const welcomeMessages = makeWelcomeMessages('新会话已开始');
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
      const derivedTitle = deriveSessionTitleFromMessages(mergedMessages);

      if (replayRequestRef.current !== requestId) return;

      rememberSessionTitle(targetSessionId, derivedTitle);
      const nextMessages = mergedMessages.length > 0
        ? mapSessionMessages(mergedMessages)
        : (cachedMessages?.length ? cachedMessages : mapSessionMessages(mergedMessages));

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

  async function handleBatchDeleteSessions() {
    if (selectedSessionIds.size === 0) return;
    setIsBatchDeleting(true);
    const deletedIds: string[] = [];

    try {
      // Loop over the selected session IDs and delete them one by one
      for (const id of Array.from(selectedSessionIds)) {
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

      setSelectedSessionIds(new Set());
      setIsSelectMode(false);
      setSessionError('');

      if (sessionId && deletedIds.includes(sessionId)) {
        resetConversation('会话已被删除，请开始新的对话');
        await loadSessions(null);
      } else {
        await loadSessions(sessionId);
      }
    } catch (err: unknown) {
      setSessionError(err instanceof Error ? err.message : '批量删除过程中出现错误');
    } finally {
      setIsBatchDeleting(false);
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
    if (role === 'user') {
      fetchApi<ApiEnvelope<{ user_id?: string }>>(serverUrl, apiKey, '/api/v1/system/whoami')
        .then((res) => {
          const whoami = unwrapResult(res, '获取当前用户失败');
          if (whoami.user_id) {
            setUsers([{ user_id: whoami.user_id }]);
            setSelectedUserId(whoami.user_id);
          }
        })
        .catch(() => {
          setUsers([]);
          setSelectedUserId('');
        });
    } else if (selectedAccountId) {
      fetchApi<ApiEnvelope<UserOption[]>>(serverUrl, apiKey, `/api/v1/admin/accounts/${selectedAccountId}/users`)
        .then((res) => {
          const nextUsers = unwrapResult(res, '获取用户列表失败') || [];
          setUsers(nextUsers);
          setSelectedUserId((prev) => {
            if (prev && nextUsers.some((user) => user.user_id === prev)) return prev;
            return nextUsers[0]?.user_id || '';
          });
        })
        .catch(() => {
          setUsers([]);
          setSelectedUserId('');
        });
    } else {
      setUsers([]);
      setSelectedUserId('');
    }
  }, [selectedAccountId, serverUrl, apiKey, role]);

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
    sessionMessageCacheRef.current = readStoredSessionMessages(storageKey);
  }, [serverUrl, role, accountId, userId, selectedAccountId, selectedUserId]);

  useEffect(() => {
    setSessions([]);
    setSessionQuery('');
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
      resetConversation('正在同步会话上下文');
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

  const handleSend = async () => {
    if (!input.trim() || loading || !selectedUserId) return;

    const userMsg = input.trim();
    setInput('');
    resetInputHeight();
    setLoading(true);

    const botId = `bot-${Date.now()}`;
    const userMessageKey = `user-${Date.now()}`;
    let activeSessionId = sessionId;

    setMessages((prev) => [
      ...prev,
      { key: userMessageKey, role: 'user', text: userMsg },
      { key: botId, role: 'bot', text: '', status: '思考中...', loading: true, steps: [] },
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
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
        body: JSON.stringify({ message: userMsg, session_id: activeSessionId, user_id: selectedUserId }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (!res.body) throw new Error('No response body');

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      let finalContent = '';

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
            let status = '思考中...';
            if (evt.event === 'tool_call') {
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

            if (evt.event === 'response') {
              finalContent = typeof evt.data === 'string' ? evt.data : JSON.stringify(evt.data);
            }

            setMessages((prev) => prev.map((message) => {
              if (message.key === botId) {
                const currentSteps = message.steps || [];
                const isNewStep = status !== '思考中...' && status !== 'Bot 回复' && currentSteps[currentSteps.length - 1] !== status;
                const nextSteps = isNewStep ? [...currentSteps, status] : currentSteps;
                return { ...message, status, text: finalContent || '', steps: nextSteps };
              }
              return message;
            }));
          } catch (err) {
            if (err instanceof Error) throw err;
          }
        }
      }

      setMessages((prev) => prev.map((message) => (
        message.key === botId ? { ...message, text: finalContent || '（无回复）', loading: false } : message
      )));
    } catch (err: any) {
      setMessages((prev) => prev.map((message) => (
        message.key === botId ? { ...message, text: `错误: ${err.message}`, status: 'Error', loading: false } : message
      )));
    } finally {
      setLoading(false);
      if (activeSessionId) {
        void loadSessions(activeSessionId);
      }
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
  const currentIdentityLabel = role === 'user'
    ? (selectedUserId || userId || '当前用户')
    : (selectedUserId || '请选择用户');
  const normalizedQuery = sessionQuery.trim().toLowerCase();
  const filteredSessions = sessions.filter((session) => {
    const title = getSessionTitle(session);
    const subtitle = getSessionSubtitle(session);
    if (!normalizedQuery) return true;
    const haystack = [
      title,
      subtitle,
      session.session_id,
      formatDateTime(session.created_at),
      formatDateTime(session.updated_at),
    ].join(' ').toLowerCase();
    return haystack.includes(normalizedQuery);
  });
  const groupedSessions = ['今天', '昨天', '近 7 天', '更早']
    .map((label) => ({
      label,
      items: filteredSessions.filter((session) => getSessionGroupLabel(session.updated_at || session.created_at) === label),
    }))
    .filter((group) => group.items.length > 0);
  const activeSessionTitle = sessionId
    ? (renamedSessionTitles[sessionId] || derivedSessionTitles[sessionId] || (activeSessionMeta ? getSessionTitle(activeSessionMeta) : '当前会话'))
    : '未开始新会话';

  return (
    <div className="chat-layout">
      <aside className="chat-session-panel">
        <div className="chat-session-panel-header">
          <div>
            <div className="chat-session-panel-title">
              <History size={16} /> 会话管理
            </div>
            <div className="chat-session-panel-subtitle">{currentIdentityLabel}</div>
          </div>
          <div style={{ display: 'flex', gap: '8px' }}>
            {sessions.length > 0 && (
              <button
                className={`btn btn-sm ${isSelectMode ? 'btn-primary' : 'btn-ghost'}`}
                onClick={() => {
                  setIsSelectMode(!isSelectMode);
                  if (isSelectMode) setSelectedSessionIds(new Set());
                }}
                disabled={!ready || busy || isBatchDeleting}
                title={isSelectMode ? "取消管理" : "批量管理"}
              >
                {isSelectMode ? "完成" : "管理"}
              </button>
            )}
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => void loadSessions(sessionId)}
              disabled={!ready || busy || isSelectMode || isBatchDeleting}
              title="刷新会话历史"
            >
              <RefreshCw size={14} />
            </button>
          </div>
        </div>

        <div className="chat-session-toolbar">
          <button className="btn btn-primary chat-session-create-btn" onClick={handleNewSession} disabled={!ready || busy}>
            <Plus size={16} /> 新建会话
          </button>
        </div>

        <div className="chat-session-search">
          <Search size={16} />
          <input
            className="input"
            value={sessionQuery}
            onChange={(e) => setSessionQuery(e.target.value)}
            placeholder="搜索 Session ID / 标题 / 时间"
            disabled={!ready}
          />
        </div>

        <div className="chat-session-summary">
          {normalizedQuery ? `匹配 ${filteredSessions.length} / ${sessions.length} 个会话` : `共 ${sessions.length} 个会话`}
        </div>

        <div className="chat-session-list">
          {!ready && <div className="chat-session-empty">请选择用户后查看会话历史。</div>}
          {ready && sessionListLoading && (
            <div className="chat-session-loading">
              <div className="loader" />
            </div>
          )}
          {ready && !sessionListLoading && groupedSessions.map((group) => (
            <section key={group.label} className="chat-session-group">
              <div className="chat-session-group-title">{group.label}</div>
              <div className="chat-session-group-list">
                {group.items.map((session) => {
                  const title = getSessionTitle(session);
                  const subtitle = getSessionSubtitle(session);
                  const menuId = `session:${session.session_id}`;

                  return (
                    <div key={session.session_id} className={`chat-session-item ${session.session_id === sessionId && !isSelectMode ? 'active' : ''} ${isSelectMode && selectedSessionIds.has(session.session_id) ? 'selected' : ''}`}>
                      <button
                        className={`chat-session-item-trigger ${isSelectMode ? 'select-mode' : ''}`}
                        onClick={() => {
                          if (isSelectMode) {
                            const next = new Set(selectedSessionIds);
                            if (next.has(session.session_id)) {
                              next.delete(session.session_id);
                            } else {
                              next.add(session.session_id);
                            }
                            setSelectedSessionIds(next);
                          } else {
                            if (session.session_id !== sessionId) void handleSelectSession(session.session_id);
                          }
                        }}
                        disabled={busy || isBatchDeleting}
                        title={title}
                      >
                        {isSelectMode && (
                          <div className="chat-session-item-checkbox">
                            {selectedSessionIds.has(session.session_id) ? (
                              <CheckSquare size={16} color="var(--primary)" fill="rgba(99, 102, 241, 0.2)" />
                            ) : (
                              <Square size={16} color="var(--muted)" />
                            )}
                          </div>
                        )}
                        <div className="chat-session-item-main">
                          <div className="chat-session-item-title" title={title}>{title}</div>
                          <div className="chat-session-item-subtitle" title={session.session_id}>{subtitle}</div>
                        </div>
                        <div className="chat-session-item-meta">
                          <span>{formatRelativeTime(session.updated_at || session.created_at)}</span>
                        </div>
                      </button>
                      
                      {!isSelectMode && (
                        <div
                          ref={activeActionMenu === menuId ? actionMenuRef : null}
                          className={`chat-session-item-actions ${activeActionMenu === menuId ? 'open' : ''}`}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <button
                            className={`chat-session-icon-btn ${activeActionMenu === menuId ? 'active' : ''}`}
                            onClick={() => toggleActionMenu(menuId)}
                            disabled={busy}
                            title="会话操作"
                            aria-label="会话操作"
                          >
                            <MoreHorizontal size={14} />
                          </button>
                          {activeActionMenu === menuId && (
                            <div className="chat-session-menu">
                              <button className="chat-session-menu-item" onClick={() => openRenameDialog(session)}>
                                <Pencil size={14} /> 重命名
                              </button>
                              <button className="chat-session-menu-item danger" onClick={() => { closeActionMenu(); setDeleteTarget(session); }}>
                                <Trash2 size={14} /> 删除
                              </button>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          ))}
          {ready && !sessionListLoading && sessions.length === 0 && (
            <div className="chat-session-empty">暂无历史会话，发送第一条消息后会自动记录。</div>
          )}
          {ready && !sessionListLoading && sessions.length > 0 && filteredSessions.length === 0 && (
            <div className="chat-session-empty">没有匹配的历史会话。</div>
          )}
        </div>

        {isSelectMode && (
          <div className="chat-session-batch-toolbar">
            <div className="batch-toolbar-info">
              已选 <span>{selectedSessionIds.size}</span> 项
            </div>
            <div className="batch-toolbar-actions">
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => {
                  if (selectedSessionIds.size === filteredSessions.length) {
                    setSelectedSessionIds(new Set());
                  } else {
                    setSelectedSessionIds(new Set(filteredSessions.map(s => s.session_id)));
                  }
                }}
                disabled={isBatchDeleting}
              >
                {selectedSessionIds.size === filteredSessions.length && filteredSessions.length > 0 ? '全不选' : '全选'}
              </button>
              <button
                className="btn btn-danger btn-sm"
                onClick={() => void handleBatchDeleteSessions()}
                disabled={selectedSessionIds.size === 0 || isBatchDeleting}
              >
                {isBatchDeleting ? <div className="loader" style={{ width: 14, height: 14, borderWidth: 2 }} /> : <Trash2 size={14} />}
                {isBatchDeleting ? '删除中...' : '删除'}
              </button>
            </div>
          </div>
        )}
      </aside>

      <div className="chat-main-panel">
        <div className="chat-config-bar">
          <div className="chat-current-session">
            <span className="chat-current-label">当前会话</span>
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
                  title="当前会话操作"
                  aria-label="当前会话操作"
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
            <code title={sessionId || ''}>{sessionId ? shortenSessionId(sessionId) : '待创建'}</code>
            <span className="chat-current-meta">{messages.filter(m => m.key !== 'welcome').length} 条消息</span>
            <span className="chat-current-meta">
              最近更新 {formatRelativeTime(activeSessionMeta?.updated_at || activeSessionMeta?.created_at)}
            </span>
          </div>

          {!hideUserSelector && role !== 'user' && (
            <>
              {role === 'root' && (
                <div className="chat-config-note">
                  Bot 使用服务端固定 Account。当前仅展示工作区 <code>{selectedAccountId || 'default'}</code> 的用户与会话。
                </div>
              )}
              <div className="chat-config-selector">
                <label>User</label>
                <select
                  className="select"
                  value={selectedUserId}
                  onChange={(e) => setSelectedUserId(e.target.value)}
                  disabled={busy}
                >
                  {users.map((user) => <option key={user.user_id} value={user.user_id}>{user.user_id}</option>)}
                  {users.length === 0 && <option value="" disabled>无</option>}
                </select>
              </div>
            </>
          )}
        </div>

        {sessionError && (
          <div className="chat-inline-error">{sessionError}</div>
        )}

        <div className="chat-feed-container">
          <div className="chat-messages">
            {(sessionReplayLoading || sessionMutating) && (
              <div className="chat-banner">
                <div className="loader" />
                <span>{sessionReplayLoading ? '正在加载会话...' : '正在更新会话...'}</span>
              </div>
            )}

            {messages.map((message) => (
              <div key={message.key} className={`chat-row ${message.role}`}>
                <div className={`chat-avatar ${message.role === 'user' ? 'user-av' : 'bot-av'}`}>
                  {message.role === 'user' ? <User size={18} /> : <Bot size={18} />}
                </div>
                <div className="chat-content-wrap">
                  {message.role === 'bot' && message.steps && message.steps.length > 0 && (message.loading || message.steps.length > 1) ? (
                    <div className="chat-status-container">
                      <details className="chat-process-details" open={message.loading}>
                        <summary className="chat-process-summary">
                          <div className="chat-process-summary-left">
                            {message.loading ? <Loader2 size={12} className="chat-status-icon" /> : <Zap size={12} className="chat-status-finished-icon" />}
                            <span>{message.loading ? message.status : `使用了 ${message.steps.length} 个步骤思考`}</span>
                          </div>
                          <ChevronRight size={14} className="chat-process-chevron" />
                        </summary>
                        <div className="chat-process-timeline">
                          {message.steps.map((step, i) => (
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
                      <div className="typing-dots"><span /><span /><span /></div>
                    ) : (
                      <div className="markdown-body">
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            img(props) {
                              return (
                                <img
                                  {...props}
                                  style={{ maxWidth: '100%', borderRadius: '8px', cursor: 'zoom-in', marginTop: '8px' }}
                                  onClick={() => setPreviewImage(props.src || null)}
                                />
                              );
                            },
                          }}
                        >
                          {message.text}
                        </ReactMarkdown>
                      </div>
                    )}
                  </div>
                  {message.createdAt && (
                    <div className={`chat-meta ${message.role === 'user' ? 'align-right' : ''}`}>
                      {formatDateTime(message.createdAt)}
                    </div>
                  )}
                </div>
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>
        </div>

        <div className="chat-input-wrapper">
          <div className="chat-input-box">
            <textarea
              ref={inputRef}
              value={input}
              onChange={handleInput}
              onKeyDown={handleKeyDown}
              placeholder={selectedUserId ? '给 Bot 发送消息...' : '正在同步用户信息...'}
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
