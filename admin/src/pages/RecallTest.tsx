import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  AlertTriangle,
  ChevronDown,
  FileText,
  RefreshCw,
  Search,
} from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import './RecallTest.css';

interface KnowledgeDocument {
  entry_type: 'document';
  document_id: string;
  display_name: string;
  source_type: string;
  source_ref: string;
  resource_root_uri: string;
  folder_path: string;
  source_format?: string | null;
  created_at: string;
  updated_at: string;
  reason?: string;
  instruction?: string;
  processing_status?: 'processing' | 'ready' | 'failed';
  processing_error?: string;
  processing_started_at?: string | null;
  processing_completed_at?: string | null;
  has_local_copy: boolean;
}

interface RecallResourceHit {
  context_type: string;
  uri: string;
  level: number;
  score: number;
  category?: string;
  match_reason?: string;
  abstract?: string | null;
  overview?: string | null;
}

interface SearchFindResultPayload {
  resources?: RecallResourceHit[];
  total?: number;
}

const DEFAULT_RECALL_LIMIT = 10;
const RECALL_LIMIT_OPTIONS = [5, 10, 20, 50];

const formatTime = (value: string) => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
};

const formatPath = (path: string) => (path ? `/${path}` : '/');

const formatScore = (value?: number | null) => (
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : '--'
);

const normalizeDocumentProcessingStatus = (
  status?: KnowledgeDocument['processing_status'] | string | null,
): 'processing' | 'ready' | 'failed' => {
  if (status === 'processing' || status === 'failed') return status;
  return 'ready';
};

const formatDocumentProcessingStatus = (status?: KnowledgeDocument['processing_status'] | string | null) => {
  switch (normalizeDocumentProcessingStatus(status)) {
    case 'processing':
      return '处理中';
    case 'failed':
      return '处理失败';
    default:
      return '已完成';
  }
};

const matchesResourceRootUri = (uri: string, resourceRootUri: string) => {
  if (!uri || !resourceRootUri) return false;
  return uri === resourceRootUri || uri.startsWith(resourceRootUri.endsWith('/') ? resourceRootUri : `${resourceRootUri}/`);
};

const getRecallHitPreview = (hit: RecallResourceHit) => (
  hit.abstract?.trim() || hit.overview?.trim() || '该结果暂时没有可展示的摘要。'
);

const RecallTest: React.FC = () => {
  const { serverUrl, apiKey, accountId, userId } = useAuth();
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [listQuery, setListQuery] = useState('');
  const [documentAbstracts, setDocumentAbstracts] = useState<Record<string, string>>({});
  const [documentAbstractErrors, setDocumentAbstractErrors] = useState<Record<string, string>>({});
  const [loadingDocumentAbstractId, setLoadingDocumentAbstractId] = useState<string | null>(null);
  const [showAbstract, setShowAbstract] = useState(false);
  const [recallQuery, setRecallQuery] = useState('');
  const [recallLimit, setRecallLimit] = useState(DEFAULT_RECALL_LIMIT);
  const [recallLoading, setRecallLoading] = useState(false);
  const [recallError, setRecallError] = useState('');
  const [recallResults, setRecallResults] = useState<RecallResourceHit[]>([]);
  const [recallResultTotal, setRecallResultTotal] = useState(0);
  const [recallSubmittedQuery, setRecallSubmittedQuery] = useState('');
  const [recallDocumentId, setRecallDocumentId] = useState<string | null>(null);
  const recallRunSeq = useRef(0);

  const loadDocuments = useCallback(async () => {
    if (!serverUrl) return;
    setLoading(true);
    setError('');
    try {
      const response = await fetchApi<{ result: KnowledgeDocument[] }>(
        serverUrl,
        apiKey,
        '/api/v1/knowledge-documents',
        {
          method: 'GET',
          account: accountId || undefined,
          user: userId || undefined,
        },
      );
      setDocuments(Array.isArray(response.result) ? response.result : []);
    } catch (err: any) {
      setError(err?.message || '加载文档列表失败');
    } finally {
      setLoading(false);
    }
  }, [accountId, apiKey, serverUrl, userId]);

  useEffect(() => {
    void loadDocuments();
  }, [loadDocuments]);

  const filteredDocuments = useMemo(() => {
    const query = listQuery.trim().toLowerCase();
    return documents
      .filter((document) => {
        if (!query) return true;
        return [
          document.display_name,
          document.source_ref,
          document.folder_path,
          document.resource_root_uri,
        ]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(query));
      })
      .slice()
      .sort((a, b) => {
        const aTime = new Date(a.updated_at).getTime();
        const bTime = new Date(b.updated_at).getTime();
        return bTime - aTime;
      });
  }, [documents, listQuery]);

  useEffect(() => {
    if (filteredDocuments.length === 0) {
      setSelectedDocumentId(null);
      return;
    }
    if (!selectedDocumentId || !filteredDocuments.some((document) => document.document_id === selectedDocumentId)) {
      const preferred = filteredDocuments.find((document) => (
        normalizeDocumentProcessingStatus(document.processing_status) === 'ready'
      )) || filteredDocuments[0];
      setSelectedDocumentId(preferred.document_id);
    }
  }, [filteredDocuments, selectedDocumentId]);

  const selectedDocument = useMemo(() => (
    selectedDocumentId
      ? documents.find((document) => document.document_id === selectedDocumentId) || null
      : null
  ), [documents, selectedDocumentId]);

  const selectedDocumentProcessingStatus = selectedDocument
    ? normalizeDocumentProcessingStatus(selectedDocument.processing_status)
    : 'ready';

  useEffect(() => {
    recallRunSeq.current += 1;
    setRecallLoading(false);
    setRecallError('');
    setRecallResults([]);
    setRecallResultTotal(0);
    setRecallSubmittedQuery('');
    setRecallDocumentId(null);
    setShowAbstract(false);
  }, [selectedDocumentId]);

  useEffect(() => {
    if (!selectedDocument) return;
    if (selectedDocumentProcessingStatus !== 'ready') return;

    const documentId = selectedDocument.document_id;
    if (documentAbstracts[documentId] || documentAbstractErrors[documentId]) return;

    let cancelled = false;

    const loadDocumentAbstract = async () => {
      setLoadingDocumentAbstractId(documentId);
      try {
        const params = new URLSearchParams({ uri: selectedDocument.resource_root_uri });
        const response = await fetchApi<{ result: string }>(
          serverUrl,
          apiKey,
          `/api/v1/content/abstract?${params.toString()}`,
          {
            method: 'GET',
            account: accountId || undefined,
            user: userId || undefined,
          },
        );
        if (!cancelled) {
          setDocumentAbstracts((prev) => ({
            ...prev,
            [documentId]: typeof response.result === 'string'
              ? response.result
              : JSON.stringify(response.result, null, 2),
          }));
        }
      } catch (err: any) {
        if (!cancelled) {
          setDocumentAbstractErrors((prev) => ({
            ...prev,
            [documentId]: err?.message || '摘要加载失败',
          }));
        }
      } finally {
        if (!cancelled) {
          setLoadingDocumentAbstractId((prev) => (prev === documentId ? null : prev));
        }
      }
    };

    void loadDocumentAbstract();

    return () => {
      cancelled = true;
    };
  }, [
    accountId,
    apiKey,
    documentAbstractErrors,
    documentAbstracts,
    selectedDocument,
    selectedDocumentProcessingStatus,
    serverUrl,
    userId,
  ]);

  const handleRunRecallTest = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedDocument) return;

    const query = recallQuery.trim();
    if (!query) {
      setRecallError('请输入要测试的检索问题。');
      return;
    }
    if (selectedDocumentProcessingStatus !== 'ready') {
      setRecallError('当前文档尚未完成处理，暂时无法执行召回测试。');
      return;
    }

    const requestId = ++recallRunSeq.current;

    setRecallLoading(true);
    setRecallError('');

    try {
      const data = await fetchApi<{ result?: SearchFindResultPayload }>(
        serverUrl,
        apiKey,
        '/api/v1/search/find',
        {
          method: 'POST',
          account: accountId || undefined,
          user: userId || undefined,
          body: JSON.stringify({
            query,
            limit: recallLimit,
            target_uri: 'viking://resources',
          }),
        },
      );
      if (recallRunSeq.current !== requestId) return;

      const result = data.result || {};
      const resources = Array.isArray(result.resources) ? result.resources : [];
      setRecallResults(resources);
      setRecallResultTotal(typeof result.total === 'number' ? result.total : resources.length);
      setRecallSubmittedQuery(query);
      setRecallDocumentId(selectedDocument.document_id);
    } catch (err: any) {
      if (recallRunSeq.current !== requestId) return;
      setRecallResults([]);
      setRecallResultTotal(0);
      setRecallSubmittedQuery(query);
      setRecallDocumentId(selectedDocument.document_id);
      setRecallError(err?.message || '召回测试失败');
    } finally {
      if (recallRunSeq.current === requestId) {
        setRecallLoading(false);
      }
    }
  };

  const hasRecallRunForSelectedDocument = Boolean(
    selectedDocument
    && recallDocumentId === selectedDocument.document_id
    && recallSubmittedQuery,
  );

  const recallMatches = useMemo(() => {
    if (!selectedDocument) return [];
    if (recallDocumentId !== selectedDocument.document_id) return [];
    return recallResults
      .map((hit, index) => ({ hit, index }))
      .filter(({ hit }) => matchesResourceRootUri(hit.uri, selectedDocument.resource_root_uri));
  }, [recallDocumentId, recallResults, selectedDocument]);

  const recallBestMatch = recallMatches[0] || null;

  const selectedDocumentAbstract = selectedDocument
    ? documentAbstracts[selectedDocument.document_id] || ''
    : '';
  const selectedDocumentAbstractError = selectedDocument
    ? documentAbstractErrors[selectedDocument.document_id] || ''
    : '';
  const selectedDocumentAbstractLoading = selectedDocument
    ? loadingDocumentAbstractId === selectedDocument.document_id
    : false;

  const readyCount = useMemo(() => (
    documents.filter((document) => normalizeDocumentProcessingStatus(document.processing_status) === 'ready').length
  ), [documents]);

  const selectedSummary = useMemo(() => {
    if (!selectedDocument) return '';
    const pieces = [
      formatPath(selectedDocument.folder_path || ''),
      selectedDocument.source_format || '待识别',
      selectedDocument.processing_completed_at ? formatTime(selectedDocument.processing_completed_at) : '',
    ].filter(Boolean);
    return pieces.join(' · ');
  }, [selectedDocument]);

  return (
    <div className="rt-root">
      <section className="rt-toolbar">
        <div className="rt-toolbar-copy">
          <div className="rt-toolbar-title">检索召回测试</div>
          <div className="rt-toolbar-subtitle">
            选择文档，输入真实问题，验证它是否能在全资源库检索中进入 Top-K。
          </div>
        </div>
        <div className="rt-toolbar-actions">
          <span className="rt-toolbar-meta">{documents.length} 篇文档 · {readyCount} 篇可测试</span>
          <button className="btn btn-ghost btn-sm" onClick={() => void loadDocuments()} disabled={loading}>
            <RefreshCw size={14} className={loading ? 'rt-spin' : ''} /> 刷新列表
          </button>
        </div>
      </section>

      <div className="rt-layout">
        <aside className="rt-sidebar">
          <div className="rt-sidebar-header">
            <div className="rt-card-title">文档列表</div>
            <div className="rt-sidebar-count">{filteredDocuments.length}</div>
          </div>

          <div className="rt-search-box">
            <Search size={15} />
            <input
              className="rt-search-input"
              placeholder="按名称、目录、来源搜索"
              value={listQuery}
              onChange={(e) => setListQuery(e.target.value)}
            />
          </div>

          {loading ? (
            <div className="rt-empty"><div className="loader" /></div>
          ) : error ? (
            <div className="rt-empty rt-empty-error">
              <AlertTriangle size={18} />
              <span>{error}</span>
            </div>
          ) : filteredDocuments.length === 0 ? (
            <div className="rt-empty">
              <span>没有匹配的文档。</span>
            </div>
          ) : (
            <div className="rt-doc-list">
              {filteredDocuments.map((document) => {
                const status = normalizeDocumentProcessingStatus(document.processing_status);
                const selected = document.document_id === selectedDocumentId;
                return (
                  <button
                    key={document.document_id}
                    className={`rt-doc-item ${selected ? 'active' : ''}`}
                    onClick={() => setSelectedDocumentId(document.document_id)}
                  >
                    <div className="rt-doc-name-row">
                      <FileText size={15} />
                      <span className="rt-doc-name">{document.display_name}</span>
                    </div>
                    <div className="rt-doc-path">{formatPath(document.folder_path || '')}</div>
                    <div className="rt-doc-foot">
                      <span className={`rt-status ${status}`}>
                        {formatDocumentProcessingStatus(document.processing_status)}
                      </span>
                      <span className="rt-doc-time">{formatTime(document.updated_at)}</span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </aside>

        <section className="rt-main">
          {!selectedDocument ? (
            <div className="rt-card rt-empty">
              <FileText size={28} />
              <span>从左侧选择一篇文档开始测试。</span>
            </div>
          ) : (
            <>
              <section className="rt-card rt-target-card">
                <div className="rt-target-head">
                  <div className="rt-target-copy">
                    <div className="rt-card-kicker">当前目标</div>
                    <div className="rt-target-title">{selectedDocument.display_name}</div>
                    <div className="rt-target-summary">{selectedSummary}</div>
                  </div>
                  <span className={`rt-status ${selectedDocumentProcessingStatus}`}>
                    {formatDocumentProcessingStatus(selectedDocument.processing_status)}
                  </span>
                </div>

                <div className="rt-meta-grid">
                  <div className="rt-meta-item">
                    <span className="rt-meta-label">所在目录</span>
                    <span className="rt-meta-value">{formatPath(selectedDocument.folder_path || '')}</span>
                  </div>
                  <div className="rt-meta-item">
                    <span className="rt-meta-label">来源</span>
                    <span className="rt-meta-value">{selectedDocument.source_ref}</span>
                  </div>
                  <div className="rt-meta-item">
                    <span className="rt-meta-label">格式</span>
                    <span className="rt-meta-value">{selectedDocument.source_format || '待识别'}</span>
                  </div>
                  <div className="rt-meta-item">
                    <span className="rt-meta-label">完成时间</span>
                    <span className="rt-meta-value">
                      {selectedDocument.processing_completed_at
                        ? formatTime(selectedDocument.processing_completed_at)
                        : '未完成'}
                    </span>
                  </div>
                </div>
              </section>

              <section className="rt-card rt-form-card">
                <div className="rt-card-header">
                  <div>
                    <div className="rt-card-title">测试问题</div>
                    <div className="rt-card-desc">执行一次全资源库 `find`，判断当前文档是否命中。</div>
                  </div>
                  <label className="rt-topk">
                    <span className="rt-meta-label">Top-K</span>
                    <select
                      className="select rt-topk-select"
                      value={recallLimit}
                      onChange={(e) => setRecallLimit(Number(e.target.value))}
                    >
                      {RECALL_LIMIT_OPTIONS.map((option) => (
                        <option key={option} value={option}>
                          Top-{option}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                <div className="rt-target-uri">
                  <span className="rt-meta-label">目标 URI</span>
                  <code className="rt-code">{selectedDocument.resource_root_uri}</code>
                </div>

                <form className="rt-form" onSubmit={handleRunRecallTest}>
                  <textarea
                    className="textarea rt-textarea"
                    placeholder={`例如：${selectedDocument.display_name} 主要讲了什么？`}
                    value={recallQuery}
                    onChange={(e) => setRecallQuery(e.target.value)}
                    rows={4}
                  />
                  <div className="rt-form-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => setShowAbstract((prev) => !prev)}
                    >
                      <ChevronDown size={14} className={showAbstract ? 'rt-chevron open' : 'rt-chevron'} />
                      {showAbstract ? '收起摘要参考' : '展开摘要参考'}
                    </button>
                    <button
                      type="submit"
                      className="btn btn-primary"
                      disabled={recallLoading || !recallQuery.trim() || selectedDocumentProcessingStatus !== 'ready'}
                    >
                      {recallLoading ? '测试中...' : '开始测试'}
                    </button>
                  </div>
                </form>

                {recallError && <div className="rt-inline-error">{recallError}</div>}

                {showAbstract && (
                  <div className="rt-abstract-panel">
                    <div className="rt-card-subtitle">摘要参考</div>
                    {selectedDocumentProcessingStatus === 'processing' ? (
                      <div className="rt-empty rt-empty-inline">
                        <span>文档仍在处理中，摘要生成后会显示在这里。</span>
                      </div>
                    ) : selectedDocumentProcessingStatus === 'failed' ? (
                      <div className="rt-empty rt-empty-error rt-empty-inline">
                        <AlertTriangle size={18} />
                        <span>{selectedDocument.processing_error || '文档处理失败，无法加载摘要。'}</span>
                      </div>
                    ) : selectedDocumentAbstractLoading ? (
                      <div className="rt-empty rt-empty-inline"><div className="loader" /></div>
                    ) : selectedDocumentAbstractError ? (
                      <div className="rt-empty rt-empty-error rt-empty-inline">
                        <AlertTriangle size={18} />
                        <span>{selectedDocumentAbstractError}</span>
                      </div>
                    ) : (
                      <div className="rt-markdown">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {selectedDocumentAbstract || '暂无摘要'}
                        </ReactMarkdown>
                      </div>
                    )}
                  </div>
                )}
              </section>

              <section className="rt-card rt-results-card">
                <div className="rt-card-header">
                  <div>
                    <div className="rt-card-title">测试结果</div>
                    <div className="rt-card-desc">展示当前问题下的命中情况和返回结果。</div>
                  </div>
                  {hasRecallRunForSelectedDocument && (
                    <div className="rt-results-meta">
                      <span>{recallResults.length} 条返回</span>
                      {recallResultTotal > recallResults.length && <span>共 {recallResultTotal} 条候选</span>}
                    </div>
                  )}
                </div>

                {selectedDocumentProcessingStatus === 'processing' ? (
                  <div className="rt-empty">
                    <span>文档仍在解析和索引中，请稍后再做召回测试。</span>
                  </div>
                ) : selectedDocumentProcessingStatus === 'failed' ? (
                  <div className="rt-empty rt-empty-error">
                    <AlertTriangle size={18} />
                    <span>当前文档处理失败，暂时无法测试召回效果。</span>
                  </div>
                ) : recallLoading ? (
                  <div className="rt-empty"><div className="loader" /></div>
                ) : hasRecallRunForSelectedDocument ? (
                  <div className="rt-results">
                    <div className={`rt-result-summary ${recallBestMatch ? 'hit' : 'miss'}`}>
                      <div className="rt-result-main">
                        <span className="rt-result-badge">
                          {recallBestMatch ? `命中 #${recallBestMatch.index + 1}` : `Top-${recallLimit} 未命中`}
                        </span>
                        <div className="rt-result-copy">
                          <div className="rt-result-query">{recallSubmittedQuery}</div>
                          <div className="rt-result-text">
                            {recallBestMatch
                              ? `当前文档命中了 ${recallMatches.length} 个结果，最佳 score 为 ${formatScore(recallBestMatch.hit.score)}。`
                              : '当前文档没有进入本次返回结果，可以尝试换一种问法，或适当调大 Top-K。'}
                          </div>
                        </div>
                      </div>
                    </div>

                    {recallResults.length > 0 ? (
                      <div className="rt-hit-list">
                        {recallResults.map((hit, index) => {
                          const isTargetHit = matchesResourceRootUri(hit.uri, selectedDocument.resource_root_uri);
                          return (
                            <div key={`${hit.uri}-${index}`} className={`rt-hit-card ${isTargetHit ? 'target' : ''}`}>
                              <div className="rt-hit-top">
                                <div className="rt-hit-rank">#{index + 1}</div>
                                <div className="rt-hit-badges">
                                  {isTargetHit && <span className="rt-pill target">当前文档</span>}
                                  {hit.match_reason && <span className="rt-pill subtle">{hit.match_reason}</span>}
                                  <span className="rt-pill score">score {formatScore(hit.score)}</span>
                                </div>
                              </div>
                              <div className="rt-hit-uri">{hit.uri}</div>
                              <div className="rt-hit-preview">{getRecallHitPreview(hit)}</div>
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="rt-empty rt-empty-inline">
                        <span>这次检索没有返回任何资源结果。</span>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="rt-empty">
                    <span>输入一个问题后点击“开始测试”，这里会显示命中情况和返回结果。</span>
                  </div>
                )}
              </section>
            </>
          )}
        </section>
      </div>
    </div>
  );
};

export default RecallTest;
