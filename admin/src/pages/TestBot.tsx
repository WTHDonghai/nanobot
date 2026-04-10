import React, { useEffect, useState } from 'react';
import { AlertCircle } from 'lucide-react';
import BrandMark from '../components/branding/BrandMark';
import { fetchApi } from '../services/api';
import ChatApp from '../components/chat/ChatApp';
import './TestBot.css';

const PUBLIC_PAGE_TITLE = '西软客服助理';
// const SERVICE_TOPICS = ['入住办理', '早餐时间', '发票开具', 'Wi-Fi 指引', '设施咨询', '联系人工'];

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

const PageBrand = () => (
  <div className="testbot-brand-lockup">
    <BrandMark size="lg" className="testbot-brand-mark" />
    <div>
      <div className="testbot-brand-title">support-kb</div>
    </div>
  </div>
);

// ─── Error Screen ────────────────────────────────────────────────────────────

const ErrorScreen = ({ message }: { message: string }) => (
  <div className="testbot-error-screen">
    <div className="testbot-error-card">
      <PageBrand />
      <div className="testbot-error-icon">
        <AlertCircle size={40} />
      </div>
      {/* <h1 className="testbot-error-title">无法启动{PUBLIC_PAGE_TITLE}</h1> */}
      <p className="testbot-error-desc">{message}</p>
      {/* <div className="testbot-error-hint"> */}
      {/*   <p>这是公开访问页，不需要注册，也不需要附带用户 API Key。</p> */}
      {/*   <code>/guest/</code> */}
      {/* </div> */}
    </div>
  </div>
);

// ─── Loading Screen ──────────────────────────────────────────────────────────

const LoadingScreen = () => (
  <div className="testbot-error-screen">
    <div className="testbot-loading-card">
      <PageBrand />
      <div className="loader" style={{ width: 36, height: 36 }} />
      <p className="testbot-loading-text">正在连接…</p>
    </div>
  </div>
);

// ─── Main Component ──────────────────────────────────────────────────────────

const TestBot: React.FC = () => {
  const serverUrl = window.location.origin.replace(/\/$/, '');

  const [whoami, setWhoami] = useState<WhoamiResult | null>(null);
  const [authError, setAuthError] = useState('');
  const [authLoading, setAuthLoading] = useState(true);

  // Force light theme for the public test page; restore whatever was set before on unmount.
  useEffect(() => {
    const root = document.documentElement;
    const previousTitle = document.title;
    const previousTheme = root.getAttribute('data-theme');
    document.title = PUBLIC_PAGE_TITLE;
    root.setAttribute('data-theme', 'light');
    return () => {
      document.title = previousTitle;
      if (previousTheme) {
        root.setAttribute('data-theme', previousTheme);
      } else {
        root.removeAttribute('data-theme');
      }
    };
  }, []);

  useEffect(() => {
    fetchApi<ApiEnvelope<WhoamiResult>>(serverUrl, '', '/api/v1/system/whoami')
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
  }, [serverUrl]);

  if (authLoading) return <LoadingScreen />;
  if (authError || !whoami) {
    return (
      <ErrorScreen
        message={`访问身份验证失败：${authError || '无法获取身份信息'}。请确认服务端已开启匿名 public_bot 配置。`}
      />
    );
  }

  return (
    <div className="testbot-root">
      <section className="testbot-hero">
        <div className="testbot-hero-copy">
          <span className="testbot-hero-badge">24 小时在线服务</span>
          {/* <h1>{PUBLIC_PAGE_TITLE}</h1> */}
        </div>
        {/* <div className="testbot-hero-topics" aria-label="可咨询事项"> */}
        {/*   {SERVICE_TOPICS.map((topic) => ( */}
        {/*     <span key={topic} className="testbot-topic-chip">{topic}</span> */}
        {/*   ))} */}
        {/* </div> */}
      </section>
      <ChatApp
        serverUrl={serverUrl}
        apiKey=""
        userId={whoami.user_id}
        accountId={whoami.account_id}
        role={whoami.role || 'user'}
        experience="guest"
        hideUserSelector={true}
      />
    </div>
  );
};

export default TestBot;
