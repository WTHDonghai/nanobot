import React, { useState, useRef, useCallback, useEffect } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { fetchApi } from '../services/api';
import {
  Database, Plus, CheckCircle, AlertTriangle, FolderOpen, Link,
  Upload, X, File, RefreshCw, Trash2, ChevronRight, ChevronDown, Eye,
} from 'lucide-react';
import './Pages.css';

// ─── Types ──────────────────────────────────────────────────────────────────

type TabMode = 'list' | 'url' | 'upload';

interface ResourceNode {
  uri: string;
  name: string;
  type: 'file' | 'directory';
  children?: ResourceNode[];
  size?: number;
}

interface UploadItem {
  file: File;
  status: 'pending' | 'uploading' | 'done' | 'error';
  error?: string;
}

// ─── Inline Confirm Modal ────────────────────────────────────────────────────

const ConfirmModal = ({ message, onConfirm, onCancel }: {
  message: string; onConfirm: () => void; onCancel: () => void;
}) => (
  <div className="modal-overlay" onClick={e => { if (e.target === e.currentTarget) onCancel(); }}>
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

// ─── Preview Modal ───────────────────────────────────────────────────────────

const PreviewModal = ({ uri, name, isDir, serverUrl, apiKey, onClose }: {
  uri: string; name: string; isDir: boolean; serverUrl: string; apiKey: string; onClose: () => void;
}) => {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let canceled = false;
    const fetchContent = async () => {
      try {
        const endpoint = isDir ? '/api/v1/content/abstract' : '/api/v1/content/read';
        const params = new URLSearchParams({ uri, limit: '500' });
        const res = await fetch(`${serverUrl}${endpoint}?${params}`, {
          headers: { 'X-API-Key': apiKey },
        });
        const data = await res.json();
        if (!res.ok) {
          const detail = data?.error?.message || data?.detail;
          throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || 'Failed to fetch preview');
        }
        if (!canceled) setContent(typeof data.result === 'string' ? data.result : JSON.stringify(data.result, null, 2));
      } catch (err: any) {
        if (!canceled) setError(err.message);
      } finally {
        if (!canceled) setLoading(false);
      }
    };
    fetchContent();
    return () => { canceled = true; };
  }, [uri, isDir, serverUrl, apiKey]);

  return (
    <div className="modal-overlay" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={{ maxWidth: 800, width: '90%' }}>
        <div className="modal-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Eye size={18} /> 预览: {name} {isDir && <span style={{fontSize:'0.8rem', opacity:0.7}}>(目录摘要)</span>}
        </div>
        <div className="modal-body" style={{ maxHeight: '60vh', overflowY: 'auto', background: 'var(--bg1)', padding: 16, borderRadius: 6 }}>
          {loading ? (
            <div className="loader" style={{ margin: '20px auto' }} />
          ) : error ? (
            <div style={{ color: 'var(--danger)' }}>{error}</div>
          ) : (
            <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: '0.85rem', fontFamily: 'monospace', color: 'var(--text)' }}>
              {content || '(空)'}
            </pre>
          )}
        </div>
        <div className="modal-footer">
          <button className="btn btn-ghost" onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  );
};

// ─── Toast notification (no browser alert) ───────────────────────────────────

const Toast = ({ msg, type, onClose }: { msg: string; type: 'success' | 'error'; onClose: () => void }) => {
  useEffect(() => {
    const t = setTimeout(onClose, 4000);
    return () => clearTimeout(t);
  }, [onClose]);
  return (
    <div style={{
      position: 'fixed', bottom: 32, right: 32, zIndex: 9999,
      padding: '14px 20px', borderRadius: 10, maxWidth: 420,
      display: 'flex', alignItems: 'center', gap: 10,
      background: type === 'success' ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)',
      border: `1px solid ${type === 'success' ? 'rgba(16,185,129,0.4)' : 'rgba(239,68,68,0.4)'}`,
      color: type === 'success' ? 'var(--success)' : 'var(--danger)',
      backdropFilter: 'blur(8px)', boxShadow: '0 4px 24px rgba(0,0,0,0.3)',
    }}>
      {type === 'success' ? <CheckCircle size={18} /> : <AlertTriangle size={18} />}
      <span style={{ flex: 1, fontSize: '0.9rem' }}>{msg}</span>
      <button className="btn btn-ghost btn-sm" style={{ padding: '2px' }} onClick={onClose}>
        <X size={14} />
      </button>
    </div>
  );
};

// ─── Resource Tree Node ──────────────────────────────────────────────────────

const ResourceTreeNode = ({ node, depth, onDelete, onPreview }: {
  node: ResourceNode; depth: number; onDelete: (uri: string, name: string, isDir: boolean) => void; onPreview: (uri: string, name: string, isDir: boolean) => void;
}) => {
  const [open, setOpen] = useState(depth < 1);
  const isDir = node.type === 'directory';

  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '7px 12px', paddingLeft: `${12 + depth * 20}px`,
        borderRadius: 6, cursor: isDir ? 'pointer' : 'default',
        background: 'transparent', transition: 'background 0.15s',
      }}
        onMouseEnter={e => { e.currentTarget.style.background = 'var(--bg3)'; (e.currentTarget.lastElementChild as HTMLElement).style.opacity = '1'; }}
        onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; (e.currentTarget.lastElementChild as HTMLElement).style.opacity = '0'; }}
      >
        {isDir ? (
          <button
            className="btn btn-ghost btn-sm"
            style={{ padding: '2px 4px' }}
            onClick={() => setOpen(o => !o)}
          >
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        ) : (
          <span style={{ width: 22 }} />
        )}
        {isDir
          ? <FolderOpen size={16} style={{ color: 'var(--primary)', flexShrink: 0 }} />
          : <File size={15} style={{ opacity: 0.55, flexShrink: 0 }} />
        }
        <span style={{
          flex: 1, fontSize: '0.88rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          color: isDir ? 'var(--primary)' : 'var(--text)',
        }}>
          {node.name}
        </span>
        {node.size !== undefined && !isDir && (
          <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginRight: 8 }}>
            {node.size >= 1024 * 1024
              ? `${(node.size / 1024 / 1024).toFixed(1)} MB`
              : node.size >= 1024
                ? `${(node.size / 1024).toFixed(1)} KB`
                : `${node.size} B`}
          </span>
        )}
        <div style={{ display: 'flex', gap: 6, opacity: 0, transition: 'opacity 0.15s' }}>
          <button
            className="btn btn-ghost btn-sm"
            style={{ padding: '3px 8px', fontSize: '0.78rem' }}
            onClick={(e) => { e.stopPropagation(); onPreview(node.uri, node.name, isDir); }}
            title="预览内容"
          >
            <Eye size={13} />
          </button>
          <button
            className="btn btn-danger btn-sm"
            style={{ padding: '3px 8px', fontSize: '0.78rem' }}
            onClick={(e) => { e.stopPropagation(); onDelete(node.uri, node.name, isDir); }}
            title="删除"
          >
            <Trash2 size={13} />
          </button>
        </div>
      </div>
      {isDir && open && node.children?.map(child => (
        <ResourceTreeNode key={child.uri} node={child} depth={depth + 1} onDelete={onDelete} onPreview={onPreview} />
      ))}
    </div>
  );
};

// ─── Upload temp file helper ──────────────────────────────────────────────────

async function uploadTempFile(serverUrl: string, apiKey: string, file: File): Promise<string> {
  const formData = new FormData();
  formData.append('file', file, file.name);
  const response = await fetch(`${serverUrl}/api/v1/resources/temp_upload`, {
    method: 'POST',
    headers: { 'X-API-Key': apiKey },
    body: formData,
  });
  const data = await response.json();
  if (!response.ok) {
    const detail = data?.error?.message || data?.detail;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || `Upload failed: ${response.status}`);
  }
  return data.result.temp_file_id;
}

// ─── normalize API ls result to ResourceNode[] ───────────────────────────────

function normalizeNodes(items: any[]): ResourceNode[] {
  if (!items) return [];

  // 1. Build a map of all items by URI
  const nodeMap = new Map<string, ResourceNode>();
  items.forEach((item: any) => {
    const uri: string = item.uri || item.path || '';
    const name: string = item.name || uri.split('/').filter(Boolean).pop() || uri;
    const isDir = item.type === 'directory' || item.is_dir === true || item.type === 'dir' || item.isDir === true;
    
    nodeMap.set(uri, {
      uri,
      name,
      type: isDir ? 'directory' : 'file',
      size: item.size,
      children: isDir ? [] : undefined,
    });
  });

  // 2. Reconstruct the tree based on URI hierarchy
  const roots: ResourceNode[] = [];

  nodeMap.forEach((node, uri) => {
    // Determine the parent URI by removing the last segment
    // E.g., viking://resources/foo/bar -> viking://resources/foo
    // If the path ends with '/', strip it first
    let sanitizedUri = uri.endsWith('/') ? uri.slice(0, -1) : uri;
    const parts = sanitizedUri.split('/');
    
    // Find parent. Minimum parts should be 3: "viking:", "", "resources"
    if (parts.length > 3) {
      parts.pop();
      let parentUri = parts.join('/');
      // Make sure parent URI maps exactly (some URIs might end in / in the API)
      let parentNode = nodeMap.get(parentUri) || nodeMap.get(parentUri + '/');
      
      if (parentNode) {
        if (!parentNode.children) parentNode.children = [];
        parentNode.children.push(node);
      } else {
        roots.push(node);
      }
    } else {
      roots.push(node);
    }
  });

  // It's possible the original request started at `viking://resources/` and all elements
  // consider it their parent, but `viking://resources/` itself might not be in the list.
  // In that case, top-level items under the root directory will have no parent in the map.
  return roots;
}

// ─── Resources Page ──────────────────────────────────────────────────────────

const Resources: React.FC = () => {
  const { serverUrl, apiKey } = useAuth();
  const [tab, setTab] = useState<TabMode>('list');
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null);

  // ── Resource List ─────────────────────────────────────────────────────────

  const [resources, setResources] = useState<ResourceNode[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<{ uri: string; label: string; recursive: boolean } | null>(null);
  const [previewData, setPreviewData] = useState<{ uri: string; name: string; isDir: boolean } | null>(null);

  const loadResources = useCallback(async () => {
    setListLoading(true);
    setListError('');
    try {
      const params = new URLSearchParams({ uri: 'viking://resources/', output: 'original', level_limit: '10', node_limit: '2000' });
      const rawRes = await fetch(`${serverUrl}/api/v1/fs/tree?${params}`, {
        headers: { 'X-API-Key': apiKey },
      });
      const data = await rawRes.json();
      if (!rawRes.ok) {
        const detail = data?.error?.message || data?.detail;
        throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || 'Failed to list resources');
      }
      const items = Array.isArray(data.result) ? data.result : (data.result?.children || data.result?.items || []);
      setResources(normalizeNodes(items));
    } catch (e: any) {
      setListError(e.message);
    } finally {
      setListLoading(false);
    }
  }, [serverUrl, apiKey]);

  useEffect(() => { if (tab === 'list') loadResources(); }, [tab, loadResources]);

  const handleDeleteRequest = (uri: string, name: string, isDir: boolean) => {
    setConfirmDelete({ uri, label: name, recursive: isDir });
  };

  const handlePreviewRequest = (uri: string, name: string, isDir: boolean) => {
    setPreviewData({ uri, name, isDir });
  };

  const doDelete = async () => {
    if (!confirmDelete) return;
    const { uri, recursive } = confirmDelete;
    setConfirmDelete(null);
    try {
      const params = new URLSearchParams({ uri, recursive: recursive ? 'true' : 'false' });
      const rawRes = await fetch(`${serverUrl}/api/v1/fs?${params}`, {
        method: 'DELETE',
        headers: { 'X-API-Key': apiKey },
      });
      const data = await rawRes.json();
      if (!rawRes.ok) {
        const detail = data?.error?.message || data?.detail;
        throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail) || 'Delete failed');
      }
      setToast({ msg: `已删除: ${confirmDelete.label}`, type: 'success' });
      loadResources();
    } catch (e: any) {
      setToast({ msg: '删除失败：' + e.message, type: 'error' });
    }
  };

  // ── URL mode ──────────────────────────────────────────────────────────────

  const [url, setUrl] = useState('');
  const [urlLoading, setUrlLoading] = useState(false);

  const handleAddUrl = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setUrlLoading(true);
    try {
      await fetchApi(serverUrl, apiKey, '/api/v1/resources', {
        method: 'POST',
        body: JSON.stringify({ path: url.trim(), wait: false }),
      });
      setToast({ msg: '资源添加任务已提交，后台正在处理中...', type: 'success' });
      setUrl('');
    } catch (err: any) {
      setToast({ msg: '添加失败：' + err.message, type: 'error' });
    } finally {
      setUrlLoading(false);
    }
  };

  // ── Upload mode ───────────────────────────────────────────────────────────

  const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dirInputRef = useRef<HTMLInputElement>(null);

  const HIDDEN_FILE_RE = /(^|\/)\.([^/]+)/;

  const addFiles = useCallback((files: FileList | null) => {
    if (!files) return;
    const filtered = Array.from(files).filter(f => {
      const pathToCheck = f.webkitRelativePath || f.name;
      return !HIDDEN_FILE_RE.test(pathToCheck);
    });
    const skipped = files.length - filtered.length;
    setUploadItems(prev => [...prev, ...filtered.map(f => ({ file: f, status: 'pending' as const }))]);
    if (skipped > 0) setToast({ msg: `已过滤 ${skipped} 个隐藏文件（以 . 开头）`, type: 'success' });
    setToast(null); // reset so it can show fresh
    if (skipped > 0) setTimeout(() => setToast({ msg: `已自动过滤 ${skipped} 个隐藏文件（以 . 开头）`, type: 'success' }), 50);
  }, []);

  const removeItem = (idx: number) => setUploadItems(prev => prev.filter((_, i) => i !== idx));

  const handleUploadAll = async () => {
    const pending = uploadItems.some(i => i.status === 'pending');
    if (!pending) return;
    setUploading(true);
    let done = 0; let errors = 0;

    for (let idx = 0; idx < uploadItems.length; idx++) {
      if (uploadItems[idx].status !== 'pending') continue;
      setUploadItems(prev => prev.map((it, i) => i === idx ? { ...it, status: 'uploading' } : it));
      try {
        const tempFileId = await uploadTempFile(serverUrl, apiKey, uploadItems[idx].file);
        await fetchApi(serverUrl, apiKey, '/api/v1/resources', {
          method: 'POST',
          body: JSON.stringify({ temp_file_id: tempFileId, wait: false }),
        });
        setUploadItems(prev => prev.map((it, i) => i === idx ? { ...it, status: 'done' } : it));
        done++;
      } catch (err: any) {
        setUploadItems(prev => prev.map((it, i) => i === idx ? { ...it, status: 'error', error: err.message } : it));
        errors++;
      }
    }
    setUploading(false);
    setToast({
      msg: errors === 0
        ? `全部 ${done} 个文件上传成功，后台正在处理...`
        : `完成 ${done} 个，失败 ${errors} 个`,
      type: errors === 0 ? 'success' : 'error',
    });
  };

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    addFiles(e.dataTransfer.files);
  }, [addFiles]);

  const pendingCount = uploadItems.filter(i => i.status === 'pending').length;

  // ─────────────────────────────────────────────────────────────────────────

  return (
    <div>
      {/* ── Tab Bar ── */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 24 }}>
        <button className={`btn ${tab === 'list' ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setTab('list')}>
          <Database size={16} /> 资源列表
        </button>
        <button className={`btn ${tab === 'url' ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setTab('url')}>
          <Link size={16} /> 添加 URL
        </button>
        <button className={`btn ${tab === 'upload' ? 'btn-primary' : 'btn-ghost'}`} onClick={() => setTab('upload')}>
          <Upload size={16} /> 上传本地文件
        </button>
      </div>

      {/* ── Resource List ── */}
      {tab === 'list' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <span style={{ fontSize: '0.9rem', color: 'var(--text-muted)' }}>
              显示 <code>viking://resources/</code> 下所有已入库的资源
            </span>
            <button className="btn btn-ghost btn-sm" onClick={loadResources} disabled={listLoading}>
              <RefreshCw size={14} style={{ animation: listLoading ? 'spin 1s linear infinite' : 'none' }} /> 刷新
            </button>
          </div>

          {listError && (
            <div style={{ color: 'var(--danger)', padding: 12, border: '1px solid var(--danger)', borderRadius: 8, background: 'rgba(239,68,68,0.1)', marginBottom: 16 }}>
              {listError}
            </div>
          )}

          <div className="table-wrap" style={{ padding: 0 }}>
            {listLoading ? (
              <div className="empty"><div className="loader" /></div>
            ) : resources.length === 0 ? (
              <div className="empty" style={{ padding: 40 }}>暂无资源，请通过"添加 URL"或"上传本地文件"录入</div>
            ) : (
              <div style={{ padding: '8px 0' }}>
                {resources.map(node => (
                  <ResourceTreeNode key={node.uri} node={node} depth={0} onDelete={handleDeleteRequest} onPreview={handlePreviewRequest} />
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── URL Mode ── */}
      {tab === 'url' && (
        <div className="card" style={{ maxWidth: 620 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16, color: 'var(--primary)' }}>
            <Link size={20} />
            <h2 style={{ fontSize: '1.2rem', margin: 0 }}>添加远程资源</h2>
          </div>
          <p style={{ color: 'var(--text-muted)', marginBottom: 24, fontSize: '0.9rem' }}>
            支持 HTTP(S) 文档链接、Git 仓库地址等。系统会自动抓取并进行语义向量化处理，异步执行。
          </p>
          <form onSubmit={handleAddUrl}>
            <div className="form-group">
              <label>资源地址</label>
              <input
                type="text"
                className="input"
                placeholder="https://example.com/docs  或  git@github.com:org/repo.git"
                value={url}
                onChange={e => setUrl(e.target.value)}
                required
              />
            </div>
            <div style={{ marginTop: 24, display: 'flex', justifyContent: 'flex-end' }}>
              <button type="submit" className="btn btn-primary" disabled={urlLoading || !url.trim()}>
                {urlLoading
                  ? <div className="loader" style={{ width: 16, height: 16, borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3) rgba(255,255,255,0.3) rgba(255,255,255,0.3) #fff' }} />
                  : <><Plus size={16} /> 添加并处理</>}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* ── Upload Mode ── */}
      {tab === 'upload' && (
        <div style={{ maxWidth: 680 }}>
          <div className="card">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16, color: 'var(--primary)' }}>
              <Upload size={20} />
              <h2 style={{ fontSize: '1.2rem', margin: 0 }}>上传本地文件或目录</h2>
            </div>
            <p style={{ color: 'var(--text-muted)', marginBottom: 20, fontSize: '0.9rem' }}>
              支持拖拽或选择文件夹。以 <code>.</code> 开头的隐藏文件会自动过滤。文件将先上传至服务器临时区，再批量完成向量化。
            </p>

            {/* Drag & Drop Zone */}
            <div
              onDrop={onDrop}
              onDragOver={e => e.preventDefault()}
              onClick={() => fileInputRef.current?.click()}
              style={{
                border: '2px dashed var(--border)', borderRadius: 10,
                padding: '28px 24px', textAlign: 'center', cursor: 'pointer',
                transition: 'border-color 0.2s, background 0.2s', marginBottom: 10,
              }}
              onMouseEnter={e => (e.currentTarget.style.borderColor = 'var(--primary)')}
              onMouseLeave={e => (e.currentTarget.style.borderColor = 'var(--border)')}
            >
              <FolderOpen size={32} style={{ opacity: 0.35, marginBottom: 10 }} />
              <div style={{ fontSize: '0.9rem', color: 'var(--text-muted)' }}>
                拖拽文件到此，或<span style={{ color: 'var(--primary)', marginLeft: 4 }}>点击选择文件</span>
              </div>
              <input ref={fileInputRef} type="file" multiple style={{ display: 'none' }} onChange={e => addFiles(e.target.files)} />
            </div>

            <button className="btn btn-ghost btn-sm" style={{ marginBottom: 20 }} onClick={() => dirInputRef.current?.click()}>
              <FolderOpen size={14} /> 选择整个目录
            </button>
            <input
              ref={dirInputRef}
              type="file"
              // @ts-ignore
              webkitdirectory="true"
              multiple
              style={{ display: 'none' }}
              onChange={e => addFiles(e.target.files)}
            />

            {uploadItems.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                  <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                    已选 {uploadItems.length} 个文件（{pendingCount} 待处理）
                  </span>
                  <button className="btn btn-ghost btn-sm" onClick={() => { setUploadItems(prev => prev.filter(i => i.status === 'pending' || i.status === 'error')); }}>
                    清除已完成
                  </button>
                </div>
                <div style={{ maxHeight: 260, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 5 }}>
                  {uploadItems.map((item, idx) => (
                    <div key={idx} style={{
                      display: 'flex', alignItems: 'center', gap: 10, padding: '7px 12px',
                      borderRadius: 8, background: 'var(--bg1)',
                      border: `1px solid ${item.status === 'done' ? 'rgba(16,185,129,0.3)' : item.status === 'error' ? 'rgba(239,68,68,0.3)' : 'var(--border)'}`,
                    }}>
                      <File size={13} style={{ flexShrink: 0, opacity: 0.5 }} />
                      <span style={{ flex: 1, fontSize: '0.82rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {item.file.webkitRelativePath || item.file.name}
                      </span>
                      <span style={{ fontSize: '0.75rem', flexShrink: 0, color: item.status === 'done' ? 'var(--success)' : item.status === 'error' ? 'var(--danger)' : item.status === 'uploading' ? 'var(--primary)' : 'var(--text-muted)' }}>
                        {item.status === 'done' ? '✓ 完成' : item.status === 'error' ? `✗ ${item.error}` : item.status === 'uploading' ? '上传中...' : '待处理'}
                      </span>
                      {item.status === 'pending' && (
                        <button className="btn btn-ghost btn-sm" style={{ padding: '2px 4px' }} onClick={() => removeItem(idx)}>
                          <X size={12} />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
              {uploadItems.length > 0 && (
                <button className="btn btn-ghost" onClick={() => setUploadItems([])}>全部清除</button>
              )}
              <button className="btn btn-primary" disabled={uploading || pendingCount === 0} onClick={handleUploadAll}>
                {uploading
                  ? <><div className="loader" style={{ width: 14, height: 14, borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3) rgba(255,255,255,0.3) rgba(255,255,255,0.3) #fff' }} /> 处理中...</>
                  : <><Upload size={16} /> 上传并处理（{pendingCount}）</>}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Preview Modal ── */}
      {previewData && (
        <PreviewModal
          uri={previewData.uri}
          name={previewData.name}
          isDir={previewData.isDir}
          serverUrl={serverUrl}
          apiKey={apiKey}
          onClose={() => setPreviewData(null)}
        />
      )}

      {/* ── Delete Confirm ── */}
      {confirmDelete && (
        <ConfirmModal
          message={`确定删除资源 "${confirmDelete.label}"？${confirmDelete.recursive ? '（包含目录下所有内容）' : ''}此操作不可恢复。`}
          onConfirm={doDelete}
          onCancel={() => setConfirmDelete(null)}
        />
      )}

      {/* ── Toast ── */}
      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
};

export default Resources;
