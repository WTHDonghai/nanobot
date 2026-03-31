import React, { useEffect, useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import './Pages.css';

const RootDashboard: React.FC = () => {
  const { serverUrl, apiKey } = useAuth();
  const [data, setData] = useState<any>({ accounts: [], health: {}, ready: {} });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    const fetchData = async () => {
      try {
        const [accRes, healthRes, readyRes] = await Promise.all([
          fetchApi(serverUrl, apiKey, '/api/v1/admin/accounts').catch(() => ({ result: [] })),
          fetchApi(serverUrl, apiKey, '/health', { method: 'GET' }).catch(() => ({})),
          fetchApi(serverUrl, apiKey, '/ready', { method: 'GET' }).catch(() => ({}))
        ]);
        if (mounted) {
          setData({ accounts: accRes.result || [], health: healthRes, ready: readyRes });
          setLoading(false);
        }
      } catch (err) {
        if (mounted) setLoading(false);
      }
    };
    fetchData();
    return () => { mounted = false; };
  }, [serverUrl, apiKey]);

  if (loading) return <div className="empty"><div className="loader"></div></div>;

  const { accounts, health, ready } = data;
  const userCount = accounts.reduce((acc: number, cur: any) => acc + (cur.user_count || 0), 0);
  const version = health.version || '-';
  const checks = ready.checks || {};
  const isReady = ready.status === 'ready';

  return (
    <div>
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">租户总数 (Accounts)</div>
          <div className="stat-value primary">{accounts.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">全局用户数 (Users)</div>
          <div className="stat-value">{userCount}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">系统版本</div>
          <div className="stat-value" style={{ fontSize: '1.1rem', color: 'var(--light)' }}>{version}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">部署状态</div>
          <div className={`stat-value ${isReady ? 'success' : 'warning'}`} style={{ fontSize: '1.1rem' }}>
            {isReady ? '就绪' : '异常'}
          </div>
        </div>
      </div>
      
      <div className="section-title">
        <svg width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" style={{ marginRight: 8 }}>
          <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
        </svg>
        底座子系统状态
      </div>
      <div className="health-grid">
        {Object.entries(checks).map(([k, v]) => (
          <div className="card" key={k}>
            <div className="health-card-title">{k}</div>
            <div className={`health-card-value ${v === 'ok' ? 'health-ok' : v === 'not_configured' ? 'health-warn' : 'health-err'}`}>
              {String(v)}
            </div>
          </div>
        ))}
      </div>
      
      <hr className="divider" />
      <div className="section-title">近期创建账号</div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Account ID</th><th>创建时间</th><th>用户数</th></tr>
          </thead>
          <tbody>
            {accounts.slice(0, 5).map((a: any) => (
              <tr key={a.account_id}>
                <td><code style={{ color: 'var(--primary)' }}>{a.account_id}</code></td>
                <td style={{ color: 'var(--muted)' }}>{a.created_at ? new Date(a.created_at).toLocaleString() : '-'}</td>
                <td>{a.user_count}</td>
              </tr>
            ))}
            {accounts.length === 0 && <tr><td colSpan={3} className="empty">暂无账号</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const TenantDashboard: React.FC = () => {
  const { serverUrl, apiKey, accountId } = useAuth();
  const [data, setData] = useState<any>({ users: [], health: {}, ready: {} });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    const fetchData = async () => {
      try {
        const [usersRes, healthRes, readyRes] = await Promise.all([
          fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${accountId}/users`).catch(() => ({ result: [] })),
          fetchApi(serverUrl, apiKey, '/health', { method: 'GET' }).catch(() => ({})),
          fetchApi(serverUrl, apiKey, '/ready', { method: 'GET' }).catch(() => ({}))
        ]);
        if (mounted) {
          setData({ users: usersRes.result || [], health: healthRes, ready: readyRes });
          setLoading(false);
        }
      } catch (err) {
        if (mounted) setLoading(false);
      }
    };
    if (accountId) fetchData();
    return () => { mounted = false; };
  }, [serverUrl, apiKey, accountId]);

  if (loading) return <div className="empty"><div className="loader"></div></div>;

  const { users, health, ready } = data;
  const version = health.version || '-';
  const checks = ready.checks || {};
  const isReady = ready.status === 'ready';

  return (
    <div>
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">工作区用户数</div>
          <div className="stat-value primary">{users.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Admin 权限数量</div>
          <div className="stat-value">{users.filter((u: any) => u.role === 'admin').length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">服务器端版本</div>
          <div className="stat-value" style={{ fontSize: '1.1rem', color: 'var(--light)' }}>{version}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">大模型连接状态</div>
          <div className={`stat-value ${isReady ? 'success' : 'warning'}`} style={{ fontSize: '1.1rem' }}>
            {isReady ? '就绪' : '不可用'}
          </div>
        </div>
      </div>
      
      <hr className="divider" />
      <div className="section-title">工作区子系统依赖</div>
      <div className="health-grid">
        {Object.entries(checks).map(([k, v]) => (
          <div className="card" key={k}>
            <div className="health-card-title">{k}</div>
            <div className={`health-card-value ${v === 'ok' ? 'health-ok' : v === 'not_configured' ? 'health-warn' : 'health-err'}`}>
              {String(v)}
            </div>
          </div>
        ))}
      </div>
      
      <hr className="divider" />
      <div className="section-title">近期活动组成员</div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>User ID</th><th>账号权限角色</th></tr>
          </thead>
          <tbody>
            {users.slice(0, 5).map((u: any) => (
              <tr key={u.user_id}>
                <td><code style={{ color: 'var(--primary)' }}>{u.user_id}</code></td>
                <td><span className="badge badge-user">{u.role}</span></td>
              </tr>
            ))}
            {users.length === 0 && <tr><td colSpan={2} className="empty">暂无用户</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const Dashboard: React.FC = () => {
  const { role } = useAuth();
  if (role === 'root') {
    return <RootDashboard />;
  }
  return <TenantDashboard />;
};

export default Dashboard;
