interface FetchOptions extends RequestInit {
  account?: string;
  user?: string;
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
