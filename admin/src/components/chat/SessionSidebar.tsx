import React, { useState, useRef, useEffect } from 'react';
import {
  CheckSquare,
  History,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Square,
  Trash2,
} from 'lucide-react';
import BrandMark from '../branding/BrandMark';
import { SessionSummary } from './types';
import { formatDateTime, formatRelativeTime, getSessionGroupLabel } from './utils';

export type SessionSidebarProps = {
  sessions: SessionSummary[];
  sessionId: string | null;
  ready: boolean;
  busy: boolean;
  sessionListLoading: boolean;
  currentIdentityLabel: string;
  notReadyMessage: string;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onRefreshSessions: () => void;
  onRenameSession: (session: SessionSummary) => void;
  onDeleteSession: (session: SessionSummary) => void;
  onBatchDelete: (ids: string[]) => Promise<void>;
  getSessionTitle: (session: SessionSummary) => string;
  getSessionSubtitle: (session: SessionSummary) => string;
};

const SessionSidebar: React.FC<SessionSidebarProps> = ({
  sessions,
  sessionId,
  ready,
  busy,
  sessionListLoading,
  currentIdentityLabel,
  notReadyMessage,
  onSelectSession,
  onNewSession,
  onRefreshSessions,
  onRenameSession,
  onDeleteSession,
  onBatchDelete,
  getSessionTitle,
  getSessionSubtitle,
}) => {
  const [sessionQuery, setSessionQuery] = useState('');
  const [isSelectMode, setIsSelectMode] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<Set<string>>(new Set());
  const [isBatchDeleting, setIsBatchDeleting] = useState(false);
  const [activeActionMenu, setActiveActionMenu] = useState<string | null>(null);
  const actionMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!activeActionMenu) return;
    function handlePointerDown(e: MouseEvent) {
      if (actionMenuRef.current?.contains(e.target as Node)) return;
      setActiveActionMenu(null);
    }
    document.addEventListener('mousedown', handlePointerDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
    };
  }, [activeActionMenu]);

  useEffect(() => {
    // If sessions list changes completely or not ready, reset states
    if (!ready) {
      setIsSelectMode(false);
      setSelectedSessionIds(new Set());
      setSessionQuery('');
      setActiveActionMenu(null);
    }
  }, [ready]);

  const toggleActionMenu = (menuId: string) => {
    setActiveActionMenu((prev) => (prev === menuId ? null : menuId));
  };

  const closeActionMenu = () => {
    setActiveActionMenu(null);
  };

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

  const handleBatchDelete = async () => {
    if (selectedSessionIds.size === 0) return;
    setIsBatchDeleting(true);
    await onBatchDelete(Array.from(selectedSessionIds));
    setIsBatchDeleting(false);
    setSelectedSessionIds(new Set());
    setIsSelectMode(false);
  };

  return (
    <aside className="chat-session-panel">
      <div className="chat-session-panel-header">
        <div className="chat-session-panel-heading">
          <div className="chat-session-brand">
            <BrandMark size="sm" className="chat-session-brand-mark" />
            <div className="chat-session-brand-copy">
              <div className="chat-session-brand-title">support-agent</div>
            </div>
          </div>
          <div className="chat-session-panel-subtitle">{currentIdentityLabel}</div>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          {sessions.length > 0 && (
            <button
              className={`btn btn-sm ${isSelectMode ? 'btn-primary' : 'btn-ghost'}`}
              onClick={() => {
                setIsSelectMode(!isSelectMode);
                if (!isSelectMode) setSelectedSessionIds(new Set());
              }}
              disabled={!ready || busy || isBatchDeleting}
              title={isSelectMode ? '取消管理' : '批量管理'}
            >
              {isSelectMode ? '完成' : '管理'}
            </button>
          )}
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => onRefreshSessions()}
            disabled={!ready || busy || isSelectMode || isBatchDeleting}
            title="刷新会话历史"
          >
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

      <div className="chat-session-toolbar">
        <button className="btn btn-primary chat-session-create-btn" onClick={onNewSession} disabled={!ready || busy}>
          <Plus size={16} /> 新建会话
        </button>
      </div>

      <div className="chat-session-search">
        <Search size={16} />
        <input
          className="input"
          value={sessionQuery}
          onChange={(e) => setSessionQuery(e.target.value)}
          placeholder="搜索 Session / 标题 / 时间"
          disabled={!ready}
        />
      </div>

      <div className="chat-session-summary">
        {normalizedQuery ? `匹配 ${filteredSessions.length} / ${sessions.length} 个会话` : `共 ${sessions.length} 个会话`}
      </div>

      <div className="chat-session-list">
        {!ready && <div className="chat-session-empty">{notReadyMessage}</div>}
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
                          if (session.session_id !== sessionId) {
                            onSelectSession(session.session_id);
                          }
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
                            <button className="chat-session-menu-item" onClick={() => { closeActionMenu(); onRenameSession(session); }}>
                              <Pencil size={14} /> 重命名
                            </button>
                            <button className="chat-session-menu-item danger" onClick={() => { closeActionMenu(); onDeleteSession(session); }}>
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
                  setSelectedSessionIds(new Set(filteredSessions.map((s) => s.session_id)));
                }
              }}
              disabled={isBatchDeleting}
            >
              {selectedSessionIds.size === filteredSessions.length && filteredSessions.length > 0 ? '全不选' : '全选'}
            </button>
            <button
              className="btn btn-danger btn-sm"
              onClick={() => void handleBatchDelete()}
              disabled={selectedSessionIds.size === 0 || isBatchDeleting}
            >
              {isBatchDeleting ? <div className="loader" style={{ width: 14, height: 14, borderWidth: 2 }} /> : <Trash2 size={14} />}
              {isBatchDeleting ? '删除中...' : '删除'}
            </button>
          </div>
        </div>
      )}
    </aside>
  );
};

export default SessionSidebar;
