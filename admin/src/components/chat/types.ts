export type ApiEnvelope<T> = {
  status?: string;
  result?: T;
  error?: {
    message?: string;
  };
};

export type ChatExperience = 'default' | 'guest';

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
  feedback_count?: number;
  positive_feedback_count?: number;
  negative_feedback_count?: number;
  latest_feedback_at?: string;
  feedback_memory_pending_count?: number;
  feedback_memory_completed_count?: number;
  feedback_memory_failed_count?: number;
  feedback_memory_skipped_count?: number;
  feedback_memory_extracted_count?: number;
};

export type SessionContextPart = {
  type?: string;
  text?: string;
  abstract?: string;
  context_type?: string;
  tool_name?: string;
  tool_status?: string;
  tool_output?: string;
};

export type SessionContextMessage = {
  id?: string;
  role: 'user' | 'assistant';
  parts?: SessionContextPart[];
  created_at?: string;
  feedback?: MessageFeedback;
  metadata?: Record<string, unknown>;
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

export type GuidedQuestionSuggestion = {
  id: string;
  display_text: string;
  canonical_question: string;
  token?: string;
  source_uris?: string[];
  confidence?: string;
  selected?: boolean;
};

export type ChatMessage = {
  key: string;
  messageId?: string;
  role: 'user' | 'bot';
  text: string;
  status?: string;
  loading?: boolean;
  streaming?: boolean;
  createdAt?: string;
  elapsedMs?: number;
  steps?: string[];
  iterationCount?: number;
  feedback?: 'up' | 'down';
  suggestions?: GuidedQuestionSuggestion[];
  feedbackDetail?: MessageFeedback;
};

export type MessageFeedback = {
  message_id?: string;
  value?: 'up' | 'down';
  created_at?: string;
  updated_at?: string;
  reason_tags?: string[];
  memory_status?: 'pending' | 'completed' | 'failed' | 'skipped';
  memory_task_id?: string;
  memory_extracted_count?: number;
  memory_error?: string;
  memory_updated_at?: string;
};
