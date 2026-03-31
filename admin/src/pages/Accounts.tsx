import React, { useState, useEffect } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import { Plus, Trash2, Key, ChevronDown, ChevronRight, Copy, Check, AlertTriangle } from 'lucide-react';
import './Pages.css';

// ─── Generic Modal ────────────────────────────────────────────────────────────
const Modal = ({ title, children, onClose, footer }: any) => (
  <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="modal">
      <div className="modal-title">{title}</div>
      <div className="modal-body">{children}</div>
      {footer && <div className="modal-footer">{footer}</div>}
    </div>
  </div>
);

// ─── Confirm Modal (replaces window.confirm) ──────────────────────────────────
const ConfirmModal = ({ message, onConfirm, onCancel, danger = false }: {
  message: string; onConfirm: () => void; onCancel: () => void; danger?: boolean;
}) => (
  <Modal
    title={<span style={{ display: 'flex', alignItems: 'center', gap: 8, color: danger ? 'var(--danger)' : undefined }}>
      {danger && <AlertTriangle size={18} />} 操作确认
    </span>}
    onClose={onCancel}
    footer={<>
      <button className="btn btn-ghost" onClick={onCancel}>取消</button>
      <button className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={onConfirm}>确认</button>
    </>}
  >
    <p style={{ margin: 0, lineHeight: 1.6 }}>{message}</p>
  </Modal>
);

// ─── API Key Display Modal (replaces window.alert for keys) ──────────────────
const ApiKeyModal = ({ title, userId, apiKey: newKey, onClose }: {
  title: string; userId: string; apiKey: string; onClose: () => void;
}) => {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(newKey).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };
  return (
    <Modal title={title} onClose={onClose} footer={
      <button className="btn btn-primary" onClick={onClose}>我已保存，关闭</button>
    }>
      <p style={{ marginBottom: 8, color: 'var(--text-muted)', fontSize: '0.9rem' }}>
        用户 <code style={{ color: 'var(--primary)' }}>{userId}</code> 的 API Key（仅展示一次，请立即复制保存）：
      </p>
      <div style={{
        background: 'var(--bg1)', border: '1px solid var(--border)',
        borderRadius: 8, padding: '12px 16px', fontFamily: 'monospace',
        fontSize: '0.85rem', wordBreak: 'break-all', position: 'relative',
        display: 'flex', alignItems: 'center', gap: 12, marginBottom: 4
      }}>
        <span style={{ flex: 1 }}>{newKey}</span>
        <button className="btn btn-ghost btn-sm" onClick={handleCopy} title="复制">
          {copied ? <Check size={14} style={{ color: 'var(--success)' }} /> : <Copy size={14} />}
        </button>
      </div>
      <p style={{ margin: 0, fontSize: '0.8rem', color: 'var(--danger)' }}>
        ⚠️ 此 Key 不会再次显示，请务必妥善保存后再关闭此窗口。
      </p>
    </Modal>
  );
};

// ─── UserRow ──────────────────────────────────────────────────────────────────
const UserRow = ({ accountId, user, onRefresh, onError, serverUrl, apiKey }: any) => {
  const [loading, setLoading] = useState('');
  const [confirm, setConfirm] = useState<{ action: string; msg: string } | null>(null);
  const [shownKey, setShownKey] = useState<{ key: string; userId: string } | null>(null);

  const handleRegenKey = () => {
    setConfirm({ action: 'regen', msg: `确定重置用户 ${user.user_id} 的 API Key？旧 Key 将立即失效。` });
  };

  const handleDelete = () => {
    setConfirm({ action: 'delete', msg: `确定删除用户 ${user.user_id}？此操作不可撤销。` });
  };

  const handleSetRole = async (newRole: string) => {
    setLoading('role');
    try {
      await fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${accountId}/users/${user.user_id}/role`, {
        method: 'PUT', body: JSON.stringify({ role: newRole })
      });
      onRefresh();
    } catch (e: any) { onError(e.message); }
    setLoading('');
  };

  const doConfirm = async () => {
    if (!confirm) return;
    const action = confirm.action;
    setConfirm(null);
    setLoading(action);
    try {
      if (action === 'regen') {
        const res = await fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${accountId}/users/${user.user_id}/key`, { method: 'POST' });
        setShownKey({ key: res.result?.user_key, userId: user.user_id });
      } else if (action === 'delete') {
        await fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${accountId}/users/${user.user_id}`, { method: 'DELETE' });
        onRefresh();
      }
    } catch (e: any) { onError(e.message); }
    setLoading('');
  };

  return (
    <>
      <tr>
        <td><code>{user.user_id}</code></td>
        <td>
          <select
            className="select"
            style={{ width: 'auto', padding: '6px 10px', fontSize: '0.8rem' }}
            value={user.role}
            onChange={(e) => handleSetRole(e.target.value)}
            disabled={loading === 'role'}
          >
            <option value="user">user</option>
            <option value="admin">admin</option>
            <option value="root">root</option>
          </select>
        </td>
        <td className="td-actions">
          <button className="btn btn-ghost btn-sm" onClick={handleRegenKey} disabled={loading === 'regen'} title="重置 Key">
            <Key size={14} />
          </button>
          <button className="btn btn-danger btn-sm" onClick={handleDelete} disabled={loading === 'delete'} title="删除用户">
            <Trash2 size={14} />
          </button>
        </td>
      </tr>

      {confirm && (
        <tr style={{ display: 'contents' }}>
          <td style={{ display: 'contents' }}>
            <ConfirmModal
              message={confirm.msg}
              danger={confirm.action === 'delete'}
              onConfirm={doConfirm}
              onCancel={() => setConfirm(null)}
            />
          </td>
        </tr>
      )}

      {shownKey && (
        <ApiKeyModal
          title="API Key 已重置"
          userId={shownKey.userId}
          apiKey={shownKey.key}
          onClose={() => setShownKey(null)}
        />
      )}
    </>
  );
};

// ─── AccountRow ─────────────────────────────────────────────────────────────
const AccountRow = ({ account, onRefresh, onError, serverUrl, apiKey }: any) => {
  const [expanded, setExpanded] = useState(false);
  const [users, setUsers] = useState<any[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [modalType, setModalType] = useState('');
  const [newUserId, setNewUserId] = useState('');
  const [newRole, setNewRole] = useState('user');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [shownKey, setShownKey] = useState<{ key: string; userId: string } | null>(null);

  const loadUsers = async () => {
    setUsersLoading(true);
    try {
      const res = await fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${account.account_id}/users`);
      setUsers(res.result || []);
    } catch (e: any) { onError(e.message); }
    setUsersLoading(false);
  };

  useEffect(() => {
    if (expanded) loadUsers();
  }, [expanded]);

  const handleDeleteAcc = async () => {
    setConfirmDelete(false);
    try {
      await fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${account.account_id}`, { method: 'DELETE' });
      onRefresh();
    } catch (e: any) { onError(e.message); }
  };

  const handleAddUser = async () => {
    if (!newUserId) return;
    try {
      const res = await fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${account.account_id}/users`, {
        method: 'POST', body: JSON.stringify({ user_id: newUserId, role: newRole })
      });
      setModalType('');
      setNewUserId('');
      setShownKey({ key: res.result?.user_key, userId: newUserId });
      loadUsers();
    } catch (e: any) { onError(e.message); }
  };

  return (
    <>
      <tr>
        <td>
          <button className="btn btn-ghost btn-sm" onClick={() => setExpanded(!expanded)} style={{ fontFamily: 'monospace' }}>
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            {account.account_id}
          </button>
        </td>
        <td style={{ color: 'var(--muted)' }}>{account.created_at ? new Date(account.created_at).toLocaleString() : '-'}</td>
        <td><span className="badge badge-user">{account.user_count} 用户</span></td>
        <td className="td-actions">
          <button className="btn btn-danger btn-sm" onClick={() => setConfirmDelete(true)}>删除账号</button>
        </td>
      </tr>

      {expanded && (
        <tr>
          <td colSpan={4} style={{ padding: 0, background: 'var(--bg3)' }}>
            <div style={{ padding: '16px 20px 20px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
                <span style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--light)' }}>用户列表 ({users.length})</span>
                <button className="btn btn-success btn-sm" onClick={() => setModalType('addUser')}>+ 添加用户</button>
              </div>
              {usersLoading ? <div className="loader" style={{ width: 16, height: 16 }} /> : (
                <table style={{ width: '100%', fontSize: '0.85rem' }}>
                  <thead><tr><th>User ID</th><th>角色</th><th>操作</th></tr></thead>
                  <tbody>
                    {users.map(u => <UserRow key={u.user_id} user={u} accountId={account.account_id} onRefresh={loadUsers} onError={onError} serverUrl={serverUrl} apiKey={apiKey} />)}
                    {users.length === 0 && <tr><td colSpan={3} className="empty">暂无用户</td></tr>}
                  </tbody>
                </table>
              )}
            </div>
          </td>
        </tr>
      )}

      {confirmDelete && (
        <ConfirmModal
          message={`确定删除账号 ${account.account_id} 及其所有数据？此操作不可恢复！`}
          danger
          onConfirm={handleDeleteAcc}
          onCancel={() => setConfirmDelete(false)}
        />
      )}

      {modalType === 'addUser' && (
        <Modal
          title={`添加用户至 ${account.account_id}`}
          onClose={() => setModalType('')}
          footer={<><button className="btn btn-ghost" onClick={() => setModalType('')}>取消</button><button className="btn btn-primary" onClick={handleAddUser}>保存</button></>}
        >
          <div className="form-group"><label>User ID</label><input className="input" value={newUserId} onChange={e => setNewUserId(e.target.value)} placeholder="user123" /></div>
          <div className="form-group" style={{ marginTop: 16 }}><label>Role</label><select className="select" value={newRole} onChange={e => setNewRole(e.target.value)}><option value="user">user</option><option value="admin">admin</option></select></div>
        </Modal>
      )}

      {shownKey && (
        <ApiKeyModal
          title={`账号 ${account.account_id} · 新用户创建成功`}
          userId={shownKey.userId}
          apiKey={shownKey.key}
          onClose={() => setShownKey(null)}
        />
      )}
    </>
  );
};

// ─── RootAccountsView ───────────────────────────────────────────────────────
const RootAccountsView: React.FC = () => {
  const { serverUrl, apiKey } = useAuth();
  const [accounts, setAccounts] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [modalType, setModalType] = useState('');
  const [newAccId, setNewAccId] = useState('');
  const [newAccAdmin, setNewAccAdmin] = useState('');
  const [shownKey, setShownKey] = useState<{ key: string; userId: string; accountId: string } | null>(null);

  const loadAccounts = async () => {
    setLoading(true);
    try {
      const res = await fetchApi(serverUrl, apiKey, '/api/v1/admin/accounts');
      setAccounts(res.result || []);
      setError('');
    } catch (err: any) {
      setError(err.message);
    }
    setLoading(false);
  };

  useEffect(() => { loadAccounts(); }, [serverUrl, apiKey]);

  const handleCreateAccount = async () => {
    if (!newAccId || !newAccAdmin) return;
    try {
      const res = await fetchApi(serverUrl, apiKey, '/api/v1/admin/accounts', {
        method: 'POST', body: JSON.stringify({ account_id: newAccId, admin_user_id: newAccAdmin })
      });
      setModalType('');
      setShownKey({ key: res.result?.user_key, userId: newAccAdmin, accountId: newAccId });
      setNewAccId('');
      setNewAccAdmin('');
      loadAccounts();
    } catch (e: any) { setError(e.message); }
  };

  if (loading) return <div className="empty"><div className="loader"></div></div>;

  return (
    <div>
      {error && <div style={{ color: 'var(--danger)', marginBottom: 16, padding: 12, border: '1px solid var(--danger)', borderRadius: 8, background: 'rgba(239,68,68,0.1)' }}>{error}</div>}

      <div className="table-wrap">
        <div className="table-header">
          <span className="table-header-title">所有账号 ({accounts.length})</span>
          <button className="btn btn-primary btn-sm" onClick={() => setModalType('create')}>
            <Plus size={16} style={{ marginRight: 4 }} /> 新建账号
          </button>
        </div>
        <table>
          <thead><tr><th>Account ID</th><th>创建时间</th><th>用户数</th><th>操作</th></tr></thead>
          <tbody>
            {accounts.map(a => <AccountRow key={a.account_id} account={a} onRefresh={loadAccounts} onError={setError} serverUrl={serverUrl} apiKey={apiKey} />)}
            {accounts.length === 0 && <tr><td colSpan={4} className="empty">暂无数据</td></tr>}
          </tbody>
        </table>
      </div>

      {modalType === 'create' && (
        <Modal
          title="创建新账号"
          onClose={() => setModalType('')}
          footer={<><button className="btn btn-ghost" onClick={() => setModalType('')}>取消</button><button className="btn btn-primary" onClick={handleCreateAccount}>创建</button></>}
        >
          <div className="form-group"><label>Account ID</label><input className="input" value={newAccId} onChange={e => setNewAccId(e.target.value)} placeholder="my-account" /></div>
          <div className="form-group" style={{ marginTop: 16 }}><label>Admin User ID</label><input className="input" value={newAccAdmin} onChange={e => setNewAccAdmin(e.target.value)} placeholder="admin" /></div>
        </Modal>
      )}

      {shownKey && (
        <ApiKeyModal
          title={`账号 ${shownKey.accountId} 创建成功`}
          userId={shownKey.userId}
          apiKey={shownKey.key}
          onClose={() => setShownKey(null)}
        />
      )}
    </div>
  );
};

// ─── TenantUsersView ───────────────────────────────────────────────────────
const TenantUsersView: React.FC = () => {
  const { serverUrl, apiKey, accountId } = useAuth();
  const [users, setUsers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [modalType, setModalType] = useState('');
  const [newUserId, setNewUserId] = useState('');
  const [newRole, setNewRole] = useState('user');
  const [shownKey, setShownKey] = useState<{ key: string; userId: string } | null>(null);

  const loadUsers = async () => {
    setLoading(true);
    try {
      const res = await fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${accountId}/users`);
      setUsers(res.result || []);
      setError('');
    } catch (e: any) { setError(e.message); }
    setLoading(false);
  };

  useEffect(() => { if (accountId) loadUsers(); }, [serverUrl, apiKey, accountId]);

  const handleAddUser = async () => {
    if (!newUserId) return;
    try {
      const res = await fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${accountId}/users`, {
        method: 'POST', body: JSON.stringify({ user_id: newUserId, role: newRole })
      });
      setModalType('');
      setShownKey({ key: res.result?.user_key, userId: newUserId });
      setNewUserId('');
      loadUsers();
    } catch (e: any) { setError(e.message); }
  };

  if (loading && !users.length) return <div className="empty"><div className="loader"></div></div>;

  return (
    <div>
      {error && <div style={{ color: 'var(--danger)', marginBottom: 16, padding: 12, border: '1px solid var(--danger)', borderRadius: 8, background: 'rgba(239,68,68,0.1)' }}>{error}</div>}
      <div className="table-wrap">
        <div className="table-header">
          <span className="table-header-title">工作区组成员 ({users.length})</span>
          <button className="btn btn-success btn-sm" onClick={() => setModalType('addUser')}>
            <Plus size={16} style={{ marginRight: 4 }} /> 添加用户
          </button>
        </div>
        <table>
          <thead><tr><th>User ID</th><th>角色</th><th>操作</th></tr></thead>
          <tbody>
            {users.map(u => <UserRow key={u.user_id} user={u} accountId={accountId} onRefresh={loadUsers} onError={setError} serverUrl={serverUrl} apiKey={apiKey} />)}
            {users.length === 0 && <tr><td colSpan={3} className="empty">暂无用户</td></tr>}
          </tbody>
        </table>
      </div>

      {modalType === 'addUser' && (
        <Modal
          title={`添加用户至工作区 ${accountId}`}
          onClose={() => setModalType('')}
          footer={<><button className="btn btn-ghost" onClick={() => setModalType('')}>取消</button><button className="btn btn-primary" onClick={handleAddUser}>保存</button></>}
        >
          <div className="form-group"><label>User ID</label><input className="input" value={newUserId} onChange={e => setNewUserId(e.target.value)} placeholder="user123" /></div>
          <div className="form-group" style={{ marginTop: 16 }}><label>Role</label><select className="select" value={newRole} onChange={e => setNewRole(e.target.value)}><option value="user">user</option><option value="admin">admin</option></select></div>
        </Modal>
      )}

      {shownKey && (
        <ApiKeyModal
          title="新用户创建成功"
          userId={shownKey.userId}
          apiKey={shownKey.key}
          onClose={() => setShownKey(null)}
        />
      )}
    </div>
  );
};

// ─── Entry ─────────────────────────────────────────────────────────────────
const Accounts: React.FC = () => {
  const { role } = useAuth();
  if (role === 'root') return <RootAccountsView />;
  return <TenantUsersView />;
};

export default Accounts;
