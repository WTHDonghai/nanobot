import React, { useEffect, useState } from 'react';
import { AlertCircle } from 'lucide-react';
import { fetchApi } from '../services/api';
import ChatApp from '../components/chat/ChatApp';
import './TestBot.css';

// ─── Types ───────────────────────────────────────────────────────────────────

type ApiEnvelope<T> = {
  status?: string;
  result?: T;
  error?: { message?: string };
};

type WhoamiResult = {
  role: string;
  account_id: string;
  user_id: string;
};

// ─── Error Screen ────────────────────────────────────────────────────────────

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

// ─── Loading Screen ──────────────────────────────────────────────────────────

const LoadingScreen = () => (
  <div className="testbot-error-screen">
    <div className="testbot-loading-card">
      <div className="loader" style={{ width: 36, height: 36 }} />
      <p className="testbot-loading-text">正在验证 API Key…</p>
    </div>
  </div>
);

// ─── Main Component ──────────────────────────────────────────────────────────

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
    <div className="testbot-root">
      <ChatApp
        serverUrl={serverUrl}
        apiKey={apiKey}
        userId={whoami.user_id}
        accountId={whoami.account_id}
        role={whoami.role || 'user'}
        hideUserSelector={true}
      />
    </div>
  );
};

export default TestBot;
