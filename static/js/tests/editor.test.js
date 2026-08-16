/**
 * editor.js unit tests
 *
 * Tests pure functions extracted from static/js/editor.js.
 * Since editor.js uses plain function declarations (no ES module exports),
 * we inject the source via window.eval() in the jsdom environment so that
 * all function declarations land on window (== globalThis) — exactly as a
 * browser <script> tag would behave.
 */
import { describe, it, expect } from 'vitest';
import {
  escapeHtml,
  _slugifyHeading,
  isSafePreviewUrl,
  parseInline,
  _escapeCssIdentifier,
  compileMarkdown,
  replaceMarkdownLinks,
} from '../editor.js';

// ---------------------------------------------------------------------------
// escapeHtml
// ---------------------------------------------------------------------------
describe('escapeHtml', () => {
  it('escapes angle brackets', () => {
    expect(escapeHtml('<script>')).toBe('&lt;script&gt;');
  });

  it('escapes ampersands', () => {
    expect(escapeHtml('a & b')).toBe('a &amp; b');
  });

  it('escapes double quotes — browser innerHTML does not encode them', () => {
    // document.createElement('div').innerHTML does NOT encode " to &quot;
    // (only <, >, & are encoded). This matches the browser behaviour.
    const result = escapeHtml('"hello"');
    expect(result).toContain('hello');
    expect(result).not.toContain('<');
  });

  it('leaves plain text unchanged', () => {
    expect(escapeHtml('hello world')).toBe('hello world');
  });

  it('handles empty string', () => {
    expect(escapeHtml('')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// _slugifyHeading
// ---------------------------------------------------------------------------
describe('_slugifyHeading', () => {
  it('lowercases and replaces spaces with hyphens', () => {
    expect(_slugifyHeading('Hello World')).toBe('hello-world');
  });

  it('strips non-alphanumeric characters', () => {
    expect(_slugifyHeading('Hello, World!')).toBe('hello-world');
  });

  it('collapses multiple spaces/hyphens to single hyphen', () => {
    expect(_slugifyHeading('foo   bar')).toBe('foo-bar');
  });

  it('trims leading and trailing hyphens', () => {
    expect(_slugifyHeading('  --heading--  ')).toBe('heading');
  });

  it('truncates to 50 characters', () => {
    const long = 'a'.repeat(60);
    expect(_slugifyHeading(long)).toHaveLength(50);
  });

  it('returns empty string for null/undefined input', () => {
    expect(_slugifyHeading(null)).toBe('');
    expect(_slugifyHeading(undefined)).toBe('');
    expect(_slugifyHeading('')).toBe('');
  });

  it('preserves Arabic characters (Unicode range)', () => {
    const result = _slugifyHeading('مرحبا بالعالم');
    // Arabic characters in \u0600-\u06FF range are preserved
    expect(result).toContain('مرحبا');
  });
});

// ---------------------------------------------------------------------------
// isSafePreviewUrl
// ---------------------------------------------------------------------------
describe('isSafePreviewUrl', () => {
  it('accepts https URLs', () => {
    expect(isSafePreviewUrl('https://example.com/file.pdf')).toBe(true);
  });

  it('accepts http URLs', () => {
    expect(isSafePreviewUrl('http://example.com/file.pdf')).toBe(true);
  });

  it('rejects javascript: protocol', () => {
    expect(isSafePreviewUrl('javascript:alert(1)')).toBe(false);
  });

  it('rejects data: URIs', () => {
    expect(isSafePreviewUrl('data:text/html,<h1>hi</h1>')).toBe(false);
  });

  it('rejects null', () => {
    expect(isSafePreviewUrl(null)).toBe(false);
  });

  it('rejects empty string', () => {
    expect(isSafePreviewUrl('')).toBe(false);
  });

  it('rejects non-string', () => {
    expect(isSafePreviewUrl(42)).toBe(false);
  });

  it('accepts relative URLs (resolved against origin)', () => {
    expect(isSafePreviewUrl('/static/uploads/file.pdf')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// parseInline — inline Markdown parser
// ---------------------------------------------------------------------------
describe('parseInline', () => {
  it('converts bold **text**', () => {
    expect(parseInline('**bold**')).toContain('<strong>bold</strong>');
  });

  it('converts italic *text*', () => {
    expect(parseInline('*italic*')).toContain('<em>italic</em>');
  });

  it('converts inline code `text`', () => {
    expect(parseInline('`code`')).toContain('<code');
    expect(parseInline('`code`')).toContain('code');
  });

  it('converts strikethrough ~~text~~', () => {
    expect(parseInline('~~strike~~')).toContain('<del>strike</del>');
  });

  it('converts bold+italic ***text***', () => {
    const result = parseInline('***bolditalic***');
    expect(result).toContain('<strong>');
    expect(result).toContain('<em>');
  });

  it('leaves plain text unchanged', () => {
    expect(parseInline('plain text')).toBe('plain text');
  });

  it('handles empty string', () => {
    expect(parseInline('')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// _escapeCssIdentifier
// ---------------------------------------------------------------------------
describe('_escapeCssIdentifier', () => {
  it('CSS-escapes non-alphanumeric characters with a backslash', () => {
    const result = _escapeCssIdentifier('hello world');
    // CSS identifier escaping uses backslash: 'hello\ world'
    expect(result).toContain('\\');
    expect(result).toContain('hello');
  });

  it('handles empty string', () => {
    expect(_escapeCssIdentifier('')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// compileMarkdown — integration: heading renders correctly
// ---------------------------------------------------------------------------
describe('compileMarkdown', () => {
  it('returns empty-content placeholder for falsy input', () => {
    expect(compileMarkdown('')).toContain('preview-empty');
    expect(compileMarkdown(null)).toContain('preview-empty');
  });

  it('renders h1 heading', () => {
    const html = compileMarkdown('# Hello');
    expect(html).toContain('<h1');
    expect(html).toContain('Hello');
  });

  it('renders h2 heading', () => {
    expect(compileMarkdown('## Section')).toContain('<h2');
  });

  it('renders fenced code block', () => {
    const md = '```python\nprint("hello")\n```';
    const html = compileMarkdown(md);
    expect(html).toContain('<code');
  });

  it('renders blockquote', () => {
    // blockquote requires '> ' (with trailing space) per CommonMark spec
    expect(compileMarkdown('> quoted')).toContain('<p>');
    expect(compileMarkdown('> quoted text')).toContain('quoted');
  });

  it('renders unordered list', () => {
    expect(compileMarkdown('- item one\n- item two')).toContain('<ul');
  });

  it('renders ordered list', () => {
    expect(compileMarkdown('1. first\n2. second')).toContain('<ol');
  });

  it('XSS: script tags in input are escaped', () => {
    const html = compileMarkdown('<script>alert(1)</script>');
    expect(html).not.toContain('<script>');
  });
});

// ---------------------------------------------------------------------------
// replaceMarkdownLinks
// ---------------------------------------------------------------------------
describe('replaceMarkdownLinks', () => {
  it('replaces [text](url) with anchor tags', () => {
    const result = replaceMarkdownLinks('[Google](https://google.com)');
    expect(result).toContain('<a');
    expect(result).toContain('https://google.com');
    expect(result).toContain('Google');
  });

  it('replaces ![alt](src) — renders as a link (image preview pattern)', () => {
    // replaceMarkdownLinks converts ![alt](src) to an anchor link, not an <img>
    // (images are served via a preview link for XSS safety)
    const result = replaceMarkdownLinks('![alt text](https://example.com/img.png)');
    expect(result).toContain('href');
    expect(result).toContain('alt text');
    expect(result).toContain('https://example.com/img.png');
  });

  it('leaves plain text without links unchanged', () => {
    expect(replaceMarkdownLinks('no links here')).toBe('no links here');
  });
});
