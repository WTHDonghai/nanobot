import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import './Login.css';

// Server URL is always the same origin as the page — no need to configure.
const SERVER_URL = typeof window !== 'undefined' ? window.location.origin : '';

const Login: React.FC = () => {
  const [key, setKey] = useState('');
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { setAuth } = useAuth();
  const navigate = useNavigate();

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      // Verify identity via whoami — server URL is always same-origin
      const whoamiRes = await fetchApi(SERVER_URL, key, '/api/v1/system/whoami', { method: 'GET' });

      if (!whoamiRes.result || !whoamiRes.result.role) {
        throw new Error('无法获取身份信息');
      }

      const { role, account_id } = whoamiRes.result;

      if (role === 'user') {
        throw new Error('权限不足：普通用户无权访问管理与工作区面板');
      }

      setAuth(SERVER_URL, key, role, account_id, remember);
      navigate('/dashboard');
    } catch (err: any) {
      setError('登录失败：' + err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-header">
          <div className="login-logo-icon">
            <svg width="22" height="22" fill="none" stroke="#fff" strokeWidth="2.5" viewBox="0 0 24 24">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
            </svg>
          </div>
          <h1>OpenViking<br/><span className="login-subtitle-text">Admin Panel</span></h1>
        </div>
        <p className="login-desc">使用 Root API Key 或租户管理员 Key 登录</p>

        <form onSubmit={handleLogin}>
          <div className="form-group">
            <label>API Key</label>
            <input
              type="password"
              className="input"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="输入您的 API Key"
              autoFocus
            />
          </div>
          <div className="form-group checkbox-group" style={{ marginTop: '16px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <input
              type="checkbox"
              id="remember"
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
              style={{ width: '16px', height: '16px', accentColor: 'var(--primary)' }}
            />
            <label htmlFor="remember" style={{ margin: 0, textTransform: 'none', cursor: 'pointer' }}>下次自动登录 (Remember me)</label>
          </div>
          <button type="submit" className="btn btn-primary login-btn" disabled={loading}>
            {loading ? <div className="loader" style={{ width: 16, height: 16, borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3) rgba(255,255,255,0.3) rgba(255,255,255,0.3) #fff' }} /> : '登录'}
          </button>

          {error && <div className="error-msg">{error}</div>}
        </form>

        <p style={{ marginTop: 20, fontSize: '0.78rem', color: 'var(--text-muted)', textAlign: 'center', opacity: 0.6 }}>
          连接到 {SERVER_URL}
        </p>
      </div>
    </div>
  );
};

export default Login;
