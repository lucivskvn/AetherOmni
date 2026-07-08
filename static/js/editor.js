/**
 * editor.js — Split-pane live Markdown editor with scroll sync, RTL detection,
 * and a robust block + inline parser. Fully XSS-safe via HTML escaping before parse.
 */

document.addEventListener('DOMContentLoaded', () => {
    const editor  = document.getElementById('markdown-input');
    const preview = document.getElementById('html-preview');
    const deleteForm = document.getElementById('delete-document-form');
    const editorForm = document.getElementById('editor-form');

    // ── 1. Initial render on load ─────────────────────────────────────────────
    if (editor && preview) {
        const initial = editor.value;
        if (initial.trim()) {
            preview.innerHTML = compileMarkdown(initial);
            applyPostRenderFeatures(preview);
        }

        // Live recompile as user types
        editor.addEventListener('input', () => {
            preview.innerHTML = compileMarkdown(editor.value);
            applyPostRenderFeatures(preview);
        });

        // Bi-directional proportional scroll synchronisation
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

    function insertFormatting(action) {
        if (!editor) return;
        const start = editor.selectionStart;
        const end = editor.selectionEnd;
        const text = editor.value;
        const selected = text.substring(start, end);
        let insertion = '';
        let selectionOffset = 0;

        switch(action) {
            case 'bold':
                insertion = `**${selected || 'bold text'}**`;
                selectionOffset = selected ? 0 : 2;
                break;
            case 'italic':
                insertion = `*${selected || 'italic text'}*`;
                selectionOffset = selected ? 0 : 1;
                break;
            case 'heading':
                insertion = `\n# ${selected || 'Heading'}\n`;
                selectionOffset = selected ? 0 : 2;
                break;
            case 'quote':
                insertion = `\n> ${selected || 'Blockquote'}\n`;
                selectionOffset = selected ? 0 : 2;
                break;
            case 'code':
                insertion = `\n\`\`\`\n${selected || 'code block'}\n\`\`\`\n`;
                selectionOffset = selected ? 0 : 4;
                break;
            case 'link':
                insertion = `[${selected || 'link text'}](https://example.com)`;
                selectionOffset = selected ? 0 : 1;
                break;
            case 'table':
                insertion = `\n| Header 1 | Header 2 |\n| --- | --- |\n| Cell 1 | Cell 2 |\n`;
                break;
            case 'bullet':
                insertion = `\n- ${selected || 'List item'}`;
                selectionOffset = selected ? 0 : 2;
                break;
            case 'number':
                insertion = `\n1. ${selected || 'List item'}`;
                selectionOffset = selected ? 0 : 3;
                break;
        }

        editor.value = text.substring(0, start) + insertion + text.substring(end);
        editor.focus();
        if (selected) {
            editor.setSelectionRange(start, start + insertion.length);
        } else {
            const cursorPosition = start + insertion.length - selectionOffset - (action === 'link' ? 21 : 0);
            editor.setSelectionRange(cursorPosition, cursorPosition);
        }
        
        // Refresh preview and state
        preview.innerHTML = compileMarkdown(editor.value);
        applyPostRenderFeatures(preview);
        markUnsaved();
    }

    // ── 3. Keyboard Shortcuts (Ctrl+B, Ctrl+I, Ctrl+S) ───────────────────────
    if (editor) {
        editor.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
                e.preventDefault();
                insertFormatting('bold');
            } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'i') {
                e.preventDefault();
                insertFormatting('italic');
            } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
                e.preventDefault();
                if (editorForm) {
                    editorForm.submit();
                }
            }
        });
    }

    // ── 4. Fullscreen Curation workspace Toggle ───────────────────────────────
    const fullscreenToggle = document.getElementById('btn-fullscreen-toggle');
    const workspaceGrid = document.getElementById('curation-workspace-grid');
    if (fullscreenToggle && workspaceGrid) {
        fullscreenToggle.addEventListener('click', () => {
            workspaceGrid.classList.toggle('fullscreen');
            if (workspaceGrid.classList.contains('fullscreen')) {
                fullscreenToggle.title = "Exit Fullscreen Curation";
                fullscreenToggle.innerHTML = '<i data-lucide="minimize-2" style="width:15px; height:15px;"></i>';
            } else {
                fullscreenToggle.title = "Toggle Fullscreen Curation";
                fullscreenToggle.innerHTML = '<i data-lucide="maximize-2" style="width:15px; height:15px;"></i>';
            }
            if (typeof lucide !== 'undefined' && lucide.createIcons) {
                try { lucide.createIcons(); } catch (_) {}
            }
        });
    }

    // ── 5. Unsaved changes warning & RTL Direction detection ─────────────────
    const unsavedBadge = document.getElementById('unsaved-badge');
    const savedBadge = document.getElementById('saved-badge');
    const initialContent = editor ? editor.value : '';
    const titleInput = document.getElementById('doc-title-input');
    const authorInput = document.querySelector('input[name="author"]');
    const langInput = document.querySelector('input[name="language"]');
    
    const initialTitle = titleInput ? titleInput.value : '';
    const initialAuthor = authorInput ? authorInput.value : '';
    const initialLang = langInput ? langInput.value : '';
    
    let isUnsaved = false;

    function detectEditorDirection() {
        if (!editor) return;
        const text = (editor.value || '').trim();
        const ARABIC_REGEX = /[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]/;
        const LATIN_REGEX = /[A-Za-z]/;
        
        let firstStrong = null;
        for (let i = 0; i < text.length; i++) {
            const char = text[i];
            if (LATIN_REGEX.test(char)) {
                firstStrong = 'latin';
                break;
            } else if (ARABIC_REGEX.test(char)) {
                firstStrong = 'arabic';
                break;
            }
        }
        
        if (firstStrong === 'arabic') {
            editor.dir = 'rtl';
            editor.classList.add('rtl');
            editor.style.textAlign = 'right';
            editor.style.fontFamily = "'Scheherazade New', 'Amiri', 'Noto Sans Arabic', var(--font-body), sans-serif";
            editor.style.fontSize = "18px";
        } else {
            editor.dir = 'ltr';
            editor.classList.remove('rtl');
            editor.style.textAlign = 'left';
            editor.style.fontFamily = "'JetBrains Mono', 'Fira Code', 'Courier New', monospace";
            editor.style.fontSize = "13.5px";
        }
    }

    // Run direction detection initially
    if (editor) {
        detectEditorDirection();
    }

    function checkDirty() {
        const contentDirty = editor && editor.value !== initialContent;
        const titleDirty = titleInput && titleInput.value !== initialTitle;
        const authorDirty = authorInput && authorInput.value !== initialAuthor;
        const langDirty = langInput && langInput.value !== initialLang;
        
        if (editor) {
            detectEditorDirection();
        }
        
        if (contentDirty || titleDirty || authorDirty || langDirty) {
            markUnsaved();
        } else {
            clearUnsaved();
        }
    }

    if (editor) {
        editor.addEventListener('input', checkDirty);
    }
    if (titleInput) {
        titleInput.addEventListener('input', checkDirty);
    }
    if (authorInput) {
        authorInput.addEventListener('input', checkDirty);
    }
    if (langInput) {
        langInput.addEventListener('input', checkDirty);
    }

    function markUnsaved() {
        isUnsaved = true;
        if (unsavedBadge) unsavedBadge.style.display = 'inline-flex';
        if (savedBadge) savedBadge.style.display = 'none';
    }

    function clearUnsaved() {
        isUnsaved = false;
        if (unsavedBadge) unsavedBadge.style.display = 'none';
        if (savedBadge) savedBadge.style.display = 'inline-flex';
    }

    window.addEventListener('beforeunload', (e) => {
        if (isUnsaved) {
            e.preventDefault();
            e.returnValue = 'You have unsaved changes in your document editor. Are you sure you want to leave?';
        }
    });

    if (editorForm) {
        editorForm.addEventListener('submit', () => {
            isUnsaved = false;
        });
    }

    // ── 6. SFT Training Dataset copy-to-clipboard ────────────────────────────
    const copySftBtn = document.getElementById('btn-copy-sft');
    if (copySftBtn) {
        copySftBtn.addEventListener('click', () => {
            const datasetNode = document.getElementById('qa-dataset-json');
            if (!datasetNode) return;
            try {
                const dataset = JSON.parse(datasetNode.textContent);
                const formatted = dataset.map(qa => ({
                    messages: [
                        { role: "user", content: qa.question },
                        { role: "assistant", content: qa.answer }
                    ]
                }));
                const jsonText = JSON.stringify(formatted, null, 2);
                navigator.clipboard.writeText(jsonText)
                    .then(() => {
                        const origText = copySftBtn.innerHTML;
                        copySftBtn.innerHTML = '<i data-lucide="check" style="width:14px; height:14px;"></i> Copied!';
                        if (typeof lucide !== 'undefined' && lucide.createIcons) {
                            try { lucide.createIcons(); } catch (_) {}
                        }
                        setTimeout(() => {
                            copySftBtn.innerHTML = origText;
                            if (typeof lucide !== 'undefined' && lucide.createIcons) {
                                try { lucide.createIcons(); } catch (_) {}
                            }
                        }, 2000);
                    })
                    .catch(err => {
                        alert('Failed to copy SFT JSON: ' + err);
                    });
            } catch (e) {
                alert('Failed to parse Q&A dataset: ' + e);
            }
        });
    }

    // ── 7. Delete confirmation ────────────────────────────────────────────────
    if (deleteForm) {
        deleteForm.addEventListener('submit', (e) => {
            if (!confirm('Are you sure you want to permanently delete this document?')) {
                e.preventDefault();
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
        for (let i = 0; i < text.length; i++) {
            const char = text[i];
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
            el.classList.add('arabic-text');
            el.classList.add('rtl');
        } else {
            el.removeAttribute('dir');
            el.classList.remove('arabic-text');
            el.classList.remove('rtl');
        }
    });

    // Re-render any lucide icons added dynamically
    if (typeof lucide !== 'undefined' && lucide.createIcons) {
        try { lucide.createIcons(); } catch (_) { /* ignore */ }
    }
}

/**
 * Robust line-by-line Markdown compiler.
 * Parses the full CommonMark subset used by our pipeline:
 *   - Headings h1–h4, Paragraphs, Blank lines
 *   - Fenced code blocks (with optional language label)
 *   - Blockquotes, Unordered lists, Ordered lists
 *   - Pipe tables (GitHub-flavoured)
 *   - Horizontal rules
 *   - Inline: bold, italic, inline-code, links, images, strikethrough
 * Returns sanitised HTML (XSS-safe via upfront escaping).
 */
function compileMarkdown(markdown) {
    if (!markdown) return '<p class="preview-empty">No content yet.</p>';

    // ── Step 1: Escape raw HTML for XSS safety ───────────────────────────────
    let escaped = markdown
        .replace(/&/g,  '&amp;')
        .replace(/</g,  '&lt;')
        .replace(/>/g,  '&gt;')
        .replace(/"/g,  '&quot;');

    let yamlHtml = '';
    let bodyText = escaped;

    // Check if markdown starts with YAML frontmatter
    if (escaped.startsWith('---\n') || escaped.startsWith('---\r\n')) {
        const lineBreak = escaped.startsWith('---\r\n') ? '\r\n' : '\n';
        const delimiter = lineBreak + '---' + lineBreak;
        const nextDash = escaped.indexOf(delimiter, 5);
        if (nextDash !== -1) {
            const yamlText = escaped.substring(4, nextDash);
            bodyText = escaped.substring(nextDash + delimiter.length);

            // Parse YAML keys and values
            const lines = yamlText.split(lineBreak);
            let rowsHtml = '';
            lines.forEach(line => {
                const colonIdx = line.indexOf(':');
                if (colonIdx !== -1) {
                    const key = line.substring(0, colonIdx).trim();
                    const val = line.substring(colonIdx + 1).trim().replace(/^[&quot;&apos;&lt;&gt;]*|[&quot;&apos;&lt;&gt;]*$/g, ''); // strip quotes
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

    const lines = bodyText.split('\n');
    let html = yamlHtml;

    // Parser state
    let inList        = false;
    let listType      = null;    // 'ul' | 'ol'
    let inBlockquote  = false;
    let inCodeBlock   = false;
    let codeLang      = '';
    let inTable       = false;
    let tableHasHead  = false;

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const trimmed = line.trim();

        // ── Code fence ───────────────────────────────────────────────────────
        if (trimmed.startsWith('```')) {
            if (inCodeBlock) {
                html += '</code></pre>\n';
                inCodeBlock = false;
                codeLang = '';
            } else {
                html += closeAll();
                codeLang = trimmed.slice(3).trim();
                const langAttr = codeLang ? ` class="language-${codeLang}"` : '';
                const langLabel = codeLang
                    ? `<span class="code-lang-label">${codeLang}</span>`
                    : '';
                html += `<pre>${langLabel}<code${langAttr}>`;
                inCodeBlock = true;
            }
            continue;
        }
        if (inCodeBlock) {
            html += line + '\n';
            continue;
        }

        // ── Pipe table ───────────────────────────────────────────────────────
        const isTableRow = trimmed.startsWith('|') && trimmed.endsWith('|');
        if (isTableRow) {
            html += closeLists() + closeBlockquote();

            // Separator row |---|:---:|
            if (/^\|[\s|:-]+\|$/.test(trimmed)) {
                if (!inTable) { inTable = true; tableHasHead = false; html += '<table>\n'; }
                if (!tableHasHead) {
                    tableHasHead = true;
                    // Retroactively close <thead> that we opened speculatively
                    // (the row before separator was already emitted as <th>)
                    html += '</tr></thead>\n<tbody>\n';
                }
                continue;
            }

            const cells = trimmed
                .slice(1, -1)               // strip leading/trailing |
                .split('|')
                .map(c => c.trim());

            if (!inTable) {
                inTable = true;
                tableHasHead = false;
                html += '<table>\n<thead>\n<tr>\n';
                cells.forEach(c => { html += `<th>${parseInline(c)}</th>\n`; });
                // Don't close <tr>/<thead> yet — wait for separator row
            } else if (!tableHasHead) {
                // Still in header (no separator seen yet)
                html += '<tr>\n';
                cells.forEach(c => { html += `<th>${parseInline(c)}</th>\n`; });
            } else {
                html += '<tr>\n';
                cells.forEach(c => { html += `<td>${parseInline(c)}</td>\n`; });
                html += '</tr>\n';
            }
            continue;
        } else if (inTable) {
            html += '</tbody>\n</table>\n';
            inTable = false;
            tableHasHead = false;
        }

        // ── Blockquote ───────────────────────────────────────────────────────
        if (trimmed.startsWith('&gt; ')) {
            html += closeLists();
            if (!inBlockquote) { html += '<blockquote>\n'; inBlockquote = true; }
            html += `<p>${parseInline(trimmed.slice(5))}</p>\n`;
            continue;
        } else {
            html += closeBlockquote();
        }

        // ── Ordered / Unordered lists ─────────────────────────────────────────
        const ulM = trimmed.match(/^[-*+]\s+(.*)$/);
        const olM = trimmed.match(/^\d+\.\s+(.*)$/);

        if (ulM) {
            if (!inList || listType !== 'ul') {
                html += closeLists();
                html += '<ul>\n';
                inList = true; listType = 'ul';
            }
            html += `<li>${parseInline(ulM[1])}</li>\n`;
            continue;
        }
        if (olM) {
            if (!inList || listType !== 'ol') {
                html += closeLists();
                html += '<ol>\n';
                inList = true; listType = 'ol';
            }
            html += `<li>${parseInline(olM[1])}</li>\n`;
            continue;
        }
        html += closeLists();

        // ── Headings ──────────────────────────────────────────────────────────
        if (trimmed.startsWith('#### ')) { html += `<h4>${parseInline(trimmed.slice(5))}</h4>\n`; continue; }
        if (trimmed.startsWith('### '))  { html += `<h3>${parseInline(trimmed.slice(4))}</h3>\n`; continue; }
        if (trimmed.startsWith('## '))   { html += `<h2>${parseInline(trimmed.slice(3))}</h2>\n`; continue; }
        if (trimmed.startsWith('# '))    { html += `<h1>${parseInline(trimmed.slice(2))}</h1>\n`; continue; }

        // ── Horizontal rule ───────────────────────────────────────────────────
        if (/^(---+|\*\*\*+|___+)$/.test(trimmed)) { html += '<hr>\n'; continue; }

        // ── Blank line ────────────────────────────────────────────────────────
        if (!trimmed) { html += '\n'; continue; }

        // ── Default: paragraph ────────────────────────────────────────────────
        html += `<p>${parseInline(line)}</p>\n`;
    }

    html += closeAll();

    // Restore safe <div> elements the LLM may emit for RTL wrappers
    html = html
        .replace(/&lt;div\s+([^&]*?dir=(?:&quot;|')rtl(?:&quot;|')[^&]*?)&gt;/gi, '<div $1>')
        .replace(/&lt;div\s+([^&]*?class=(?:&quot;|')[^&]*?(?:&quot;|')[^&]*?)&gt;/gi, '<div $1>')
        .replace(/&lt;div&gt;/gi, '<div>')
        .replace(/&lt;\/div&gt;/gi, '</div>');

    return html;

    // ── Container closers ─────────────────────────────────────────────────────
    function closeLists() {
        if (!inList) return '';
        const tag = listType === 'ul' ? '</ul>' : '</ol>';
        inList = false; listType = null;
        return tag + '\n';
    }
    function closeBlockquote() {
        if (!inBlockquote) return '';
        inBlockquote = false;
        return '</blockquote>\n';
    }
    function closeTable() {
        if (!inTable) return '';
        inTable = false; tableHasHead = false;
        return '</tbody>\n</table>\n';
    }
    function closeAll() {
        return closeLists() + closeBlockquote() + closeTable();
    }

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
        t = t.replace(/!\[([^\]]*)\]\(((?:[^()]+|\([^()]*\))+)\)/g,
            '<img src="$2" alt="$1" style="max-width:100%;border-radius:6px;margin:8px 0;">');

        // Link: [text](href)
        t = t.replace(/\[([^\]]+)\]\(((?:[^()]+|\([^()]*\))+)\)/g,
            '<a href="$2" target="_blank" rel="noopener" class="preview-link">$1</a>');

        return t;
    }
}
