import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle,
  ChevronRight,
  Copy,
  Eye,
  File,
  FileText,
  FolderOpen,
  FolderPlus,
  Home,
  LayoutGrid,
  Link as LinkIcon,
  List,
  RefreshCw,
  Trash2,
  Upload,
  X,
} from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import MarkdownRenderer from '../components/markdown/MarkdownRenderer';
import './Resources.css';

interface KnowledgeFolder {
  entry_type: 'folder';
  folder_id: string;
  name: string;
  path: string;
  parent_path: string;
  created_at: string;
  updated_at: string;
}

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

interface UploadItem {
  file: File;
  status: 'pending' | 'uploading' | 'done' | 'error';
  error?: string;
}

type KnowledgeEntry = KnowledgeFolder | KnowledgeDocument;
type DrawerMode = 'upload' | 'url' | 'new-folder' | 'rename-folder' | 'move-document' | null;
type ViewMode = 'icon' | 'list';
type ContextMenuMode = 'workspace' | 'folder' | 'document';

interface ContextMenuState {
  x: number;
  y: number;
  mode: ContextMenuMode;
  targetPath: string;
  entry: KnowledgeEntry | null;
}

type ContextMenuAction =
  | {
      kind: 'action';
      key: string;
      label: string;
      icon: React.ReactNode;
      onSelect: () => void;
      danger?: boolean;
    }
  | {
      kind: 'separator';
      key: string;
    };

const ROOT_LABEL = '资源库';
const CONTEXT_MENU_WIDTH = 228;
const CONTEXT_MENU_GUTTER = 12;

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

const entryKey = (entry: KnowledgeEntry) => (
  entry.entry_type === 'folder' ? `folder:${entry.folder_id}` : `document:${entry.document_id}`
);

const isFolderEntry = (entry: KnowledgeEntry | null | undefined): entry is KnowledgeFolder => (
  Boolean(entry && entry.entry_type === 'folder')
);

const isDocumentEntry = (entry: KnowledgeEntry | null | undefined): entry is KnowledgeDocument => (
  Boolean(entry && entry.entry_type === 'document')
);

const buildTenantHeaders = (
  apiKey: string,
  accountId: string | null,
  userId: string | null,
) => {
  const headers = new Headers();
  if (apiKey) headers.set('X-API-Key', apiKey);
  if (accountId) headers.set('X-OpenViking-Account', accountId);
  if (userId) headers.set('X-OpenViking-User', userId);
  return headers;
};

const isDocxDocument = (document: KnowledgeDocument) => (
  (document.source_format || '').toLowerCase().includes('docx')
);

const DOCX_PREVIEW_ORDER_FILENAME = '.preview-order.json';
const DOCX_ASSET_PLACEHOLDER_RE = /!\[([^\]]*)\]\(ov-asset:\/\/([^)]+)\)/g;
const IMAGE_MIME_TYPES: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.bmp': 'image/bmp',
};

const normalizeDocxMarkdown = (markdown: string) => (
  markdown
    .replace(/<ins>(.*?)<\/ins>/g, '$1')
    .trim()
);

type DocxPreviewChunk = {
  uri: string;
  content: string;
};

const collectDocxPreviewMarkdownUris = (items: unknown[]) => (
  items
    .filter((item: unknown): item is string => typeof item === 'string')
    .filter((uri: string) => uri.endsWith('.md'))
    .filter((uri: string) => {
      const filename = uri.split('/').pop() || '';
      return !filename.startsWith('.');
    })
);

const extractDocxPreviewRelativePaths = (value: unknown): string[] => {
  if (!value || typeof value !== 'object') return [];
  const markdownPaths = (value as { markdown_paths?: unknown }).markdown_paths;
  if (!Array.isArray(markdownPaths)) return [];
  return markdownPaths.filter((item: unknown): item is string => typeof item === 'string');
};

const buildDocxPreviewUriFromRelativePath = (resourceRootUri: string, relativePath: string) => (
  `${resourceRootUri.replace(/\/+$/, '')}/${relativePath.replace(/^\/+/, '')}`
);

const extractFilenameStem = (uri: string) => {
  const filename = uri.split('/').pop() || '';
  return filename.replace(/\.md$/i, '');
};

const extractChunkIndex = (uri: string) => {
  const matched = extractFilenameStem(uri).match(/_(\d+)$/);
  return matched ? Number.parseInt(matched[1], 10) : 1;
};

const stripChunkIndexSuffix = (value: string) => value.replace(/_\d+$/, '');

const CHINESE_DIGIT_VALUES: Record<string, number> = {
  '零': 0,
  '一': 1,
  '二': 2,
  '两': 2,
  '三': 3,
  '四': 4,
  '五': 5,
  '六': 6,
  '七': 7,
  '八': 8,
  '九': 9,
};

const CHINESE_UNIT_VALUES: Record<string, number> = {
  '十': 10,
  '百': 100,
  '千': 1000,
  '万': 10000,
};

const parseChineseNumber = (value: string): number | null => {
  const normalized = value.trim();
  if (!normalized) return null;

  let total = 0;
  let section = 0;
  let current = 0;

  for (const char of normalized) {
    if (char in CHINESE_DIGIT_VALUES) {
      current = CHINESE_DIGIT_VALUES[char];
      continue;
    }
    if (!(char in CHINESE_UNIT_VALUES)) {
      return null;
    }
    const unit = CHINESE_UNIT_VALUES[char];
    if (unit === 10000) {
      section = (section + (current || 0)) * unit;
      total += section;
      section = 0;
      current = 0;
      continue;
    }
    section += (current || 1) * unit;
    current = 0;
  }

  return total + section + current;
};

const parseHierarchicalArabicNumbers = (label: string): number[] | null => {
  const matched = label.trim().match(/^(?:第\s*)?(\d+(?:[.．]\d+)*)/);
  if (!matched) return null;
  return matched[1]
    .split(/[.．]/)
    .map((segment) => Number.parseInt(segment, 10))
    .filter((segment) => Number.isFinite(segment));
};

const parseSectionOrderTokens = (label: string): number[] | null => {
  const normalized = label.trim();
  if (!normalized) return null;

  const arabicTokens = parseHierarchicalArabicNumbers(normalized);
  if (arabicTokens && arabicTokens.length > 0) return arabicTokens;

  const patterns = [
    /^第\s*([零一二三四五六七八九十百千万两]+)\s*[章节部篇卷条款节]?/,
    /^[（(【[]?\s*([零一二三四五六七八九十百千万两]+)\s*[)）】\]]?\s*[、.．]?/,
    /^([零一二三四五六七八九十百千万两]+)\s*[、.．)]?/,
  ];

  for (const pattern of patterns) {
    const matched = normalized.match(pattern);
    if (!matched) continue;
    const parsed = parseChineseNumber(matched[1]);
    if (parsed !== null) return [parsed];
  }

  return null;
};

const extractFirstMarkdownHeading = (content: string) => {
  const matched = content.match(/^\s{0,3}#{1,6}\s+(.+)$/m);
  return matched?.[1]?.trim() || '';
};

const compareOrderTokens = (left: number[] | null, right: number[] | null) => {
  if (!left && !right) return 0;
  if (!left) return 1;
  if (!right) return -1;

  const maxLength = Math.max(left.length, right.length);
  for (let index = 0; index < maxLength; index += 1) {
    const leftValue = left[index];
    const rightValue = right[index];
    if (leftValue === undefined) return -1;
    if (rightValue === undefined) return 1;
    if (leftValue !== rightValue) return leftValue - rightValue;
  }
  return 0;
};

const sortDocxPreviewChunks = (chunks: DocxPreviewChunk[]) => (
  chunks
    .map((chunk, index) => {
      const headingLabel = extractFirstMarkdownHeading(chunk.content);
      const filenameLabel = stripChunkIndexSuffix(extractFilenameStem(chunk.uri));
      return {
        ...chunk,
        originalIndex: index,
        chunkIndex: extractChunkIndex(chunk.uri),
        orderTokens: parseSectionOrderTokens(headingLabel) || parseSectionOrderTokens(filenameLabel),
      };
    })
    .sort((left, right) => {
      const orderComparison = compareOrderTokens(left.orderTokens, right.orderTokens);
      if (orderComparison !== 0) return orderComparison;
      if (left.chunkIndex !== right.chunkIndex) return left.chunkIndex - right.chunkIndex;
      return left.originalIndex - right.originalIndex;
    })
    .map(({ uri, content }) => ({ uri, content }))
);

const applyDocxPreviewManifestOrder = (
  listedUris: string[],
  manifestRelativePaths: string[],
  resourceRootUri: string,
) => {
  const listedSet = new Set(listedUris);
  const orderedUris = manifestRelativePaths
    .map((relativePath) => buildDocxPreviewUriFromRelativePath(resourceRootUri, relativePath))
    .filter((uri) => listedSet.has(uri));

  const orderedSet = new Set(orderedUris);
  return [
    ...orderedUris,
    ...listedUris.filter((uri) => !orderedSet.has(uri)),
  ];
};

async function readDocxPreviewManifest(
  serverUrl: string,
  apiKey: string,
  accountId: string | null,
  userId: string | null,
  resourceRootUri: string,
): Promise<string[] | null> {
  const headers = buildTenantHeaders(apiKey, accountId, userId);
  const params = new URLSearchParams({
    uri: `${resourceRootUri}/${DOCX_PREVIEW_ORDER_FILENAME}`,
  });
  const response = await fetch(`${serverUrl}/api/v1/content/read?${params}`, { headers });
  if (!response.ok) {
    return null;
  }

  const data = await response.json();
  const rawValue = data?.result;
  const parsedValue = typeof rawValue === 'string'
    ? (() => {
      try {
        return JSON.parse(rawValue);
      } catch {
        return null;
      }
    })()
    : rawValue;

  const relativePaths = extractDocxPreviewRelativePaths(parsedValue);
  return relativePaths.length > 0 ? relativePaths : null;
}

const inferImageMimeType = (filename: string) => {
  const normalized = filename.toLowerCase();
  const matchedEntry = Object.entries(IMAGE_MIME_TYPES).find(([extension]) => normalized.endsWith(extension));
  return matchedEntry?.[1] || 'image/png';
};

async function materializeDocxMarkdownImages(
  serverUrl: string,
  apiKey: string,
  accountId: string | null,
  userId: string | null,
  resourceRootUri: string,
  markdown: string,
): Promise<{ markdown: string; objectUrls: string[] }> {
  const headers = buildTenantHeaders(apiKey, accountId, userId);
  const assetMatches = Array.from(markdown.matchAll(DOCX_ASSET_PLACEHOLDER_RE));
  const assetNames = Array.from(new Set(assetMatches.map((match) => match[2])));

  if (assetNames.length === 0) {
    return { markdown, objectUrls: [] };
  }

  const resolvedAssetUrls = new Map<string, string | null>();
  const objectUrls: string[] = [];

  await Promise.all(assetNames.map(async (assetName) => {
    try {
      const params = new URLSearchParams({ uri: `${resourceRootUri}/_images/${assetName}` });
      const response = await fetch(`${serverUrl}/api/v1/content/download?${params}`, { headers });
      if (!response.ok) {
        resolvedAssetUrls.set(assetName, null);
        return;
      }

      const rawBlob = await response.blob();
      const blob = rawBlob.type.startsWith('image/')
        ? rawBlob
        : new Blob([await rawBlob.arrayBuffer()], { type: inferImageMimeType(assetName) });
      const objectUrl = URL.createObjectURL(blob);
      objectUrls.push(objectUrl);
      resolvedAssetUrls.set(assetName, objectUrl);
    } catch {
      resolvedAssetUrls.set(assetName, null);
    }
  }));

  const materializedMarkdown = markdown.replace(
    DOCX_ASSET_PLACEHOLDER_RE,
    (_match, altText: string, assetName: string) => {
      const objectUrl = resolvedAssetUrls.get(assetName);
      if (objectUrl) {
        return `![${altText}](${objectUrl})`;
      }
      return altText?.trim() ? `\n\n> 图片：${altText.trim()}\n\n` : '\n\n> 图片\n\n';
    },
  );

  return { markdown: materializedMarkdown, objectUrls };
}

async function uploadTempFile(
  serverUrl: string,
  apiKey: string,
  accountId: string | null,
  userId: string | null,
  file: File,
): Promise<string> {
  const body = new FormData();
  body.append('file', file, file.name);
  const res = await fetch(`${serverUrl}/api/v1/resources/temp_upload`, {
    method: 'POST',
    headers: buildTenantHeaders(apiKey, accountId, userId),
    body,
  });
  const data = await res.json();
  if (!res.ok) {
    const detail = data?.error?.message || data?.detail;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || '上传失败');
  }
  return data.result.temp_file_id;
}

const Toast = ({ msg, type, onClose }: { msg: string; type: 'success' | 'error'; onClose: () => void }) => {
  useEffect(() => {
    const timer = setTimeout(onClose, 4000);
    return () => clearTimeout(timer);
  }, [onClose]);

  return (
    <div className={`fm-toast fm-toast-${type}`}>
      {type === 'success' ? <CheckCircle size={18} /> : <AlertTriangle size={18} />}
      <span>{msg}</span>
      <button className="btn btn-ghost btn-sm" onClick={onClose} style={{ padding: '2px' }}>
        <X size={14} />
      </button>
    </div>
  );
};

const ConfirmModal = ({
  message,
  onConfirm,
  onCancel,
}: {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}) => (
  <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
    <div className="modal" onClick={(e) => e.stopPropagation()}>
      <div className="modal-title fm-danger-title">
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

const PreviewModal = ({
  document,
  serverUrl,
  apiKey,
  accountId,
  userId,
  onClose,
}: {
  document: KnowledgeDocument;
  serverUrl: string;
  apiKey: string;
  accountId: string | null;
  userId: string | null;
  onClose: () => void;
}) => {
  const [content, setContent] = useState<string>('');
  const [contentMode, setContentMode] = useState<'text' | 'markdown'>('text');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [hint, setHint] = useState('');

  useEffect(() => {
    let cancelled = false;
    let previewObjectUrls: string[] = [];
    const run = async () => {
      setLoading(true);
      setError('');
      setHint('');
      try {
        const headers = buildTenantHeaders(apiKey, accountId, userId);

        if (isDocxDocument(document)) {
          const listParams = new URLSearchParams({
            uri: document.resource_root_uri,
            simple: 'true',
            recursive: 'true',
            output: 'original',
            limit: '500',
          });
          const listRes = await fetch(`${serverUrl}/api/v1/fs/ls?${listParams}`, {
            headers,
          });
          const listData = await listRes.json();
          if (!listRes.ok) {
            const detail = listData?.error?.message || listData?.detail;
            throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || '预览失败');
          }

          const listedMarkdownUris = collectDocxPreviewMarkdownUris(
            Array.isArray(listData.result) ? (listData.result as unknown[]) : [],
          );
          const manifestRelativePaths = await readDocxPreviewManifest(
            serverUrl,
            apiKey,
            accountId,
            userId,
            document.resource_root_uri,
          );
          const markdownUris = manifestRelativePaths
            ? applyDocxPreviewManifestOrder(
              listedMarkdownUris,
              manifestRelativePaths,
              document.resource_root_uri,
            )
            : listedMarkdownUris;

          if (markdownUris.length > 0) {
            const markdownChunks = await Promise.all(markdownUris.map(async (uri: string) => {
              const readParams = new URLSearchParams({ uri });
              const readRes = await fetch(`${serverUrl}/api/v1/content/read?${readParams}`, {
                headers,
              });
              const readData = await readRes.json();
              if (!readRes.ok) {
                const detail = readData?.error?.message || readData?.detail;
                throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || '预览失败');
              }
              return {
                uri,
                content: typeof readData.result === 'string'
                  ? readData.result
                  : JSON.stringify(readData.result, null, 2),
              };
            }));
            const orderedChunks = manifestRelativePaths
              ? markdownChunks
              : sortDocxPreviewChunks(markdownChunks);

            if (!cancelled) {
              const materialized = await materializeDocxMarkdownImages(
                serverUrl,
                apiKey,
                accountId,
                userId,
                document.resource_root_uri,
                normalizeDocxMarkdown(
                  orderedChunks
                    .map((chunk) => chunk.content)
                    .filter(Boolean)
                    .join('\n\n'),
                ),
              );
              previewObjectUrls = materialized.objectUrls;
              setContentMode('markdown');
              setContent(materialized.markdown);
              setHint(
                manifestRelativePaths
                  ? `DOCX 正文预览，共加载 ${markdownUris.length} 个片段`
                  : `DOCX 正文预览，共加载 ${markdownUris.length} 个片段`,
              );
            }
            return;
          }
        }

        const params = new URLSearchParams({ uri: document.resource_root_uri, limit: '500' });
        const res = await fetch(`${serverUrl}/api/v1/content/abstract?${params}`, {
          headers,
        });
        const data = await res.json();
        if (!res.ok) {
          const detail = data?.error?.message || data?.detail;
          throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || '预览失败');
        }
        if (!cancelled) {
          const value = data.result;
          setContentMode('text');
          setContent(typeof value === 'string' ? value : JSON.stringify(value, null, 2));
          setHint(isDocxDocument(document) ? '未找到正文片段，已回退为摘要预览' : '摘要预览');
        }
      } catch (err: any) {
        if (!cancelled) setError(err?.message || '预览失败');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    run();
    return () => {
      cancelled = true;
      previewObjectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [accountId, apiKey, document, serverUrl, userId]);

  return (
    <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={{ maxWidth: 840, width: '90%' }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Eye size={18} /> 预览: {document.display_name}
        </div>
        <div className="modal-body">
          {hint && <div className="fm-preview-hint">{hint}</div>}
          <div className="fm-preview-box">
            {loading ? (
              <div className="fm-state"><div className="loader" /></div>
            ) : error ? (
              <div className="fm-state fm-state-error">{error}</div>
            ) : contentMode === 'markdown' ? (
              <MarkdownRenderer
                className="fm-preview-markdown"
                imageClassName="fm-preview-image"
                content={content || '(空)'}
              />
            ) : (
              <pre>{content || '(空)'}</pre>
            )}
          </div>
        </div>
        <div className="modal-footer">
          <button className="btn btn-ghost" onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  );
};

const UrlDrawer = ({
  open,
  onClose,
  currentPath,
  onSubmitted,
}: {
  open: boolean;
  onClose: () => void;
  currentPath: string;
  onSubmitted: (url: string) => Promise<void>;
}) => {
  const [url, setUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setLoading(true);
    setError('');
    try {
      await onSubmitted(url.trim());
      setUrl('');
      onClose();
    } catch (err: any) {
      setError(err?.message || '添加失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fm-drawer-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="fm-drawer open" onClick={(e) => e.stopPropagation()}>
        <div className="fm-drawer-header">
          <div className="fm-drawer-title"><LinkIcon size={18} /> 添加远程文档</div>
          <button className="btn btn-ghost btn-sm" style={{ padding: '4px' }} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="fm-drawer-body">
          <div className="fm-drawer-desc">
            文档会被导入到 <strong>{formatPath(currentPath)}</strong>。
          </div>
          <form id="fm-url-form" onSubmit={handleSubmit}>
            <div className="form-group">
              <label>远程地址</label>
              <input
                type="text"
                className="input"
                placeholder="https://example.com/guide.pdf 或 git@github.com:org/repo.git"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                autoFocus
                required
              />
            </div>
            {error && <div className="fm-inline-error">{error}</div>}
          </form>
        </div>
        <div className="fm-drawer-footer">
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button type="submit" form="fm-url-form" className="btn btn-primary" disabled={loading || !url.trim()}>
            {loading ? '提交中...' : '添加文档'}
          </button>
        </div>
      </div>
    </div>
  );
};

const NewFolderDrawer = ({
  open,
  onClose,
  currentPath,
  onSubmitted,
}: {
  open: boolean;
  onClose: () => void;
  currentPath: string;
  onSubmitted: (name: string) => Promise<void>;
}) => {
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    setError('');
    try {
      await onSubmitted(name.trim());
      setName('');
      onClose();
    } catch (err: any) {
      setError(err?.message || '创建目录失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fm-drawer-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="fm-drawer open" onClick={(e) => e.stopPropagation()}>
        <div className="fm-drawer-header">
          <div className="fm-drawer-title"><FolderPlus size={18} /> 新建目录</div>
          <button className="btn btn-ghost btn-sm" style={{ padding: '4px' }} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="fm-drawer-body">
          <div className="fm-drawer-desc">
            新目录将创建在 <strong>{formatPath(currentPath)}</strong> 下。
          </div>
          <form id="fm-folder-form" onSubmit={handleSubmit}>
            <div className="form-group">
              <label>目录名称</label>
              <input
                type="text"
                className="input"
                placeholder="例如：产品手册"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                required
              />
            </div>
            {error && <div className="fm-inline-error">{error}</div>}
          </form>
        </div>
        <div className="fm-drawer-footer">
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button type="submit" form="fm-folder-form" className="btn btn-primary" disabled={loading || !name.trim()}>
            {loading ? '创建中...' : '创建目录'}
          </button>
        </div>
      </div>
    </div>
  );
};

const RenameFolderDrawer = ({
  open,
  folder,
  onClose,
  onSubmitted,
}: {
  open: boolean;
  folder: KnowledgeFolder | null;
  onClose: () => void;
  onSubmitted: (name: string) => Promise<void>;
}) => {
  const [name, setName] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open || !folder) return;
    setName(folder.name);
    setError('');
  }, [folder, open]);

  if (!open || !folder) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    setError('');
    try {
      await onSubmitted(name.trim());
      onClose();
    } catch (err: any) {
      setError(err?.message || '重命名目录失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fm-drawer-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="fm-drawer open" onClick={(e) => e.stopPropagation()}>
        <div className="fm-drawer-header">
          <div className="fm-drawer-title"><FolderOpen size={18} /> 重命名目录</div>
          <button className="btn btn-ghost btn-sm" style={{ padding: '4px' }} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="fm-drawer-body">
          <form id="fm-rename-folder-form" onSubmit={handleSubmit}>
            <div className="form-group">
              <label>新目录名称</label>
              <input
                type="text"
                className="input"
                placeholder="输入新的目录名称"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                required
              />
            </div>
            {error && <div className="fm-inline-error">{error}</div>}
          </form>
        </div>
        <div className="fm-drawer-footer">
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button type="submit" form="fm-rename-folder-form" className="btn btn-primary" disabled={loading || !name.trim()}>
            {loading ? '保存中...' : '保存名称'}
          </button>
        </div>
      </div>
    </div>
  );
};

const MoveDocumentDrawer = ({
  open,
  document,
  folders,
  onClose,
  onSubmitted,
}: {
  open: boolean;
  document: KnowledgeDocument | null;
  folders: KnowledgeFolder[];
  onClose: () => void;
  onSubmitted: (folderPath: string) => Promise<void>;
}) => {
  const [targetPath, setTargetPath] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open || !document) return;
    setTargetPath(document.folder_path || '');
    setError('');
  }, [document, open]);

  if (!open || !document) return null;

  const sortedFolders = folders
    .slice()
    .sort((a, b) => a.path.localeCompare(b.path, 'zh-CN'));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      await onSubmitted(targetPath);
      onClose();
    } catch (err: any) {
      setError(err?.message || '移动文档失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fm-drawer-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="fm-drawer open" onClick={(e) => e.stopPropagation()}>
        <div className="fm-drawer-header">
          <div className="fm-drawer-title"><FileText size={18} /> 移动文档</div>
          <button className="btn btn-ghost btn-sm" style={{ padding: '4px' }} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="fm-drawer-body">
          <form id="fm-move-document-form" onSubmit={handleSubmit}>
            <div className="form-group">
              <label>目标目录</label>
              <select
                className="input"
                value={targetPath}
                onChange={(e) => setTargetPath(e.target.value)}
              >
                <option value="">/</option>
                {sortedFolders.map((folder) => (
                  <option key={folder.folder_id} value={folder.path}>
                    {formatPath(folder.path)}
                  </option>
                ))}
              </select>
            </div>
            {error && <div className="fm-inline-error">{error}</div>}
          </form>
        </div>
        <div className="fm-drawer-footer">
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button
            type="submit"
            form="fm-move-document-form"
            className="btn btn-primary"
            disabled={loading || targetPath === (document.folder_path || '')}
          >
            {loading ? '移动中...' : '移动文档'}
          </button>
        </div>
      </div>
    </div>
  );
};

const UploadDrawer = ({
  open,
  onClose,
  currentPath,
  onUploaded,
}: {
  open: boolean;
  onClose: () => void;
  currentPath: string;
  onUploaded: (files: File[]) => Promise<void>;
}) => {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  if (!open) return null;

  const addFiles = (files: FileList | null) => {
    if (!files) return;
    setItems((prev) => [...prev, ...Array.from(files).map((file) => ({ file, status: 'pending' as const }))]);
  };

  const removeItem = (idx: number) => {
    setItems((prev) => prev.filter((_, index) => index !== idx));
  };

  const handleUpload = async () => {
    const files = items.filter((item) => item.status === 'pending').map((item) => item.file);
    if (!files.length) return;
    setItems((prev) => prev.map((item) => (
      item.status === 'pending' ? { ...item, status: 'uploading' } : item
    )));
    setUploading(true);
    try {
      await onUploaded(files);
      setItems((prev) => prev.map((item) => (
        item.status === 'uploading' ? { ...item, status: 'done' } : item
      )));
      setItems([]);
      onClose();
    } catch (err: any) {
      const message = err?.message || '上传失败';
      setItems((prev) => prev.map((item) => (
        item.status === 'pending' || item.status === 'uploading'
          ? { ...item, status: 'error', error: message }
          : item
      )));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="fm-drawer-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="fm-drawer open" onClick={(e) => e.stopPropagation()}>
        <div className="fm-drawer-header">
          <div className="fm-drawer-title"><Upload size={18} /> 上传原始文档</div>
          <button className="btn btn-ghost btn-sm" style={{ padding: '4px' }} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="fm-drawer-body">
          <div className="fm-drawer-desc">
            选中的文件会上传到 <strong>{formatPath(currentPath)}</strong>。
          </div>
          <div
            className={`fm-dropzone ${dragOver ? 'drag-over' : ''}`}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              addFiles(e.dataTransfer.files);
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload size={28} />
            <div>点击选择文件，或把文档拖到这里</div>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              addFiles(e.target.files);
              e.currentTarget.value = '';
            }}
          />

          {items.length > 0 && (
            <div className="fm-upload-list">
              {items.map((item, index) => (
                <div key={`${item.file.name}-${index}`} className={`fm-upload-item ${item.status}`}>
                  <div>
                    <div className="fm-upload-name">{item.file.name}</div>
                    <div className="fm-upload-meta">
                      {(item.file.size / 1024).toFixed(1)} KB
                      {item.error ? ` · ${item.error}` : ''}
                    </div>
                  </div>
                  <div className="fm-upload-actions">
                    <span className="fm-upload-status">
                      {item.status === 'done' ? '已完成' : item.status === 'error' ? '失败' : item.status === 'uploading' ? '上传中' : '待上传'}
                    </span>
                    {!uploading && (
                      <button className="btn btn-ghost btn-sm" onClick={() => removeItem(index)}>
                        <X size={14} />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="fm-drawer-footer">
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={uploading || items.length === 0} onClick={handleUpload}>
            {uploading ? '处理中...' : `上传 ${items.length} 个文件`}
          </button>
        </div>
      </div>
    </div>
  );
};

const Resources = () => {
  const { serverUrl, apiKey, accountId, userId } = useAuth();
  const [folders, setFolders] = useState<KnowledgeFolder[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [drawer, setDrawer] = useState<DrawerMode>(null);
  const [confirmDelete, setConfirmDelete] = useState<KnowledgeEntry | null>(null);
  const [previewDoc, setPreviewDoc] = useState<KnowledgeDocument | null>(null);
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [documentAbstracts, setDocumentAbstracts] = useState<Record<string, string>>({});
  const [documentAbstractErrors, setDocumentAbstractErrors] = useState<Record<string, string>>({});
  const [loadingDocumentAbstractId, setLoadingDocumentAbstractId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('icon');
  const [currentPath, setCurrentPath] = useState('');
  const [drawerPath, setDrawerPath] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [inspectorWidth, setInspectorWidth] = useState(320);
  const isResizingInspector = useRef(false);
  const contextMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isResizingInspector.current) return;
      e.preventDefault();
      const newWidth = document.body.clientWidth - e.clientX;
      if (newWidth > 200 && newWidth < 800) {
        setInspectorWidth(newWidth);
      }
    };
    const handleMouseUp = () => {
      if (isResizingInspector.current) {
        isResizingInspector.current = false;
        document.body.style.cursor = '';
      }
    };
    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, []);

  const showToast = useCallback((msg: string, type: 'success' | 'error' = 'success') => {
    setToast({ msg, type });
  }, []);

  const loadLibrary = useCallback(async () => {
    if (!serverUrl) return;
    setLoading(true);
    setError('');
    try {
      const [documentsData, foldersData] = await Promise.all([
        fetchApi<{ result: KnowledgeDocument[] }>(
          serverUrl,
          apiKey,
          '/api/v1/knowledge-documents',
          {
            method: 'GET',
            account: accountId || undefined,
            user: userId || undefined,
          },
        ),
        fetchApi<{ result: KnowledgeFolder[] }>(
          serverUrl,
          apiKey,
          '/api/v1/knowledge-folders',
          {
            method: 'GET',
            account: accountId || undefined,
            user: userId || undefined,
          },
        ),
      ]);
      const nextDocuments = Array.isArray(documentsData.result) ? documentsData.result : [];
      const nextFolders = Array.isArray(foldersData.result) ? foldersData.result : [];
      const validPaths = new Set(nextFolders.map((folder) => folder.path));

      setDocuments(nextDocuments);
      setFolders(nextFolders);
      setCurrentPath((prev) => (prev && validPaths.has(prev) ? prev : ''));
      setSelectedKey((prev) => {
        if (!prev) return null;
        const allKeys = new Set([
          ...nextFolders.map((entry) => entryKey(entry)),
          ...nextDocuments.map((entry) => entryKey(entry)),
        ]);
        return allKeys.has(prev) ? prev : null;
      });
    } catch (err: any) {
      setError(err?.message || '加载资源库失败');
    } finally {
      setLoading(false);
    }
  }, [accountId, apiKey, serverUrl, userId]);

  useEffect(() => {
    loadLibrary();
  }, [loadLibrary]);

  useEffect(() => {
    if (!contextMenu) return;

    const closeMenu = () => {
      setContextMenu(null);
    };

    const handlePointerDown = (event: MouseEvent) => {
      if (event && contextMenuRef.current?.contains(event.target as Node)) {
        return;
      }
      closeMenu();
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        closeMenu();
      }
    };

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    window.addEventListener('resize', closeMenu);
    window.addEventListener('scroll', closeMenu, true);

    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('resize', closeMenu);
      window.removeEventListener('scroll', closeMenu, true);
    };
  }, [contextMenu]);

  useEffect(() => {
    if (drawer || previewDoc || confirmDelete) {
      setContextMenu(null);
    }
  }, [confirmDelete, drawer, previewDoc]);

  const folderMap = useMemo(() => (
    new Map(folders.map((folder) => [entryKey(folder), folder]))
  ), [folders]);

  const documentMap = useMemo(() => (
    new Map(documents.map((document) => [entryKey(document), document]))
  ), [documents]);

  const selectedEntry = useMemo<KnowledgeEntry | null>(() => {
    if (!selectedKey) return null;
    return folderMap.get(selectedKey) || documentMap.get(selectedKey) || null;
  }, [documentMap, folderMap, selectedKey]);

  const selectedDocumentAbstract = isDocumentEntry(selectedEntry)
    ? documentAbstracts[selectedEntry.document_id] || ''
    : '';
  const selectedDocumentAbstractError = isDocumentEntry(selectedEntry)
    ? documentAbstractErrors[selectedEntry.document_id] || ''
    : '';
  const selectedDocumentAbstractLoading = isDocumentEntry(selectedEntry)
    ? loadingDocumentAbstractId === selectedEntry.document_id
    : false;
  const selectedDocumentProcessingStatus = isDocumentEntry(selectedEntry)
    ? normalizeDocumentProcessingStatus(selectedEntry.processing_status)
    : 'ready';

  const hasProcessingDocuments = useMemo(() => (
    documents.some((document) => normalizeDocumentProcessingStatus(document.processing_status) === 'processing')
  ), [documents]);

  useEffect(() => {
    if (!hasProcessingDocuments) return undefined;
    const timer = window.setInterval(() => {
      void loadLibrary();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [hasProcessingDocuments, loadLibrary]);

  useEffect(() => {
    if (!isDocumentEntry(selectedEntry)) return;
    if (selectedDocumentProcessingStatus !== 'ready') return;

    const documentId = selectedEntry.document_id;
    if (documentAbstracts[documentId] || documentAbstractErrors[documentId]) return;

    let cancelled = false;

    const loadDocumentAbstract = async () => {
      setLoadingDocumentAbstractId(documentId);
      try {
        const params = new URLSearchParams({ uri: selectedEntry.resource_root_uri });
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
    selectedDocumentProcessingStatus,
    selectedEntry,
    serverUrl,
    userId,
  ]);

  const activeDrawerPath = drawerPath ?? currentPath;

  const openPath = useCallback((path: string) => {
    setCurrentPath(path);
    setSelectedKey(null);
    setContextMenu(null);
  }, []);

  const closeDrawer = useCallback(() => {
    setDrawer(null);
    setDrawerPath(null);
  }, []);

  const openPathDrawer = useCallback((nextDrawer: 'upload' | 'url' | 'new-folder', path: string) => {
    setDrawerPath(path);
    setDrawer(nextDrawer);
    setContextMenu(null);
  }, []);

  const revealDocument = useCallback((document: KnowledgeDocument) => {
    setCurrentPath(document.folder_path || '');
    setSelectedKey(entryKey(document));
    setContextMenu(null);
  }, []);

  const remapPathAfterFolderRename = useCallback((path: string, oldPath: string, newPath: string) => {
    if (path === oldPath) return newPath;
    if (path.startsWith(`${oldPath}/`)) return `${newPath}${path.slice(oldPath.length)}`;
    return path;
  }, []);

  const breadcrumbSegments = useMemo(() => {
    const parts = currentPath ? currentPath.split('/') : [];
    return [
      { label: ROOT_LABEL, path: '' },
      ...parts.map((segment, index) => ({
        label: segment,
        path: parts.slice(0, index + 1).join('/'),
      })),
    ];
  }, [currentPath]);

  const folderTree = useMemo(() => (
    folders
      .slice()
      .sort((a, b) => a.path.localeCompare(b.path, 'zh-CN'))
      .map((folder) => ({
        ...folder,
        depth: folder.path.split('/').length - 1,
      }))
  ), [folders]);

  const visibleFolders = useMemo(() => (
    folders
      .filter((folder) => folder.parent_path === currentPath)
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  ), [currentPath, folders]);

  const visibleDocuments = useMemo(() => (
    documents
      .filter((document) => (document.folder_path || '') === currentPath)
      .slice()
      .sort((a, b) => a.display_name.localeCompare(b.display_name, 'zh-CN'))
  ), [currentPath, documents]);

  const visibleEntries = useMemo<KnowledgeEntry[]>(() => (
    [...visibleFolders, ...visibleDocuments]
  ), [visibleDocuments, visibleFolders]);

  const folderStatsByPath = useMemo(() => {
    const stats = new Map<string, { childFolderCount: number; childDocumentCount: number }>();

    for (const folder of folders) {
      stats.set(folder.path, { childFolderCount: 0, childDocumentCount: 0 });
    }

    for (const folder of folders) {
      const parentStats = stats.get(folder.parent_path);
      if (parentStats) {
        parentStats.childFolderCount += 1;
      }
    }

    for (const document of documents) {
      const parentStats = stats.get(document.folder_path || '');
      if (parentStats) {
        parentStats.childDocumentCount += 1;
      }
    }

    return stats;
  }, [documents, folders]);

  const currentFolderStats = useMemo(() => {
    if (!isFolderEntry(selectedEntry)) return null;
    return folderStatsByPath.get(selectedEntry.path) || { childFolderCount: 0, childDocumentCount: 0 };
  }, [folderStatsByPath, selectedEntry]);

  const isFolderEmpty = useCallback((folder: KnowledgeFolder) => {
    const stats = folderStatsByPath.get(folder.path);
    return !stats || (stats.childFolderCount === 0 && stats.childDocumentCount === 0);
  }, [folderStatsByPath]);

  const canDeleteSelectedFolder = useMemo(() => (
    isFolderEntry(selectedEntry) ? isFolderEmpty(selectedEntry) : false
  ), [isFolderEmpty, selectedEntry]);

  const handleCopy = async (value: string, successMsg: string) => {
    try {
      await navigator.clipboard.writeText(value);
      showToast(successMsg);
    } catch {
      showToast('复制失败，请检查浏览器权限', 'error');
    }
  };

  const handleCreateFolder = async (name: string, targetPath = currentPath) => {
    const data = await fetchApi<{ result: KnowledgeFolder }>(
      serverUrl,
      apiKey,
      '/api/v1/knowledge-folders',
      {
        method: 'POST',
        account: accountId || undefined,
        user: userId || undefined,
        body: JSON.stringify({ name, parent_path: targetPath }),
      },
    );
    const folder = data.result;
    await loadLibrary();
    openPath(folder.path);
    setSelectedKey(`folder:${folder.folder_id}`);
    showToast(`已创建目录：${folder.name}`);
  };

  const handleAddUrl = async (url: string, targetPath = currentPath) => {
    await fetchApi(serverUrl, apiKey, '/api/v1/resources', {
      method: 'POST',
      account: accountId || undefined,
      user: userId || undefined,
      body: JSON.stringify({ path: url, wait: false, folder_path: targetPath }),
    });
    await loadLibrary();
    showToast(`文档已导入到 ${formatPath(targetPath)}`);
  };

  const handleUploadFiles = async (files: File[], targetPath = currentPath) => {
    let done = 0;
    let failures = 0;
    let firstError = '';

    for (const file of files) {
      try {
        const tempId = await uploadTempFile(serverUrl, apiKey, accountId, userId, file);
        await fetchApi(serverUrl, apiKey, '/api/v1/resources', {
          method: 'POST',
          account: accountId || undefined,
          user: userId || undefined,
          body: JSON.stringify({ temp_file_id: tempId, wait: false, folder_path: targetPath }),
        });
        done += 1;
      } catch (err: any) {
        failures += 1;
        if (!firstError) firstError = err?.message || '上传失败';
      }
    }

    await loadLibrary();
    if (failures === 0) {
      showToast(`已导入 ${done} 个文档到 ${formatPath(targetPath)}`);
      return;
    }

    showToast(`成功 ${done} 个，失败 ${failures} 个：${firstError}`, 'error');
    throw new Error(firstError || '部分文件上传失败');
  };

  const handleRenameFolder = async (name: string) => {
    if (!isFolderEntry(selectedEntry)) return;
    const originalPath = selectedEntry.path;
    const data = await fetchApi<{ result: KnowledgeFolder }>(
      serverUrl,
      apiKey,
      `/api/v1/knowledge-folders/${selectedEntry.folder_id}/rename`,
      {
        method: 'POST',
        account: accountId || undefined,
        user: userId || undefined,
        body: JSON.stringify({ name }),
      },
    );
    const renamedFolder = data.result;
    setCurrentPath((prev) => remapPathAfterFolderRename(prev, originalPath, renamedFolder.path));
    setSelectedKey(`folder:${selectedEntry.folder_id}`);
    await loadLibrary();
    showToast(`目录已重命名为：${renamedFolder.name}`);
  };

  const handleMoveDocument = async (folderPath: string) => {
    if (!isDocumentEntry(selectedEntry)) return;
    const data = await fetchApi<{ result: KnowledgeDocument }>(
      serverUrl,
      apiKey,
      `/api/v1/knowledge-documents/${selectedEntry.document_id}/move`,
      {
        method: 'POST',
        account: accountId || undefined,
        user: userId || undefined,
        body: JSON.stringify({ folder_path: folderPath }),
      },
    );
    await loadLibrary();
    setSelectedKey(`document:${selectedEntry.document_id}`);
    showToast(`文档已移动到 ${formatPath(data.result.folder_path || '')}`);
  };

  const doDelete = async () => {
    if (!confirmDelete) return;
    if (isFolderEntry(confirmDelete) && !isFolderEmpty(confirmDelete)) {
      showToast('仅空目录允许删除，请先清空子目录和文档', 'error');
      setConfirmDelete(null);
      return;
    }
    const key = entryKey(confirmDelete);
    setDeletingKey(key);
    try {
      if (isFolderEntry(confirmDelete)) {
        await fetchApi(serverUrl, apiKey, `/api/v1/knowledge-folders/${confirmDelete.folder_id}`, {
          method: 'DELETE',
          account: accountId || undefined,
          user: userId || undefined,
        });
        if (currentPath === confirmDelete.path) {
          openPath(confirmDelete.parent_path);
        }
        showToast(`已删除目录：${confirmDelete.name}`);
      } else {
        await fetchApi(serverUrl, apiKey, `/api/v1/knowledge-documents/${confirmDelete.document_id}`, {
          method: 'DELETE',
          account: accountId || undefined,
          user: userId || undefined,
        });
        showToast(`已删除文档：${confirmDelete.display_name}`);
      }
      setConfirmDelete(null);
      setSelectedKey(null);
      await loadLibrary();
    } catch (err: any) {
      showToast(err?.message || '删除失败', 'error');
    } finally {
      setDeletingKey(null);
    }
  };

  const requestDeleteEntry = useCallback((entry: KnowledgeEntry) => {
    if (isFolderEntry(entry) && !isFolderEmpty(entry)) {
      showToast('仅空目录允许删除，请先清空子目录和文档', 'error');
      return;
    }
    setConfirmDelete(entry);
  }, [isFolderEmpty, showToast]);

  const openWorkspaceContextMenu = useCallback((event: React.MouseEvent<HTMLElement>, targetPath = currentPath) => {
    const target = event.target as HTMLElement;
    if (target.closest('[data-entry-key]')) return;
    event.preventDefault();
    event.stopPropagation();
    setSelectedKey(null);
    setContextMenu({
      x: event.clientX,
      y: event.clientY,
      mode: 'workspace',
      targetPath,
      entry: null,
    });
  }, [currentPath]);

  const openEntryContextMenu = useCallback((event: React.MouseEvent<HTMLElement>, entry: KnowledgeEntry) => {
    event.preventDefault();
    event.stopPropagation();
    setSelectedKey(entryKey(entry));
    setContextMenu({
      x: event.clientX,
      y: event.clientY,
      mode: isFolderEntry(entry) ? 'folder' : 'document',
      targetPath: isFolderEntry(entry) ? entry.path : entry.folder_path || '',
      entry,
    });
  }, []);

  const contextMenuItems = useMemo<ContextMenuAction[]>(() => {
    if (!contextMenu) return [];

    if (contextMenu.mode === 'workspace') {
      return [
        {
          kind: 'action',
          key: 'new-folder',
          label: '新建目录',
          icon: <FolderPlus size={15} />,
          onSelect: () => openPathDrawer('new-folder', contextMenu.targetPath),
        },
        {
          kind: 'action',
          key: 'upload',
          label: '上传文档',
          icon: <Upload size={15} />,
          onSelect: () => openPathDrawer('upload', contextMenu.targetPath),
        },
        {
          kind: 'action',
          key: 'url',
          label: '添加远程文档',
          icon: <LinkIcon size={15} />,
          onSelect: () => openPathDrawer('url', contextMenu.targetPath),
        },
        { kind: 'separator', key: 'workspace-separator' },
        {
          kind: 'action',
          key: 'refresh',
          label: '刷新资源库',
          icon: <RefreshCw size={15} />,
          onSelect: () => {
            setContextMenu(null);
            void loadLibrary();
          },
        },
      ];
    }

    if (contextMenu.mode === 'folder' && isFolderEntry(contextMenu.entry)) {
      const folder = contextMenu.entry;
      const canDeleteFolder = isFolderEmpty(folder);
      return [
        {
          kind: 'action',
          key: 'open-folder',
          label: '打开目录',
          icon: <FolderOpen size={15} />,
          onSelect: () => openPath(folder.path),
        },
        {
          kind: 'action',
          key: 'new-child-folder',
          label: '新建子目录',
          icon: <FolderPlus size={15} />,
          onSelect: () => {
            setCurrentPath(folder.path);
            openPathDrawer('new-folder', folder.path);
          },
        },
        {
          kind: 'action',
          key: 'upload-to-folder',
          label: '上传到此处',
          icon: <Upload size={15} />,
          onSelect: () => {
            setCurrentPath(folder.path);
            openPathDrawer('upload', folder.path);
          },
        },
        {
          kind: 'action',
          key: 'add-remote-to-folder',
          label: '导入远程文档',
          icon: <LinkIcon size={15} />,
          onSelect: () => {
            setCurrentPath(folder.path);
            openPathDrawer('url', folder.path);
          },
        },
        { kind: 'separator', key: 'folder-separator' },
        {
          kind: 'action',
          key: 'copy-folder-path',
          label: '复制目录路径',
          icon: <Copy size={15} />,
          onSelect: () => {
            setContextMenu(null);
            void handleCopy(formatPath(folder.path), '目录路径已复制');
          },
        },
        {
          kind: 'action',
          key: 'rename-folder',
          label: '重命名目录',
          icon: <FolderOpen size={15} />,
          onSelect: () => {
            setContextMenu(null);
            setDrawer('rename-folder');
          },
        },
        ...(canDeleteFolder ? [{
          kind: 'action' as const,
          key: 'delete-folder',
          label: '删除目录',
          icon: <Trash2 size={15} />,
          danger: true,
          onSelect: () => {
            setContextMenu(null);
            requestDeleteEntry(folder);
          },
        }] : []),
      ];
    }

    if (contextMenu.mode === 'document' && isDocumentEntry(contextMenu.entry)) {
      const document = contextMenu.entry;
      return [
        {
          kind: 'action',
          key: 'preview-document',
          label: '预览文档',
          icon: <Eye size={15} />,
          onSelect: () => {
            setContextMenu(null);
            setPreviewDoc(document);
          },
        },
        {
          kind: 'action',
          key: 'open-document-folder',
          label: '打开所在目录',
          icon: <FolderOpen size={15} />,
          onSelect: () => revealDocument(document),
        },
        {
          kind: 'action',
          key: 'move-document',
          label: '移动文档',
          icon: <FileText size={15} />,
          onSelect: () => {
            setContextMenu(null);
            setDrawer('move-document');
          },
        },
        { kind: 'separator', key: 'document-separator' },
        {
          kind: 'action',
          key: 'copy-document-source',
          label: '复制来源地址',
          icon: <Copy size={15} />,
          onSelect: () => {
            setContextMenu(null);
            void handleCopy(document.source_ref, '来源已复制');
          },
        },
        {
          kind: 'action',
          key: 'delete-document',
          label: '删除文档',
          icon: <Trash2 size={15} />,
          danger: true,
          onSelect: () => {
            setContextMenu(null);
            requestDeleteEntry(document);
          },
        },
      ];
    }

    return [];
  }, [contextMenu, handleCopy, isFolderEmpty, loadLibrary, openPath, openPathDrawer, requestDeleteEntry, revealDocument]);

  const contextMenuTitle = useMemo(() => {
    if (!contextMenu) return '';
    if (contextMenu.mode === 'workspace') {
      return `当前位置 ${formatPath(contextMenu.targetPath)}`;
    }
    if (contextMenu.mode === 'folder' && isFolderEntry(contextMenu.entry)) {
      return contextMenu.entry.name;
    }
    if (contextMenu.mode === 'document' && isDocumentEntry(contextMenu.entry)) {
      return contextMenu.entry.display_name;
    }
    return '';
  }, [contextMenu]);

  const contextMenuSubtitle = useMemo(() => {
    if (!contextMenu) return '';
    if (contextMenu.mode === 'workspace') {
      return '空白区域操作';
    }
    if (contextMenu.mode === 'folder') {
      return formatPath(contextMenu.targetPath);
    }
    if (contextMenu.mode === 'document' && isDocumentEntry(contextMenu.entry)) {
      return formatPath(contextMenu.entry.folder_path || '');
    }
    return '';
  }, [contextMenu]);

  const contextMenuPosition = useMemo(() => {
    if (!contextMenu || typeof window === 'undefined') return null;
    const actionCount = contextMenuItems.filter((item) => item.kind === 'action').length;
    const estimatedHeight = Math.max(156, actionCount * 42 + 78);
    return {
      left: Math.max(
        CONTEXT_MENU_GUTTER,
        Math.min(contextMenu.x, window.innerWidth - CONTEXT_MENU_WIDTH - CONTEXT_MENU_GUTTER),
      ),
      top: Math.max(
        CONTEXT_MENU_GUTTER,
        Math.min(contextMenu.y, window.innerHeight - estimatedHeight - CONTEXT_MENU_GUTTER),
      ),
    };
  }, [contextMenu, contextMenuItems]);

  const emptyState = (
    <div className="fm-state">
      <FolderOpen size={34} style={{ opacity: 0.35 }} />
      <span>{currentPath ? '这个目录下还没有内容' : '资源库还是空的'}</span>
      <span className="fm-state-tip">双击目录可以进入，右键空白处可以新建目录、上传文档或导入远程内容。</span>
      <div className="fm-empty-actions">
        <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); openPathDrawer('new-folder', currentPath); }}>
          <FolderPlus size={14} /> 新建目录
        </button>
        <button className="btn btn-primary btn-sm" onClick={(e) => { e.stopPropagation(); openPathDrawer('upload', currentPath); }}>
          <Upload size={14} /> 上传文档
        </button>
      </div>
    </div>
  );

  return (
    <div className="fm-root" onClick={() => { setSelectedKey(null); setContextMenu(null); }}>
      <div className="fm-toolbar" onClick={(e) => e.stopPropagation()}>
        <div className="fm-breadcrumb">
          {breadcrumbSegments.map((segment, index) => (
            <React.Fragment key={segment.path || '__root__'}>
              {index > 0 && <span className="fm-breadcrumb-sep"><ChevronRight size={14} /></span>}
              <button
                className={`fm-breadcrumb-btn ${index === breadcrumbSegments.length - 1 ? 'active' : ''}`}
                onClick={() => openPath(segment.path)}
              >
                {index === 0 ? <Home size={14} /> : null}
                <span>{segment.label}</span>
              </button>
            </React.Fragment>
          ))}
        </div>

        <div className="fm-toolbar-right">
          {isFolderEntry(selectedEntry) && (
            <button className="btn btn-ghost" onClick={() => setDrawer('rename-folder')}>
              <FolderOpen size={15} /> 重命名目录
            </button>
          )}
          {/*isDocumentEntry(selectedEntry) && (
            <button className="btn btn-ghost" onClick={() => setDrawer('move-document')}>
              <FileText size={15} /> 移动文档
            </button>
          )*/}
          <button className="fm-nav-btn" onClick={loadLibrary} disabled={loading} title="刷新">
            <RefreshCw size={15} className={loading ? 'fm-spin' : ''} />
          </button>
          <div className="fm-view-toggle">
            <button
              className={`fm-view-btn ${viewMode === 'icon' ? 'active' : ''}`}
              onClick={() => setViewMode('icon')}
              title="图标视图"
            >
              <LayoutGrid size={15} />
            </button>
            <button
              className={`fm-view-btn ${viewMode === 'list' ? 'active' : ''}`}
              onClick={() => setViewMode('list')}
              title="列表视图"
            >
              <List size={15} />
            </button>
          </div>
          <button className="btn btn-ghost" onClick={() => openPathDrawer('new-folder', currentPath)}>
            <FolderPlus size={15} /> 新建目录
          </button>
          <button className="btn btn-ghost" onClick={() => openPathDrawer('url', currentPath)}>
            <LinkIcon size={15} /> 添加远程文档
          </button>
          <button className="btn btn-primary" onClick={() => openPathDrawer('upload', currentPath)}>
            <Upload size={15} /> 上传文档
          </button>
        </div>
      </div>

      <div className="fm-body" style={{ gridTemplateColumns: `232px minmax(0, 1fr) auto ${inspectorWidth}px` }}>
        <aside className="fm-sidebar" onClick={(e) => e.stopPropagation()}>
          <div className="fm-sidebar-title">目录树</div>
          <div className="fm-sidebar-actions">
            <button className="btn btn-ghost btn-sm" onClick={() => openPathDrawer('new-folder', currentPath)}>
              <FolderPlus size={14} /> 新建目录
            </button>
          </div>

          <div className="fm-location-list" onContextMenu={(e) => openWorkspaceContextMenu(e)}>
            <button
              className={`fm-side-item ${currentPath === '' ? 'active' : ''}`}
              onClick={() => openPath('')}
              onContextMenu={(e) => openWorkspaceContextMenu(e, '')}
            >
              <span className="fm-side-icon"><Home size={16} /></span>
              <span className="fm-side-label">{ROOT_LABEL}</span>
              <span className="fm-side-count">{visibleEntries.length}</span>
            </button>
            {folderTree.map((folder) => (
              <button
                key={folder.folder_id}
                data-entry-key={entryKey(folder)}
                className={`fm-side-item fm-side-folder-tree ${currentPath === folder.path ? 'active' : ''}`}
                style={{ paddingLeft: `${12 + folder.depth * 16}px` }}
                onClick={() => openPath(folder.path)}
                onContextMenu={(e) => openEntryContextMenu(e, folder)}
                title={folder.path}
              >
                <span className="fm-side-icon"><FolderOpen size={16} /></span>
                <span className="fm-side-label">{folder.name}</span>
              </button>
            ))}
          </div>
        </aside>

        <section className="fm-content" onContextMenu={(e) => openWorkspaceContextMenu(e)}>
          <div className="fm-content-guide">
            <span className="fm-content-guide-title">资源管理器</span>
            <span className="fm-content-guide-text">
              双击打开项目，右键查看更多操作，操作默认落在当前目录。文档处理中时页面会自动刷新状态。
            </span>
          </div>
          {loading ? (
            <div className="fm-state">
              <div className="loader" />
              <span>正在加载资源库...</span>
            </div>
          ) : error ? (
            <div className="fm-state fm-state-error">
              <AlertTriangle size={18} />
              <span>{error}</span>
              <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); loadLibrary(); }}>重试</button>
            </div>
          ) : visibleEntries.length === 0 ? (
            emptyState
          ) : viewMode === 'icon' ? (
            <div className="fm-icon-grid">
              {visibleEntries.map((entry) => {
                const selected = selectedKey === entryKey(entry);
                const folder = isFolderEntry(entry);
                const documentStatus = !folder
                  ? normalizeDocumentProcessingStatus(entry.processing_status)
                  : 'ready';
                return (
                  <div
                    key={entryKey(entry)}
                    data-entry-key={entryKey(entry)}
                    className={`fm-icon-item ${selected ? 'selected' : ''}`}
                    onClick={(e) => { e.stopPropagation(); setSelectedKey(entryKey(entry)); }}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      if (folder) {
                        openPath(entry.path);
                      } else {
                        setPreviewDoc(entry);
                      }
                    }}
                    onContextMenu={(e) => openEntryContextMenu(e, entry)}
                    title={folder ? entry.path : entry.display_name}
                  >
                    <div className="fm-icon-img">
                      {folder ? <FolderOpen size={44} className="fm-folder-icon" /> : <FileText size={40} className="fm-file-icon" />}
                    </div>
                    <span className="fm-icon-name">{folder ? entry.name : entry.display_name}</span>
                    {!folder && (
                      <span className={`fm-doc-status ${documentStatus}`}>
                        {formatDocumentProcessingStatus(entry.processing_status)}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <table className="fm-list-table">
              <thead>
                <tr>
                  <th>名称</th>
                  {/*<th>类型</th>*/}
                  <th>格式</th>
                  <th>状态</th>
                  <th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                {visibleEntries.map((entry) => {
                  const selected = selectedKey === entryKey(entry);
                  const folder = isFolderEntry(entry);
                  const documentStatus = !folder
                    ? normalizeDocumentProcessingStatus(entry.processing_status)
                    : 'ready';
                  return (
                    <tr
                      key={entryKey(entry)}
                      data-entry-key={entryKey(entry)}
                      className={`fm-list-row ${selected ? 'selected' : ''}`}
                      onClick={(e) => { e.stopPropagation(); setSelectedKey(entryKey(entry)); }}
                      onDoubleClick={(e) => {
                        e.stopPropagation();
                        if (folder) {
                          openPath(entry.path);
                        } else {
                          setPreviewDoc(entry);
                        }
                      }}
                      onContextMenu={(e) => openEntryContextMenu(e, entry)}
                      title={folder ? entry.path : entry.display_name}
                    >
                      <td>
                        <div className="fm-list-name-cell">
                          {folder ? <FolderOpen size={16} className="fm-folder-icon" /> : <File size={15} className="fm-file-icon" />}
                          <span>{folder ? entry.name : entry.display_name}</span>
                        </div>
                      </td>
                      <td>{folder ? '--' : entry.source_format || '待识别'}</td>
                      <td>
                        {folder ? '--' : (
                          <span className={`fm-doc-status ${documentStatus}`}>
                            {formatDocumentProcessingStatus(entry.processing_status)}
                          </span>
                        )}
                      </td>
                      <td>{formatTime(entry.updated_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>

        <div 
          className="fm-resizer" 
          onMouseDown={() => {
            isResizingInspector.current = true;
            document.body.style.cursor = 'col-resize';
          }}
        />

        <aside className="fm-inspector" onClick={(e) => e.stopPropagation()}>
          {selectedEntry ? (
            isFolderEntry(selectedEntry) ? (
              <>
                <div className="fm-inspector-header">
                  <div className="fm-inspector-title">
                    <FolderOpen size={18} />
                    <span>{selectedEntry.name}</span>
                  </div>
                  {/*<span className="fm-badge">目录</span>*/}
                </div>

                <div className="fm-inspector-actions">
                  <button className="btn btn-ghost btn-sm" onClick={() => openPath(selectedEntry.path)}>
                    <FolderOpen size={14} /> 打开
                  </button>
                  <button className="btn btn-ghost btn-sm" onClick={() => setDrawer('rename-folder')}>
                    <FolderOpen size={14} /> 重命名
                  </button>
                  <button className="btn btn-ghost btn-sm" onClick={() => {
                    openPath(selectedEntry.path);
                    openPathDrawer('new-folder', selectedEntry.path);
                  }}>
                    <FolderPlus size={14} /> 新建子目录
                  </button>
                  {canDeleteSelectedFolder && (
                    <button className="btn btn-danger btn-sm" disabled={deletingKey === entryKey(selectedEntry)} onClick={() => requestDeleteEntry(selectedEntry)}>
                      <Trash2 size={14} /> 删除
                    </button>
                  )}
                </div>

                <div className="fm-prop-list">
                  <div className="fm-prop-item">
                    <span className="fm-prop-label">目录路径</span>
                    <div className="fm-prop-value">
                      <span>{formatPath(selectedEntry.path)}</span>
                      <button className="btn btn-ghost btn-sm" title="复制路径" onClick={() => handleCopy(formatPath(selectedEntry.path), '目录路径已复制')}>
                        <Copy size={13} />
                      </button>
                    </div>
                  </div>
                  <div className="fm-prop-item">
                    <span className="fm-prop-label">子目录</span>
                    <span className="fm-prop-value">{currentFolderStats?.childFolderCount ?? 0}</span>
                  </div>
                  <div className="fm-prop-item">
                    <span className="fm-prop-label">文档</span>
                    <span className="fm-prop-value">{currentFolderStats?.childDocumentCount ?? 0}</span>
                  </div>
                  <div className="fm-prop-item">
                    <span className="fm-prop-label">创建时间</span>
                    <span className="fm-prop-value">{formatTime(selectedEntry.created_at)}</span>
                  </div>
                  <div className="fm-prop-item">
                    <span className="fm-prop-label">更新时间</span>
                    <span className="fm-prop-value">{formatTime(selectedEntry.updated_at)}</span>
                  </div>
                  {!canDeleteSelectedFolder && (
                    <div className="fm-prop-item vertical">
                      <span className="fm-prop-label">删除限制</span>
                      <div className="fm-prop-value">
                        请先清空子目录和文档，再删除该目录。
                      </div>
                    </div>
                  )}
                </div>
              </>
            ) : (
              <>
                <div className="fm-inspector-header">
                  <div className="fm-inspector-title">
                    <FileText size={18} />
                    <span>{selectedEntry.display_name}</span>
                  </div>
                </div>

                <div className="fm-inspector-actions">
                  <button className="btn btn-ghost btn-sm" onClick={() => setPreviewDoc(selectedEntry)}>
                    <Eye size={14} /> 预览
                  </button>
                  {/*<button className="btn btn-ghost btn-sm" onClick={() => setDrawer('move-document')}>
                    <FileText size={14} /> 移动
                  </button>*/}
                  {/*<button
                    className="btn btn-ghost btn-sm"
                    onClick={() => openPath(selectedEntry.folder_path || '')}
                  >
                    <FolderOpen size={14} /> 打开所在目录
                  </button>*/}
                  <button className="btn btn-danger btn-sm" disabled={deletingKey === entryKey(selectedEntry)} onClick={() => requestDeleteEntry(selectedEntry)}>
                    <Trash2 size={14} /> 删除
                  </button>
                </div>

                <div className="fm-prop-list fm-prop-list-grow">
                  <div className="fm-prop-item">
                    <span className="fm-prop-label">所在目录</span>
                    <span className="fm-prop-value">{formatPath(selectedEntry.folder_path || '')}</span>
                  </div>

                  <div className="fm-prop-item">
                    <span className="fm-prop-label">来源</span>
                    <div className="fm-prop-value">
                      <span title={selectedEntry.source_ref} className="fm-truncate-text">{selectedEntry.source_ref}</span>
                      <button className="btn btn-ghost btn-sm" title="复制来源" onClick={() => handleCopy(selectedEntry.source_ref, '来源已复制')}>
                        <Copy size={13} />
                      </button>
                    </div>
                  </div>

                  {/*<div className="fm-prop-item">
                    <span className="fm-prop-label">同步到 resource</span>
                    <div className="fm-prop-value">
                      <span title={selectedEntry.resource_root_uri} className="fm-truncate-text">{selectedEntry.resource_root_uri}</span>
                      <button className="btn btn-ghost btn-sm" title="复制 URI" onClick={() => handleCopy(selectedEntry.resource_root_uri, 'resource URI 已复制')}>
                        <Copy size={13} />
                      </button>
                    </div>
                  </div>*/}

                  <div className="fm-prop-item">
                    <span className="fm-prop-label">格式</span>
                    <span className="fm-prop-value">{selectedEntry.source_format || '待识别'}</span>
                  </div>

                  <div className="fm-prop-item">
                    <span className="fm-prop-label">处理状态</span>
                    <span className="fm-prop-value">
                      <span className={`fm-doc-status ${selectedDocumentProcessingStatus}`}>
                        {formatDocumentProcessingStatus(selectedEntry.processing_status)}
                      </span>
                    </span>
                  </div>
                  
                  {/*<div className="fm-prop-item">
                    <span className="fm-prop-label">保存原件</span>
                    <span className="fm-prop-value">{selectedEntry.has_local_copy ? '是' : '否'}</span>
                  </div>*/}

                  <div className="fm-prop-item">
                    <span className="fm-prop-label">创建时间</span>
                    <span className="fm-prop-value">{formatTime(selectedEntry.created_at)}</span>
                  </div>

                  <div className="fm-prop-item">
                    <span className="fm-prop-label">更新时间</span>
                    <span className="fm-prop-value">{formatTime(selectedEntry.updated_at)}</span>
                  </div>

                  {selectedEntry.processing_completed_at && selectedDocumentProcessingStatus === 'ready' && (
                    <div className="fm-prop-item">
                      <span className="fm-prop-label">完成时间</span>
                      <span className="fm-prop-value">{formatTime(selectedEntry.processing_completed_at)}</span>
                    </div>
                  )}

                  {(selectedEntry.reason || selectedEntry.instruction) && (
                    <div className="fm-prop-item vertical">
                      <span className="fm-prop-label">备注信息</span>
                      <div className="fm-prop-value">
                        {selectedEntry.reason || selectedEntry.instruction}
                      </div>
                    </div>
                  )}

                  <div className="fm-prop-item vertical fm-prop-item-grow">
                    <span className="fm-prop-label">Abstract 摘要</span>
                    <div className="fm-prop-value">
                      {selectedDocumentProcessingStatus === 'processing' ? (
                        <span className="fm-prop-note">文档正在后台解析、摘要和索引处理中，完成后会显示摘要。</span>
                      ) : selectedDocumentProcessingStatus === 'failed' ? (
                        <span className="fm-prop-note fm-prop-note-error">
                          {selectedEntry.processing_error || '文档处理失败，暂时无法生成摘要。'}
                        </span>
                      ) : selectedDocumentAbstractLoading ? (
                        <span className="fm-prop-note">正在加载摘要...</span>
                      ) : selectedDocumentAbstractError ? (
                        <span className="fm-prop-note fm-prop-note-error">{selectedDocumentAbstractError}</span>
                      ) : (
                        <div className="fm-prop-markdown-scroll">
                          <MarkdownRenderer
                            className="fm-prop-markdown"
                            content={selectedDocumentAbstract || '暂无摘要'}
                          />
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </>
            )
          ) : (
            <div className="fm-state">
              <FolderOpen size={30} style={{ opacity: 0.3 }} />
              <span>选择一个目录或文档查看详情</span>
            </div>
          )}
        </aside>
      </div>

      <div className="fm-statusbar">
        <span>{visibleEntries.length} 个项目</span>
        <span className="fm-statusbar-sep" />
        <span className="fm-status-selected">当前位置: {formatPath(currentPath)}</span>
        <span className="fm-statusbar-sep" />
        <span>{hasProcessingDocuments ? '检测到处理中任务 · 页面自动刷新' : '双击打开 · 右键操作'}</span>
        {selectedEntry && (
          <>
            <span className="fm-statusbar-sep" />
            <span className="fm-status-path">
              已选中: {isFolderEntry(selectedEntry) ? selectedEntry.name : selectedEntry.display_name}
            </span>
          </>
        )}
      </div>

      <UploadDrawer
        open={drawer === 'upload'}
        onClose={closeDrawer}
        currentPath={activeDrawerPath}
        onUploaded={(files) => handleUploadFiles(files, activeDrawerPath)}
      />

      <UrlDrawer
        open={drawer === 'url'}
        onClose={closeDrawer}
        currentPath={activeDrawerPath}
        onSubmitted={(url) => handleAddUrl(url, activeDrawerPath)}
      />

      <NewFolderDrawer
        open={drawer === 'new-folder'}
        onClose={closeDrawer}
        currentPath={activeDrawerPath}
        onSubmitted={(name) => handleCreateFolder(name, activeDrawerPath)}
      />

      <RenameFolderDrawer
        open={drawer === 'rename-folder'}
        folder={isFolderEntry(selectedEntry) ? selectedEntry : null}
        onClose={closeDrawer}
        onSubmitted={handleRenameFolder}
      />

      <MoveDocumentDrawer
        open={drawer === 'move-document'}
        document={isDocumentEntry(selectedEntry) ? selectedEntry : null}
        folders={folders}
        onClose={closeDrawer}
        onSubmitted={handleMoveDocument}
      />

      {contextMenu && contextMenuPosition && (
        <div
          ref={contextMenuRef}
          className="fm-context-menu"
          style={contextMenuPosition}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="fm-context-menu-header">
            <div className="fm-context-menu-title">{contextMenuTitle}</div>
            <div className="fm-context-menu-subtitle">{contextMenuSubtitle}</div>
          </div>
          <div className="fm-context-menu-list">
            {contextMenuItems.map((item) => (
              item.kind === 'separator' ? (
                <div key={item.key} className="fm-context-menu-separator" />
              ) : (
                <button
                  key={item.key}
                  className={`fm-context-menu-item ${item.danger ? 'danger' : ''}`}
                  onClick={item.onSelect}
                >
                  <span className="fm-context-menu-item-main">
                    <span className="fm-context-menu-icon">{item.icon}</span>
                    <span>{item.label}</span>
                  </span>
                </button>
              )
            ))}
          </div>
        </div>
      )}

      {previewDoc && (
        <PreviewModal
          document={previewDoc}
          serverUrl={serverUrl}
          apiKey={apiKey}
          accountId={accountId}
          userId={userId}
          onClose={() => setPreviewDoc(null)}
        />
      )}

      {confirmDelete && (
        <ConfirmModal
          message={
            isFolderEntry(confirmDelete)
              ? `确定删除目录 "${confirmDelete.name}"？仅空目录允许删除。`
              : `确定删除 "${confirmDelete.display_name}"？系统会同步删除对应的 resource 内容与向量数据，此操作不可恢复。`
          }
          onConfirm={doDelete}
          onCancel={() => setConfirmDelete(null)}
        />
      )}

      {toast && (
        <Toast
          msg={toast.msg}
          type={toast.type}
          onClose={() => setToast(null)}
        />
      )}
    </div>
  );
};

export default Resources;
