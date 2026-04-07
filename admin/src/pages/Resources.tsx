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

const ROOT_LABEL = '资源库';

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

const formatSourceType = (sourceType: string) => {
  switch (sourceType) {
    case 'file':
      return '本地文件';
    case 'directory':
      return '本地目录';
    case 'remote':
      return '远程来源';
    default:
      return '未知来源';
  }
};

const formatPath = (path: string) => (path ? `/${path}` : '/');

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
    <div className="modal">
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      setLoading(true);
      setError('');
      try {
        const params = new URLSearchParams({ uri: document.resource_root_uri, limit: '500' });
        const res = await fetch(`${serverUrl}/api/v1/content/abstract?${params}`, {
          headers: buildTenantHeaders(apiKey, accountId, userId),
        });
        const data = await res.json();
        if (!res.ok) {
          const detail = data?.error?.message || data?.detail;
          throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || '预览失败');
        }
        if (!cancelled) {
          const value = data.result;
          setContent(typeof value === 'string' ? value : JSON.stringify(value, null, 2));
        }
      } catch (err: any) {
        if (!cancelled) setError(err?.message || '预览失败');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    run();
    return () => { cancelled = true; };
  }, [accountId, apiKey, document.resource_root_uri, serverUrl, userId]);

  return (
    <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={{ maxWidth: 840, width: '90%' }}>
        <div className="modal-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Eye size={18} /> 预览: {document.display_name}
        </div>
        <div className="modal-body">
          <div className="fm-preview-box">
            {loading ? (
              <div className="fm-state"><div className="loader" /></div>
            ) : error ? (
              <div className="fm-state fm-state-error">{error}</div>
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
      <div className="fm-drawer open">
        <div className="fm-drawer-header">
          <div className="fm-drawer-title"><LinkIcon size={18} /> 添加远程文档</div>
          <button className="btn btn-ghost btn-sm" style={{ padding: '4px' }} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="fm-drawer-body">
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
      <div className="fm-drawer open">
        <div className="fm-drawer-header">
          <div className="fm-drawer-title"><FolderPlus size={18} /> 新建目录</div>
          <button className="btn btn-ghost btn-sm" style={{ padding: '4px' }} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="fm-drawer-body">
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
      <div className="fm-drawer open">
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
      <div className="fm-drawer open">
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
      <div className="fm-drawer open">
        <div className="fm-drawer-header">
          <div className="fm-drawer-title"><Upload size={18} /> 上传原始文档</div>
          <button className="btn btn-ghost btn-sm" style={{ padding: '4px' }} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="fm-drawer-body">
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
  const [viewMode, setViewMode] = useState<ViewMode>('icon');
  const [currentPath, setCurrentPath] = useState('');

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

  const openPath = useCallback((path: string) => {
    setCurrentPath(path);
    setSelectedKey(null);
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

  const currentFolderStats = useMemo(() => {
    if (!isFolderEntry(selectedEntry)) return null;
    const childFolderCount = folders.filter((folder) => folder.parent_path === selectedEntry.path).length;
    const childDocumentCount = documents.filter((document) => document.folder_path === selectedEntry.path).length;
    return { childFolderCount, childDocumentCount };
  }, [documents, folders, selectedEntry]);

  const handleCopy = async (value: string, successMsg: string) => {
    try {
      await navigator.clipboard.writeText(value);
      showToast(successMsg);
    } catch {
      showToast('复制失败，请检查浏览器权限', 'error');
    }
  };

  const handleCreateFolder = async (name: string) => {
    const data = await fetchApi<{ result: KnowledgeFolder }>(
      serverUrl,
      apiKey,
      '/api/v1/knowledge-folders',
      {
        method: 'POST',
        account: accountId || undefined,
        user: userId || undefined,
        body: JSON.stringify({ name, parent_path: currentPath }),
      },
    );
    const folder = data.result;
    await loadLibrary();
    openPath(folder.path);
    setSelectedKey(`folder:${folder.folder_id}`);
    showToast(`已创建目录：${folder.name}`);
  };

  const handleAddUrl = async (url: string) => {
    await fetchApi(serverUrl, apiKey, '/api/v1/resources', {
      method: 'POST',
      account: accountId || undefined,
      user: userId || undefined,
      body: JSON.stringify({ path: url, wait: false, folder_path: currentPath }),
    });
    await loadLibrary();
    showToast(`文档已导入到 ${formatPath(currentPath)}`);
  };

  const handleUploadFiles = async (files: File[]) => {
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
          body: JSON.stringify({ temp_file_id: tempId, wait: false, folder_path: currentPath }),
        });
        done += 1;
      } catch (err: any) {
        failures += 1;
        if (!firstError) firstError = err?.message || '上传失败';
      }
    }

    await loadLibrary();
    if (failures === 0) {
      showToast(`已导入 ${done} 个文档到 ${formatPath(currentPath)}`);
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

  const emptyState = (
    <div className="fm-state">
      <FolderOpen size={34} style={{ opacity: 0.35 }} />
      <span>{currentPath ? '这个目录下还没有内容' : '资源库还是空的'}</span>
      <div className="fm-empty-actions">
        <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); setDrawer('new-folder'); }}>
          <FolderPlus size={14} /> 新建目录
        </button>
        <button className="btn btn-primary btn-sm" onClick={(e) => { e.stopPropagation(); setDrawer('upload'); }}>
          <Upload size={14} /> 上传文档
        </button>
      </div>
    </div>
  );

  return (
    <div className="fm-root" onClick={() => setSelectedKey(null)}>
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
          {isDocumentEntry(selectedEntry) && (
            <button className="btn btn-ghost" onClick={() => setDrawer('move-document')}>
              <FileText size={15} /> 移动文档
            </button>
          )}
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
          <button className="btn btn-ghost" onClick={() => setDrawer('new-folder')}>
            <FolderPlus size={15} /> 新建目录
          </button>
          <button className="btn btn-ghost" onClick={() => setDrawer('url')}>
            <LinkIcon size={15} /> 添加远程文档
          </button>
          <button className="btn btn-primary" onClick={() => setDrawer('upload')}>
            <Upload size={15} /> 上传文档
          </button>
        </div>
      </div>

      <div className="fm-body">
        <aside className="fm-sidebar" onClick={(e) => e.stopPropagation()}>
          <div className="fm-sidebar-title">目录树</div>
          <div className="fm-sidebar-actions">
            <button className="btn btn-ghost btn-sm" onClick={() => setDrawer('new-folder')}>
              <FolderPlus size={14} /> 新建目录
            </button>
          </div>

          <div className="fm-location-list">
            <button
              className={`fm-side-item ${currentPath === '' ? 'active' : ''}`}
              onClick={() => openPath('')}
            >
              <span className="fm-side-icon"><Home size={16} /></span>
              <span className="fm-side-label">{ROOT_LABEL}</span>
              <span className="fm-side-count">{visibleEntries.length}</span>
            </button>
            {folderTree.map((folder) => (
              <button
                key={folder.folder_id}
                className={`fm-side-item fm-side-folder-tree ${currentPath === folder.path ? 'active' : ''}`}
                style={{ paddingLeft: `${12 + folder.depth * 16}px` }}
                onClick={() => openPath(folder.path)}
                title={folder.path}
              >
                <span className="fm-side-icon"><FolderOpen size={16} /></span>
                <span className="fm-side-label">{folder.name}</span>
              </button>
            ))}
          </div>
        </aside>

        <section className="fm-content">
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
                return (
                  <div
                    key={entryKey(entry)}
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
                    title={folder ? entry.path : entry.display_name}
                  >
                    <div className="fm-icon-img">
                      {folder ? <FolderOpen size={44} className="fm-folder-icon" /> : <FileText size={40} className="fm-file-icon" />}
                    </div>
                    <span className="fm-icon-name">{folder ? entry.name : entry.display_name}</span>
                    <div className="fm-icon-meta">
                      <span className="fm-badge">
                        {folder ? '目录' : formatSourceType(entry.source_type)}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <table className="fm-list-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>类型</th>
                  <th>格式</th>
                  <th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                {visibleEntries.map((entry) => {
                  const selected = selectedKey === entryKey(entry);
                  const folder = isFolderEntry(entry);
                  return (
                    <tr
                      key={entryKey(entry)}
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
                      title={folder ? entry.path : entry.display_name}
                    >
                      <td>
                        <div className="fm-list-name-cell">
                          {folder ? <FolderOpen size={16} className="fm-folder-icon" /> : <File size={15} className="fm-file-icon" />}
                          <span>{folder ? entry.name : entry.display_name}</span>
                        </div>
                      </td>
                      <td>{folder ? '目录' : formatSourceType(entry.source_type)}</td>
                      <td>{folder ? '--' : entry.source_format || '待识别'}</td>
                      <td>{formatTime(entry.updated_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>

        <aside className="fm-inspector" onClick={(e) => e.stopPropagation()}>
          {selectedEntry ? (
            isFolderEntry(selectedEntry) ? (
              <>
                <div className="fm-inspector-header">
                  <div className="fm-inspector-title">
                    <FolderOpen size={18} />
                    <span>{selectedEntry.name}</span>
                  </div>
                  <span className="fm-badge">目录</span>
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
                    setDrawer('new-folder');
                  }}>
                    <FolderPlus size={14} /> 新建子目录
                  </button>
                  <button className="btn btn-danger btn-sm" disabled={deletingKey === entryKey(selectedEntry)} onClick={() => setConfirmDelete(selectedEntry)}>
                    <Trash2 size={14} /> 删除
                  </button>
                </div>

                <div className="fm-detail-block">
                  <div className="fm-detail-label">目录路径</div>
                  <div className="fm-detail-value">{formatPath(selectedEntry.path)}</div>
                  <button className="btn btn-ghost btn-sm" onClick={() => handleCopy(formatPath(selectedEntry.path), '目录路径已复制')}>
                    <Copy size={13} /> 复制路径
                  </button>
                </div>

                <div className="fm-detail-grid">
                  <div>
                    <div className="fm-detail-label">子目录</div>
                    <div className="fm-detail-value">{currentFolderStats?.childFolderCount ?? 0}</div>
                  </div>
                  <div>
                    <div className="fm-detail-label">文档</div>
                    <div className="fm-detail-value">{currentFolderStats?.childDocumentCount ?? 0}</div>
                  </div>
                  <div>
                    <div className="fm-detail-label">创建时间</div>
                    <div className="fm-detail-value">{formatTime(selectedEntry.created_at)}</div>
                  </div>
                  <div>
                    <div className="fm-detail-label">更新时间</div>
                    <div className="fm-detail-value">{formatTime(selectedEntry.updated_at)}</div>
                  </div>
                </div>
              </>
            ) : (
              <>
                <div className="fm-inspector-header">
                  <div className="fm-inspector-title">
                    <FileText size={18} />
                    <span>{selectedEntry.display_name}</span>
                  </div>
                  <span className="fm-badge">{formatSourceType(selectedEntry.source_type)}</span>
                </div>

                <div className="fm-inspector-actions">
                  <button className="btn btn-ghost btn-sm" onClick={() => setPreviewDoc(selectedEntry)}>
                    <Eye size={14} /> 预览
                  </button>
                  <button className="btn btn-ghost btn-sm" onClick={() => setDrawer('move-document')}>
                    <FileText size={14} /> 移动
                  </button>
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => openPath(selectedEntry.folder_path || '')}
                  >
                    <FolderOpen size={14} /> 打开所在目录
                  </button>
                  <button className="btn btn-danger btn-sm" disabled={deletingKey === entryKey(selectedEntry)} onClick={() => setConfirmDelete(selectedEntry)}>
                    <Trash2 size={14} /> 删除
                  </button>
                </div>

                <div className="fm-detail-block">
                  <div className="fm-detail-label">所在目录</div>
                  <div className="fm-detail-value">{formatPath(selectedEntry.folder_path || '')}</div>
                </div>

                <div className="fm-detail-block">
                  <div className="fm-detail-label">来源</div>
                  <div className="fm-detail-value" title={selectedEntry.source_ref}>{selectedEntry.source_ref}</div>
                  <button className="btn btn-ghost btn-sm" onClick={() => handleCopy(selectedEntry.source_ref, '来源已复制')}>
                    <Copy size={13} /> 复制来源
                  </button>
                </div>

                <div className="fm-detail-block">
                  <div className="fm-detail-label">同步到 resource</div>
                  <div className="fm-detail-value" title={selectedEntry.resource_root_uri}>{selectedEntry.resource_root_uri}</div>
                  <button className="btn btn-ghost btn-sm" onClick={() => handleCopy(selectedEntry.resource_root_uri, 'resource URI 已复制')}>
                    <Copy size={13} /> 复制 URI
                  </button>
                </div>

                <div className="fm-detail-grid">
                  <div>
                    <div className="fm-detail-label">格式</div>
                    <div className="fm-detail-value">{selectedEntry.source_format || '待识别'}</div>
                  </div>
                  <div>
                    <div className="fm-detail-label">保存原件</div>
                    <div className="fm-detail-value">{selectedEntry.has_local_copy ? '是' : '否'}</div>
                  </div>
                  <div>
                    <div className="fm-detail-label">创建时间</div>
                    <div className="fm-detail-value">{formatTime(selectedEntry.created_at)}</div>
                  </div>
                  <div>
                    <div className="fm-detail-label">更新时间</div>
                    <div className="fm-detail-value">{formatTime(selectedEntry.updated_at)}</div>
                  </div>
                </div>

                {(selectedEntry.reason || selectedEntry.instruction) && (
                  <div className="fm-detail-block">
                    <div className="fm-detail-label">备注信息</div>
                    <div className="fm-detail-value">
                      {selectedEntry.reason || selectedEntry.instruction}
                    </div>
                  </div>
                )}
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
        onClose={() => setDrawer(null)}
        currentPath={currentPath}
        onUploaded={handleUploadFiles}
      />

      <UrlDrawer
        open={drawer === 'url'}
        onClose={() => setDrawer(null)}
        currentPath={currentPath}
        onSubmitted={handleAddUrl}
      />

      <NewFolderDrawer
        open={drawer === 'new-folder'}
        onClose={() => setDrawer(null)}
        currentPath={currentPath}
        onSubmitted={handleCreateFolder}
      />

      <RenameFolderDrawer
        open={drawer === 'rename-folder'}
        folder={isFolderEntry(selectedEntry) ? selectedEntry : null}
        onClose={() => setDrawer(null)}
        onSubmitted={handleRenameFolder}
      />

      <MoveDocumentDrawer
        open={drawer === 'move-document'}
        document={isDocumentEntry(selectedEntry) ? selectedEntry : null}
        folders={folders}
        onClose={() => setDrawer(null)}
        onSubmitted={handleMoveDocument}
      />

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
