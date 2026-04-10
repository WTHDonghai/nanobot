interface FetchOptions extends RequestInit {
  account?: string;
  user?: string;
}

export interface HumanHandoffRequestPayload {
  session_id?: string;
  user_id?: string;
  reason?: string;
  summary?: string;
  latest_user_message?: string;
  latest_assistant_message?: string;
  source?: string;
  metadata?: Record<string, unknown>;
}

export interface HumanHandoffResponse {
  success: boolean;
  status: string;
  message: string;
  handoff_id?: string | null;
  entry_url?: string | null;
  service_response?: Record<string, unknown>;
  timestamp?: string;
}

export const fetchApi = async <T = any>(
  serverUrl: string,
  apiKey: string,
  path: string,
  options: FetchOptions = {}
): Promise<T> => {
  const headers = new Headers(options.headers || {});
  
  if (!headers.has('Content-Type') && options.body) {
    headers.set('Content-Type', 'application/json');
  }
  
  if (apiKey) {
    headers.set('X-API-Key', apiKey);
  }
  
  if (options.account) {
    headers.set('X-OpenViking-Account', options.account);
  }
  
  if (options.user) {
    headers.set('X-OpenViking-User', options.user);
  }

  const { account, user, ...fetchConfig } = options;

  const response = await fetch(`${serverUrl}${path}`, {
    ...fetchConfig,
    credentials: fetchConfig.credentials ?? 'same-origin',
    headers,
  });

  const isJson = response.headers.get('content-type')?.includes('application/json');
  const data = isJson ? await response.json() : await response.text();

  if (!response.ok) {
    let errMsg = (data as any)?.error?.message;
    const detail = (data as any)?.detail;
    if (!errMsg && detail) {
      errMsg = typeof detail === 'string' ? detail : JSON.stringify(detail);
    }
    errMsg = errMsg || `HTTP Error ${response.status}`;
    throw new Error(errMsg);
  }

  return data as T;
};

export const requestHumanHandoff = (
  serverUrl: string,
  apiKey: string,
  payload: HumanHandoffRequestPayload
): Promise<HumanHandoffResponse> => (
  fetchApi<HumanHandoffResponse>(serverUrl, apiKey, '/bot/v1/handoff', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
);
