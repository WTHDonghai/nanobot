import React, { useState, useEffect } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import { AlertTriangle } from 'lucide-react';
import './Pages.css';

// ─── Confirm Modal ──────────────────────────────────────────────────────────
const ConfirmModal = ({ message, onConfirm, onCancel }: {
  message: string; onConfirm: () => void; onCancel: () => void;
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

// ─── SessionRow ──────────────────────────────────────────────────────────────
const SessionRow = ({ s, serverUrl, apiKey, selectedAcc, selectedUser, onDelete }: any) => {
  const [confirm, setConfirm] = useState(false);

  const doDelete = async () => {
    setConfirm(false);
    await fetchApi(serverUrl, apiKey, `/api/v1/sessions/${s.session_id || s}`, {
      method: 'DELETE', account: selectedAcc, user: selectedUser
    });
    onDelete();
  };

  return (
    <>
      <tr key={s.session_id || s}>
        <td><code>{s.session_id || s}</code></td>
        <td>{s.message_count ?? '-'}</td>
        <td>{s.pending_tokens ?? '-'}</td>
        <td className="td-actions">
          <button className="btn btn-danger btn-sm" onClick={() => setConfirm(true)}>删除</button>
        </td>
      </tr>
      {confirm && (
        <ConfirmModal
          message={`确认删除会话 ${s.session_id || s}？此操作不可撤销。`}
          onConfirm={doDelete}
          onCancel={() => setConfirm(false)}
        />
      )}
    </>
  );
};

// ─── Sessions Page ───────────────────────────────────────────────────────────
const Sessions: React.FC = () => {
  const { serverUrl, apiKey, role, accountId } = useAuth();
  const [accounts, setAccounts] = useState<any[]>([]);
  const [selectedAcc, setSelectedAcc] = useState('');
  const [selectedUser, setSelectedUser] = useState('default');
  const [sessions, setSessions] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);

  // Root sees all accounts; tenant admin is locked to their own account
  useEffect(() => {
    if (role === 'root') {
      fetchApi(serverUrl, apiKey, '/api/v1/admin/accounts')
        .then(res => setAccounts(res.result || []))
        .catch(console.error);
    } else if (accountId) {
      setAccounts([{ account_id: accountId }]);
      setSelectedAcc(accountId);
    }
  }, [serverUrl, apiKey, role, accountId]);

  useEffect(() => {
    let mounted = true;
    if (!selectedAcc) {
      if (mounted) setSessions([]);
      return;
    }
    setLoading(true);
    fetchApi(serverUrl, apiKey, '/api/v1/sessions', { account: selectedAcc, user: selectedUser })
      .then(res => { if (mounted) { setSessions(res.result || []); setError(''); } })
      .catch(e => { if (mounted) { setError(e.message); setSessions([]); } })
      .finally(() => { if (mounted) setLoading(false); });

    return () => { mounted = false; };
  }, [serverUrl, apiKey, selectedAcc, selectedUser, reloadKey]);

  const triggerReload = () => setReloadKey(k => k + 1);

  return (
    <div>
      <div style={{ display: 'flex', gap: 16, marginBottom: 24 }}>
        {role === 'root' && (
          <div className="form-group" style={{ flex: 1 }}>
            <label>选择账号</label>
            <select className="select" value={selectedAcc} onChange={e => setSelectedAcc(e.target.value)}>
              <option value="">-- 选择账号 --</option>
              {accounts.map(a => <option key={a.account_id} value={a.account_id}>{a.account_id}</option>)}
            </select>
          </div>
        )}
        {role !== 'root' && (
          <div className="form-group" style={{ flex: 1 }}>
            <label>当前工作区</label>
            <input className="input" value={selectedAcc} disabled style={{ opacity: 0.6 }} />
          </div>
        )}
        <div className="form-group" style={{ flex: 1 }}>
          <label>User ID</label>
          <input className="input" value={selectedUser} onChange={e => setSelectedUser(e.target.value)} placeholder="default" />
        </div>
      </div>

      {error && selectedAcc ? (
        <div style={{ color: 'var(--danger)', padding: 12, border: '1px solid var(--danger)', borderRadius: 8, background: 'rgba(239,68,68,0.1)' }}>{error}</div>
      ) : (
        <div className="table-wrap">
          <div className="table-header">
            <span className="table-header-title">会话列表 ({sessions.length})</span>
          </div>
          <table>
            <thead><tr><th>Session ID</th><th>消息数</th><th>Token 估算</th><th>操作</th></tr></thead>
            <tbody>
              {loading
                ? <tr><td colSpan={4}><div className="loader" style={{ margin: '20px auto', display: 'block' }} /></td></tr>
                : sessions.map(s => (
                  <SessionRow
                    key={s.session_id || s}
                    s={s}
                    serverUrl={serverUrl}
                    apiKey={apiKey}
                    selectedAcc={selectedAcc}
                    selectedUser={selectedUser}
                    onDelete={triggerReload}
                  />
                ))
              }
              {!loading && sessions.length === 0 && (
                <tr><td colSpan={4} className="empty">{selectedAcc ? '暂无会话' : '请先选择账号'}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default Sessions;
