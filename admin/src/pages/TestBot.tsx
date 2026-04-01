import React, { useEffect, useRef, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Bot,
  ChevronRight,
  History,
  Loader2,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  SendHorizontal,
  Trash2,
  User,
  Zap,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './BotChat.css';
import './TestBot.css';

// ─── Types (same as BotChat) ────────────────────────────────────────────────

type ApiEnvelope<T> = {
  status?: string;
  result?: T;
  error?: { message?: string };
};

type RawSessionListItem = { session_id?: string } | string;

type SessionSummary = {
  session_id: string;
  created_at?: string;
  updated_at?: string;
  message_count?: number;
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

// ─── Constants & Helpers (same as BotChat) ──────────────────────────────────

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
  return new Date(value).toLocaleDateString('zh-CN');
};

const formatDateTime = (value?: string): string => {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
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
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
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
  const candidate = sessionMessages.find((m) => m.role === 'user' && getPrimaryTextFromMessage(m))
    || sessionMessages.find((m) => getPrimaryTextFromMessage(m));
  if (!candidate) return '';
  return getPrimaryTextFromMessage(candidate).slice(0, 200);
};

const renderMessageText = (parts: SessionContextPart[] = []): string => {
  const textBlocks = parts.filter((p) => p.type === 'text' && p.text?.trim()).map((p) => p.text!.trim());
  const contextLines = parts
    .filter((p) => p.type === 'context' && p.abstract?.trim())
    .map((p, i) => `${i + 1}. [${p.context_type || 'context'}] ${p.abstract!.trim()}`);
  const toolLines = parts
    .filter((p) => p.type === 'tool')
    .map((p, i) => `${i + 1}. ${p.tool_name || 'tool'} (${p.tool_status || 'done'})`);
  const sections: string[] = [];
  if (textBlocks.length > 0) sections.push(textBlocks.join('\n\n'));
  if (contextLines.length > 0) sections.push(['**关联上下文**', ...contextLines].join('\n'));
  if (toolLines.length > 0) sections.push(['**工具调用**', ...toolLines].join('\n'));
  return sections.join('\n\n').trim() || '（空消息）';
};

const mapSessionMessages = (sessionMessages: SessionContextMessage[]): ChatMessage[] => {
  if (sessionMessages.length === 0) return makeWelcomeMessages('该会话暂无消息，可以继续提问');
  return sessionMessages.map((m, i) => ({
    key: m.id || `${m.role}-${i}`,
    role: m.role === 'assistant' ? 'bot' : 'user',
    text: renderMessageText(m.parts),
    createdAt: m.created_at,
  }));
};

// ─── Fetch helper (sans AuthContext) ────────────────────────────────────────

const fetchApi = async <T = any>(
  serverUrl: string,
  apiKey: string,
  path: string,
  options: RequestInit & { account?: string; user?: string } = {},
): Promise<T> => {
  const headers = new Headers(options.headers || {});
  if (!headers.has('Content-Type') && options.body) headers.set('Content-Type', 'application/json');
  if (apiKey) headers.set('X-API-Key', apiKey);
  if (options.account) headers.set('X-OpenViking-Account', options.account);
  if (options.user) headers.set('X-OpenViking-User', options.user);
  const { account, user, ...fetchConfig } = options as any;
  const res = await fetch(`${serverUrl}${path}`, { ...fetchConfig, headers });
  const isJson = res.headers.get('content-type')?.includes('application/json');
  const data = isJson ? await res.json() : await res.text();
  if (!res.ok) {
    const errMsg = (data as any)?.error?.message || `HTTP Error ${res.status}`;
    throw new Error(errMsg);
  }
  return data as T;
};

// ─── Sub-components ──────────────────────────────────────────────────────────

const ConfirmModal = ({
  message, onConfirm, onCancel,
}: { message: string; onConfirm: () => void; onCancel: () => void }) => (
  <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
    <div className="modal">
      <div className="modal-title" style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--danger)' }}>
        <AlertTriangle size={18} /> 操作确认
      </div>
      <div className="modal-body"><p style={{ margin: 0, lineHeight: 1.6 }}>{message}</p></div>
      <div className="modal-footer">
        <button className="btn btn-ghost" onClick={onCancel}>取消</button>
        <button className="btn btn-danger" onClick={onConfirm}>确认删除</button>
      </div>
    </div>
  </div>
);

const RenameModal = ({
  value, defaultTitle, canReset, onChange, onConfirm, onReset, onCancel,
}: {
  value: string; defaultTitle: string; canReset: boolean;
  onChange: (v: string) => void; onConfirm: () => void; onReset: () => void; onCancel: () => void;
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
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); onConfirm(); } }}
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

// ─── Error Screen ──────────────────────────────────────────────────────────

const ErrorScreen = ({ message }: { message: string }) => (
  <div className="testbot-error-screen">
    <div className="testbot-error-card">
      <div className="testbot-error-icon">
        <AlertCircle size={40} />
      </div>
      <h1 className="testbot-error-title">无法启动 Bot 测试页</h1>
      <p className="testbot-error-desc">{message}</p>
      <div className="testbot-error-hint">
        <p>请在 URL 中提供 API Key：</p>
        <code>/admin/test-bot?api-key=YOUR_KEY</code>
      </div>
    </div>
  </div>
);

// ─── Loading Screen ─────────────────────────────────────────────────────────

const LoadingScreen = () => (
  <div className="testbot-error-screen">
    <div className="testbot-loading-card">
      <div className="loader" style={{ width: 36, height: 36 }} />
      <p className="testbot-loading-text">正在验证 API Key…</p>
    </div>
  </div>
);

// ─── Main Component ──────────────────────────────────────────────────────────

type WhoamiResult = {
  role: string;
  account_id: string;
  user_id: string;
};

const TestBot: React.FC = () => {
  const params = new URLSearchParams(window.location.search);
  const apiKey = params.get('api-key') || '';
  const serverUrl = window.location.origin.replace(/\/$/, '');

  const [whoami, setWhoami] = useState<WhoamiResult | null>(null);
  const [authError, setAuthError] = useState('');
  const [authLoading, setAuthLoading] = useState(true);

  useEffect(() => {
    if (!apiKey) {
      setAuthLoading(false);
      return;
    }
    fetchApi<ApiEnvelope<WhoamiResult>>(serverUrl, apiKey, '/api/v1/system/whoami')
      .then((res) => {
        if (!res.result?.account_id || !res.result?.user_id) {
          throw new Error('服务端未返回账户/用户信息');
        }
        setWhoami(res.result);
      })
      .catch((err: unknown) => {
        setAuthError(err instanceof Error ? err.message : '身份验证失败');
      })
      .finally(() => setAuthLoading(false));
  }, [apiKey, serverUrl]);

  if (!apiKey) {
    return <ErrorScreen message="缺少必需参数 api-key。请通过 URL 提供有效的 API Key。" />;
  }
  if (authLoading) return <LoadingScreen />;
  if (authError || !whoami) {
    return <ErrorScreen message={`API Key 验证失败：${authError || '无法获取身份信息'}`} />;
  }

  return (
    <TestBotInner
      apiKey={apiKey}
      serverUrl={serverUrl}
      userId={whoami.user_id}
      accountId={whoami.account_id}
    />
  );
};

const TestBotInner: React.FC<{
  apiKey: string;
  serverUrl: string;
  userId: string;
  accountId: string;
}> = ({ apiKey, serverUrl, userId, accountId }) => {
  const [messages, setMessages] = useState<ChatMessage[]>(makeWelcomeMessages());
  const [input, setInput] = useState('');
  const selectedUserId = userId;
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

  const actionMenuRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const sessionMessageCacheRef = useRef<Record<string, ChatMessage[]>>({});
  const sessionListRequestRef = useRef(0);
  const replayRequestRef = useRef(0);

  // ─── Storage keys ───────────────────────────────────────────────────────

  const storageScopeKey = `ov:testbot:${serverUrl}:${accountId}:${selectedUserId}`;

  function getSessionTitlesKey() { return `${storageScopeKey}:renamed`; }
  function getDerivedTitlesKey() { return `${storageScopeKey}:derived`; }
  function getCacheKey() { return `${storageScopeKey}:cache`; }
  function getLastSessionKey() { return `${storageScopeKey}:last`; }

  // ─── Helpers ────────────────────────────────────────────────────────────

  function scrollToBottom() { messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }
  function resetInputHeight() {
    if (inputRef.current) { inputRef.current.style.height = 'auto'; inputRef.current.style.overflowY = 'hidden'; }
  }

  function resetConversation(status = '') {
    try { window.sessionStorage.removeItem(getLastSessionKey()); } catch {}
    setSessionId(null);
    setActiveSessionMeta(null);
    setMessages(makeWelcomeMessages(status));
    setInput('');
    resetInputHeight();
  }

  function getSessionRequestOptions() {
    return { account: accountId, user: selectedUserId };
  }

  function rememberSessionTitle(targetId: string, nextTitle: string, overwrite = false) {
    const normalized = toSingleLine(nextTitle).slice(0, 200);
    if (!normalized) return;
    setDerivedSessionTitles((prev) => {
      if (!overwrite && prev[targetId]) return prev;
      if (prev[targetId] === normalized) return prev;
      const next = { ...prev, [targetId]: normalized };
      try { window.localStorage.setItem(getDerivedTitlesKey(), JSON.stringify(next)); } catch {}
      return next;
    });
  }

  function getDefaultSessionTitle(session: SessionSummary): string {
    if (derivedSessionTitles[session.session_id]) return derivedSessionTitles[session.session_id];
    const t = formatShortSessionTime(session.created_at || session.updated_at);
    if (t !== '未命名会话') return `${t} 的会话`;
    if ((session.message_count ?? 0) === 0) return '空白新会话';
    return '未命名会话';
  }

  function getSessionTitle(session: SessionSummary): string {
    return renamedSessionTitles[session.session_id] || getDefaultSessionTitle(session);
  }

  function toggleActionMenu(menuId: string) { setActiveActionMenu((p) => p === menuId ? null : menuId); }
  function closeActionMenu() { setActiveActionMenu(null); }

  function openRenameDialog(s: SessionSummary) {
    closeActionMenu();
    setRenameTarget(s);
    setRenameValue(getSessionTitle(s));
  }

  function updateRenamedTitles(updater: (p: Record<string, string>) => Record<string, string>) {
    setRenamedSessionTitles((prev) => {
      const next = updater(prev);
      try {
        const key = getSessionTitlesKey();
        if (Object.keys(next).length === 0) window.localStorage.removeItem(key);
        else window.localStorage.setItem(key, JSON.stringify(next));
      } catch {}
      return next;
    });
  }

  function confirmRenameSession() {
    if (!renameTarget) return;
    const normalized = toSingleLine(renameValue).slice(0, 200);
    if (!normalized) return;
    updateRenamedTitles((p) => ({ ...p, [renameTarget.session_id]: normalized }));
    setRenameTarget(null);
    setRenameValue('');
  }

  function resetRenamedTitle() {
    if (!renameTarget) return;
    updateRenamedTitles((p) => {
      if (!p[renameTarget.session_id]) return p;
      const next = { ...p };
      delete next[renameTarget.session_id];
      return next;
    });
    setRenameTarget(null);
    setRenameValue('');
  }

  function setCachedMessages(targetId: string, msgs: ChatMessage[]) {
    const next = { ...sessionMessageCacheRef.current, [targetId]: msgs };
    sessionMessageCacheRef.current = next;
    try { window.sessionStorage.setItem(getCacheKey(), JSON.stringify(next)); } catch {}
  }

  function removeCachedMessages(targetId: string) {
    const next = { ...sessionMessageCacheRef.current };
    delete next[targetId];
    sessionMessageCacheRef.current = next;
    try { window.sessionStorage.setItem(getCacheKey(), JSON.stringify(next)); } catch {}
  }

  // ─── API calls ──────────────────────────────────────────────────────────

  async function fetchSessionDetail(targetId: string): Promise<SessionSummary> {
    const res = await fetchApi<ApiEnvelope<SessionSummary>>(
      serverUrl, apiKey, `/api/v1/sessions/${encodeURIComponent(targetId)}`, getSessionRequestOptions(),
    );
    return unwrapResult(res, '加载会话详情失败');
  }

  async function loadSessions(preferredId?: string | null): Promise<SessionSummary[]> {
    const requestId = ++sessionListRequestRef.current;
    setSessionListLoading(true);
    try {
      const res = await fetchApi<ApiEnvelope<RawSessionListItem[]>>(
        serverUrl, apiKey, '/api/v1/sessions', getSessionRequestOptions(),
      );
      const rawList = unwrapResult(res, '获取会话列表失败');
      const ids = rawList.map(getSessionId).filter(Boolean);
      const details = await Promise.all(
        ids.map(async (id) => { try { return await fetchSessionDetail(id); } catch { return { session_id: id }; } }),
      );
      if (sessionListRequestRef.current !== requestId) return [];
      details.sort((a, b) => getSessionSortTime(b) - getSessionSortTime(a));
      setSessions(details);

      const activeId = preferredId === undefined ? sessionId : preferredId;
      if (!activeId) {
        setActiveSessionMeta(null);
      } else {
        const matched = details.find((s) => s.session_id === activeId) || null;
        setActiveSessionMeta(matched);
        if (!matched && sessionId === activeId) resetConversation('当前会话已不存在，请重新开始');
      }

      setSessionError('');
      return details;
    } catch (err: unknown) {
      if (sessionListRequestRef.current !== requestId) return [];
      setSessions([]);
      setSessionError(err instanceof Error ? err.message : '获取会话列表失败');
      return [];
    } finally {
      if (sessionListRequestRef.current === requestId) setSessionListLoading(false);
    }
  }

  async function requestNewSession(): Promise<string> {
    const res = await fetchApi<ApiEnvelope<{ session_id: string }>>(
      serverUrl, apiKey, '/api/v1/sessions', { method: 'POST', ...getSessionRequestOptions() },
    );
    return unwrapResult(res, '新建会话失败').session_id;
  }

  async function handleNewSession() {
    if (loading || sessionReplayLoading || sessionMutating) return;
    closeActionMenu();
    setSessionMutating(true);
    try {
      const nextId = await requestNewSession();
      const welcomeMsgs = makeWelcomeMessages('新会话已开始');
      setCachedMessages(nextId, welcomeMsgs);
      try { window.sessionStorage.setItem(getLastSessionKey(), nextId); } catch {}
      setSessionId(nextId);
      setActiveSessionMeta({ session_id: nextId, message_count: 0 });
      setSessionError('');
      setMessages(welcomeMsgs);
      setInput('');
      resetInputHeight();
      inputRef.current?.focus();
      await loadSessions(nextId);
    } catch (err: unknown) {
      setSessionError(err instanceof Error ? err.message : '新建会话失败');
    } finally {
      setSessionMutating(false);
    }
  }

  async function handleSelectSession(targetId: string) {
    if (loading || sessionReplayLoading || sessionMutating) return;
    closeActionMenu();
    const requestId = ++replayRequestRef.current;
    const cached = sessionMessageCacheRef.current[targetId];
    const cachedMeta = sessions.find((s) => s.session_id === targetId) || null;

    if (cached?.length) {
      try { window.sessionStorage.setItem(getLastSessionKey(), targetId); } catch {}
      setSessionId(targetId);
      setActiveSessionMeta(cachedMeta || { session_id: targetId });
      setMessages(cached);
      setInput('');
      resetInputHeight();
      setSessionError('');
    }

    setSessionReplayLoading(true);
    try {
      const detailPromise = fetchSessionDetail(targetId).catch(() => null);
      const ctxRes = await fetchApi<ApiEnvelope<SessionContextResult>>(
        serverUrl, apiKey,
        `/api/v1/sessions/${encodeURIComponent(targetId)}/context?token_budget=${MAX_SESSION_CONTEXT_BUDGET}`,
        getSessionRequestOptions(),
      );
      const context = unwrapResult(ctxRes, '加载会话失败');
      const archiveIds = Array.from(new Set([
        context.latest_archive_id || '',
        ...(context.pre_archive_abstracts || []).map((a) => a.archive_id),
      ].filter(Boolean))).sort((a, b) => getArchiveIndex(a) - getArchiveIndex(b));

      const archives = await Promise.all(
        archiveIds.map(async (archiveId) => {
          try {
            const r = await fetchApi<ApiEnvelope<SessionArchiveResult>>(
              serverUrl, apiKey,
              `/api/v1/sessions/${encodeURIComponent(targetId)}/archives/${encodeURIComponent(archiveId)}`,
              getSessionRequestOptions(),
            );
            return unwrapResult(r, `加载归档失败`);
          } catch { return null; }
        }),
      );

      if (replayRequestRef.current !== requestId) return;

      const archivedMsgs = archives
        .filter((a): a is SessionArchiveResult => Boolean(a))
        .flatMap((a) => a.messages || []);
      const mergedMsgs = [...archivedMsgs, ...(context.messages || [])];
      const detail = await detailPromise;
      const derivedTitle = deriveSessionTitleFromMessages(mergedMsgs);

      if (replayRequestRef.current !== requestId) return;

      rememberSessionTitle(targetId, derivedTitle);
      const nextMessages = mergedMsgs.length > 0
        ? mapSessionMessages(mergedMsgs)
        : (cached?.length ? cached : mapSessionMessages(mergedMsgs));

      setCachedMessages(targetId, nextMessages);
      try { window.sessionStorage.setItem(getLastSessionKey(), targetId); } catch {}
      setSessionId(targetId);
      setActiveSessionMeta(detail || { session_id: targetId });
      setMessages(nextMessages);
      setInput('');
      resetInputHeight();
      setSessionError('');
      await loadSessions(targetId);
    } catch (err: unknown) {
      if (replayRequestRef.current !== requestId) return;
      setSessionError(err instanceof Error ? err.message : '加载会话失败');
    } finally {
      if (replayRequestRef.current === requestId) setSessionReplayLoading(false);
    }
  }

  async function confirmDeleteSession() {
    if (!deleteTarget) return;
    setSessionMutating(true);
    try {
      await fetchApi<any>(
        serverUrl, apiKey,
        `/api/v1/sessions/${encodeURIComponent(deleteTarget.session_id)}`,
        { method: 'DELETE', ...getSessionRequestOptions() },
      );
      const deletedId = deleteTarget.session_id;
      closeActionMenu();
      removeCachedMessages(deletedId);
      setDerivedSessionTitles((prev) => {
        const next = { ...prev };
        delete next[deletedId];
        try { window.localStorage.setItem(getDerivedTitlesKey(), JSON.stringify(next)); } catch {}
        return next;
      });
      updateRenamedTitles((prev) => {
        const next = { ...prev };
        delete next[deletedId];
        return next;
      });
      setDeleteTarget(null);
      if (renameTarget?.session_id === deletedId) { setRenameTarget(null); setRenameValue(''); }
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

  // ─── Effects ──────────────────────────────────────────────────────────

  useEffect(() => { scrollToBottom(); }, [messages]);

  useEffect(() => {
    if (!activeActionMenu) return;
    function handlePointerDown(e: MouseEvent) {
      if (actionMenuRef.current?.contains(e.target as Node)) return;
      closeActionMenu();
    }
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [activeActionMenu]);

  useEffect(() => {
    if (!sessionId) return;
    setCachedMessages(sessionId, messages);
  }, [sessionId, messages]);

  // Load persisted titles and cache on mount
  useEffect(() => {
    try {
      const renamed = window.localStorage.getItem(getSessionTitlesKey());
      if (renamed) setRenamedSessionTitles(JSON.parse(renamed));
      const derived = window.localStorage.getItem(getDerivedTitlesKey());
      if (derived) setDerivedSessionTitles(JSON.parse(derived));
      const cache = window.sessionStorage.getItem(getCacheKey());
      if (cache) sessionMessageCacheRef.current = JSON.parse(cache);
    } catch {}

    // Restore last active session
    try {
      const lastId = window.sessionStorage.getItem(getLastSessionKey());
      if (lastId && sessionMessageCacheRef.current[lastId]?.length) {
        setSessionId(lastId);
        setMessages(sessionMessageCacheRef.current[lastId]);
      }
    } catch {}
  }, []);

  // Load sessions on mount
  useEffect(() => { void loadSessions(sessionId); }, [serverUrl, apiKey, accountId, selectedUserId]);

  // ─── Send message ──────────────────────────────────────────────────────

  const handleSend = async () => {
    if (!input.trim() || loading || !selectedUserId) return;
    const userMsg = input.trim();
    setInput('');
    resetInputHeight();
    setLoading(true);

    const botId = `bot-${Date.now()}`;
    const userMsgKey = `user-${Date.now()}`;
    let activeId = sessionId;

    setMessages((prev) => [
      ...prev,
      { key: userMsgKey, role: 'user', text: userMsg },
      { key: botId, role: 'bot', text: '', status: '思考中...', loading: true, steps: [] },
    ]);

    try {
      if (!activeId) {
        activeId = await requestNewSession();
        try { window.sessionStorage.setItem(getLastSessionKey(), activeId); } catch {}
        setSessionId(activeId);
        setActiveSessionMeta({ session_id: activeId, message_count: 0 });
      }
      if (activeId) rememberSessionTitle(activeId, userMsg);

      const url = `${serverUrl}/bot/v1/chat/stream`;
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
        body: JSON.stringify({ message: userMsg, session_id: activeId, user_id: selectedUserId }),
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
                  if (match) { name = match[1]; try { args = JSON.parse(match[2]); } catch {} }
                }
                if (!name) {
                  name = evt.data?.name || '';
                  const argsStr = evt.data?.arguments || evt.data?.args || '';
                  try { args = typeof argsStr === 'string' && argsStr.startsWith('{') ? JSON.parse(argsStr) : argsStr || {}; } catch {}
                }
                if (name.includes('search') || name.includes('检索')) {
                  const query = args.query || args.keyword || args.q || '';
                  displayStatus = query ? `正在搜索: "${query}"` : '正在检索资料库...';
                } else if (name.includes('read_file') || name.includes('read')) {
                  const path = args.path || args.file_path || args.filename || '';
                  let filename = path.split('/').pop() || path;
                  if (filename.endsWith('.md')) filename = filename.slice(0, -3);
                  displayStatus = filename ? `正在读取文件: ${filename}` : '正在读取文件...';
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
                    displayStatus = uniqueNames.length > 0
                      ? `提取到相关资源: ${uniqueNames.slice(0, 2).join('、')}${uniqueNames.length > 2 ? ' 等' : ''}`
                      : `找到 ${uriMatches.length} 份匹配资料`;
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
                try { const p = JSON.parse(evt.data) as { error?: string }; detail = p.error || evt.data; } catch { detail = evt.data; }
              }
              throw new Error(detail);
            }

            if (evt.event === 'response') {
              finalContent = typeof evt.data === 'string' ? evt.data : JSON.stringify(evt.data);
            }

            setMessages((prev) => prev.map((m) => {
              if (m.key === botId) {
                const currentSteps = m.steps || [];
                const isNewStep = status !== '思考中...' && status !== 'Bot 回复' && currentSteps[currentSteps.length - 1] !== status;
                return { ...m, status, text: finalContent || '', steps: isNewStep ? [...currentSteps, status] : currentSteps };
              }
              return m;
            }));
          } catch (err) { if (err instanceof Error) throw err; }
        }
      }

      setMessages((prev) => prev.map((m) =>
        m.key === botId ? { ...m, text: finalContent || '（无回复）', loading: false } : m,
      ));
    } catch (err: any) {
      setMessages((prev) => prev.map((m) =>
        m.key === botId ? { ...m, text: `错误: ${err.message}`, status: 'Error', loading: false } : m,
      ));
    } finally {
      setLoading(false);
      if (activeId) void loadSessions(activeId);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void handleSend(); }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    e.target.style.height = 'auto';
    const sh = e.target.scrollHeight;
    e.target.style.height = `${Math.min(sh, 200)}px`;
    e.target.style.overflowY = sh > 200 ? 'auto' : 'hidden';
  };

  // ─── Derived values ──────────────────────────────────────────────────────

  const busy = loading || sessionReplayLoading || sessionMutating;
  const normalizedQuery = sessionQuery.trim().toLowerCase();
  const filteredSessions = sessions.filter((s) => {
    if (!normalizedQuery) return true;
    const haystack = [
      getSessionTitle(s), s.session_id,
      formatDateTime(s.created_at), formatDateTime(s.updated_at),
    ].join(' ').toLowerCase();
    return haystack.includes(normalizedQuery);
  });
  const groupedSessions = ['今天', '昨天', '近 7 天', '更早']
    .map((label) => ({
      label,
      items: filteredSessions.filter((s) => getSessionGroupLabel(s.updated_at || s.created_at) === label),
    }))
    .filter((g) => g.items.length > 0);

  const activeSessionTitle = sessionId
    ? (renamedSessionTitles[sessionId] || derivedSessionTitles[sessionId] || (activeSessionMeta ? getSessionTitle(activeSessionMeta) : '当前会话'))
    : '未开始新会话';

  // ─── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="testbot-root">
      {/* Reuse the same chat-layout structure from BotChat */}
      <div className="chat-layout testbot-chat-layout">
        {/* Session panel */}
        <aside className="chat-session-panel">
          <div className="chat-session-panel-header">
            <div>
              <div className="chat-session-panel-title"><History size={16} /> 会话管理</div>
              <div className="chat-session-panel-subtitle">{selectedUserId}</div>
            </div>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => void loadSessions(sessionId)}
              disabled={busy}
              title="刷新会话历史"
            >
              <RefreshCw size={14} />
            </button>
          </div>

          <div className="chat-session-toolbar">
            <button className="btn btn-primary chat-session-create-btn" onClick={() => void handleNewSession()} disabled={busy}>
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
            />
          </div>

          <div className="chat-session-summary">
            {normalizedQuery ? `匹配 ${filteredSessions.length} / ${sessions.length} 个会话` : `共 ${sessions.length} 个会话`}
          </div>

          <div className="chat-session-list">
            {sessionListLoading && <div className="chat-session-loading"><div className="loader" /></div>}
            {!sessionListLoading && groupedSessions.map((group) => (
              <section key={group.label} className="chat-session-group">
                <div className="chat-session-group-title">{group.label}</div>
                <div className="chat-session-group-list">
                  {group.items.map((s) => {
                    const title = getSessionTitle(s);
                    const menuId = `session:${s.session_id}`;
                    return (
                      <div key={s.session_id} className={`chat-session-item ${s.session_id === sessionId ? 'active' : ''}`}>
                        <button
                          className="chat-session-item-trigger"
                          onClick={() => void handleSelectSession(s.session_id)}
                          disabled={busy}
                          title={title}
                        >
                          <div className="chat-session-item-main">
                            <div className="chat-session-item-title" title={title}>{title}</div>
                            <div className="chat-session-item-subtitle" title={s.session_id}>{s.session_id.slice(0, 12)}…</div>
                          </div>
                          <div className="chat-session-item-meta">
                            <span>{formatRelativeTime(s.updated_at || s.created_at)}</span>
                          </div>
                        </button>
                        <div
                          ref={activeActionMenu === menuId ? actionMenuRef : null}
                          className={`chat-session-item-actions ${activeActionMenu === menuId ? 'open' : ''}`}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <button
                            className={`chat-session-icon-btn ${activeActionMenu === menuId ? 'active' : ''}`}
                            onClick={() => toggleActionMenu(menuId)}
                            disabled={busy}
                          >
                            <MoreHorizontal size={14} />
                          </button>
                          {activeActionMenu === menuId && (
                            <div className="chat-session-menu">
                              <button className="chat-session-menu-item" onClick={() => openRenameDialog(s)}>
                                <Pencil size={14} /> 重命名
                              </button>
                              <button className="chat-session-menu-item danger" onClick={() => { closeActionMenu(); setDeleteTarget(s); }}>
                                <Trash2 size={14} /> 删除
                              </button>
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>
            ))}
            {!sessionListLoading && sessions.length === 0 && (
              <div className="chat-session-empty">暂无历史会话，发送第一条消息后会自动记录。</div>
            )}
            {!sessionListLoading && sessions.length > 0 && filteredSessions.length === 0 && (
              <div className="chat-session-empty">没有匹配的历史会话。</div>
            )}
          </div>
        </aside>

        {/* Main chat panel */}
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
              <span className="chat-current-meta">{messages.filter((m) => m.key !== 'welcome').length} 条消息</span>
              <span className="chat-current-meta">
                最近更新 {formatRelativeTime(activeSessionMeta?.updated_at || activeSessionMeta?.created_at)}
              </span>
            </div>
          </div>

          {sessionError && <div className="chat-inline-error">{sessionError}</div>}

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
                              {message.loading
                                ? <Loader2 size={12} className="chat-status-icon" />
                                : <Zap size={12} className="chat-status-finished-icon" />}
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
                placeholder="给 Bot 发送消息..."
                disabled={loading}
                rows={1}
              />
              <button
                className="chat-send-btn"
                onClick={() => void handleSend()}
                disabled={loading || !input.trim()}
              >
                {loading ? (
                  <div className="loader" style={{ width: 14, height: 14, borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3) rgba(255,255,255,0.3) rgba(255,255,255,0.3) #fff' }} />
                ) : (
                  <SendHorizontal size={18} style={{ position: 'relative', left: -1 }} />
                )}
              </button>
            </div>
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
          onReset={resetRenamedTitle}
          onCancel={() => { setRenameTarget(null); setRenameValue(''); }}
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

export default TestBot;
