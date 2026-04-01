import React from 'react';
import { useAuth } from '../contexts/AuthContext';
import ChatApp from '../components/chat/ChatApp';

const BotChat: React.FC = () => {
  const { serverUrl, apiKey, role, accountId, userId } = useAuth();

  return (
    <div className="chat-layout">
      <ChatApp
        serverUrl={serverUrl || ''}
        apiKey={apiKey || ''}
        accountId={accountId || ''}
        userId={userId || ''}
        role={role || ''}
      />
    </div>
  );
};

export default BotChat;
