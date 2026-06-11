import type { jsPDF } from 'jspdf';

const EXPORT_STAGE_WIDTH = 860;
const PDF_MARGIN = 28;
const PDF_BLOCK_GAP = 18;
const IMAGE_FETCH_TIMEOUT_MS = 8000;
const IMAGE_LOAD_TIMEOUT_MS = 5000;
const FONT_READY_TIMEOUT_MS = 2000;
const MAX_CANVAS_PIXELS = 16_000_000;
const DEFAULT_IMAGE_QUALITY = 0.94;
const LIGHT_THEME_VARS: Record<string, string> = {
  '--bg': '#f1f5f9',
  '--bg2': '#ffffff',
  '--bg3': '#e8edf5',
  '--card': 'rgba(0,0,0,0.03)',
  '--border': 'rgba(0,0,0,0.09)',
  '--primary': '#4f46e5',
  '--primary-dim': 'rgba(79,70,229,0.10)',
  '--success': '#059669',
  '--danger': '#dc2626',
  '--warning': '#d97706',
  '--text': '#0f172a',
  '--muted': '#64748b',
  '--light': '#475569',
};

type ExportChatPdfOptions = {
  title: string;
  exportedAtLabel: string;
  filenameBase?: string;
  messageNodes: HTMLElement[];
  apiKey?: string;
  captureScale?: number;
  imageQuality?: number;
};

function sanitizeFilename(value: string): string {
  const normalized = value
    .replace(/[\\/:*?"<>|]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  return (normalized || 'chat-export').slice(0, 80);
}

function waitForImageLoad(img: HTMLImageElement, timeoutMs = IMAGE_LOAD_TIMEOUT_MS): Promise<void> {
  if (img.complete) {
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    const done = () => {
      window.clearTimeout(timeout);
      img.removeEventListener('load', done);
      img.removeEventListener('error', done);
      resolve();
    };
    const timeout = window.setTimeout(done, timeoutMs);

    img.addEventListener('load', done);
    img.addEventListener('error', done);
  });
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('timeout')), timeoutMs);
    promise
      .then((value) => resolve(value))
      .catch((error) => reject(error))
      .finally(() => window.clearTimeout(timeout));
  });
}

async function fetchBlobWithTimeout(url: string, init: RequestInit, timeoutMs = IMAGE_FETCH_TIMEOUT_MS): Promise<Blob> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return await withTimeout(response.blob(), timeoutMs);
  } finally {
    controller.abort();
    window.clearTimeout(timeout);
  }
}

async function waitForFonts(): Promise<void> {
  if (!('fonts' in document)) return;

  await Promise.race([
    document.fonts.ready.then(() => undefined).catch(() => undefined),
    new Promise<void>((resolve) => {
      window.setTimeout(resolve, FONT_READY_TIMEOUT_MS);
    }),
  ]);
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error || new Error('读取图片失败'));
    reader.readAsDataURL(blob);
  });
}

async function inlineImage(img: HTMLImageElement, apiKey?: string): Promise<void> {
  const src = img.currentSrc || img.getAttribute('src') || '';
  if (!src || src.startsWith('data:')) {
    await waitForImageLoad(img);
    return;
  }

  const sameOrigin: RequestCredentials = 'same-origin';
  const attempts: RequestInit[] = [
    { credentials: sameOrigin },
    ...(apiKey ? [{ credentials: sameOrigin, headers: { 'X-API-Key': apiKey } }] : []),
  ];

  for (const requestInit of attempts) {
    try {
      const blob = await fetchBlobWithTimeout(src, requestInit);
      const dataUrl = await withTimeout(blobToDataUrl(blob), IMAGE_LOAD_TIMEOUT_MS);
      if (!dataUrl) continue;
      img.src = dataUrl;
      break;
    } catch {
      // Fall back to the original URL if we cannot inline it.
    }
  }

  await waitForImageLoad(img);
}

async function inlineImages(root: HTMLElement, apiKey?: string): Promise<void> {
  const images = Array.from(root.querySelectorAll('img'));
  await Promise.all(images.map((img) => inlineImage(img, apiKey)));
}

function buildExportStage(title: string, exportedAtLabel: string): HTMLDivElement {
  const stage = document.createElement('div');
  stage.className = 'chat-export-stage';
  stage.setAttribute('data-theme', 'light');
  stage.style.width = `${EXPORT_STAGE_WIDTH}px`;

  Object.entries(LIGHT_THEME_VARS).forEach(([key, value]) => {
    stage.style.setProperty(key, value);
  });

  const header = document.createElement('div');
  header.className = 'chat-export-header';

  const heading = document.createElement('div');
  heading.className = 'chat-export-title';
  heading.textContent = title;

  const meta = document.createElement('div');
  meta.className = 'chat-export-subtitle';
  meta.textContent = `导出时间：${exportedAtLabel}`;

  header.appendChild(heading);
  header.appendChild(meta);
  stage.appendChild(header);

  return stage;
}

function cloneMessageNode(node: HTMLElement): HTMLElement {
  const clone = node.cloneNode(true) as HTMLElement;
  clone.classList.add('chat-export-row');
  clone.querySelectorAll('[data-export-ignore="true"]').forEach((element) => element.remove());
  return clone;
}

async function renderBlockCanvas(
  element: HTMLElement,
  captureElement: typeof import('html2canvas').default,
  preferredScale?: number,
): Promise<HTMLCanvasElement> {
  const elementWidth = Math.max(element.scrollWidth, element.offsetWidth, 1);
  const elementHeight = Math.max(element.scrollHeight, element.offsetHeight, 1);
  const baseScale = preferredScale ?? Math.min(window.devicePixelRatio || 1, 2);
  const maxScale = Math.sqrt(MAX_CANVAS_PIXELS / Math.max(elementWidth * elementHeight, 1));
  const scale = Math.min(baseScale, maxScale);

  return captureElement(element, {
    backgroundColor: '#ffffff',
    scale,
    useCORS: true,
    logging: false,
  });
}

function createCanvasSlice(source: HTMLCanvasElement, startY: number, height: number): HTMLCanvasElement {
  const slice = document.createElement('canvas');
  slice.width = source.width;
  slice.height = height;
  const context = slice.getContext('2d');
  if (!context) {
    throw new Error('无法创建导出画布');
  }

  context.fillStyle = '#ffffff';
  context.fillRect(0, 0, slice.width, slice.height);
  context.drawImage(
    source,
    0,
    startY,
    source.width,
    height,
    0,
    0,
    source.width,
    height,
  );

  return slice;
}

function addCanvasToPdf(
  pdf: jsPDF,
  canvas: HTMLCanvasElement,
  margin: number,
  gap: number,
  currentY: number,
  imageQuality = DEFAULT_IMAGE_QUALITY,
): number {
  const pageWidth = pdf.internal.pageSize.getWidth();
  const pageHeight = pdf.internal.pageSize.getHeight();
  const targetWidth = pageWidth - margin * 2;
  const maxBlockHeight = pageHeight - margin * 2;
  const sourcePixelsPerPoint = canvas.width / targetWidth;
  const canvasHeight = canvas.height / sourcePixelsPerPoint;
  let remainingPixels = canvas.height;
  let sourceOffsetY = 0;
  let nextY = currentY;

  if (canvasHeight <= maxBlockHeight && nextY + canvasHeight > pageHeight - margin) {
    pdf.addPage();
    nextY = margin;
  }

  while (remainingPixels > 0) {
    const availableHeight = pageHeight - margin - nextY;
    if (availableHeight <= 1) {
      pdf.addPage();
      nextY = margin;
      continue;
    }

    const maxSlicePixels = Math.max(1, Math.floor(Math.min(availableHeight, maxBlockHeight) * sourcePixelsPerPoint));
    const slicePixels = Math.min(remainingPixels, maxSlicePixels);
    const sliceCanvas = createCanvasSlice(canvas, sourceOffsetY, slicePixels);
    const sliceHeight = slicePixels / sourcePixelsPerPoint;

    pdf.addImage(
      sliceCanvas.toDataURL('image/jpeg', imageQuality),
      'JPEG',
      margin,
      nextY,
      targetWidth,
      sliceHeight,
      undefined,
      'FAST',
    );

    sourceOffsetY += slicePixels;
    remainingPixels -= slicePixels;
    nextY += sliceHeight;

    if (remainingPixels > 0) {
      pdf.addPage();
      nextY = margin;
    }
  }

  return nextY + gap;
}

export async function exportChatSubsetToPdf({
  title,
  exportedAtLabel,
  filenameBase,
  messageNodes,
  apiKey,
  captureScale,
  imageQuality = DEFAULT_IMAGE_QUALITY,
}: ExportChatPdfOptions): Promise<void> {
  if (messageNodes.length === 0) {
    throw new Error('没有可导出的消息');
  }

  const stage = buildExportStage(title, exportedAtLabel);
  document.body.appendChild(stage);

  try {
    const [{ default: captureElement }, { jsPDF: JsPdf }] = await Promise.all([
      import('html2canvas'),
      import('jspdf'),
    ]);

    messageNodes.forEach((node) => {
      stage.appendChild(cloneMessageNode(node));
    });

    await waitForFonts();
    await inlineImages(stage, apiKey);

    const blocks = Array.from(stage.children) as HTMLElement[];
    const pdf = new JsPdf({ orientation: 'p', unit: 'pt', format: 'a4', compress: true });
    let currentY = PDF_MARGIN;

    for (const [index, block] of blocks.entries()) {
      const canvas = await renderBlockCanvas(block, captureElement, captureScale);
      currentY = addCanvasToPdf(pdf, canvas, PDF_MARGIN, PDF_BLOCK_GAP, currentY, imageQuality);

      if (index < blocks.length - 1 && currentY >= pdf.internal.pageSize.getHeight() - PDF_MARGIN) {
        pdf.addPage();
        currentY = PDF_MARGIN;
      }
    }

    const safeTitle = sanitizeFilename(filenameBase || title);
    pdf.save(`${safeTitle}.pdf`);
  } finally {
    stage.remove();
  }
}
