import React from 'react';
import { BookOpen } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export type MarkdownReferenceTarget = {
  href: string;
  uri: string;
  token?: string;
};

export type MarkdownRendererProps = {
  content: string;
  className?: string;
  imageClassName?: string;
  referenceLinkClassName?: string;
  serverUrl?: string;
  onImageClick?: (src: string) => void;
  onReferenceClick?: (target: MarkdownReferenceTarget, label: string) => void;
  resolveImageSrc?: (src: string) => string | undefined;
};

const passthroughUrlTransform = (url: string) => url;

export function getTextFromReactNode(node: React.ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(getTextFromReactNode).join('');
  if (React.isValidElement<{ children?: React.ReactNode }>(node)) {
    return getTextFromReactNode(node.props.children);
  }
  return '';
}

export function normalizeMarkdownForDisplay(value: string): string {
  if (!value) return value;

  const normalized = value.replace(/\r\n?/g, '\n');
  const segments = normalized.split(/(```[\s\S]*?```)/g);

  return segments
    .map((segment, index) => {
      if (index % 2 === 1) return segment;

      return segment
        .replace(/\n{3,}/g, '\n\n')
        .replace(
          /(^|\n)(\s*(?:\d+\.|[\-*+])\s*)\n+(?=\S)/g,
          (_match, prefix: string, marker: string) => `${prefix}${marker.trimEnd()} `,
        )
        .trim();
    })
    .filter(Boolean)
    .join('\n\n');
}

export function resolveBotMarkdownImageSrc(
  src?: string | null,
  serverUrl?: string,
): string | undefined {
  const rawSrc = String(src || '').trim();
  if (!rawSrc) return undefined;

  const base = serverUrl || (typeof window !== 'undefined' ? window.location.origin : 'http://localhost');

  if (rawSrc.startsWith('send://')) {
    return new URL(`/bot/v1/images/${rawSrc.slice('send://'.length)}`, base).toString();
  }

  try {
    const url = new URL(rawSrc, base);
    if (url.pathname.startsWith('/bot/v1/images/')) {
      return new URL(`${url.pathname}${url.search}${url.hash}`, base).toString();
    }
  } catch {
    if (rawSrc.startsWith('/bot/v1/images/')) {
      return new URL(rawSrc, base).toString();
    }
  }

  return rawSrc;
}

export function getMarkdownReferenceTarget(
  href?: string | null,
  serverUrl?: string,
): MarkdownReferenceTarget | null {
  const rawHref = String(href || '').trim();
  if (!rawHref || !rawHref.includes('/bot/v1/resources/preview')) return null;

  try {
    const base = serverUrl || (typeof window !== 'undefined' ? window.location.origin : 'http://localhost');
    const url = new URL(rawHref, base);
    if (!url.pathname.endsWith('/bot/v1/resources/preview')) return null;

    const uri = url.searchParams.get('uri') || '';
    const token = url.searchParams.get('token') || '';
    if (!uri.startsWith('viking://resources/')) return null;

    return {
      href: rawHref,
      uri,
      ...(token ? { token } : {}),
    };
  } catch {
    return null;
  }
}

const MarkdownRenderer: React.FC<MarkdownRendererProps> = ({
  content,
  className,
  imageClassName,
  referenceLinkClassName = 'markdown-reference-link',
  serverUrl,
  onImageClick,
  onReferenceClick,
  resolveImageSrc,
}) => {
  const components = {
    a({ href, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { children?: React.ReactNode }) {
      const referenceTarget = getMarkdownReferenceTarget(href, serverUrl);
      if (!referenceTarget || !onReferenceClick) {
        return (
          <a href={href} target="_blank" rel="noreferrer" {...props}>
            {children}
          </a>
        );
      }

      const label = getTextFromReactNode(children);
      return (
        <button
          type="button"
          className={referenceLinkClassName}
          onClick={() => onReferenceClick(referenceTarget, label)}
          title="在右侧预览参考文档"
        >
          <BookOpen size={13} />
          <span>{children}</span>
        </button>
      );
    },
    img(props: React.ImgHTMLAttributes<HTMLImageElement>) {
      const src = (resolveImageSrc || ((value: string) => resolveBotMarkdownImageSrc(value, serverUrl)))(
        typeof props.src === 'string' ? props.src : '',
      );

      return (
        <img
          {...props}
          src={src}
          className={imageClassName || props.className}
          style={imageClassName ? props.style : { maxWidth: '100%', borderRadius: '8px', cursor: 'zoom-in', marginTop: '8px', ...props.style }}
          loading={props.loading || 'lazy'}
          onClick={() => {
            if (src) onImageClick?.(src);
          }}
        />
      );
    },
  };

  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={passthroughUrlTransform}
        components={components}
      >
        {normalizeMarkdownForDisplay(content)}
      </ReactMarkdown>
    </div>
  );
};

export default MarkdownRenderer;
