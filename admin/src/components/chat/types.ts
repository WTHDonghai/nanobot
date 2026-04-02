export type ApiEnvelope<T> = {
  status?: string;
  result?: T;
  error?: {
    message?: string;
  };
};

export type UserOption = {
  user_id: string;
};

export type RawSessionListItem = {
  session_id?: string;
} | string;

export type SessionSummary = {
  session_id: string;
  created_at?: string;
  updated_at?: string;
  message_count?: number;
  pending_tokens?: number;
  commit_count?: number;
  last_commit_at?: string;
};

export type SessionContextPart = {
  type?: string;
  text?: string;
  abstract?: string;
  context_type?: string;
  tool_name?: string;
  tool_status?: string;
};

export type SessionContextMessage = {
  id?: string;
  role: 'user' | 'assistant';
  parts?: SessionContextPart[];
  created_at?: string;
};

export type SessionContextResult = {
  latest_archive_id?: string;
  pre_archive_abstracts?: Array<{ archive_id: string; abstract?: string }>;
  messages?: SessionContextMessage[];
};

export type SessionArchiveResult = {
  archive_id: string;
  messages?: SessionContextMessage[];
};

export type ChatMessage = {
  key: string;
  role: 'user' | 'bot';
  text: string;
  status?: string;
  loading?: boolean;
  createdAt?: string;
  elapsedMs?: number;
  steps?: string[];
};
