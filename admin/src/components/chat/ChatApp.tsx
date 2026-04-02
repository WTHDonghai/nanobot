import React, { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  ChevronRight,
  Loader2,
  MoreHorizontal,
  Pencil,
  SendHorizontal,
  Trash2,
  User,
  Zap,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { fetchApi } from '../../services/api';
import {
  ApiEnvelope,
  ChatMessage,
  RawSessionListItem,
  SessionArchiveResult,
  SessionContextResult,
  SessionSummary,
  UserOption,
} from './types';
import {
  MAX_SESSION_CONTEXT_BUDGET,
  deriveSessionTitleFromMessages,
  formatDateTime,
  formatRelativeTime,
  getArchiveIndex,
  getSessionSortTime,
  getSessionId,
  makeWelcomeMessages,
  mapSessionMessages,
  readStoredSessionMessages,
  readStoredSessionTitles,
  unwrapResult,
  shortenSessionId,
  toSingleLine,
  formatShortSessionTime,
} from './utils';
import SessionSidebar from './SessionSidebar';
import './ChatApp.css';

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

function summarizeReasoningEvent(data: unknown): string {
  const raw = typeof data === 'string' ? data : JSON.stringify(data ?? '');
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
  const raw = typeof data === 'string' ? data : JSON.stringify(data ?? '');
  const match = raw.match(/Iteration\s+(\d+)\/(\d+)/i);
  if (match) {
    return `第 ${match[1]} / ${match[2]} 轮规划中...`;
  }
  return '正在进入下一轮分析...';
}

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
            if (evt.event === 'reasoning') {
              status = summarizeReasoningEvent(evt.data);
            } else if (evt.event === 'iteration') {
              status = summarizeIterationEvent(evt.data);
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
  const activeSessionTitle = sessionId
    ? (renamedSessionTitles[sessionId] || derivedSessionTitles[sessionId] || (activeSessionMeta ? getSessionTitle(activeSessionMeta) : '当前会话'))
    : '未开始新会话';

  return (
    <div className="chat-layout">
      <SessionSidebar
        sessions={sessions}
        sessionId={sessionId}
        ready={ready}
        busy={busy}
        sessionListLoading={sessionListLoading}
        currentIdentityLabel={currentIdentityLabel}
        onSelectSession={(id) => { void handleSelectSession(id); }}
        onNewSession={handleNewSession}
        onRefreshSessions={() => { void loadSessions(sessionId); }}
        onRenameSession={openRenameDialog}
        onDeleteSession={(session) => { setDeleteTarget(session); }}
        onBatchDelete={handleBatchDeleteSessions}
        getSessionTitle={getSessionTitle}
        getSessionSubtitle={getSessionSubtitle}
      />

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
