import React, { useState, useRef, useEffect } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import { SendHorizontal, Bot, User } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './BotChat.css';

const BotChat: React.FC = () => {
  const { serverUrl, apiKey, role, accountId } = useAuth();
  const [messages, setMessages] = useState<any[]>([{ role: 'bot', text: '你好！我是 XMS 技术支持专员。有什么可以帮助你？', status: 'XMS Support Bot' }]);
  const [input, setInput] = useState('');

  const [users, setUsers] = useState<any[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>('');
  const [selectedUserId, setSelectedUserId] = useState<string>('');

  const [sessionId, setSessionId] = useState(crypto.randomUUID());
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // Root no longer switches account here. Bot account is fixed on the server side,
  // so this page only uses the login context account to load users.
  useEffect(() => {
    if (role !== 'user') {
      setSelectedAccountId(accountId || 'default');
    }
  }, [role, accountId]);

  // Fetch users when account changes
  useEffect(() => {
    if (role === 'user') {
      fetchApi(serverUrl, apiKey, `/api/v1/system/whoami`)
        .then(res => {
          if (res.result && res.result.user_id) {
            setUsers([{ user_id: res.result.user_id }]);
            setSelectedUserId(res.result.user_id);
          }
        })
        .catch(() => {
          setUsers([]);
          setSelectedUserId('');
        });
    } else if (selectedAccountId) {
      fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${selectedAccountId}/users`)
        .then(res => {
          const usrs = res.result || [];
          setUsers(usrs);
          if (usrs.length > 0) setSelectedUserId(usrs[0].user_id);
          else setSelectedUserId('');
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

  const handleNewSession = () => {
    setSessionId(crypto.randomUUID());
    setMessages([{ role: 'bot', text: '你好！我是 XMS 技术支持专员。有什么可以帮助你？', status: '新会话已开始' }]);
  };

  const handleSend = async () => {
    if (!input.trim() || loading || !selectedUserId) return;
    
    const userMsg = input.trim();
    setInput('');
    setLoading(true);

    const botId = Date.now().toString();
    setMessages(prev => [
      ...prev,
      { role: 'user', text: userMsg },
      { role: 'bot', id: botId, text: '', status: 'Thinking...', loading: true }
    ]);

    try {
      const url = `${serverUrl}/bot/v1/chat/stream`;
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
        body: JSON.stringify({ message: userMsg, session_id: sessionId, user_id: selectedUserId })
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
            let status = 'Thinking...';
            if (evt.event === 'tool_call') status = '调用工具...';
            else if (evt.event === 'tool_result') status = '分析结果...';
            else if (evt.event === 'response') status = 'Bot 回复';

            if (evt.event === 'response') {
              finalContent = typeof evt.data === 'string' ? evt.data : JSON.stringify(evt.data);
            }

            setMessages(prev => prev.map(m => 
              m.id === botId ? { ...m, status, text: finalContent || '' } : m
            ));
          } catch (e) {}
        }
      }
      
      setMessages(prev => prev.map(m => 
        m.id === botId ? { ...m, text: finalContent || '（无回复）', loading: false } : m
      ));
    } catch (err: any) {
      setMessages(prev => prev.map(m => 
        m.id === botId ? { ...m, text: `错误: ${err.message}`, status: 'Error', loading: false } : m
      ));
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px';
  };

  return (
    <div className="chat-layout">
      {/* Hide config bar entirely for users */}
      {role !== 'user' && (
        <div className="chat-config-bar">
          {role === 'root' && (
            <div className="chat-config-note">
              Bot 使用服务端固定 Account，不支持在此页面切换。
              当前仅展示工作区 <code>{selectedAccountId || 'default'}</code> 的用户列表。
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <label style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>User</label>
            <select className="select" style={{ padding: '4px 8px', fontSize: '0.85rem' }} value={selectedUserId} onChange={e => setSelectedUserId(e.target.value)}>
              {users.map(u => <option key={u.user_id} value={u.user_id}>{u.user_id}</option>)}
              {users.length === 0 && <option value="" disabled>无</option>}
            </select>
          </div>
          <button className="btn btn-ghost btn-sm" onClick={handleNewSession} style={{ marginLeft: 'auto' }}>重置会话</button>
        </div>
      )}

      <div className="chat-feed-container">
        <div className="chat-messages">
          {messages.map((m, i) => (
            <div key={i} className={`chat-row ${m.role}`}>
              <div className={`chat-avatar ${m.role === 'user' ? 'user-av' : 'bot-av'}`}>
                {m.role === 'user' ? <User size={18} /> : <Bot size={18} />}
              </div>
              <div className="chat-content-wrap">
                {m.role === 'bot' && m.status && m.status !== 'Bot 回复' && (
                  <div className="chat-status">{m.status}</div>
                )}
                <div className="chat-bubble">
                  {m.loading && !m.text ? (
                     <div className="typing-dots"><span/><span/><span/></div>
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
                          }
                        }}
                      >
                        {m.text}
                      </ReactMarkdown>
                    </div>
                  )}
                </div>
              </div>
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>
      </div>

      <div className="chat-input-wrapper">
        <div className="chat-input-box">
          <textarea 
            value={input} 
            onChange={handleInput} 
            onKeyDown={handleKeyDown} 
            placeholder={selectedUserId ? "给 Bot 发送消息..." : "正在同步用户信息..."} 
            disabled={loading || !selectedUserId}
            rows={1}
          />
          <button className="chat-send-btn" onClick={handleSend} disabled={loading || !selectedUserId || !input.trim()}>
            {loading ? (
              <div className="loader" style={{ width: 14, height: 14, borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3) rgba(255,255,255,0.3) rgba(255,255,255,0.3) #fff' }} />
            ) : (
              <SendHorizontal size={18} style={{position: 'relative', left: -1}} />
            )}
          </button>
        </div>
      </div>

      {previewImage && (
        <div className="image-preview-overlay" onClick={() => setPreviewImage(null)}>
          <img src={previewImage} alt="Fullscreen preview" onClick={(e) => e.stopPropagation()} />
        </div>
      )}
    </div>
  );
};

export default BotChat;
