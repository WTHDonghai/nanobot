import React, { useState, useRef, useEffect } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import './BotChat.css';

const BotChat: React.FC = () => {
  const { serverUrl, apiKey, role, accountId } = useAuth();
  const [messages, setMessages] = useState<any[]>([{ role: 'bot', text: '你好！我是 XMS 技术支持专员。有什么可以帮助你？', status: 'XMS Support Bot' }]);
  const [input, setInput] = useState('');
  
  const [accounts, setAccounts] = useState<any[]>([]);
  const [users, setUsers] = useState<any[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>('');
  const [selectedUserId, setSelectedUserId] = useState<string>('');

  const [sessionId, setSessionId] = useState(crypto.randomUUID());
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // Fetch accounts based on role
  useEffect(() => {
    if (role === 'root') {
      fetchApi(serverUrl, apiKey, '/api/v1/admin/accounts')
        .then(res => {
          const accs = res.result || [];
          setAccounts(accs);
          if (accs.length > 0) setSelectedAccountId(accs[0].account_id);
        })
        .catch(() => setAccounts([]));
    } else if (accountId) {
      setAccounts([{ account_id: accountId }]);
      setSelectedAccountId(accountId);
    }
  }, [serverUrl, apiKey, role, accountId]);

  // Fetch users when account changes
  useEffect(() => {
    if (selectedAccountId) {
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
  }, [selectedAccountId, serverUrl, apiKey]);

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

  return (
    <div className="chat-layout">
      <div className="chat-config" style={{ display: 'flex', gap: 10, alignItems: 'flex-end', marginBottom: 16 }}>
        {role === 'root' && (
          <div className="form-group" style={{ margin: 0, flex: 1 }}>
            <label>账号 (Account)</label>
            <select className="select" style={{ padding: '9px 14px' }} value={selectedAccountId} onChange={e => setSelectedAccountId(e.target.value)}>
              {accounts.map(a => <option key={a.account_id} value={a.account_id}>{a.account_id}</option>)}
            </select>
          </div>
        )}
        <div className="form-group" style={{ margin: 0, flex: 1 }}>
          <label>发信用户 (User)</label>
          <select className="select" style={{ padding: '9px 14px' }} value={selectedUserId} onChange={e => setSelectedUserId(e.target.value)}>
            {users.map(u => <option key={u.user_id} value={u.user_id}>{u.user_id}</option>)}
            {users.length === 0 && <option value="" disabled>暂无用户</option>}
          </select>
        </div>
        <button className="btn btn-ghost" onClick={handleNewSession}>新会话</button>
      </div>

      <div className="chat-messages">
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg ${m.role}`}>
            {m.role === 'bot' && <div className="chat-status">{m.status}</div>}
            <div className="chat-bubble">
              {m.loading && !m.text ? (
                 <div className="typing-dots"><span/><span/><span/></div>
              ) : m.text}
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      <div className="chat-input-row" style={{ display: 'flex', gap: 10, marginTop: 16 }}>
        <textarea 
          className="textarea" 
          style={{ flex: 1, resize: 'none', height: 46 }}
          value={input} 
          onChange={e => setInput(e.target.value)} 
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); } }} 
          placeholder={selectedUserId ? "输入消息，Enter 发送" : "请先选择发信用户"} 
          disabled={loading || !selectedUserId}
        />
        <button className="btn btn-primary" onClick={handleSend} disabled={loading || !selectedUserId}>发送</button>
      </div>
    </div>
  );
};

export default BotChat;
