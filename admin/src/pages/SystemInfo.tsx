import React, { useEffect, useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';

const SystemInfo: React.FC = () => {
  const { serverUrl, apiKey } = useAuth();
  const [data, setData] = useState<any>({ health: {}, ready: {} });
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    Promise.all([
      fetchApi(serverUrl, apiKey, '/health', { method: 'GET' }).catch(() => ({})),
      fetchApi(serverUrl, apiKey, '/ready', { method: 'GET' }).catch(() => ({}))
    ]).then(([health, ready]) => {
      if (mounted) { setData({ health, ready }); setLoading(false); }
    });
    return () => { mounted = false; };
  }, [serverUrl, apiKey]);

  if (loading) return <div className="empty"><div className="loader"></div></div>;

  return (
    <div>
      <div className="section-title">系统接口</div>
      <div style={{ display: 'flex', gap: 16, marginBottom: 32 }}>
        <a href={`${serverUrl}/docs`} target="_blank" rel="noreferrer" className="btn btn-ghost" style={{ border: '1px solid var(--border)', background: 'var(--card)' }}>
          Swagger UI
        </a>
        <a href={`${serverUrl}/redoc`} target="_blank" rel="noreferrer" className="btn btn-ghost" style={{ border: '1px solid var(--border)', background: 'var(--card)' }}>
          ReDoc
        </a>
      </div>

      <div className="section-title">Health 信息</div>
      <pre style={{ background: 'var(--card)', padding: 20, borderRadius: 14, border: '1px solid var(--border)', fontSize: '0.9rem', overflowX: 'auto', marginBottom: 24, color: 'var(--success)' }}>
        {JSON.stringify(data.health, null, 2)}
      </pre>

      <div className="section-title">Ready 状态</div>
      <pre style={{ background: 'var(--card)', padding: 20, borderRadius: 14, border: '1px solid var(--border)', fontSize: '0.9rem', overflowX: 'auto', color: 'var(--warning)' }}>
        {JSON.stringify(data.ready, null, 2)}
      </pre>
    </div>
  );
};

export default SystemInfo;
