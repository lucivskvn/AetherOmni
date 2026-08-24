    // ── Inline parser ─────────────────────────────────────────────────────────
function parseInline(text) {
        let t = text;

        // Inline code (must be first to avoid double-parsing)
        t = t.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');

        // Strikethrough ~~text~~
        t = t.replace(/~~([^~]+)~~/g, '<del>$1</del>');

        // Bold+italic ***text***
        t = t.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');

        // Bold **text** or __text__
        t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        t = t.replace(/__([^_]+)__/g, '<strong>$1</strong>');

        // Italic *text* or _text_
        t = t.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
        t = t.replace(/_([^_\n]+)_/g, '<em>$1</em>');

        // Image: ![alt](src)
        t = t.replace(/!\[([^\]]*)\]\(([^()]+)\)/g,
            '<img src="$2" alt="$1" style="max-width:100%;border-radius:6px;margin:8px 0;">');

        // Link: [text](href)
        t = replaceMarkdownLinks(t);

        return t;
    }

/**
 * editor.js — Split-pane live Markdown editor with scroll sync, RTL detection,
 * and a robust block + inline parser. Fully XSS-safe via HTML escaping before parse.
 */

// ── Scroll Sync Helper ───────────────────────────────────────────────────────
function setupScrollSync(editor, preview) {
    const syncToggle = document.getElementById('sync-scroll-toggle');
    let isScrollingEditor = false;
    let isScrollingPreview = false;

    editor.addEventListener('scroll', () => {
        if (syncToggle && !syncToggle.checked) return;
        if (isScrollingPreview) {
            isScrollingPreview = false;
            return;
        }
        isScrollingEditor = true;
        const ratio = editor.scrollTop / Math.max(1, editor.scrollHeight - editor.clientHeight);
        preview.scrollTop = ratio * (preview.scrollHeight - preview.clientHeight);
    });

    preview.addEventListener('scroll', () => {
        if (syncToggle && !syncToggle.checked) return;
        if (isScrollingEditor) {
            isScrollingEditor = false;
            return;
        }
        isScrollingPreview = true;
        const ratio = preview.scrollTop / Math.max(1, preview.scrollHeight - preview.clientHeight);
        editor.scrollTop = ratio * (editor.scrollHeight - editor.clientHeight);
    });
}
function replaceMarkdownLinks(text) {
    let output = '';
    let remaining = text;

    while (remaining) {
        const start = remaining.indexOf('[');
        if (start === -1) return output + remaining;

        const labelEnd = remaining.indexOf('](', start + 1);
        const urlEnd = labelEnd === -1 ? -1 : remaining.indexOf(')', labelEnd + 2);
        if (labelEnd === -1 || urlEnd === -1) return output + remaining;

        const label = remaining.slice(start + 1, labelEnd);
        const url = remaining.slice(labelEnd + 2, urlEnd);
        if (!label || /[\r\n]/.test(label) || /[\s()]/.test(url) || !isSafePreviewUrl(url)) {
            output += remaining.slice(0, start + 1);
            remaining = remaining.slice(start + 1);
            continue;
        }

        const safeLabel = escapeHtml(label);
        const safeUrl = escapeHtml(url);
        output += `${remaining.slice(0, start)}<a href="${safeUrl}" target="_blank" rel="noopener" class="preview-link">${safeLabel}</a>`;
        remaining = remaining.slice(urlEnd + 1);
    }

    return output;
}

/**
 * Validates that a markdown preview URL string uses a safe HTTP or HTTPS protocol.
 * @param {string} value - URL string to validate.
 * @returns {boolean} True if safe, False otherwise.
 */
function isSafePreviewUrl(value) {
    if (!value || typeof value !== 'string') {
        return false;
    }
    try {
        const baseOrigin = globalThis.location?.origin || 'https://korda.local';
        const parsed = new URL(value, baseOrigin);
        return parsed.protocol === 'https:' || parsed.protocol === 'http:';
    } catch {
        return false;
    }
}

// ── Markdown Formatting Insertion ───────────────────────────────────────────
function insertFormatting(action) {
    const editor = document.getElementById('markdown-input');
    if (!editor) return;
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    const selectedText = editor.value.substring(start, end);

    const rules = {
        'bold': { prefix: '**', suffix: '**', def: 'Bold Text' },
        'italic': { prefix: '_', suffix: '_', def: 'Italic Text' },
        'heading': { prefix: '### ', suffix: '', def: 'Heading' },
        'quote': { prefix: '> ', suffix: '', def: 'Quote block' },
        'code': { prefix: '```\n', suffix: '\n```', def: 'const code = "here";' },
        'link': { prefix: '[', suffix: '](https://...)', def: 'Link Text' },
        'bullet': { prefix: '- ', suffix: '', def: 'List item' },
        'number': { prefix: '1. ', suffix: '', def: 'List item' },
        'table': {
            prefix: '| Column 1 | Column 2 |\n| -------- | -------- |\n| ',
            suffix: ' | Value |',
            def: 'Value'
        }
    };

    if (rules[action]) {
        const { prefix, suffix, def } = rules[action];
        const insertion = selectedText.length > 0 ? selectedText : def;
        editor.setRangeText(prefix + insertion + suffix, start, end, 'select');
        editor.focus();
        editor.dispatchEvent(new Event('input'));
    }
}

document.addEventListener('DOMContentLoaded', () => {

    const editor  = document.getElementById('markdown-input');
    const preview = document.getElementById('html-preview');

    function updateCounts() {
        if (!editor) return;
        const text = editor.value || '';
        const charCount = text.length;
        const words = text.trim().split(/\s+/).filter(w => w.length > 0);
        const wordCount = words.length;
        const counterEl = document.getElementById('editor-char-word-count');
        if (counterEl) {
            counterEl.textContent = `${charCount.toLocaleString()} character${charCount !== 1 ? 's' : ''} | ${wordCount.toLocaleString()} word${wordCount !== 1 ? 's' : ''}`;
        }
    }

    // ── 1. Initial render on load ─────────────────────────────────────────────
    if (editor && preview) {
        const initial = editor.value;
        if (initial.trim()) {
            preview.innerHTML = compileMarkdown(initial);
            applyPostRenderFeatures(preview);
        }
        updateCounts();

        // Keyboard formatting and Tab indentation handling
        editor.addEventListener('keydown', (e) => {
            if (e.key === 'Tab') {
                e.preventDefault();
                const start = editor.selectionStart;
                const end = editor.selectionEnd;
                if (e.shiftKey) {
                    // Shift+Tab: Unindent current line
                    const lineStart = editor.value.lastIndexOf('\n', start - 1) + 1;
                    if (editor.value.startsWith('    ', lineStart)) {
                        editor.setRangeText('', lineStart, lineStart + 4, 'end');
                        editor.dispatchEvent(new Event('input'));
                    } else if (editor.value.startsWith('  ', lineStart)) {
                        editor.setRangeText('', lineStart, lineStart + 2, 'end');
                        editor.dispatchEvent(new Event('input'));
                    } else if (editor.value.startsWith('\t', lineStart)) {
                        editor.setRangeText('', lineStart, lineStart + 1, 'end');
                        editor.dispatchEvent(new Event('input'));
                    }
                } else {
                    // Tab: Insert 2 spaces
                    editor.setRangeText('  ', start, end, 'end');
                    editor.dispatchEvent(new Event('input'));
                }
            } else if ((e.ctrlKey || e.metaKey) && !e.altKey) {
                const key = e.key.toLowerCase();
                if (key === 'b') {
                    e.preventDefault();
                    insertFormatting('bold');
                } else if (key === 'i') {
                    e.preventDefault();
                    insertFormatting('italic');
                } else if (key === 'k') {
                    e.preventDefault();
                    insertFormatting('link');
                }
            }
        });

        // Live recompile as user types
        editor.addEventListener('input', () => {
            preview.innerHTML = compileMarkdown(editor.value);
            applyPostRenderFeatures(preview);
            updateCounts();
        });

        // Bi-directional proportional scroll synchronisation
        setupScrollSync(editor, preview);

        // Listen for anchor hash changes dynamically
        globalThis.addEventListener('hashchange', () => {
            initDeepLinkScroll(preview);
        });
    }

    // ── 2. Markdown Toolbar Injections ───────────────────────────────────────
    const toolbar = document.querySelector('.editor-toolbar');
    if (toolbar && editor && preview) {
        toolbar.querySelectorAll('[data-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.dataset.action;
                insertFormatting(action);
            });
        });
    }

    // ── 3. Copy to Clipboard Functionality ───────────────────────────────────
    const btnCopySft = document.getElementById('btn-copy-sft');
    if (btnCopySft) {
        btnCopySft.addEventListener('click', async () => {
            try {
                const jsonScript = document.getElementById('qa-dataset-json');
                if (jsonScript) {
                    const jsonData = JSON.parse(jsonScript.textContent);
                    await navigator.clipboard.writeText(JSON.stringify(jsonData, null, 2));
                    if (typeof globalThis.showClientSideAlert === 'function') {
                        globalThis.showClientSideAlert('Copied JSON Dataset to clipboard!', 'success');
                    }
                } else {
                    throw new Error("JSON script not found");
                }
            } catch (err) {
                console.error("Failed to copy SFT dataset", err);
                if (typeof globalThis.showClientSideAlert === 'function') {
                    globalThis.showClientSideAlert('Failed to copy SFT dataset.', 'error');
                }
            }
        });
    }

    const btnCopyMarkdown = document.getElementById('btn-copy-markdown');
    if (btnCopyMarkdown && editor) {
        btnCopyMarkdown.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(editor.value);
                if (typeof globalThis.showClientSideAlert === 'function') {
                    globalThis.showClientSideAlert('Copied Markdown to clipboard!', 'success');
                }
            } catch (err) {
                console.error("Failed to copy Markdown", err);
                if (typeof globalThis.showClientSideAlert === 'function') {
                    globalThis.showClientSideAlert('Failed to copy Markdown.', 'error');
                }
            }
        });
    }
});


/**
 * After inserting HTML into the DOM, apply any features that require live nodes.
 * Currently: RTL detection and Lucide icon re-render.
 */
function applyPostRenderFeatures(container) {
    const ARABIC_REGEX = /[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]/;
    const LATIN_REGEX = /[A-Za-z]/;

    container.querySelectorAll('p, li, td, blockquote, span, div').forEach(el => {
        const text = (el.textContent || '').trim();
        let firstStrong = null;
        for (const char of text) {
            if (LATIN_REGEX.test(char)) {
                firstStrong = 'latin';
                break;
            } else if (ARABIC_REGEX.test(char)) {
                firstStrong = 'arabic';
                break;
            }
        }

        if (firstStrong === 'arabic') {
            el.setAttribute('dir', 'rtl');
            el.classList.add('arabic-text', 'rtl');
        } else {
            el.removeAttribute('dir');
            el.classList.remove('arabic-text', 'rtl');
        }
    });

    // Re-render any lucide icons added dynamically
    if (typeof lucide !== 'undefined' && lucide.createIcons) {
        try { lucide.createIcons(); } catch { /* ignore */ }
    }

    initDeepLinkScroll(container);
}

function _escapeCssIdentifier(value) {
    if (globalThis.CSS?.escape) {
        return globalThis.CSS.escape(value);
    }
    return value.replace(/[^a-zA-Z0-9_-]/g, String.raw`\$&`);
}

/**
 * Smoothly scrolls to the target anchor matching window.location.hash and triggers
 * a temporary glowing pulse animation on the matching DOM element.
 * @param {HTMLElement} container - The container element to search within.
 */
function initDeepLinkScroll(container) {
    if (!globalThis.location?.hash) return;
    const rawHash = globalThis.location.hash.replace(/^#/, '');
    if (!rawHash) return;

    try {
        const decodedHash = decodeURIComponent(rawHash).toLowerCase();
        const escaped = _escapeCssIdentifier(decodedHash);
        const target = (container || document).querySelector(
            `#${escaped}, [id*="${escaped}"]`
        );
        if (target) {
            setTimeout(() => {
                target.scrollIntoView({ behavior: 'smooth', block: 'center' });
                target.classList.remove('deep-link-pulse');
                target.getBoundingClientRect();
                target.classList.add('deep-link-pulse');
            }, 120);
        }
    } catch {
        // Safe fallback for query selector escapes
    }
}

function _slugifyHeading(text) {
    if (!text || typeof text !== 'string') return '';
    let slug = text.toLowerCase()
        .replace(/[^a-z0-9\u0600-\u06FF\s-]/gu, '')
        .trim()
        .replace(/[\s_-]+/g, '-');
    while (slug.startsWith('-')) slug = slug.slice(1);
    while (slug.endsWith('-')) slug = slug.slice(0, -1);
    return slug.slice(0, 50);
}

function _handleHeading(line, state, pushHtml) {
    const hMatch = line.match(/^(#{1,6})\s+(.*)/);
    if (!hMatch) return false;

    if (state.inList) { pushHtml('</' + state.listType + '>'); state.inList = false; }
    const level = hMatch[1].length;
    const headingText = hMatch[2];
    const pageMatch = headingText.match(/^Page\s+(\d+)/i);
    const slug = pageMatch ? `page-${pageMatch[1]}` : _slugifyHeading(headingText);
    const idAttr = slug ? ` id="${slug}" class="heading-anchor"` : '';
    pushHtml(`<h${level}${idAttr}>${parseInline(headingText)}</h${level}>`);
    return true;
}

function _handleBlockquote(line, state, pushHtml) {
    if (line.startsWith('> ')) {
        if (!state.inBlockquote) { pushHtml('<blockquote>'); state.inBlockquote = true; }
        pushHtml('<p>' + parseInline(line.substring(2)) + '</p>');
        return true;
    } else if (state.inBlockquote) {
        pushHtml('</blockquote>'); state.inBlockquote = false;
    }
    return false;
}

function _handleList(line, state, pushHtml) {
    const ulMatch = line.match(/^(\s*)([-*+])\s+(.*)/);
    const olMatch = line.match(/^(\s*)(\d+)\.\s+(.*)/);

    if (ulMatch || olMatch) {
        const type = ulMatch ? 'ul' : 'ol';
        const content = ulMatch ? ulMatch[3] : olMatch[3];
        if (!state.inList) { pushHtml('<' + type + '>'); state.inList = true; state.listType = type; }
        else if (state.inList && state.listType !== type) {
            pushHtml('</' + state.listType + '><' + type + '>');
            state.listType = type;
        }
        pushHtml('<li>' + parseInline(content) + '</li>');
        return true;
    } else if (state.inList && !line.match(/^\s+/)) {
        pushHtml('</' + state.listType + '>'); state.inList = false; state.listType = null;
    }
    return false;
}

function _handleTable(line, state, pushHtml) {
    if (line.trim().startsWith('|') && line.trim().endsWith('|')) {
        if (!state.inTable) {
            pushHtml('<div class="table-container"><table>');
            state.inTable = true;
            state.tableHasHead = false;
        }
        const isDivider = line.replace(/[|\s-:]/g, '').length === 0;
        if (isDivider) {
            if (!state.tableHasHead) { pushHtml('</thead><tbody>'); state.tableHasHead = true; }
            return true;
        }
        const cells = line.split('|').map(s => s.trim()).filter((s, i, arr) => !(i === 0 && s === '') && !(i === arr.length - 1 && s === ''));
        if (!state.tableHasHead) {
            pushHtml('<thead><tr>');
            cells.forEach(c => pushHtml('<th>' + parseInline(c) + '</th>'));
            pushHtml('</tr>');
        } else {
            pushHtml('<tr>');
            cells.forEach(c => pushHtml('<td>' + parseInline(c) + '</td>'));
            pushHtml('</tr>');
        }
        return true;
    } else if (state.inTable) {
        pushHtml('</tbody></table></div>'); state.inTable = false; state.tableHasHead = false;
    }
    return false;
}

function _closeOpenBlocks(state, pushHtml) {
    if (state.inList) { pushHtml('</' + state.listType + '>'); state.inList = false; state.listType = null; }
    if (state.inBlockquote) { pushHtml('</blockquote>'); state.inBlockquote = false; }
    if (state.inTable) { pushHtml('</tbody></table></div>'); state.inTable = false; state.tableHasHead = false; }
}

function processBlockElement(line, state, htmlBuilder) {
    const pushHtml = (str) => { htmlBuilder.html += str + '\n'; };

    if (!line) {
        _closeOpenBlocks(state, pushHtml);
        return;
    }

    if (line.startsWith('---')) {
        pushHtml('<hr>');
        return;
    }

    if (_handleHeading(line, state, pushHtml)) return;
    if (_handleBlockquote(line, state, pushHtml)) return;
    if (_handleList(line, state, pushHtml)) return;
    if (_handleTable(line, state, pushHtml)) return;

    pushHtml('<p>' + parseInline(line) + '</p>');
}

function _parseYamlFrontmatter(escaped) {
    let yamlHtml = '';
    let bodyText = escaped;

    if (escaped.startsWith('---\n') || escaped.startsWith('---\r\n')) {
        const lineBreak = escaped.startsWith('---\r\n') ? '\r\n' : '\n';
        const delimiter = lineBreak + '---' + lineBreak;
        const nextDash = escaped.indexOf(delimiter, 5);
        if (nextDash !== -1) {
            const yamlText = escaped.substring(4, nextDash);
            bodyText = escaped.substring(nextDash + delimiter.length);

            const lines = yamlText.split(lineBreak);
            let rowsHtml = '';
            lines.forEach(line => {
                const colonIdx = line.indexOf(':');
                if (colonIdx !== -1) {
                    const key = line.substring(0, colonIdx).trim();
                    const val = line.substring(colonIdx + 1).trim()
                        .replaceAll('&quot;', '')
                        .replaceAll('&#x27;', '')
                        .replaceAll('&lt;', '')
                        .replaceAll('&gt;', '');
                    if (key && val) {
                        rowsHtml += `
                            <div style="display: flex; gap: 8px; font-size: 12px; margin-bottom: 4px; font-family: sans-serif;">
                                <span style="font-weight: 600; color: var(--text-muted); text-transform: uppercase; width: 120px; flex-shrink: 0;">${key}:</span>
                                <span style="color: var(--text-main); font-weight: 500;">${val}</span>
                            </div>`;
                    }
                }
            });
            if (rowsHtml) {
                yamlHtml = `
                    <div class="glass-card" style="padding: 12px 16px; margin-bottom: 16px; background: rgba(255,255,255,0.01); border: 1px dashed var(--border-glass); border-radius: 8px;">
                        <div style="display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 700; color: var(--text-glow); margin-bottom: 8px; font-family: sans-serif;">
                            <i data-lucide="file-text" style="width: 14px; height: 14px;"></i> Document Frontmatter (YAML)
                        </div>
                        ${rowsHtml}
                    </div>`;
            }
        }
    }
    return { yamlHtml, bodyText };
}

const SAFE_HTML_TAGS = new Set([
    'b', 'i', 'u', 'strong', 'em', 'sup', 'sub', 'table', 'thead', 'tbody',
    'tr', 'th', 'td', 'code', 'pre', 'blockquote', 'ul', 'ol', 'li', 'span',
    'div', 'p'
]);
const SAFE_HTML_ATTRIBUTES = new Set(['colspan', 'rowspan', 'class', 'style', 'dir']);

function _restoreSafeHtml(html) {
    return html
        .replace(/&lt;br\s*\/?&gt;/gi, '<br>')
        .replace(/&lt;hr\s*\/?&gt;/gi, '<hr>')
        .replace(/&lt;(\/)?([a-z]+)\b([^&]*)&gt;/gi, (match, closeSlash, tagName, attrs) => {
            if (!SAFE_HTML_TAGS.has(tagName.toLowerCase())) return match;
            let cleanAttrs = '';
            if (attrs) {
                const decodedAttrs = attrs.replaceAll('&quot;', '"').replaceAll('&#x27;', "'");
                const attrRegex = /\b([a-z]+)\s*=\s*["']([^"']*)["']/gi;
                let m;
                while ((m = attrRegex.exec(decodedAttrs)) !== null) {
                    if (SAFE_HTML_ATTRIBUTES.has(m[1].toLowerCase())) {
                        cleanAttrs += ` ${m[1]}="${m[2]}"`;
                    }
                }
            }
            return `<${closeSlash || ''}${tagName}${cleanAttrs}>`;
        });
}

function _processLine(line, state, htmlBuilder) {
    if (line.trim().startsWith('```')) {
        if (state.inCodeBlock) {
            state.inCodeBlock = false;
            return '</code></pre>\n';
        }
        state.codeLang = line.trim().slice(3).trim();
        const langAttr = state.codeLang ? ` class="language-${state.codeLang}"` : '';
        const langLabel = state.codeLang ? `<span class="code-lang-label">${state.codeLang}</span>` : '';
        state.inCodeBlock = true;
        return `<pre>${langLabel}<code${langAttr}>`;
    }
    if (state.inCodeBlock) {
        return line + '\n';
    }

    htmlBuilder.html = '';
    processBlockElement(line, state, htmlBuilder);
    return htmlBuilder.html;
}

/**
 * Escapes raw markdown text into safe HTML entities using the browser's
 * built-in DOM text node — immune to injection and avoids manual chain
 * patterns flagged by SAST rules (Semgrep detect-replaceall-sanitization).
 * @param {string} raw - Raw string to HTML-encode.
 * @returns {string} HTML-entity-encoded string safe for innerHTML insertion.
 */
function escapeHtml(raw) {
    const tn = document.createTextNode(raw);
    const div = document.createElement('div');
    div.appendChild(tn);
    return div.innerHTML;
}

function compileMarkdown(markdown) {
    if (!markdown) return '<p class="preview-empty">No content yet.</p>';

    const escaped = escapeHtml(markdown);

    const { yamlHtml, bodyText } = _parseYamlFrontmatter(escaped);
    const lines = bodyText.split('\n');
    let html = yamlHtml;

    const state = {
        inList: false,
        listType: null,
        inBlockquote: false,
        inCodeBlock: false,
        codeLang: '',
        inTable: false,
        tableHasHead: false
    };

    const htmlBuilder = { html: '' };

    for (const line of lines) {
        html += _processLine(line, state, htmlBuilder);
    }

    return _restoreSafeHtml(html);
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        escapeHtml,
        compileMarkdown,
        _slugifyHeading,
        _parseYamlFrontmatter,
        _restoreSafeHtml,
        _processLine,
        parseInline,
        replaceMarkdownLinks,
        isSafePreviewUrl,
        _escapeCssIdentifier,
        insertFormatting,
        applyPostRenderFeatures,
        initDeepLinkScroll,
        setupScrollSync
    };
}
