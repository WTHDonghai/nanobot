import React, { useCallback, useMemo } from 'react';
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
  highlightQuery?: string;
  imageClassName?: string;
  referenceLinkClassName?: string;
  serverUrl?: string;
  onImageClick?: (src: string) => void;
  onReferenceClick?: (target: MarkdownReferenceTarget, label: string) => void;
  resolveImageSrc?: (src: string) => string | undefined;
};

const passthroughUrlTransform = (url: string) => url;

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function highlightText(value: string, query?: string): React.ReactNode {
  const needle = query?.trim();
  if (!needle) return value;

  const parts = value.split(new RegExp(`(${escapeRegExp(needle)})`, 'ig'));
  return parts.map((part, index) => (
    part.toLowerCase() === needle.toLowerCase()
      ? <mark className="markdown-search-highlight" key={`${part}-${index}`}>{part}</mark>
      : <React.Fragment key={`${part}-${index}`}>{part}</React.Fragment>
  ));
}

function highlightReactNode(node: React.ReactNode, query?: string): React.ReactNode {
  const needle = query?.trim();
  if (!needle) return node;

  if (typeof node === 'string' || typeof node === 'number') {
    return highlightText(String(node), needle);
  }

  if (Array.isArray(node)) {
    return node.map((child, index) => (
      <React.Fragment key={index}>{highlightReactNode(child, needle)}</React.Fragment>
    ));
  }

  if (React.isValidElement<{ children?: React.ReactNode }>(node) && node.props.children) {
    return React.cloneElement(node, {
      children: highlightReactNode(node.props.children, needle),
    });
  }

  return node;
}

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
  let rawSrc = String(src || '').trim();
  if (!rawSrc) return undefined;

  const nestedMarkdownImage = rawSrc.match(/^!\[[^\]]*]\((.+)\)$/);
  if (nestedMarkdownImage?.[1]) {
    rawSrc = nestedMarkdownImage[1].trim();
  }

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
  highlightQuery,
  imageClassName,
  referenceLinkClassName = 'markdown-reference-link',
  serverUrl,
  onImageClick,
  onReferenceClick,
  resolveImageSrc,
}) => {
  const renderChildren = useCallback(
    (children: React.ReactNode) => highlightReactNode(children, highlightQuery),
    [highlightQuery],
  );

  const components = useMemo(() => ({
    a({ href, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { children?: React.ReactNode }) {
      const referenceTarget = getMarkdownReferenceTarget(href, serverUrl);
      if (!referenceTarget || !onReferenceClick) {
        return (
          <a href={href} target="_blank" rel="noreferrer" {...props}>
            {renderChildren(children)}
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
          <span>{renderChildren(children)}</span>
        </button>
      );
    },
    p({ children, ...props }: React.HTMLAttributes<HTMLParagraphElement> & { children?: React.ReactNode }) {
      return <p {...props}>{renderChildren(children)}</p>;
    },
    li({ children, ...props }: React.LiHTMLAttributes<HTMLLIElement> & { children?: React.ReactNode }) {
      return <li {...props}>{renderChildren(children)}</li>;
    },
    strong({ children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
      return <strong {...props}>{renderChildren(children)}</strong>;
    },
    em({ children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
      return <em {...props}>{renderChildren(children)}</em>;
    },
    del({ children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
      return <del {...props}>{renderChildren(children)}</del>;
    },
    blockquote({ children, ...props }: React.BlockquoteHTMLAttributes<HTMLQuoteElement> & { children?: React.ReactNode }) {
      return <blockquote {...props}>{renderChildren(children)}</blockquote>;
    },
    h1({ children, ...props }: React.HTMLAttributes<HTMLHeadingElement> & { children?: React.ReactNode }) {
      return <h1 {...props}>{renderChildren(children)}</h1>;
    },
    h2({ children, ...props }: React.HTMLAttributes<HTMLHeadingElement> & { children?: React.ReactNode }) {
      return <h2 {...props}>{renderChildren(children)}</h2>;
    },
    h3({ children, ...props }: React.HTMLAttributes<HTMLHeadingElement> & { children?: React.ReactNode }) {
      return <h3 {...props}>{renderChildren(children)}</h3>;
    },
    h4({ children, ...props }: React.HTMLAttributes<HTMLHeadingElement> & { children?: React.ReactNode }) {
      return <h4 {...props}>{renderChildren(children)}</h4>;
    },
    h5({ children, ...props }: React.HTMLAttributes<HTMLHeadingElement> & { children?: React.ReactNode }) {
      return <h5 {...props}>{renderChildren(children)}</h5>;
    },
    h6({ children, ...props }: React.HTMLAttributes<HTMLHeadingElement> & { children?: React.ReactNode }) {
      return <h6 {...props}>{renderChildren(children)}</h6>;
    },
    td({ children, ...props }: React.TdHTMLAttributes<HTMLTableCellElement> & { children?: React.ReactNode }) {
      return <td {...props}>{renderChildren(children)}</td>;
    },
    th({ children, ...props }: React.ThHTMLAttributes<HTMLTableCellElement> & { children?: React.ReactNode }) {
      return <th {...props}>{renderChildren(children)}</th>;
    },
    code({ children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
      return <code {...props}>{renderChildren(children)}</code>;
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
  }), [imageClassName, onImageClick, onReferenceClick, referenceLinkClassName, renderChildren, resolveImageSrc, serverUrl]);

  const normalizedContent = useMemo(() => normalizeMarkdownForDisplay(content), [content]);

  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={passthroughUrlTransform}
        components={components}
      >
        {normalizedContent}
      </ReactMarkdown>
    </div>
  );
};

export default MarkdownRenderer;
