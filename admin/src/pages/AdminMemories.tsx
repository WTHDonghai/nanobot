import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  RefreshCw,
  Save,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import './Pages.css';

type AccountSummary = {
  account_id: string;
};

type UserSummary = {
  user_id: string;
  role: string;
};

type MemoryScope = 'all' | 'user' | 'agent';

type MemoryItem = {
  uri: string;
  user_id: string;
  agent_id: string;
  scope: 'user' | 'agent';
  category: string;
  name: string;
  preview: string;
  content_length: number;
  updated_at?: string;
  size?: number;
};

type MemoryListResult = {
  items: MemoryItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
};

type MemoryDetail = MemoryItem & {
  content: string;
};

const CATEGORY_OPTIONS = [
  { value: '', label: '全部分类' },
  { value: 'profile', label: '用户画像' },
  { value: 'preferences', label: '偏好' },
  { value: 'entities', label: '实体' },
  { value: 'events', label: '事件' },
  { value: 'cases', label: '案例' },
  { value: 'patterns', label: '模式' },
  { value: 'tools', label: '工具' },
  { value: 'skills', label: '技能' },
];

const scopeLabel = (scope: string) => {
  if (scope === 'user') return '用户';
  if (scope === 'agent') return 'Agent';
  return '全部';
};

const categoryLabel = (category: string) => (
  CATEGORY_OPTIONS.find((option) => option.value === category)?.label || category || '-'
);

const formatTime = (value?: string) => {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
};

const ALL_USERS_VALUE = '';

const isSameMemory = (left?: MemoryItem | null, right?: MemoryItem | null) => (
  Boolean(
    left
      && right
      && left.uri === right.uri
      && left.user_id === right.user_id
      && (left.agent_id || 'default') === (right.agent_id || 'default'),
  )
);

const AdminMemories: React.FC = () => {
  const { serverUrl, apiKey, role, accountId } = useAuth();
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [selectedAcc, setSelectedAcc] = useState('');
  const [selectedUser, setSelectedUser] = useState('');
  const [agentId, setAgentId] = useState('default');
  const [scope, setScope] = useState<MemoryScope>('all');
  const [category, setCategory] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [reloadKey, setReloadKey] = useState(0);

  const [items, setItems] = useState<MemoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [draftContent, setDraftContent] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<MemoryItem | null>(null);
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null);

  const viewingAllUsers = selectedUser === ALL_USERS_VALUE;
  const selectedUserValid = useMemo(
    () => viewingAllUsers || users.some((user) => user.user_id === selectedUser),
    [selectedUser, users, viewingAllUsers],
  );
  const selectedUserLabel = viewingAllUsers ? '全部用户' : selectedUser || '-';

  const showToast = useCallback((msg: string, type: 'success' | 'error' = 'success') => {
    setToast({ msg, type });
    window.setTimeout(() => setToast(null), 2600);
  }, []);

  useEffect(() => {
    if (role === 'root') {
      fetchApi<{ result: AccountSummary[] }>(serverUrl, apiKey, '/api/v1/admin/accounts')
        .then((res) => {
          const next = res.result || [];
          setAccounts(next);
          setSelectedAcc((current) => (
            next.some((account) => account.account_id === current)
              ? current
              : next[0]?.account_id || ''
          ));
        })
        .catch((err) => setError(err?.message || '加载账号失败'));
      return;
    }
    if (accountId) {
      setAccounts([{ account_id: accountId }]);
      setSelectedAcc(accountId);
    }
  }, [accountId, apiKey, role, serverUrl]);

  useEffect(() => {
    if (!selectedAcc) {
      setUsers([]);
      setSelectedUser(ALL_USERS_VALUE);
      return;
    }
    fetchApi<{ result: UserSummary[] }>(
      serverUrl,
      apiKey,
      `/api/v1/admin/accounts/${encodeURIComponent(selectedAcc)}/users`,
    )
      .then((res) => {
        const next = res.result || [];
        setUsers(next);
        setSelectedUser((current) => {
          if (current === ALL_USERS_VALUE) return ALL_USERS_VALUE;
          return next.some((user) => user.user_id === current)
            ? current
            : ALL_USERS_VALUE;
        });
      })
      .catch((err) => {
        setUsers([]);
        setSelectedUser(ALL_USERS_VALUE);
        setError(err?.message || '加载用户失败');
      });
  }, [apiKey, selectedAcc, serverUrl]);

  const loadMemories = useCallback(() => {
    if (!selectedAcc || !selectedUserValid) {
      setItems([]);
      setTotal(0);
      setTotalPages(0);
      return;
    }

    setLoading(true);
    setError('');
    const params = new URLSearchParams({
      agent_id: agentId.trim() || 'default',
      scope,
      page: String(page),
      page_size: String(pageSize),
    });
    if (!viewingAllUsers) params.set('user_id', selectedUser);
    if (category) params.set('category', category);
    if (query.trim()) params.set('q', query.trim());

    fetchApi<{ result: MemoryListResult }>(
      serverUrl,
      apiKey,
      `/api/v1/admin/accounts/${encodeURIComponent(selectedAcc)}/memories?${params}`,
    )
      .then((res) => {
        const result = res.result;
        setItems(result?.items || []);
        setTotal(result?.total || 0);
        setTotalPages(result?.total_pages || 0);
        if (result?.page && result.page !== page) setPage(result.page);
      })
      .catch((err) => {
        setItems([]);
        setTotal(0);
        setTotalPages(0);
        setError(err?.message || '加载条目失败');
      })
      .finally(() => setLoading(false));
  }, [
    agentId,
    apiKey,
    category,
    page,
    pageSize,
    query,
    scope,
    selectedAcc,
    selectedUser,
    selectedUserValid,
    serverUrl,
    viewingAllUsers,
  ]);

  useEffect(() => {
    loadMemories();
  }, [loadMemories, reloadKey]);

  useEffect(() => {
    setPage(1);
  }, [agentId, category, query, scope, selectedAcc, selectedUser]);

  const openDetail = async (item: MemoryItem) => {
    if (!selectedAcc) return;
    setDetail(item as MemoryDetail);
    setDraftContent('');
    setDetailLoading(true);
    setDetailError('');
    try {
      const params = new URLSearchParams({
        uri: item.uri,
        user_id: item.user_id,
        agent_id: item.agent_id || 'default',
      });
      const res = await fetchApi<{ result: MemoryDetail }>(
        serverUrl,
        apiKey,
        `/api/v1/admin/accounts/${encodeURIComponent(selectedAcc)}/memories/detail?${params}`,
      );
      setDetail(res.result);
      setDraftContent(res.result.content || '');
    } catch (err: any) {
      setDetailError(err?.message || '读取记忆失败');
    } finally {
      setDetailLoading(false);
    }
  };

  const saveMemory = async () => {
    if (!selectedAcc || !detail) return;
    setSaving(true);
    setDetailError('');
    try {
      await fetchApi(
        serverUrl,
        apiKey,
        `/api/v1/admin/accounts/${encodeURIComponent(selectedAcc)}/memories`,
        {
          method: 'PUT',
          body: JSON.stringify({
            user_id: detail.user_id,
            agent_id: detail.agent_id || 'default',
            uri: detail.uri,
            content: draftContent,
            wait: true,
          }),
        },
      );
      setDetail({ ...detail, content: draftContent, content_length: draftContent.length });
      setReloadKey((current) => current + 1);
      showToast('记忆已保存');
    } catch (err: any) {
      setDetailError(err?.message || '保存记忆失败');
      showToast(err?.message || '保存记忆失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const deleteMemory = async () => {
    if (!selectedAcc || !deleteTarget) return;
    const targetUri = deleteTarget.uri;
    try {
      await fetchApi(
        serverUrl,
        apiKey,
        `/api/v1/admin/accounts/${encodeURIComponent(selectedAcc)}/memories`,
        {
          method: 'DELETE',
          body: JSON.stringify({
            user_id: deleteTarget.user_id,
            agent_id: deleteTarget.agent_id || 'default',
            uri: targetUri,
          }),
        },
      );
      setDeleteTarget(null);
      if (isSameMemory(detail, deleteTarget)) {
        setDetail(null);
        setDraftContent('');
      }
      setReloadKey((current) => current + 1);
      showToast('记忆已删除');
    } catch (err: any) {
      showToast(err?.message || '删除记忆失败', 'error');
    }
  };

  const closeDetail = useCallback(() => {
    setDetail(null);
    setDraftContent('');
    setDetailError('');
  }, []);

  const detailReady = typeof detail?.content === 'string';
  const dirty = Boolean(detailReady && draftContent !== detail.content);
  const hasActiveFilters = Boolean(query.trim() || category || scope !== 'all');
  const emptyListTitle = hasActiveFilters ? '没有匹配的条目' : '暂无条目';
  const emptyListDescription = hasActiveFilters
    ? '调整搜索、类型或分类后再查看。'
    : viewingAllUsers
      ? '当前账号下还没有可管理的条目。'
      : '当前用户还没有可管理的条目。';

  useEffect(() => {
    closeDetail();
  }, [agentId, category, closeDetail, query, scope, selectedAcc, selectedUser]);

  useEffect(() => {
    if (!detail) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeDetail();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [closeDetail, detail]);

  return (
    <div className="memory-admin-page">
      {toast && (
        <div className={`memory-toast ${toast.type}`}>
          <span>{toast.msg}</span>
          <button type="button" title="关闭提示" onClick={() => setToast(null)}>
            <X size={14} />
          </button>
        </div>
      )}

      <div className="memory-toolbar">
        <div className="memory-toolbar-group memory-toolbar-context">
          <div className="form-group">
            <label>账号</label>
            <select
              className="select"
              value={selectedAcc}
              onChange={(e) => setSelectedAcc(e.target.value)}
              disabled={role !== 'root'}
            >
              {accounts.map((account) => (
                <option key={account.account_id} value={account.account_id}>{account.account_id}</option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label>用户</label>
            <select
              className="select"
              value={selectedUser}
              onChange={(e) => setSelectedUser(e.target.value)}
            >
              <option value={ALL_USERS_VALUE}>全部用户</option>
              {users.map((user) => (
                <option key={user.user_id} value={user.user_id}>
                  {user.user_id} ({user.role})
                </option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label>Agent</label>
            <input
              className="input"
              value={agentId}
              onChange={(e) => setAgentId(e.target.value)}
              placeholder="default"
            />
          </div>
        </div>
        <div className="memory-toolbar-group memory-toolbar-filters">
          <div className="form-group">
            <label>类型</label>
            <select className="select" value={scope} onChange={(e) => setScope(e.target.value as MemoryScope)}>
              <option value="all">全部</option>
              <option value="user">用户</option>
              <option value="agent">Agent</option>
            </select>
          </div>
          <div className="form-group">
            <label>分类</label>
            <select className="select" value={category} onChange={(e) => setCategory(e.target.value)}>
              {CATEGORY_OPTIONS.map((option) => (
                <option key={option.value || 'all'} value={option.value}>{option.label}</option>
              ))}
            </select>
          </div>
          <div className="form-group memory-search-field">
            <label>搜索</label>
            <div className="input-with-icon">
              <Search size={15} />
              <input
                className="input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="URI、分类或内容"
              />
            </div>
          </div>
        </div>
        <div className="memory-toolbar-actions">
          <button
            type="button"
            className="btn btn-ghost memory-refresh-btn"
            onClick={() => setReloadKey((current) => current + 1)}
            disabled={loading}
          >
            <RefreshCw size={15} /> 刷新列表
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}
      {!viewingAllUsers && !selectedUserValid && selectedAcc && !loading && (
        <div className="error-box">当前账号没有可管理的用户。</div>
      )}

      <div className="memory-admin-grid">
        <section className="table-wrap memory-list-panel">
          <div className="table-header">
            <div>
              <div className="table-header-title">条目列表</div>
              <div className="muted-text">
                共 {total} 条 · {viewingAllUsers ? '全部用户' : `用户 ${selectedUserLabel}`}
              </div>
            </div>
            <select
              className="select memory-page-size"
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value));
                setPage(1);
              }}
            >
              <option value={25}>25 / 页</option>
              <option value={50}>50 / 页</option>
              <option value={100}>100 / 页</option>
            </select>
          </div>
          <div className="memory-list-table">
            <table>
              <thead>
                <tr>
                  <th>条目</th>
                  <th>摘要</th>
                  <th>用户 / Agent</th>
                  <th>类型</th>
                  <th>分类</th>
                  <th>更新时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={7}><div className="memory-table-state"><div className="loader" /> 加载条目</div></td>
                  </tr>
                ) : items.length === 0 ? (
                  <tr>
                    <td colSpan={7}>
                      <div className="memory-table-state memory-table-empty">
                        <strong>{emptyListTitle}</strong>
                        <span>{emptyListDescription}</span>
                      </div>
                    </td>
                  </tr>
                ) : items.map((item) => (
                  <tr
                    key={`${item.user_id}:${item.agent_id}:${item.uri}`}
                    className={`memory-row ${isSameMemory(detail, item) ? 'active' : ''}`}
                    onClick={() => openDetail(item)}
                  >
                    <td>
                      <div className="memory-name-cell" title={item.uri}>
                        <strong>{item.name}</strong>
                        <code>{item.uri}</code>
                      </div>
                    </td>
                    <td>
                      <span className="memory-preview-cell" title={item.preview || undefined}>
                        {item.preview || '暂无内容预览'}
                      </span>
                    </td>
                    <td>
                      <div className="memory-owner-cell">
                        <strong>{item.user_id}</strong>
                        <code>{item.agent_id || 'default'}</code>
                      </div>
                    </td>
                    <td><span className={`memory-scope-chip ${item.scope}`}>{scopeLabel(item.scope)}</span></td>
                    <td>{categoryLabel(item.category)}</td>
                    <td>{formatTime(item.updated_at)}</td>
                    <td>
                      <div className="td-actions">
                        <button
                          className="btn btn-ghost btn-sm list-action-btn"
                          onClick={(e) => { e.stopPropagation(); openDetail(item); }}
                        >
                          详情
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="session-pagination">
            <span className="muted-text">第 {totalPages ? page : 1} / {totalPages || 1} 页</span>
            <div className="session-pagination-actions">
              <button
                className="btn btn-ghost btn-sm"
                disabled={page <= 1 || loading}
                onClick={() => setPage((current) => Math.max(1, current - 1))}
              >
                上一页
              </button>
              <button
                className="btn btn-ghost btn-sm"
                disabled={!totalPages || page >= totalPages || loading}
                onClick={() => setPage((current) => current + 1)}
              >
                下一页
              </button>
            </div>
          </div>
        </section>
      </div>

      {detail && (
        <div
          className="memory-drawer-overlay"
          onClick={(e) => { if (e.target === e.currentTarget) closeDetail(); }}
        >
          <aside
            className="memory-drawer"
            aria-label="记忆详情"
            aria-modal="true"
            role="dialog"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="memory-detail-header memory-drawer-header">
              <div>
                <div className="memory-detail-title">{detail.name}</div>
                <code>{detail.uri}</code>
              </div>
              <button
                type="button"
                className="btn btn-ghost btn-sm memory-icon-btn"
                onClick={closeDetail}
                title="关闭详情"
                aria-label="关闭记忆详情"
              >
                <X size={14} />
              </button>
            </div>

            <div className="memory-drawer-body">
              <div className="memory-detail-meta">
                <span>用户 {detail.user_id}</span>
                <span>Agent {detail.agent_id || 'default'}</span>
                <span>{scopeLabel(detail.scope)}</span>
                <span>{categoryLabel(detail.category)}</span>
                <span>{detail.content_length || draftContent.length} 字符</span>
                <span>{formatTime(detail.updated_at)}</span>
              </div>

              {detailLoading ? (
                <div className="memory-detail-state"><div className="loader" /> 读取记忆</div>
              ) : !detailReady ? (
                <div className="memory-detail-state">
                  {detailError || '记忆详情暂不可编辑，请重新打开或刷新后重试。'}
                </div>
              ) : (
                <>
                  {detailError && <div className="error-box">{detailError}</div>}
                  <textarea
                    className="textarea memory-editor"
                    value={draftContent}
                    onChange={(e) => setDraftContent(e.target.value)}
                    spellCheck={false}
                  />
                  <div className="memory-editor-actions">
                    <button
                      className="btn btn-primary"
                      onClick={saveMemory}
                      disabled={!dirty || saving}
                    >
                      <Save size={15} /> {saving ? '保存中' : '保存记忆'}
                    </button>
                    <button
                      className="btn btn-danger"
                      onClick={() => setDeleteTarget(detail)}
                      disabled={!detailReady}
                    >
                      <Trash2 size={15} /> 删除记忆
                    </button>
                  </div>
                </>
              )}

            </div>
          </aside>
        </div>
      )}

      {deleteTarget && (
        <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) setDeleteTarget(null); }}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <AlertTriangle size={18} /> 删除记忆
            </div>
            <div className="modal-body">
              <p className="muted-text">删除后会移除文件与相关向量记录。</p>
              <code style={{ display: 'block', marginTop: 12, overflowWrap: 'anywhere' }}>{deleteTarget.uri}</code>
            </div>
            <div className="modal-footer">
              <button className="btn btn-ghost" onClick={() => setDeleteTarget(null)}>取消</button>
              <button className="btn btn-danger" onClick={deleteMemory}>
                <Trash2 size={15} /> 删除记忆
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default AdminMemories;
