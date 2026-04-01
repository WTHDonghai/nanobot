import {
  ApiEnvelope,
  ChatMessage,
  RawSessionListItem,
  SessionContextMessage,
  SessionContextPart,
  SessionSummary,
} from './types';

export const WELCOME_TEXT = '你好！我是 XMS 技术支持专员。有什么可以帮助你？';
export const MAX_SESSION_CONTEXT_BUDGET = 100_000_000;

export const makeWelcomeMessages = (status = ''): ChatMessage[] => [
  { key: 'welcome', role: 'bot', text: WELCOME_TEXT, status },
];

export const unwrapResult = <T,>(response: ApiEnvelope<T>, fallbackMessage: string): T => {
  if (!response || response.status === 'error' || response.result === undefined) {
    throw new Error(response?.error?.message || fallbackMessage);
  }
  return response.result;
};

export const getSessionId = (item: RawSessionListItem): string => {
  if (typeof item === 'string') return item;
  return item.session_id || '';
};

export const getSessionSortTime = (session: SessionSummary): number => {
  const value = session.updated_at || session.created_at;
  if (!value) return 0;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? 0 : time;
};

export const getArchiveIndex = (archiveId: string): number => {
  const match = archiveId.match(/archive_(\d+)/);
  return match ? Number(match[1]) : 0;
};

export const formatDateTime = (value?: string): string => {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
};

export const formatRelativeTime = (value?: string): string => {
  if (!value) return '时间未知';
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return value;
  const diff = Date.now() - timestamp;
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return formatDateTime(value);
};

export const toSingleLine = (value: string): string => value.replace(/\s+/g, ' ').trim();

export const shortenSessionId = (value: string): string => {
  if (value.length <= 22) return value;
  return `${value.slice(0, 8)}...${value.slice(-6)}`;
};

export const formatShortSessionTime = (value?: string): string => {
  if (!value) return '未命名会话';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '未命名会话';
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).replace(',', ' ');
};

export const getSessionGroupLabel = (value?: string): string => {
  if (!value) return '更早';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '更早';

  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfTarget = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const diffDays = Math.floor((startOfToday - startOfTarget) / 86400000);

  if (diffDays <= 0) return '今天';
  if (diffDays === 1) return '昨天';
  if (diffDays < 7) return '近 7 天';
  return '更早';
};

export const getPrimaryTextFromMessage = (message: SessionContextMessage): string => {
  const textParts = (message.parts || [])
    .filter((part) => part.type === 'text' && part.text?.trim())
    .map((part) => part.text!.trim());
  return toSingleLine(textParts.join(' '));
};

export const deriveSessionTitleFromMessages = (sessionMessages: SessionContextMessage[]): string => {
  const candidate = sessionMessages.find((message) => message.role === 'user' && getPrimaryTextFromMessage(message))
    || sessionMessages.find((message) => getPrimaryTextFromMessage(message));

  if (!candidate) return '';
  return getPrimaryTextFromMessage(candidate).slice(0, 200);
};

export const renderMessageText = (parts: SessionContextPart[] = []): string => {
  const textBlocks = parts
    .filter((part) => part.type === 'text' && part.text?.trim())
    .map((part) => part.text!.trim());
  const contextLines = parts
    .filter((part) => part.type === 'context' && part.abstract?.trim())
    .map((part, index) => `${index + 1}. [${part.context_type || 'context'}] ${part.abstract!.trim()}`);
  const toolLines = parts
    .filter((part) => part.type === 'tool')
    .map((part, index) => `${index + 1}. ${part.tool_name || 'tool'} (${part.tool_status || 'done'})`);

  const sections: string[] = [];
  if (textBlocks.length > 0) sections.push(textBlocks.join('\n\n'));
  if (contextLines.length > 0) sections.push(['**关联上下文**', ...contextLines].join('\n'));
  if (toolLines.length > 0) sections.push(['**工具调用**', ...toolLines].join('\n'));

  return sections.join('\n\n').trim() || '（空消息）';
};

export const mapSessionMessages = (sessionMessages: SessionContextMessage[]): ChatMessage[] => {
  if (sessionMessages.length === 0) {
    return makeWelcomeMessages('该会话暂无消息，可以继续提问');
  }

  return sessionMessages.map((message, index) => ({
    key: message.id || `${message.role}-${index}`,
    role: message.role === 'assistant' ? 'bot' : 'user',
    text: renderMessageText(message.parts),
    createdAt: message.created_at,
  }));
};

export const readStoredSessionTitles = (storageKey: string | null): Record<string, string> => {
  if (!storageKey) return {};

  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return {};

    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return Object.entries(parsed).reduce<Record<string, string>>((acc, [key, value]) => {
      if (typeof value !== 'string') return acc;
      const normalizedValue = toSingleLine(value).slice(0, 200);
      if (!normalizedValue) return acc;
      acc[key] = normalizedValue;
      return acc;
    }, {});
  } catch {
    return {};
  }
};

export const readStoredSessionMessages = (storageKey: string | null): Record<string, ChatMessage[]> => {
  if (!storageKey) return {};

  try {
    const raw = window.sessionStorage.getItem(storageKey);
    if (!raw) return {};

    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return Object.entries(parsed).reduce<Record<string, ChatMessage[]>>((acc, [sessionId, value]) => {
      if (!Array.isArray(value)) return acc;

      const messages = value.reduce<ChatMessage[]>((items, entry, index) => {
        if (!entry || typeof entry !== 'object') return items;

        const record = entry as Record<string, unknown>;
        const role = record.role === 'user' || record.role === 'bot' ? record.role : null;
        const text = typeof record.text === 'string' ? record.text : null;
        if (!role || text === null) return items;

        items.push({
          key: typeof record.key === 'string' ? record.key : `${sessionId}-${index}`,
          role,
          text,
          status: typeof record.status === 'string' ? record.status : undefined,
          loading: typeof record.loading === 'boolean' ? record.loading : undefined,
          createdAt: typeof record.createdAt === 'string' ? record.createdAt : undefined,
        });
        return items;
      }, []);

      if (messages.length > 0) acc[sessionId] = messages;
      return acc;
    }, {});
  } catch {
    return {};
  }
};
