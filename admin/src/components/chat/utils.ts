import {
  ApiEnvelope,
  ChatMessage,
  RawSessionListItem,
  SessionContextMessage,
  SessionContextPart,
  SessionSummary,
} from './types';

export const WELCOME_TEXT = `您好，我是您的AI工作助手，可为您提供：

1）全系统操作指南；

2）常见问题排查。

请直接输入您的问题或指令。`;
export const MAX_SESSION_CONTEXT_BUDGET = 100_000_000;

export const makeWelcomeMessages = (
  status = '',
  welcomeText = WELCOME_TEXT,
): ChatMessage[] => [
  { key: 'welcome', role: 'bot', text: welcomeText, status },
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

export const formatDuration = (value?: number): string => {
  if (value === undefined || Number.isNaN(value) || value < 0) return '--';
  if (value < 1000) return `${Math.round(value)} 毫秒`;
  if (value < 60_000) return `${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)} 秒`;

  const totalSeconds = Math.round(value / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes} 分 ${seconds} 秒`;
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

export const parseIterationFromData = (data: unknown): { current: number; total: number } | undefined => {
  const raw = typeof data === 'string' ? data : String(data ?? '');
  const match = raw.match(/(?:Iteration\s+|第\s*)?(\d+)\s*\/\s*(\d+)(?:\s*轮|)/i);
  if (!match) return undefined;

  const current = Number(match[1]);
  const total = Number(match[2]);
  if (!Number.isFinite(current) || current <= 0) return undefined;

  return { current, total };
};

export const inferIterationCountFromSteps = (steps?: string[]): number | undefined => {
  if (!Array.isArray(steps) || steps.length === 0) return undefined;

  return steps.reduce<number | undefined>((maxIteration, step) => {
    if (typeof step !== 'string') return maxIteration;
    const parsed = parseIterationFromData(step);
    if (!parsed) return maxIteration;

    return maxIteration === undefined ? parsed.current : Math.max(maxIteration, parsed.current);
  }, undefined);
};

export const rewriteBotImageUris = (value: string, serverUrl: string): string => {
  if (!value || !serverUrl || (!value.includes('send://') && !value.includes('/bot/v1/images/'))) return value;

  const base = serverUrl.replace(/\/+$/, '');
  const toImageUrl = (filename: string) => `${base}/bot/v1/images/${filename}`;
  const normalizeBotImageUrl = (ref: string) => {
    try {
      const url = new URL(ref, base);
      if (url.pathname.startsWith('/bot/v1/images/')) {
        return `${base}${url.pathname}${url.search}${url.hash}`;
      }
    } catch {
      return ref;
    }
    return ref;
  };

  const markdownRewritten = value.replace(
    /!\[([^\]]*)\]\(((?:send:\/\/|https?:\/\/|\/bot\/v1\/images\/)[^)\s]+)\)/g,
    (_match, alt: string, ref: string) => {
      if (ref.startsWith('send://')) {
        const filename = ref.slice('send://'.length);
        return `![${alt}](${toImageUrl(filename)})`;
      }
      return `![${alt}](${normalizeBotImageUrl(ref)})`;
    },
  );

  return markdownRewritten.replace(
    /send:\/\/[^\s)>"']+|https?:\/\/[^\s)>"']+\/bot\/v1\/images\/[^\s)>"']+|\/bot\/v1\/images\/[^\s)>"']+/g,
    (ref, offset: number, source: string) => {
      const before = source.slice(0, offset);
      const lastOpenParen = before.lastIndexOf('](');
      const lastCloseParen = before.lastIndexOf(')');
      if (lastOpenParen > lastCloseParen) {
        return ref;
      }

      if (!ref.startsWith('send://')) {
        return normalizeBotImageUrl(ref);
      }
      const filename = ref.slice('send://'.length);
      const alt = filename.replace(/\.[^.]+$/, '');
      return `![${alt}](${toImageUrl(filename)})`;
    },
  );
};

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

export const renderMessageText = (
  parts: SessionContextPart[] = [],
  serverUrl = '',
): string => {
  const textBlocks = parts
    .filter((part) => part.type === 'text' && part.text?.trim())
    .map((part) => rewriteBotImageUris(part.text!.trim(), serverUrl));
  if (textBlocks.length > 0) {
    return textBlocks.join('\n\n').trim();
  }

  const contextBlocks = parts
    .filter((part) => part.type === 'context' && part.abstract?.trim())
    .map((part) => rewriteBotImageUris(part.abstract!.trim(), serverUrl));
  const toolBlocks = parts
    .filter((part) => part.type === 'tool')
    .map((part) => {
      const output = part.tool_output?.trim();
      if (output) return rewriteBotImageUris(output, serverUrl);

      const name = part.tool_name?.trim();
      if (!name) return '';

      return `${name} (${part.tool_status || 'done'})`;
    })
    .filter(Boolean);

  return [...contextBlocks, ...toolBlocks].join('\n\n').trim() || '（空消息）';
};

export const normalizeMarkdownForDisplay = (value: string): string => {
  if (!value) return value;

  const normalized = value.replace(/\r\n?/g, '\n');
  const segments = normalized.split(/(```[\s\S]*?```)/g);

  return segments
    .map((segment, index) => {
      if (index % 2 === 1) return segment;

      return segment
        .replace(/\n{3,}/g, '\n\n')
        .replace(
          /(^|\n)(\s*(?:\d+\.|[\-*+])\s*)\n+(?=\S)/g,
          (_match, prefix: string, marker: string) => `${prefix}${marker.trimEnd()} `,
        )
        .trim();
    })
    .filter(Boolean)
    .join('\n\n');
};

export const mapSessionMessages = (
  sessionMessages: SessionContextMessage[],
  serverUrl = '',
  welcomeText = WELCOME_TEXT,
): ChatMessage[] => {
  if (sessionMessages.length === 0) {
    return makeWelcomeMessages('该会话暂无消息，可以继续提问', welcomeText);
  }

  return sessionMessages.map((message, index) => ({
    key: message.id || `${message.role}-${index}`,
    role: message.role === 'assistant' ? 'bot' : 'user',
    text: renderMessageText(message.parts, serverUrl),
    createdAt: message.created_at,
  }));
};

export const mergeCachedMessageMetadata = (
  messages: ChatMessage[],
  cachedMessages: ChatMessage[] = [],
): ChatMessage[] => {
  if (messages.length === 0 || cachedMessages.length === 0) return messages;

  const metadataBySignature = new Map<string, ChatMessage>();
  cachedMessages.forEach((message) => {
    metadataBySignature.set(
      `${message.role}|${message.createdAt || ''}|${message.text}`,
      message,
    );
  });

  return messages.map((message) => {
    const cached = metadataBySignature.get(
      `${message.role}|${message.createdAt || ''}|${message.text}`,
    );
    if (!cached) return message;

    return {
      ...message,
      elapsedMs: message.elapsedMs ?? cached.elapsedMs,
      steps: message.steps ?? cached.steps,
      iterationCount: message.iterationCount ?? cached.iterationCount ?? inferIterationCountFromSteps(message.steps ?? cached.steps),
    };
  });
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

export const readStoredSessionMessages = (
  storageKey: string | null,
  serverUrl = '',
): Record<string, ChatMessage[]> => {
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
          text: rewriteBotImageUris(text, serverUrl),
          status: typeof record.status === 'string' ? record.status : undefined,
          loading: false,
          streaming: false,
          createdAt: typeof record.createdAt === 'string' ? record.createdAt : undefined,
          elapsedMs: typeof record.elapsedMs === 'number' ? record.elapsedMs : undefined,
          steps: Array.isArray(record.steps)
            ? record.steps.filter((step): step is string => typeof step === 'string')
            : undefined,
          iterationCount: typeof record.iterationCount === 'number'
            ? record.iterationCount
            : inferIterationCountFromSteps(
              Array.isArray(record.steps)
                ? record.steps.filter((step): step is string => typeof step === 'string')
                : undefined,
            ),
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
