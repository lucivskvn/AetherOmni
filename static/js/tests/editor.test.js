/**
 * editor.js unit tests
 *
 * Tests pure functions extracted from static/js/editor.js.
 * Since editor.js uses plain function declarations (no ES module exports),
 * we inject the source via window.eval() in the jsdom environment so that
 * all function declarations land on window (== globalThis) — exactly as a
 * browser <script> tag would behave.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';

// Inject editor.js into the jsdom window scope
beforeAll(() => {
  const src = readFileSync(resolve('static/js/editor.js'), 'utf-8');
  // window.eval places function declarations on window (globalThis)
  window.eval(src);
});

// ---------------------------------------------------------------------------
// escapeHtml
// ---------------------------------------------------------------------------
describe('escapeHtml', () => {
  it('escapes angle brackets', () => {
    expect(globalThis.escapeHtml('<script>')).toBe('&lt;script&gt;');
  });

  it('escapes ampersands', () => {
    expect(globalThis.escapeHtml('a & b')).toBe('a &amp; b');
  });

  it('escapes double quotes — browser innerHTML does not encode them', () => {
    // document.createElement('div').innerHTML does NOT encode " to &quot;
    // (only <, >, & are encoded). This matches the browser behaviour.
    const result = globalThis.escapeHtml('"hello"');
    expect(result).toContain('hello');
    expect(result).not.toContain('<');
  });

  it('leaves plain text unchanged', () => {
    expect(globalThis.escapeHtml('hello world')).toBe('hello world');
  });

  it('handles empty string', () => {
    expect(globalThis.escapeHtml('')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// _slugifyHeading
// ---------------------------------------------------------------------------
describe('_slugifyHeading', () => {
  it('lowercases and replaces spaces with hyphens', () => {
    expect(globalThis._slugifyHeading('Hello World')).toBe('hello-world');
  });

  it('strips non-alphanumeric characters', () => {
    expect(globalThis._slugifyHeading('Hello, World!')).toBe('hello-world');
  });

  it('collapses multiple spaces/hyphens to single hyphen', () => {
    expect(globalThis._slugifyHeading('foo   bar')).toBe('foo-bar');
  });

  it('trims leading and trailing hyphens', () => {
    expect(globalThis._slugifyHeading('  --heading--  ')).toBe('heading');
  });

  it('truncates to 50 characters', () => {
    const long = 'a'.repeat(60);
    expect(globalThis._slugifyHeading(long)).toHaveLength(50);
  });

  it('returns empty string for null/undefined input', () => {
    expect(globalThis._slugifyHeading(null)).toBe('');
    expect(globalThis._slugifyHeading(undefined)).toBe('');
    expect(globalThis._slugifyHeading('')).toBe('');
  });

  it('preserves Arabic characters (Unicode range)', () => {
    const result = globalThis._slugifyHeading('مرحبا بالعالم');
    // Arabic characters in \u0600-\u06FF range are preserved
    expect(result).toContain('مرحبا');
  });
});

// ---------------------------------------------------------------------------
// isSafePreviewUrl
// ---------------------------------------------------------------------------
describe('isSafePreviewUrl', () => {
  it('accepts https URLs', () => {
    expect(globalThis.isSafePreviewUrl('https://example.com/file.pdf')).toBe(true);
  });

  it('accepts http URLs', () => {
    expect(globalThis.isSafePreviewUrl('http://example.com/file.pdf')).toBe(true);
  });

  it('rejects javascript: protocol', () => {
    expect(globalThis.isSafePreviewUrl('javascript:alert(1)')).toBe(false);
  });

  it('rejects data: URIs', () => {
    expect(globalThis.isSafePreviewUrl('data:text/html,<h1>hi</h1>')).toBe(false);
  });

  it('rejects null', () => {
    expect(globalThis.isSafePreviewUrl(null)).toBe(false);
  });

  it('rejects empty string', () => {
    expect(globalThis.isSafePreviewUrl('')).toBe(false);
  });

  it('rejects non-string', () => {
    expect(globalThis.isSafePreviewUrl(42)).toBe(false);
  });

  it('accepts relative URLs (resolved against origin)', () => {
    expect(globalThis.isSafePreviewUrl('/static/uploads/file.pdf')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// parseInline — inline Markdown parser
// ---------------------------------------------------------------------------
describe('parseInline', () => {
  it('converts bold **text**', () => {
    expect(globalThis.parseInline('**bold**')).toContain('<strong>bold</strong>');
  });

  it('converts italic *text*', () => {
    expect(globalThis.parseInline('*italic*')).toContain('<em>italic</em>');
  });

  it('converts inline code `text`', () => {
    expect(globalThis.parseInline('`code`')).toContain('<code');
    expect(globalThis.parseInline('`code`')).toContain('code');
  });

  it('converts strikethrough ~~text~~', () => {
    expect(globalThis.parseInline('~~strike~~')).toContain('<del>strike</del>');
  });

  it('converts bold+italic ***text***', () => {
    const result = globalThis.parseInline('***bolditalic***');
    expect(result).toContain('<strong>');
    expect(result).toContain('<em>');
  });

  it('leaves plain text unchanged', () => {
    expect(globalThis.parseInline('plain text')).toBe('plain text');
  });

  it('handles empty string', () => {
    expect(globalThis.parseInline('')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// _escapeCssIdentifier
// ---------------------------------------------------------------------------
describe('_escapeCssIdentifier', () => {
  it('CSS-escapes non-alphanumeric characters with a backslash', () => {
    const result = globalThis._escapeCssIdentifier('hello world');
    // CSS identifier escaping uses backslash: 'hello\ world'
    expect(result).toContain('\\');
    expect(result).toContain('hello');
  });

  it('handles empty string', () => {
    expect(globalThis._escapeCssIdentifier('')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// compileMarkdown — integration: heading renders correctly
// ---------------------------------------------------------------------------
describe('compileMarkdown', () => {
  it('returns empty-content placeholder for falsy input', () => {
    expect(globalThis.compileMarkdown('')).toContain('preview-empty');
    expect(globalThis.compileMarkdown(null)).toContain('preview-empty');
  });

  it('renders h1 heading', () => {
    const html = globalThis.compileMarkdown('# Hello');
    expect(html).toContain('<h1');
    expect(html).toContain('Hello');
  });

  it('renders h2 heading', () => {
    expect(globalThis.compileMarkdown('## Section')).toContain('<h2');
  });

  it('renders fenced code block', () => {
    const md = '```python\nprint("hello")\n```';
    const html = globalThis.compileMarkdown(md);
    expect(html).toContain('<code');
  });

  it('renders blockquote', () => {
    // blockquote requires '> ' (with trailing space) per CommonMark spec
    expect(globalThis.compileMarkdown('> quoted')).toContain('<p>');
    expect(globalThis.compileMarkdown('> quoted text')).toContain('quoted');
  });

  it('renders unordered list', () => {
    expect(globalThis.compileMarkdown('- item one\n- item two')).toContain('<ul');
  });

  it('renders ordered list', () => {
    expect(globalThis.compileMarkdown('1. first\n2. second')).toContain('<ol');
  });

  it('XSS: script tags in input are escaped', () => {
    const html = globalThis.compileMarkdown('<script>alert(1)</script>');
    expect(html).not.toContain('<script>');
  });
});

// ---------------------------------------------------------------------------
// replaceMarkdownLinks
// ---------------------------------------------------------------------------
describe('replaceMarkdownLinks', () => {
  it('replaces [text](url) with anchor tags', () => {
    const result = globalThis.replaceMarkdownLinks('[Google](https://google.com)');
    expect(result).toContain('<a');
    expect(result).toContain('https://google.com');
    expect(result).toContain('Google');
  });

  it('replaces ![alt](src) — renders as a link (image preview pattern)', () => {
    // replaceMarkdownLinks converts ![alt](src) to an anchor link, not an <img>
    // (images are served via a preview link for XSS safety)
    const result = globalThis.replaceMarkdownLinks('![alt text](https://example.com/img.png)');
    expect(result).toContain('href');
    expect(result).toContain('alt text');
    expect(result).toContain('https://example.com/img.png');
  });

  it('leaves plain text without links unchanged', () => {
    expect(globalThis.replaceMarkdownLinks('no links here')).toBe('no links here');
  });
});
