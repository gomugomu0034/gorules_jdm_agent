'use client';

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * Agent replies are markdown with GFM tables. They also contain raw <details>
 * blocks, which react-markdown drops rather than rendering as HTML - that is
 * deliberate: the agent's output is model-generated, so it is treated as text
 * and never injected as markup.
 */
export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-body text-fg">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{stripHtmlWrappers(content)}</ReactMarkdown>
    </div>
  );
}

/**
 * Convert the small amount of raw HTML the agent emits into markdown.
 *
 * Without this the tags render as literal text, since react-markdown does not
 * pass HTML through - which is the behaviour we want for model output.
 */
function stripHtmlWrappers(content: string): string {
  return content
    .replace(/<\/?details>/gi, '')
    .replace(/<summary>([\s\S]*?)<\/summary>/gi, '**$1**\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/?(?:b|strong)>/gi, '**')
    .replace(/<\/?(?:i|em)>/gi, '*')
    .replace(/<\/?(?:p|div|span)[^>]*>/gi, '')
    // Collapse the '****' left behind when a bold tag wrapped existing markdown.
    .replace(/\*{4,}/g, '**');
}
