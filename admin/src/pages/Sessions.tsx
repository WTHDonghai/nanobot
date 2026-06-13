import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import MarkdownRenderer from '../components/markdown/MarkdownRenderer';
import { fetchApi } from '../services/api';
import { AlertCircle, AlertTriangle, Bot, Eye, MessageSquare, RefreshCw, Search, Wrench, X } from 'lucide-react';
import './Pages.css';

type TokenUsage = {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
};

type DailyAnalyticsRow = {
  date: string;
  active_users: number;
  session_count: number;
  message_count: number;
  user_message_count: number;
  assistant_message_count: number;
  tool_call_count: number;
  failed_tool_call_count: number;
  token_usage: TokenUsage;
};

type SessionSummary = {
  account_id: string;
  user_id: string;
  session_id: string;
  matched_message_count?: number;
  created_at?: string;
  updated_at?: string;
  first_message_at?: string;
  last_message_at?: string;
  message_count?: number;
  user_message_count?: number;
  assistant_message_count?: number;
  tool_call_count?: number;
  failed_tool_call_count?: number;
  token_usage?: TokenUsage;
};

type SessionMessage = {
  id: string;
  role: 'user' | 'assistant';
  content?: unknown;
  created_at?: string;
  parts?: Array<Record<string, unknown>>;
  token_usage?: TokenUsage;
};

type ToolRecord = {
  key: string;
  message: SessionMessage;
  tool: Record<string, unknown>;
  index: number;
};

type ParsedToolResource = {
  index: number;
  kind: string;
  uri: string;
  matchReason?: string;
  preview?: string;
  note?: string;
};

type ParsedToolResourceSection = {
  title: string;
  label: string;
  items: ParsedToolResource[];
};

type ParsedOpenVikingSearchOutput = {
  query?: string;
  targetUri?: string;
  limit?: string;
  totalMatches?: string;
  sections: ParsedToolResourceSection[];
};

type SessionDetail = SessionSummary & {
  messages?: SessionMessage[];
};

type SessionListResult = {
  items?: SessionSummary[];
  page?: number;
  page_size?: number;
  total?: number;
  total_pages?: number;
  has_prev?: boolean;
  has_next?: boolean;
};

type DetailFilter = 'all' | 'matches' | 'user' | 'assistant' | 'tools' | 'failures';
type SessionSortBy =
  | 'last_active'
  | 'created_at'
  | 'message_count'
  | 'user_message_count'
  | 'assistant_message_count'
  | 'tool_call_count'
  | 'failed_tool_call_count'
  | 'token_total'
  | 'matched_message_count'
  | 'user_id';
type SortOrder = 'asc' | 'desc';

const failedToolStatuses = new Set(['error', 'failed']);
const sessionSortOptions: Array<{ value: SessionSortBy; label: string; descLabel: string; ascLabel: string; requiresQuery?: boolean }> = [
  { value: 'last_active', label: '最后活跃', descLabel: '新到旧', ascLabel: '旧到新' },
  { value: 'created_at', label: '创建时间', descLabel: '新到旧', ascLabel: '旧到新' },
  { value: 'message_count', label: '消息量', descLabel: '多到少', ascLabel: '少到多' },
  { value: 'user_message_count', label: '用户问题', descLabel: '多到少', ascLabel: '少到多' },
  { value: 'assistant_message_count', label: 'Agent 回复', descLabel: '多到少', ascLabel: '少到多' },
  { value: 'tool_call_count', label: '工具调用', descLabel: '多到少', ascLabel: '少到多' },
  { value: 'failed_tool_call_count', label: '失败工具', descLabel: '多到少', ascLabel: '少到多' },
  { value: 'token_total', label: 'Token 消耗', descLabel: '高到低', ascLabel: '低到高' },
  { value: 'matched_message_count', label: '搜索命中', descLabel: '多到少', ascLabel: '少到多', requiresQuery: true },
  { value: 'user_id', label: '用户', descLabel: 'Z 到 A', ascLabel: 'A 到 Z' },
];

const localDateIso = (date: Date) => {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
};

const todayIso = () => localDateIso(new Date());

const daysAgoIso = (days: number) => {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return localDateIso(date);
};

const formatNumber = (value: number | undefined) => Number(value || 0).toLocaleString();

const formatTime = (value?: string) => {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
};

const messageText = (message: SessionMessage) => {
  const text = (message.parts || [])
    .filter((part) => part.type === 'text')
    .map((part) => (typeof part.text === 'string' ? part.text : ''))
    .filter(Boolean)
    .join('\n\n');
  if (text) return text;

  if (typeof message.content === 'string') return message.content;
  if (Array.isArray(message.content)) {
    return message.content
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const record = item as Record<string, unknown>;
          if (typeof record.text === 'string') return record.text;
          if (typeof record.content === 'string') return record.content;
        }
        return '';
      })
      .filter(Boolean)
      .join('\n\n');
  }
  if (message.content != null) return partValueText({ content: message.content }, 'content');
  return '';
};

const tokenTotal = (usage?: TokenUsage) => usage?.total_tokens || 0;

const partString = (part: Record<string, unknown>, key: string) => {
  const value = part[key];
  return typeof value === 'string' ? value : '';
};

const partValueText = (part: Record<string, unknown>, key: string) => {
  const value = part[key];
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try {
    return JSON.stringify(value) || '';
  } catch {
    return String(value);
  }
};

const messageTools = (message: SessionMessage) => (message.parts || []).filter((part) => part.type === 'tool');

const collectToolRecords = (messages: SessionMessage[]) => messages.flatMap((message) => (
  messageTools(message).map((tool, index) => ({
    key: `${message.id}-tool-${index}`,
    message,
    tool,
    index,
  }))
));

const toolStatus = (tool: Record<string, unknown>) => partString(tool, 'tool_status') || 'unknown';

const toolName = (tool: Record<string, unknown>) => partString(tool, 'tool_name') || 'tool';

const isFailedTool = (tool: Record<string, unknown>) => failedToolStatuses.has(toolStatus(tool).toLowerCase());

const toolMatchesQuery = (record: ToolRecord, query: string) => {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const { message, tool } = record;
  return [
    message.role,
    message.id,
    formatTime(message.created_at),
    toolName(tool),
    toolStatus(tool),
    partValueText(tool, 'tool_input'),
    partValueText(tool, 'tool_output'),
    JSON.stringify(tool, null, 2),
  ].some((value) => value.toLowerCase().includes(needle));
};

const toolTokenCount = (tool: Record<string, unknown>) => (
  Number(tool.prompt_tokens || 0) + Number(tool.completion_tokens || 0)
);

const toolInputEntries = (tool: Record<string, unknown>) => {
  const input = tool.tool_input;
  if (!input || typeof input !== 'object' || Array.isArray(input)) return [];
  return Object.entries(input as Record<string, unknown>);
};

const compactToolValue = (value: unknown) => {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const toolOutputText = (tool: Record<string, unknown>) => partValueText(tool, 'tool_output');

const sectionLabel = (title: string) => {
  if (title === 'Documents') return '命中文档';
  if (title === 'Image assets') return '命中图片';
  if (title === 'Summaries') return '摘要候选';
  if (title === 'Resources') return '资源候选';
  return title;
};

const parseOpenVikingSearchOutput = (output: string): ParsedOpenVikingSearchOutput | null => {
  if (!output.includes('OpenViking search query:') && !output.includes('Total matches:')) return null;

  const result: ParsedOpenVikingSearchOutput = { sections: [] };
  let currentSection: ParsedToolResourceSection | null = null;
  let currentItem: ParsedToolResource | null = null;

  output.split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;

    if (trimmed.startsWith('OpenViking search query:')) {
      result.query = trimmed.slice('OpenViking search query:'.length).trim();
      return;
    }
    if (trimmed.startsWith('Target URI:')) {
      result.targetUri = trimmed.slice('Target URI:'.length).trim();
      return;
    }
    if (trimmed.startsWith('Requested limit:')) {
      result.limit = trimmed.slice('Requested limit:'.length).trim();
      return;
    }
    if (trimmed.startsWith('Total matches:')) {
      result.totalMatches = trimmed.slice('Total matches:'.length).trim();
      return;
    }

    const sectionMatch = trimmed.match(/^(Documents|Image assets|Summaries|Resources):$/);
    if (sectionMatch) {
      currentSection = {
        title: sectionMatch[1],
        label: sectionLabel(sectionMatch[1]),
        items: [],
      };
      result.sections.push(currentSection);
      currentItem = null;
      return;
    }

    const itemMatch = trimmed.match(/^(\d+)\.\s+\[([^\]]+)]\s+(.+)$/);
    if (itemMatch && currentSection) {
      currentItem = {
        index: Number(itemMatch[1]),
        kind: itemMatch[2],
        uri: itemMatch[3],
      };
      currentSection.items.push(currentItem);
      return;
    }

    if (!currentItem) return;
    if (trimmed.startsWith('Match reason:')) {
      currentItem.matchReason = trimmed.slice('Match reason:'.length).trim();
      return;
    }
    if (trimmed.startsWith('Content preview:')) {
      currentItem.preview = trimmed.slice('Content preview:'.length).trim();
      return;
    }
    if (trimmed.includes('preview omitted') || trimmed.includes('Preview omitted') || trimmed.includes('Generic scope summary')) {
      currentItem.note = trimmed;
      return;
    }
    currentItem.note = currentItem.note ? `${currentItem.note}\n${trimmed}` : trimmed;
  });

  return result.query || result.totalMatches || result.sections.length > 0 ? result : null;
};

const ToolInputSummary = ({ tool, query }: { tool: Record<string, unknown>; query: string }) => {
  const entries = toolInputEntries(tool);
  if (entries.length === 0) {
    return <div className="tool-empty-note">没有记录工具参数</div>;
  }

  return (
    <div className="tool-param-list">
      {entries.map(([key, value]) => (
        <div className="tool-param-row" key={key}>
          <span>{key}</span>
          <code><HighlightedText text={compactToolValue(value)} query={query} /></code>
        </div>
      ))}
    </div>
  );
};

const OpenVikingSearchSummary = ({ summary, query }: { summary: ParsedOpenVikingSearchOutput; query: string }) => (
  <div className="tool-search-summary">
    <div className="tool-summary-metrics">
      {summary.query && <span><strong>Query</strong><HighlightedText text={summary.query} query={query} /></span>}
      {summary.targetUri && <span><strong>Target</strong><HighlightedText text={summary.targetUri} query={query} /></span>}
      {summary.limit && <span><strong>Limit</strong>{summary.limit}</span>}
      {summary.totalMatches && <span><strong>Total</strong>{summary.totalMatches}</span>}
    </div>
    {summary.sections.length > 0 ? summary.sections.map((section) => (
      <div className="tool-result-section" key={section.title}>
        <div className="tool-result-section-title">
          <span>{section.label}</span>
          <strong>{formatNumber(section.items.length)}</strong>
        </div>
        <div className="tool-result-list">
          {section.items.map((item) => (
            <div className="tool-result-item" key={`${section.title}-${item.index}-${item.uri}`}>
              <div className="tool-result-item-head">
                <span>{item.index}</span>
                <code><HighlightedText text={item.uri} query={query} /></code>
              </div>
              <div className="tool-result-kind">{item.kind}</div>
              {item.matchReason && (
                <div className="tool-result-text">
                  <strong>匹配原因</strong>
                  <span><HighlightedText text={item.matchReason} query={query} /></span>
                </div>
              )}
              {item.preview && (
                <div className="tool-result-text">
                  <strong>片段预览</strong>
                  <span><HighlightedText text={item.preview} query={query} /></span>
                </div>
              )}
              {item.note && (
                <div className="tool-result-note">
                  <HighlightedText text={item.note} query={query} />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    )) : (
      <div className="tool-empty-note">没有解析到命中文档列表</div>
    )}
  </div>
);

const ToolOutputSummary = ({ tool, query, serverUrl }: { tool: Record<string, unknown>; query: string; serverUrl: string }) => {
  const output = toolOutputText(tool);
  if (!output) return <div className="tool-empty-note">没有记录工具响应</div>;

  const searchSummary = toolName(tool) === 'openviking_search' ? parseOpenVikingSearchOutput(output) : null;
  if (searchSummary) {
    return <OpenVikingSearchSummary summary={searchSummary} query={query} />;
  }

  const preview = output.length > 4000 ? `${output.slice(0, 4000)}\n\n... 响应过长，完整内容请查看 JSON` : output;
  return (
    <MarkdownRenderer
      className="session-tool-output-markdown"
      content={preview}
      highlightQuery={query}
      serverUrl={serverUrl}
    />
  );
};

const messageMatchesQuery = (message: SessionMessage, query: string) => {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const haystacks = [message.role, messageText(message)];
  for (const part of message.parts || []) {
    haystacks.push(
      partString(part, 'tool_name'),
      partString(part, 'tool_status'),
      partValueText(part, 'tool_input'),
      partValueText(part, 'tool_output'),
      partValueText(part, 'uri'),
      partValueText(part, 'context_type'),
      partValueText(part, 'abstract')
    );
  }
  return haystacks.some((value) => value.toLowerCase().includes(needle));
};

const isRowActionTarget = (target: EventTarget | null) => (
  target instanceof HTMLElement && Boolean(target.closest('button, a, input, select, textarea, summary'))
);

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const HighlightedText = ({ text, query }: { text: string; query: string }) => {
  const needle = query.trim();
  if (!needle) return <>{text}</>;
  const parts = text.split(new RegExp(`(${escapeRegExp(needle)})`, 'ig'));
  return (
    <>
      {parts.map((part, index) => (
        part.toLowerCase() === needle.toLowerCase()
          ? <mark className="session-highlight" key={`${part}-${index}`}>{part}</mark>
          : <React.Fragment key={`${part}-${index}`}>{part}</React.Fragment>
      ))}
    </>
  );
};

const ToolRecordCard = ({ record, query, serverUrl }: { record: ToolRecord; query: string; serverUrl: string }) => {
  const failed = isFailedTool(record.tool);
  const tokens = toolTokenCount(record.tool);
  return (
    <details className={`session-tool-record ${failed ? 'has-failure' : ''}`} open={failed}>
      <summary className="tool-record-header">
        <div className="tool-record-title">
          <span className="role-chip tool">
            <Wrench size={13} />
            工具调用
          </span>
          <strong><HighlightedText text={toolName(record.tool)} query={query} /></strong>
        </div>
        <div className="tool-record-meta">
          <span>{formatTime(record.message.created_at)}</span>
          <span>来自 Agent 消息</span>
          <span className={`tool-status-label ${failed ? 'failed' : ''}`}>{toolStatus(record.tool)}</span>
          {typeof record.tool.duration_ms === 'number' && <span>{Math.round(record.tool.duration_ms)} ms</span>}
          {tokens > 0 && <span>{formatNumber(tokens)} tokens</span>}
          {failed && <span className="quality-chip danger compact"><AlertCircle size={12} /> 调用失败</span>}
        </div>
      </summary>
      <div className="tool-record-body">
        <div className="tool-readable-grid">
          <section className="tool-readable-panel">
            <div className="tool-readable-title">工具参数</div>
            <ToolInputSummary tool={record.tool} query={query} />
          </section>
          <section className="tool-readable-panel">
            <div className="tool-readable-title">响应摘要</div>
            <ToolOutputSummary tool={record.tool} query={query} serverUrl={serverUrl} />
          </section>
        </div>
        <details className="tool-json-details">
          <summary>查看完整 JSON</summary>
          <pre><HighlightedText text={JSON.stringify(record.tool, null, 2)} query={query} /></pre>
        </details>
      </div>
    </details>
  );
};

const formatDuration = (start?: string, end?: string) => {
  if (!start || !end) return '-';
  const startDate = new Date(start);
  const endDate = new Date(end);
  if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) return '-';
  const seconds = Math.max(0, Math.floor((endDate.getTime() - startDate.getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 时 ${minutes % 60} 分`;
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 时`;
};

const sessionQualityFlags = (detail: SessionDetail) => {
  const flags: Array<{ tone: 'danger' | 'warning' | 'neutral' | 'success'; label: string }> = [];
  const failedTools = detail.failed_tool_call_count || 0;
  const totalTokens = tokenTotal(detail.token_usage);
  const messageCount = detail.message_count || 0;
  if (failedTools > 0) flags.push({ tone: 'danger', label: `${formatNumber(failedTools)} 个失败工具` });
  if (totalTokens >= 50000) flags.push({ tone: 'warning', label: '高 Token 会话' });
  if (messageCount >= 20) flags.push({ tone: 'neutral', label: '长会话' });
  if (flags.length === 0) flags.push({ tone: 'success', label: '常规会话' });
  return flags;
};

const ConfirmModal = ({ message, onConfirm, onCancel }: {
  message: string; onConfirm: () => void; onCancel: () => void;
}) => (
  <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
    <div className="modal">
      <div className="modal-title" style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--danger)' }}>
        <AlertTriangle size={18} /> 操作确认
      </div>
      <div className="modal-body">
        <p style={{ margin: 0, lineHeight: 1.6 }}>{message}</p>
      </div>
      <div className="modal-footer">
        <button className="btn btn-ghost" onClick={onCancel}>取消</button>
        <button className="btn btn-danger" onClick={onConfirm}>确认删除</button>
      </div>
    </div>
  </div>
);

const DetailModal = ({ detail, loading, error, query, serverUrl, onClose }: {
  detail: SessionDetail | null;
  loading: boolean;
  error: string;
  query: string;
  serverUrl: string;
  onClose: () => void;
}) => {
  const [filter, setFilter] = useState<DetailFilter>('all');
  useEffect(() => {
    setFilter('all');
  }, [detail?.session_id, detail?.user_id]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    if (!query.trim() && filter === 'matches') setFilter('all');
  }, [filter, query]);

  const messages = useMemo(() => detail?.messages || [], [detail?.messages]);
  const toolRecords = useMemo(() => collectToolRecords(messages), [messages]);
  const normalizedQuery = query.trim();
  const matchedCount = useMemo(() => {
    if (!normalizedQuery) return 0;
    if (messages.length === 0) return detail?.matched_message_count || 0;
    return messages.filter((message) => messageMatchesQuery(message, normalizedQuery)).length;
  }, [detail?.matched_message_count, messages, normalizedQuery]);
  const userCount = useMemo(
    () => detail?.user_message_count ?? messages.filter((message) => message.role === 'user').length,
    [detail?.user_message_count, messages]
  );
  const assistantCount = useMemo(
    () => detail?.assistant_message_count ?? messages.filter((message) => message.role === 'assistant').length,
    [detail?.assistant_message_count, messages]
  );
  const toolCount = detail?.tool_call_count ?? toolRecords.length;
  const failedToolCount = useMemo(
    () => detail?.failed_tool_call_count ?? toolRecords.filter((record) => isFailedTool(record.tool)).length,
    [detail?.failed_tool_call_count, toolRecords]
  );
  const filteredMessages = useMemo(() => messages.filter((message) => {
    if (filter === 'matches') return normalizedQuery ? messageMatchesQuery(message, normalizedQuery) : true;
    if (filter === 'user') return message.role === 'user';
    if (filter === 'assistant') return message.role === 'assistant';
    if (filter === 'tools' || filter === 'failures') return false;
    return true;
  }), [filter, messages, normalizedQuery]);
  const filteredTools = useMemo(() => toolRecords.filter((record) => {
    if (filter === 'tools') return true;
    if (filter === 'failures') return isFailedTool(record.tool);
    if (filter === 'matches' && normalizedQuery) return toolMatchesQuery(record, normalizedQuery);
    return false;
  }), [filter, normalizedQuery, toolRecords]);
  const flags = useMemo(() => detail ? sessionQualityFlags(detail) : [], [detail]);
  const rawJson = useMemo(() => detail ? JSON.stringify(detail, null, 2) : '', [detail]);
  const filterOptions = useMemo(() => [
    { value: 'all' as const, label: '全部', count: messages.length || detail?.message_count || 0, show: true },
    { value: 'matches' as const, label: '命中', count: matchedCount, show: Boolean(normalizedQuery) },
    { value: 'user' as const, label: '用户', count: userCount, show: true },
    { value: 'assistant' as const, label: 'Agent 回复', count: assistantCount, show: true },
    { value: 'tools' as const, label: '工具调用', count: toolCount, show: true },
    { value: 'failures' as const, label: '失败', count: failedToolCount, show: true },
  ], [
    assistantCount,
    detail?.message_count,
    failedToolCount,
    matchedCount,
    messages.length,
    normalizedQuery,
    toolCount,
    userCount,
  ]);

  return (
    <div className="session-drawer-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <aside className="session-drawer" aria-label="会话详情" aria-modal="true" role="dialog">
        <div className="session-drawer-header">
          <div className="session-drawer-title-block">
            <div className="modal-title">会话详情</div>
            <div className="muted-text">{detail ? `${detail.user_id} / ${detail.session_id}` : '加载中'}</div>
          </div>
          <button className="btn btn-ghost btn-sm session-icon-btn" onClick={onClose} title="关闭" aria-label="关闭会话详情">
            <X size={16} />
          </button>
        </div>

        {detail && (
          <div className="session-detail-body">
            <div className="session-quality-row">
              {flags.map((flag) => (
                <span className={`quality-chip ${flag.tone}`} key={flag.label}>{flag.label}</span>
              ))}
              {normalizedQuery && (
                <span className="quality-chip neutral">命中 {formatNumber(matchedCount)} 条消息</span>
              )}
            </div>

            <div className="session-meta-grid">
              <div><span>消息</span><strong>{formatNumber(detail.message_count)}</strong></div>
              <div><span>用户问题</span><strong>{formatNumber(detail.user_message_count)}</strong></div>
              <div><span>Agent 回复</span><strong>{formatNumber(detail.assistant_message_count)}</strong></div>
              <div><span>工具失败</span><strong>{formatNumber(detail.failed_tool_call_count)}</strong></div>
              <div><span>Token</span><strong>{formatNumber(tokenTotal(detail.token_usage))}</strong></div>
              <div><span>会话跨度</span><strong>{formatDuration(detail.first_message_at || detail.created_at, detail.last_message_at || detail.updated_at)}</strong></div>
            </div>

            <div className="session-detail-toolbar">
              {filterOptions.filter((item) => item.show).map(({ value, label, count }) => (
                <button
                  className={`session-filter-chip ${filter === value ? 'active' : ''}`}
                  key={value}
                  onClick={() => setFilter(value)}
                  type="button"
                >
                  {label}<span>{formatNumber(count)}</span>
                </button>
              ))}
            </div>

            <div className="session-message-list">
              {loading ? (
                <div className="session-detail-loading inline">
                  <div className="loader" />
                  <span>正在加载完整原始记录</span>
                </div>
              ) : error ? (
                <div className="error-box session-detail-error">{error}</div>
              ) : filter === 'tools' || filter === 'failures' ? (
                filteredTools.length > 0 ? filteredTools.map((record) => (
                  <ToolRecordCard record={record} query={query} serverUrl={serverUrl} key={record.key} />
                )) : (
                  <div className="empty session-detail-empty">当前筛选下没有工具调用</div>
                )
              ) : filteredMessages.length > 0 ? filteredMessages.map((message) => {
                const tools = messageTools(message);
                const hasFailedTool = tools.some(isFailedTool);
                const text = messageText(message);
                const showInlineTools = filter !== 'assistant';
                return (
                  <div className={`session-message ${message.role} ${hasFailedTool ? 'has-failure' : ''}`} key={message.id}>
                    <div className="session-message-topline">
                      <span className={`role-chip ${message.role === 'user' ? 'user' : 'assistant'}`}>
                        {message.role === 'user' ? <MessageSquare size={13} /> : <Bot size={13} />}
                        {message.role === 'user' ? '用户' : 'Agent'}
                      </span>
                      <span>{formatTime(message.created_at)}</span>
                      {message.token_usage && <span>{formatNumber(tokenTotal(message.token_usage))} tokens</span>}
                      {hasFailedTool && <span className="quality-chip danger compact"><AlertCircle size={12} /> 工具失败</span>}
                    </div>
                    {text ? (
                      <MarkdownRenderer
                        className="session-message-markdown"
                        content={text}
                        highlightQuery={query}
                        serverUrl={serverUrl}
                      />
                    ) : tools.length === 0 ? (
                      <div className="session-message-note">该消息没有可展示的文本内容。</div>
                    ) : null}
                    {!text && !showInlineTools && tools.length > 0 && (
                      <div className="session-message-note">该 Agent 消息只有工具调用，无文本回复。</div>
                    )}
                    {!showInlineTools && tools.length > 0 && (
                      <div className="session-message-note">包含 {formatNumber(tools.length)} 次工具调用。</div>
                    )}
                    {showInlineTools && tools.length > 0 && (
                      <div className="tool-list">
                        {tools.map((tool, index) => {
                          const failed = isFailedTool(tool);
                          return (
                            <details key={`${message.id}-tool-${index}`} open={failed}>
                              <summary>
                                <span className={`tool-status-dot ${failed ? 'failed' : 'ok'}`} />
                                <span className="tool-summary-main">
                                  <Wrench size={13} />
                                  {toolName(tool)}
                                </span>
                                <span className={`tool-status-label ${failed ? 'failed' : ''}`}>{toolStatus(tool)}</span>
                                {typeof tool.duration_ms === 'number' && <span>{Math.round(tool.duration_ms)} ms</span>}
                                {(typeof tool.prompt_tokens === 'number' || typeof tool.completion_tokens === 'number') && (
                                  <span>{formatNumber(Number(tool.prompt_tokens || 0) + Number(tool.completion_tokens || 0))} tokens</span>
                                )}
                              </summary>
                              <pre><HighlightedText text={JSON.stringify(tool, null, 2)} query={query} /></pre>
                            </details>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              }) : (
                <div className="empty session-detail-empty">当前筛选下没有消息</div>
              )}
            </div>
            {!loading && !error && (
              <details className="raw-json">
                <summary>查看完整 JSON</summary>
                <pre><HighlightedText text={rawJson} query={query} /></pre>
              </details>
            )}
          </div>
        )}
        {!detail && loading && <div className="session-detail-loading"><div className="loader" /></div>}
      </aside>
    </div>
  );
};

const Sessions: React.FC = () => {
  const { serverUrl, apiKey, role, accountId } = useAuth();
  const [accounts, setAccounts] = useState<Array<{ account_id: string }>>([]);
  const [selectedAcc, setSelectedAcc] = useState('');
  const [selectedUser, setSelectedUser] = useState('');
  const [fromDate, setFromDate] = useState(daysAgoIso(13));
  const [toDate, setToDate] = useState(todayIso());
  const [query, setQuery] = useState('');
  const [sortBy, setSortBy] = useState<SessionSortBy>('last_active');
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc');
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [daily, setDaily] = useState<DailyAnalyticsRow[]>([]);
  const [totals, setTotals] = useState<DailyAnalyticsRow | null>(null);
  const [loading, setLoading] = useState(false);
  const [analyticsLoading, setAnalyticsLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [sessionTotal, setSessionTotal] = useState(0);
  const [sessionTotalPages, setSessionTotalPages] = useState(0);
  const detailRequestRef = useRef(0);

  useEffect(() => {
    if (role === 'root') {
      fetchApi(serverUrl, apiKey, '/api/v1/admin/accounts')
        .then((res) => {
          const next = res.result || [];
          setAccounts(next);
          setSelectedAcc((current) => (
            next.some((account: { account_id: string }) => account.account_id === current)
              ? current
              : next[0]?.account_id || ''
          ));
        })
        .catch(console.error);
    } else if (accountId) {
      setAccounts([{ account_id: accountId }]);
      setSelectedAcc(accountId);
    }
  }, [serverUrl, apiKey, role, accountId]);

  const buildSessionQuery = useCallback(() => {
    const params = new URLSearchParams();
    if (selectedUser.trim()) params.set('user_id', selectedUser.trim());
    if (fromDate) params.set('from_date', fromDate);
    if (toDate) params.set('to_date', toDate);
    if (query.trim()) params.set('q', query.trim());
    params.set('page', String(page));
    params.set('page_size', String(pageSize));
    params.set('sort_by', sortBy);
    params.set('sort_order', sortOrder);
    return params.toString();
  }, [fromDate, page, pageSize, query, selectedUser, sortBy, sortOrder, toDate]);

  const buildAnalyticsQuery = useCallback(() => {
    const params = new URLSearchParams();
    if (selectedUser.trim()) params.set('user_id', selectedUser.trim());
    if (fromDate) params.set('from_date', fromDate);
    if (toDate) params.set('to_date', toDate);
    return params.toString();
  }, [fromDate, selectedUser, toDate]);

  const loadSessions = useCallback(() => {
    if (!selectedAcc) {
      setSessions([]);
      setSessionTotal(0);
      setSessionTotalPages(0);
      return;
    }

    let mounted = true;
    setLoading(true);
    setError('');
    const sessionParams = buildSessionQuery();
    fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${encodeURIComponent(selectedAcc)}/sessions?${sessionParams}`)
      .then((sessionRes) => {
        if (!mounted) return;
        const sessionResult = sessionRes.result as SessionListResult | SessionSummary[] | undefined;
        if (Array.isArray(sessionResult)) {
          setSessions(sessionResult);
          setSessionTotal(sessionResult.length);
          setSessionTotalPages(sessionResult.length > 0 ? 1 : 0);
        } else {
          const items = sessionResult?.items || [];
          const responsePage = sessionResult?.page || 1;
          setSessions(items);
          setPage((current) => current === responsePage ? current : responsePage);
          setSessionTotal(sessionResult?.total || 0);
          setSessionTotalPages(sessionResult?.total_pages || 0);
        }
      })
      .catch((e) => {
        if (!mounted) return;
        setError(e.message);
        setSessions([]);
        setSessionTotal(0);
        setSessionTotalPages(0);
      })
      .finally(() => { if (mounted) setLoading(false); });

    return () => { mounted = false; };
  }, [apiKey, buildSessionQuery, selectedAcc, serverUrl]);

  const loadAnalytics = useCallback(() => {
    if (!selectedAcc) {
      setDaily([]);
      setTotals(null);
      return;
    }

    let mounted = true;
    setAnalyticsLoading(true);
    setError('');
    const analyticsParams = buildAnalyticsQuery();
    fetchApi(serverUrl, apiKey, `/api/v1/admin/accounts/${encodeURIComponent(selectedAcc)}/analytics/daily?${analyticsParams}`)
      .then((analyticsRes) => {
        if (!mounted) return;
        setDaily(analyticsRes.result?.daily || []);
        setTotals(analyticsRes.result?.totals || null);
      })
      .catch((e) => {
        if (!mounted) return;
        setError(e.message);
        setDaily([]);
        setTotals(null);
      })
      .finally(() => { if (mounted) setAnalyticsLoading(false); });

    return () => { mounted = false; };
  }, [apiKey, buildAnalyticsQuery, selectedAcc, serverUrl]);

  useEffect(() => loadSessions(), [loadSessions, reloadKey]);
  useEffect(() => loadAnalytics(), [loadAnalytics, reloadKey]);

  useEffect(() => {
    if (sortBy === 'matched_message_count' && !query.trim()) {
      setSortBy('last_active');
      setSortOrder('desc');
      setPage(1);
    }
  }, [query, sortBy]);

  const closeDetail = useCallback(() => {
    detailRequestRef.current += 1;
    setDetail(null);
    setDetailError('');
    setDetailLoading(false);
  }, []);

  const openDetail = async (session: SessionSummary) => {
    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;
    setDetail(session);
    setDetailError('');
    setDetailLoading(true);
    const params = new URLSearchParams({ user_id: session.user_id, include_messages: 'true' });
    if (query.trim()) params.set('q', query.trim());
    try {
      const res = await fetchApi(
        serverUrl,
        apiKey,
        `/api/v1/admin/accounts/${encodeURIComponent(session.account_id)}/sessions/${encodeURIComponent(session.session_id)}?${params.toString()}`
      );
      if (detailRequestRef.current === requestId) setDetail(res.result);
    } catch (e: unknown) {
      if (detailRequestRef.current === requestId) {
        setDetailError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      if (detailRequestRef.current === requestId) setDetailLoading(false);
    }
  };

  const doDelete = async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    setDeleteTarget(null);
    setError('');
    const params = new URLSearchParams({ user_id: target.user_id });
    try {
      await fetchApi(
        serverUrl,
        apiKey,
        `/api/v1/admin/accounts/${encodeURIComponent(target.account_id)}/sessions/${encodeURIComponent(target.session_id)}?${params.toString()}`,
        { method: 'DELETE' }
      );
      if (detail?.session_id === target.session_id && detail?.user_id === target.user_id) {
        closeDetail();
      }
      const currentPageHadOneItem = sessions.length === 1;
      if (currentPageHadOneItem && page > 1) {
        setPage((value) => Math.max(1, value - 1));
      } else {
        setReloadKey((key) => key + 1);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const avgTurns = useMemo(() => {
    const sessionCount = totals?.session_count || 0;
    if (!sessionCount) return '0';
    return ((totals?.user_message_count || 0) / sessionCount).toFixed(1);
  }, [totals]);
  const visibleSortOptions = useMemo(
    () => sessionSortOptions.filter((option) => !option.requiresQuery || Boolean(query.trim())),
    [query]
  );
  const currentSortOption = visibleSortOptions.find((option) => option.value === sortBy) || sessionSortOptions[0];

  const pageStart = sessionTotal === 0 ? 0 : (page - 1) * pageSize + 1;
  const pageEnd = Math.min(sessionTotal, page * pageSize);
  const canPrev = page > 1 && !loading;
  const canNext = sessionTotalPages > 0 && page < sessionTotalPages && !loading;

  return (
    <div>
      <div className="session-filters">
        {role === 'root' ? (
          <div className="form-group">
            <label>账号</label>
            <select
              className="select"
              value={selectedAcc}
              onChange={(e) => {
                setSelectedAcc(e.target.value);
                setPage(1);
              }}
            >
              <option value="">选择账号</option>
              {accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.account_id}</option>)}
            </select>
          </div>
        ) : (
          <div className="form-group">
            <label>当前工作区</label>
            <input className="input" value={selectedAcc} disabled />
          </div>
        )}
        <div className="form-group">
          <label>User ID</label>
          <input
            className="input"
            value={selectedUser}
            onChange={(e) => {
              setSelectedUser(e.target.value);
              setPage(1);
            }}
            placeholder="全部用户"
          />
        </div>
        <div className="form-group">
          <label>开始日期</label>
          <input
            className="input"
            type="date"
            value={fromDate}
            onChange={(e) => {
              setFromDate(e.target.value);
              setPage(1);
            }}
          />
        </div>
        <div className="form-group">
          <label>结束日期</label>
          <input
            className="input"
            type="date"
            value={toDate}
            onChange={(e) => {
              setToDate(e.target.value);
              setPage(1);
            }}
          />
        </div>
        <div className="form-group session-search">
          <label>搜索原始会话</label>
          <div className="input-with-icon">
            <Search size={16} />
            <input
              className="input"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(1);
              }}
              placeholder="问题、回复、工具名"
            />
          </div>
        </div>
        <button className="btn btn-primary session-refresh-btn" onClick={() => setReloadKey((key) => key + 1)} disabled={loading || analyticsLoading}>
          <RefreshCw size={16} /> 刷新
        </button>
      </div>

      {error ? <div className="error-box">{error}</div> : null}

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">活跃用户</div>
          <div className="stat-value primary">{formatNumber(totals?.active_users)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">会话数</div>
          <div className="stat-value">{formatNumber(totals?.session_count)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">平均轮次</div>
          <div className="stat-value">{avgTurns}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Token 总量</div>
          <div className="stat-value">{formatNumber(tokenTotal(totals?.token_usage))}</div>
        </div>
      </div>

      <div className="table-wrap session-daily-table">
        <div className="table-header">
          <span className="table-header-title">每日使用趋势</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>日期</th><th>用户</th><th>会话</th><th>消息</th><th>工具调用</th><th>失败工具</th><th>Token</th>
            </tr>
          </thead>
          <tbody>
            {analyticsLoading ? (
              <tr><td colSpan={7}><div className="loader" style={{ margin: '20px auto', display: 'block' }} /></td></tr>
            ) : daily.length > 0 ? daily.map((row) => (
              <tr key={row.date}>
                <td>{row.date}</td>
                <td>{formatNumber(row.active_users)}</td>
                <td>{formatNumber(row.session_count)}</td>
                <td>{formatNumber(row.message_count)}</td>
                <td>{formatNumber(row.tool_call_count)}</td>
                <td>{formatNumber(row.failed_tool_call_count)}</td>
                <td>{formatNumber(tokenTotal(row.token_usage))}</td>
              </tr>
            )) : (
              <tr><td colSpan={7} className="empty">{selectedAcc ? '暂无统计数据' : '请先选择账号'}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <hr className="divider" />

      <div className="table-wrap session-list-table">
        <div className="table-header">
          <span className="table-header-title">会话列表 (共 {formatNumber(sessionTotal)} 条)</span>
          <div className="session-list-controls">
            <div className="session-sort-control">
              <span className="muted-text">排序</span>
              <select
                className="select session-sort-select"
                value={sortBy}
                onChange={(e) => {
                  setSortBy(e.target.value as SessionSortBy);
                  setSortOrder('desc');
                  setPage(1);
                }}
                disabled={loading}
              >
                {visibleSortOptions.map((option) => (
                  <option value={option.value} key={option.value}>{option.label}</option>
                ))}
              </select>
              <button
                className="btn btn-ghost btn-sm session-sort-order-btn"
                onClick={() => {
                  setSortOrder((value) => value === 'desc' ? 'asc' : 'desc');
                  setPage(1);
                }}
                disabled={loading}
                type="button"
              >
                {sortOrder === 'desc' ? currentSortOption.descLabel : currentSortOption.ascLabel}
              </button>
            </div>
            <span className="muted-text">每页</span>
            <select
              className="select session-page-size"
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value));
                setPage(1);
              }}
              disabled={loading}
            >
              <option value={20}>20</option>
              <option value={50}>50</option>
              <option value={100}>100</option>
              <option value={200}>200</option>
            </select>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>User ID</th><th>Session ID</th><th>最后活跃</th><th>消息</th><th>工具</th><th>Token</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7}><div className="loader" style={{ margin: '20px auto', display: 'block' }} /></td></tr>
            ) : sessions.length > 0 ? sessions.map((session) => (
              <tr
                className={`session-row ${detail?.session_id === session.session_id && detail?.user_id === session.user_id ? 'active' : ''}`}
                key={`${session.user_id}:${session.session_id}`}
                onClick={(e) => {
                  if (!isRowActionTarget(e.target)) openDetail(session);
                }}
                onKeyDown={(e) => {
                  if (isRowActionTarget(e.target)) return;
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    openDetail(session);
                  }
                }}
                tabIndex={0}
                aria-selected={detail?.session_id === session.session_id && detail?.user_id === session.user_id}
              >
                <td><code>{session.user_id}</code></td>
                <td><code>{session.session_id}</code></td>
                <td>{formatTime(session.last_message_at || session.updated_at || session.created_at)}</td>
                <td>
                  <span className="session-table-metric">{formatNumber(session.message_count)}</span>
                  {query.trim() && typeof session.matched_message_count === 'number' && (
                    <span className="quality-chip neutral compact">命中 {formatNumber(session.matched_message_count)}</span>
                  )}
                </td>
                <td>
                  <span className="session-table-metric">{formatNumber(session.tool_call_count)}</span>
                  {(session.failed_tool_call_count || 0) > 0 && (
                    <span className="quality-chip danger compact">{formatNumber(session.failed_tool_call_count)} 失败</span>
                  )}
                </td>
                <td>{formatNumber(tokenTotal(session.token_usage))}</td>
                <td className="td-actions">
                  <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); openDetail(session); }}><Eye size={14} /> 查看</button>
                  <button className="btn btn-danger btn-sm" onClick={(e) => { e.stopPropagation(); setDeleteTarget(session); }}>删除</button>
                </td>
              </tr>
            )) : (
              <tr><td colSpan={7} className="empty">{selectedAcc ? '暂无会话' : '请先选择账号'}</td></tr>
            )}
          </tbody>
        </table>
        <div className="session-pagination">
          <div className="muted-text">
            {sessionTotal > 0
              ? `第 ${formatNumber(pageStart)}-${formatNumber(pageEnd)} 条 / 共 ${formatNumber(sessionTotal)} 条`
              : '共 0 条'}
          </div>
          <div className="session-pagination-actions">
            <button className="btn btn-ghost btn-sm" onClick={() => setPage((value) => Math.max(1, value - 1))} disabled={!canPrev}>
              上一页
            </button>
            <span className="session-page-indicator">
              第 {formatNumber(page)} / {formatNumber(sessionTotalPages || 1)} 页
            </span>
            <button className="btn btn-ghost btn-sm" onClick={() => setPage((value) => value + 1)} disabled={!canNext}>
              下一页
            </button>
          </div>
        </div>
      </div>

      {deleteTarget && (
        <ConfirmModal
          message={`确认删除 ${deleteTarget.user_id} 的会话 ${deleteTarget.session_id}？此操作不可撤销。`}
          onConfirm={doDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
      {detail && (
        <DetailModal
          detail={detail}
          loading={detailLoading}
          error={detailError}
          query={query}
          serverUrl={serverUrl}
          onClose={closeDetail}
        />
      )}
    </div>
  );
};

export default Sessions;
