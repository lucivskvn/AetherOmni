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

function handleSuccess(btnElement) {
    const origHTML = btnElement.innerHTML;
    const checkSvg = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check" style="color: #10b981;"><polyline points="20 6 9 17 4 12"/></svg>`;
    btnElement.innerHTML = checkSvg;
    btnElement.title = "Copied!";
    btnElement.setAttribute('aria-label', "Copied!");

    if (typeof window.showClientSideAlert === 'function') {
        window.showClientSideAlert('Copied curation markdown to clipboard successfully!', 'success');
    }

    setTimeout(() => {
        btnElement.innerHTML = origHTML;
        btnElement.title = "Copy Markdown to Clipboard (Ctrl+Shift+C)";
        btnElement.setAttribute('aria-label', "Copy Markdown to Clipboard (Ctrl+Shift+C)");
    }, 2000);
}
/**
 * editor.js — Split-pane live Markdown editor with scroll sync, RTL detection,
 * and a robust block + inline parser. Fully XSS-safe via HTML escaping before parse.
 */

document.addEventListener('DOMContentLoaded', () => {

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

    const editor  = document.getElementById('markdown-input');
    const preview = document.getElementById('html-preview');
    const deleteForm = document.getElementById('delete-document-form');
    const editorForm = document.getElementById('editor-form');

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

        // Live recompile as user types
        editor.addEventListener('input', () => {
            preview.innerHTML = compileMarkdown(editor.value);
            applyPostRenderFeatures(preview);
            updateCounts();
        });

        // Bi-directional proportional scroll synchronisation
        setupScrollSync(editor, preview);
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
/**
 * editor.js — Split-pane live Markdown editor with scroll sync, RTL detection,
 * and a robust block + inline parser. Fully XSS-safe via HTML escaping before parse.
 */

document.addEventListener('DOMContentLoaded', () => {

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

    const editor  = document.getElementById('markdown-input');
    const preview = document.getElementById('html-preview');
    const deleteForm = document.getElementById('delete-document-form');
    const editorForm = document.getElementById('editor-form');

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

        // Live recompile as user types
        editor.addEventListener('input', () => {
            preview.innerHTML = compileMarkdown(editor.value);
            applyPostRenderFeatures(preview);
            updateCounts();
        });

        // Bi-directional proportional scroll synchronisation
        setupScrollSync(editor, preview);
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
        updateCounts();
        markUnsaved();
    }

    // ── 4. Fullscreen Curation workspace Toggle ───────────────────────────────
    const fullscreenToggle = document.getElementById('btn-fullscreen-toggle');
    const workspaceGrid = document.getElementById('curation-workspace-grid');

    // Initialize ARIA state for the toggle button
    if (fullscreenToggle) {
        fullscreenToggle.setAttribute('aria-pressed', 'false');
    }

    function toggleFullscreen(forceState) {
        if (!workspaceGrid || !fullscreenToggle) return;
        const isCurrentlyFullscreen = workspaceGrid.classList.contains('fullscreen');
        const nextState = forceState !== undefined ? forceState : !isCurrentlyFullscreen;

        const SVGS = {
            maximize: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-maximize-2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>`,
            minimize: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-minimize-2"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="10" y1="14" x2="3" y2="21"/></svg>`
        };

        if (nextState) {
            workspaceGrid.classList.add('fullscreen');
            fullscreenToggle.title = "Exit Fullscreen Curation";
            fullscreenToggle.setAttribute('aria-label', "Exit Fullscreen Curation");
            fullscreenToggle.setAttribute('aria-pressed', 'true');
            fullscreenToggle.innerHTML = SVGS.minimize;
            if (editor) {
                editor.focus();
            }
        } else {
            workspaceGrid.classList.remove('fullscreen');
            fullscreenToggle.title = "Toggle Fullscreen Curation";
            fullscreenToggle.setAttribute('aria-label', "Toggle Fullscreen Curation");
            fullscreenToggle.setAttribute('aria-pressed', 'false');
            fullscreenToggle.innerHTML = SVGS.maximize;
        }
    }

    if (fullscreenToggle && workspaceGrid) {
        fullscreenToggle.addEventListener('click', () => {
            toggleFullscreen();
        });

        // Global Escape key handler to exit fullscreen curation mode
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && workspaceGrid.classList.contains('fullscreen')) {
                toggleFullscreen(false);
            }
        });
    }

    // ── 3. Keyboard Shortcuts (Ctrl+B, Ctrl+I, Ctrl+S, Ctrl+Shift+F) ─────────
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
                    const submitBtn = editorForm.querySelector('button[type="submit"]');
                    if (submitBtn) {
                        submitBtn.click();
                    } else {
                        editorForm.submit();
                    }
                }
            } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'f') {
                e.preventDefault();
                toggleFullscreen();
            } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'c') {
                e.preventDefault();
                const copyMarkdownBtn = document.getElementById('btn-copy-markdown');
                if (copyMarkdownBtn) {
                    copyMarkdownBtn.click();
                }
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

    // ── 5.5 Copy Curation Markdown to Clipboard ──────────────────────────────
    const copyMarkdownBtn = document.getElementById('btn-copy-markdown');
    if (copyMarkdownBtn && editor) {
        copyMarkdownBtn.addEventListener('click', () => {
            const content = editor.value || '';

            function handleSuccess() {
                const origHTML = copyMarkdownBtn.innerHTML;
                const checkSvg = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check" style="color: #10b981;"><polyline points="20 6 9 17 4 12"/></svg>`;
                copyMarkdownBtn.innerHTML = checkSvg;
                copyMarkdownBtn.title = "Copied!";
                copyMarkdownBtn.setAttribute('aria-label', "Copied!");

                if (typeof window.showClientSideAlert === 'function') {
                    window.showClientSideAlert('Copied curation markdown to clipboard successfully!', 'success');
                }

                setTimeout(() => {
                    copyMarkdownBtn.innerHTML = origHTML;
                    copyMarkdownBtn.title = "Copy Markdown to Clipboard (Ctrl+Shift+C)";
                    copyMarkdownBtn.setAttribute('aria-label', "Copy Markdown to Clipboard (Ctrl+Shift+C)");
                }, 2000);
            }

            function fallbackCopy() {
                try {
                    const tempTextArea = document.createElement('textarea');
                    tempTextArea.value = content;
                    tempTextArea.style.top = '0';
                    tempTextArea.style.left = '0';
                    tempTextArea.style.position = 'fixed';
                    tempTextArea.style.opacity = '0';
                    document.body.appendChild(tempTextArea);
                    tempTextArea.focus();
                    tempTextArea.select();

                    const successful = document.execCommand('copy');
                    document.body.removeChild(tempTextArea);

                    if (successful) {
                        handleSuccess();
                    } else {
                        throw new Error('execCommand copy returned false');
                    }
                } catch (fallbackErr) {
                    if (typeof window.showClientSideAlert === 'function') {
                        window.showClientSideAlert('Failed to copy Markdown content: ' + fallbackErr, 'error');
                    } else {
                        alert('Failed to copy Markdown content: ' + fallbackErr);
                    }
                }
            }

            if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
                navigator.clipboard.writeText(content)
                    .then(handleSuccess)
                    .catch(() => {
                        fallbackCopy();
                    });
            } else {
                fallbackCopy();
            }
        });
    }

    // ── 6. SFT Training Dataset copy-to-clipboard ────────────────────────────
    const copySftBtn = document.getElementById('btn-copy-sft');
    if (copySftBtn) {
        const copySvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy" style="margin-right: 4px;"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
        const checkSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check" style="margin-right: 4px; color: #10b981;"><polyline points="20 6 9 17 4 12"/></svg>`;

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
                copyTextToClipboard(
                    jsonText,
                    () => {
                        copySftBtn.innerHTML = checkSvg + 'Copied!';
                        if (typeof window.showClientSideAlert === 'function') {
                            window.showClientSideAlert('SFT Dataset exported and copied to clipboard successfully.', 'success');
                        }
                        setTimeout(() => {
                            copySftBtn.innerHTML = copySvg + ' Export SFT Dataset';
                        }, 2000);
                    },
                    (err) => {
                        if (typeof window.showClientSideAlert === 'function') {
                            window.showClientSideAlert('Failed to copy SFT JSON: ' + err, 'error');
                        } else {
                            alert('Failed to copy SFT JSON: ' + err);
                        }
                    }
                );
            } catch (e) {
                if (typeof window.showClientSideAlert === 'function') {
                    window.showClientSideAlert('Failed to parse Q&A dataset: ' + e, 'error');
                } else {
                    alert('Failed to parse Q&A dataset: ' + e);
                }
            }
        });
    }

    // ── 9. Delete confirmation ────────────────────────────────────────────────
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

function extractYamlFrontmatter(escaped) {
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
                    const val = line.substring(colonIdx + 1).trim().replace(/^[&quot;&apos;&lt;&gt;]*|[&quot;&apos;&lt;&gt;]*$/g, '');
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

function processBlockElement(line, state, htmlBuilder) {
    const pushHtml = (str) => { htmlBuilder.html += str + '\n'; };

    if (!line) {
        if (state.inList) { pushHtml('</' + state.listType + '>'); state.inList = false; state.listType = null; }
        if (state.inBlockquote) { pushHtml('</blockquote>'); state.inBlockquote = false; }
        if (state.inTable) { pushHtml('</tbody></table></div>'); state.inTable = false; state.tableHasHead = false; }
        return;
    }

    if (line.startsWith('---')) {
        pushHtml('<hr>');
        return;
    }

    const hMatch = line.match(/^(#{1,6})\s+(.*)/);
    if (hMatch) {
        if (state.inList) { pushHtml('</' + state.listType + '>'); state.inList = false; }
        const level = hMatch[1].length;
        pushHtml('<h' + level + '>' + parseInlineMarkdown(hMatch[2]) + '</h' + level + '>');
        return;
    }

    if (line.startsWith('> ')) {
        if (!state.inBlockquote) { pushHtml('<blockquote>'); state.inBlockquote = true; }
        pushHtml('<p>' + parseInlineMarkdown(line.substring(2)) + '</p>');
        return;
    } else if (state.inBlockquote) {
        pushHtml('</blockquote>'); state.inBlockquote = false;
    }

    let isListItem = false;
    const ulMatch = line.match(/^(\s*)([-*+])\s+(.*)/);
    const olMatch = line.match(/^(\s*)(\d+)\.\s+(.*)/);

    if (ulMatch || olMatch) {
        isListItem = true;
        const type = ulMatch ? 'ul' : 'ol';
        const content = ulMatch ? ulMatch[3] : olMatch[3];
        if (!state.inList) { pushHtml('<' + type + '>'); state.inList = true; state.listType = type; }
        else if (state.inList && state.listType !== type) {
            pushHtml('</' + state.listType + '><' + type + '>');
            state.listType = type;
        }
        pushHtml('<li>' + parseInlineMarkdown(content) + '</li>');
        return;
    } else if (state.inList && !line.match(/^\s+/)) {
        pushHtml('</' + state.listType + '>'); state.inList = false; state.listType = null;
    }

    if (line.trim().startsWith('|') && line.trim().endsWith('|')) {
        if (!state.inTable) {
            pushHtml('<div class="table-container"><table>');
            state.inTable = true;
            state.tableHasHead = false;
        }
        const isDivider = line.replace(/[|\s-:]/g, '').length === 0;
        if (isDivider) {
            if (!state.tableHasHead) { pushHtml('</thead><tbody>'); state.tableHasHead = true; }
            return;
        }
        const cells = line.split('|').map(s => s.trim()).filter((s, i, arr) => !(i === 0 && s === '') && !(i === arr.length - 1 && s === ''));
        if (!state.tableHasHead) {
            pushHtml('<thead><tr>');
            cells.forEach(c => pushHtml('<th>' + parseInlineMarkdown(c) + '</th>'));
            pushHtml('</tr>');
        } else {
            pushHtml('<tr>');
            cells.forEach(c => pushHtml('<td>' + parseInlineMarkdown(c) + '</td>'));
            pushHtml('</tr>');
        }
        return;
    } else if (state.inTable) {
        pushHtml('</tbody></table></div>'); state.inTable = false; state.tableHasHead = false;
    }

    pushHtml('<p>' + parseInlineMarkdown(line) + '</p>');
}
/**
 * editor.js — Split-pane live Markdown editor with scroll sync, RTL detection,
 * and a robust block + inline parser. Fully XSS-safe via HTML escaping before parse.
 */

document.addEventListener('DOMContentLoaded', () => {

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

    const editor  = document.getElementById('markdown-input');
    const preview = document.getElementById('html-preview');
    const deleteForm = document.getElementById('delete-document-form');
    const editorForm = document.getElementById('editor-form');

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

        // Live recompile as user types
        editor.addEventListener('input', () => {
            preview.innerHTML = compileMarkdown(editor.value);
            applyPostRenderFeatures(preview);
            updateCounts();
        });

        // Bi-directional proportional scroll synchronisation
        setupScrollSync(editor, preview);
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
        updateCounts();
        markUnsaved();
    }

    // ── 4. Fullscreen Curation workspace Toggle ───────────────────────────────
    const fullscreenToggle = document.getElementById('btn-fullscreen-toggle');
    const workspaceGrid = document.getElementById('curation-workspace-grid');

    // Initialize ARIA state for the toggle button
    if (fullscreenToggle) {
        fullscreenToggle.setAttribute('aria-pressed', 'false');
    }

    function toggleFullscreen(forceState) {
        if (!workspaceGrid || !fullscreenToggle) return;
        const isCurrentlyFullscreen = workspaceGrid.classList.contains('fullscreen');
        const nextState = forceState !== undefined ? forceState : !isCurrentlyFullscreen;

        const SVGS = {
            maximize: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-maximize-2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>`,
            minimize: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-minimize-2"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="10" y1="14" x2="3" y2="21"/></svg>`
        };

        if (nextState) {
            workspaceGrid.classList.add('fullscreen');
            fullscreenToggle.title = "Exit Fullscreen Curation";
            fullscreenToggle.setAttribute('aria-label', "Exit Fullscreen Curation");
            fullscreenToggle.setAttribute('aria-pressed', 'true');
            fullscreenToggle.innerHTML = SVGS.minimize;
            if (editor) {
                editor.focus();
            }
        } else {
            workspaceGrid.classList.remove('fullscreen');
            fullscreenToggle.title = "Toggle Fullscreen Curation";
            fullscreenToggle.setAttribute('aria-label', "Toggle Fullscreen Curation");
            fullscreenToggle.setAttribute('aria-pressed', 'false');
            fullscreenToggle.innerHTML = SVGS.maximize;
        }
    }

    if (fullscreenToggle && workspaceGrid) {
        fullscreenToggle.addEventListener('click', () => {
            toggleFullscreen();
        });

        // Global Escape key handler to exit fullscreen curation mode
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && workspaceGrid.classList.contains('fullscreen')) {
                toggleFullscreen(false);
            }
        });
    }

    // ── 3. Keyboard Shortcuts (Ctrl+B, Ctrl+I, Ctrl+S, Ctrl+Shift+F) ─────────
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
            } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'f') {
                e.preventDefault();
                toggleFullscreen();
            } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'c') {
                e.preventDefault();
                const copyMarkdownBtn = document.getElementById('btn-copy-markdown');
                if (copyMarkdownBtn) {
                    copyMarkdownBtn.click();
                }
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

    // ── 5.5 Copy Curation Markdown to Clipboard ──────────────────────────────
    const copyMarkdownBtn = document.getElementById('btn-copy-markdown');
    if (copyMarkdownBtn && editor) {
        copyMarkdownBtn.addEventListener('click', () => {
            const content = editor.value || '';

            function handleSuccess() {
                const origHTML = copyMarkdownBtn.innerHTML;
                const checkSvg = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check" style="color: #10b981;"><polyline points="20 6 9 17 4 12"/></svg>`;
                copyMarkdownBtn.innerHTML = checkSvg;
                copyMarkdownBtn.title = "Copied!";
                copyMarkdownBtn.setAttribute('aria-label', "Copied!");

                if (typeof window.showClientSideAlert === 'function') {
                    window.showClientSideAlert('Copied curation markdown to clipboard successfully!', 'success');
                }

                setTimeout(() => {
                    copyMarkdownBtn.innerHTML = origHTML;
                    copyMarkdownBtn.title = "Copy Markdown to Clipboard (Ctrl+Shift+C)";
                    copyMarkdownBtn.setAttribute('aria-label', "Copy Markdown to Clipboard (Ctrl+Shift+C)");
                }, 2000);
            }

            function fallbackCopy() {
                try {
                    const tempTextArea = document.createElement('textarea');
                    tempTextArea.value = content;
                    tempTextArea.style.top = '0';
                    tempTextArea.style.left = '0';
                    tempTextArea.style.position = 'fixed';
                    tempTextArea.style.opacity = '0';
                    document.body.appendChild(tempTextArea);
                    tempTextArea.focus();
                    tempTextArea.select();

                    const successful = document.execCommand('copy');
                    document.body.removeChild(tempTextArea);

                    if (successful) {
                        handleSuccess();
                    } else {
                        throw new Error('execCommand copy returned false');
                    }
                } catch (fallbackErr) {
                    if (typeof window.showClientSideAlert === 'function') {
                        window.showClientSideAlert('Failed to copy Markdown content: ' + fallbackErr, 'error');
                    } else {
                        alert('Failed to copy Markdown content: ' + fallbackErr);
                    }
                }
            }

            if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
                navigator.clipboard.writeText(content)
                    .then(handleSuccess)
                    .catch(() => {
                        fallbackCopy();
                    });
            } else {
                fallbackCopy();
            }
        });
    }

    // ── 6. SFT Training Dataset copy-to-clipboard ────────────────────────────
    const copySftBtn = document.getElementById('btn-copy-sft');
    if (copySftBtn) {
        const copySvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy" style="margin-right: 4px;"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
        const checkSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check" style="margin-right: 4px; color: #10b981;"><polyline points="20 6 9 17 4 12"/></svg>`;

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
                copyTextToClipboard(
                    jsonText,
                    () => {
                        copySftBtn.innerHTML = checkSvg + 'Copied!';
                        if (typeof window.showClientSideAlert === 'function') {
                            window.showClientSideAlert('SFT Dataset exported and copied to clipboard successfully.', 'success');
                        }
                        setTimeout(() => {
                            copySftBtn.innerHTML = copySvg + ' Export SFT Dataset';
                        }, 2000);
                    },
                    (err) => {
                        if (typeof window.showClientSideAlert === 'function') {
                            window.showClientSideAlert('Failed to copy SFT JSON: ' + err, 'error');
                        } else {
                            alert('Failed to copy SFT JSON: ' + err);
                        }
                    }
                );
            } catch (e) {
                if (typeof window.showClientSideAlert === 'function') {
                    window.showClientSideAlert('Failed to parse Q&A dataset: ' + e, 'error');
                } else {
                    alert('Failed to parse Q&A dataset: ' + e);
                }
            }
        });
    }

    // ── 9. Delete confirmation ────────────────────────────────────────────────
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
/**
 * editor.js — Split-pane live Markdown editor with scroll sync, RTL detection,
 * and a robust block + inline parser. Fully XSS-safe via HTML escaping before parse.
 */

document.addEventListener('DOMContentLoaded', () => {

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

    const editor  = document.getElementById('markdown-input');
    const preview = document.getElementById('html-preview');
    const deleteForm = document.getElementById('delete-document-form');
    const editorForm = document.getElementById('editor-form');

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

        // Live recompile as user types
        editor.addEventListener('input', () => {
            preview.innerHTML = compileMarkdown(editor.value);
            applyPostRenderFeatures(preview);
            updateCounts();
        });

        // Bi-directional proportional scroll synchronisation
        setupScrollSync(editor, preview);
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
        updateCounts();
        markUnsaved();
    }

    // ── 4. Fullscreen Curation workspace Toggle ───────────────────────────────
    const fullscreenToggle = document.getElementById('btn-fullscreen-toggle');
    const workspaceGrid = document.getElementById('curation-workspace-grid');

    // Initialize ARIA state for the toggle button
    if (fullscreenToggle) {
        fullscreenToggle.setAttribute('aria-pressed', 'false');
    }

    function toggleFullscreen(forceState) {
        if (!workspaceGrid || !fullscreenToggle) return;
        const isCurrentlyFullscreen = workspaceGrid.classList.contains('fullscreen');
        const nextState = forceState !== undefined ? forceState : !isCurrentlyFullscreen;

        const SVGS = {
            maximize: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-maximize-2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>`,
            minimize: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-minimize-2"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="10" y1="14" x2="3" y2="21"/></svg>`
        };

        if (nextState) {
            workspaceGrid.classList.add('fullscreen');
            fullscreenToggle.title = "Exit Fullscreen Curation";
            fullscreenToggle.setAttribute('aria-label', "Exit Fullscreen Curation");
            fullscreenToggle.setAttribute('aria-pressed', 'true');
            fullscreenToggle.innerHTML = SVGS.minimize;
            if (editor) {
                editor.focus();
            }
        } else {
            workspaceGrid.classList.remove('fullscreen');
            fullscreenToggle.title = "Toggle Fullscreen Curation";
            fullscreenToggle.setAttribute('aria-label', "Toggle Fullscreen Curation");
            fullscreenToggle.setAttribute('aria-pressed', 'false');
            fullscreenToggle.innerHTML = SVGS.maximize;
        }
    }

    if (fullscreenToggle && workspaceGrid) {
        fullscreenToggle.addEventListener('click', () => {
            toggleFullscreen();
        });

        // Global Escape key handler to exit fullscreen curation mode
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && workspaceGrid.classList.contains('fullscreen')) {
                toggleFullscreen(false);
            }
        });
    }

    // ── 3. Keyboard Shortcuts (Ctrl+B, Ctrl+I, Ctrl+S, Ctrl+Shift+F) ─────────
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
            } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'f') {
                e.preventDefault();
                toggleFullscreen();
            } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'c') {
                e.preventDefault();
                const copyMarkdownBtn = document.getElementById('btn-copy-markdown');
                if (copyMarkdownBtn) {
                    copyMarkdownBtn.click();
                }
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

    // ── 5.5 Copy Curation Markdown to Clipboard ──────────────────────────────
    const copyMarkdownBtn = document.getElementById('btn-copy-markdown');
    if (copyMarkdownBtn && editor) {
        copyMarkdownBtn.addEventListener('click', () => {
            const content = editor.value || '';



            function fallbackCopy() {
                try {
                    const tempTextArea = document.createElement('textarea');
                    tempTextArea.value = content;
                    tempTextArea.style.top = '0';
                    tempTextArea.style.left = '0';
                    tempTextArea.style.position = 'fixed';
                    tempTextArea.style.opacity = '0';
                    document.body.appendChild(tempTextArea);
                    tempTextArea.focus();
                    tempTextArea.select();

                    const successful = document.execCommand('copy');
                    tempTextArea.remove();

                    if (successful) {
                        handleSuccess(copyMarkdownBtn);
                    } else {
                        throw new Error('execCommand copy returned false');
                    }
                } catch (fallbackErr) {
                    if (typeof window.showClientSideAlert === 'function') {
                        window.showClientSideAlert('Failed to copy Markdown content: ' + fallbackErr, 'error');
                    } else {
                        alert('Failed to copy Markdown content: ' + fallbackErr);
                    }
                } 
            }

            if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
                navigator.clipboard.writeText(content)
                    .then(handleSuccess)
                    .catch(() => {
                        fallbackCopy();
                    });
            } else {
                fallbackCopy();
            }
        });
    }

    // ── 6. SFT Training Dataset copy-to-clipboard ────────────────────────────
    const copySftBtn = document.getElementById('btn-copy-sft');
    if (copySftBtn) {
        const copySvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy" style="margin-right: 4px;"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
        const checkSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check" style="margin-right: 4px; color: #10b981;"><polyline points="20 6 9 17 4 12"/></svg>`;

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
                copyTextToClipboard(
                    jsonText,
                    () => {
                        copySftBtn.innerHTML = checkSvg + 'Copied!';
                        if (typeof window.showClientSideAlert === 'function') {
                            window.showClientSideAlert('SFT Dataset exported and copied to clipboard successfully.', 'success');
                        }
                        setTimeout(() => {
                            copySftBtn.innerHTML = copySvg + ' Export SFT Dataset';
                        }, 2000);
                    },
                    (err) => {
                        if (typeof window.showClientSideAlert === 'function') {
                            window.showClientSideAlert('Failed to copy SFT JSON: ' + err, 'error');
                        } else {
                            alert('Failed to copy SFT JSON: ' + err);
                        }
                    }
                );
            } catch (e) {
                if (typeof window.showClientSideAlert === 'function') {
                    window.showClientSideAlert('Failed to parse Q&A dataset: ' + e, 'error');
                } else {
                    alert('Failed to parse Q&A dataset: ' + e);
                }
            }
        });
    }

    // ── 9. Delete confirmation ────────────────────────────────────────────────
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
                    const val = line.substring(colonIdx + 1).trim().replace(/^[&quot;&apos;&lt;&gt;]+|[&quot;&apos;&lt;&gt;]+$/g, ''); // strip quotes
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

    // Restore safe HTML tags (b, i, u, strong, em, sup, sub, br, hr, span, div, p, table, etc.) and their safe attributes (dir, class, style, colspan, rowspan) for live rendering
    html = html
        .replace(/&lt;br\s*\/?&gt;/gi, '<br>')
        .replace(/&lt;hr\s*\/?&gt;/gi, '<hr>')
        .replace(/&lt;(\/)?(b|i|u|strong|em|sup|sub|table|thead|tbody|tr|th|td|code|pre|blockquote|ul|ol|li)\b(.*?)\&gt;/gi, (match, closeSlash, tagName, attrs) => {
            let cleanAttrs = '';
            if (attrs) {
                const decodedAttrs = attrs.replace(/&quot;/g, '"').replace(/&#x27;/g, "'");
                const attrRegex = /\b(colspan|rowspan|class|style|dir)\s*=\s*["']([^"']*)["']/gi;
                let m;
                while ((m = attrRegex.exec(decodedAttrs)) !== null) {
                    cleanAttrs += ` ${m[1]}="${m[2]}"`;
                }
            }
            return `<${closeSlash || ''}${tagName}${cleanAttrs}>`;
        })
        .replace(/&lt;(\/)?(span|div|p)\b(.*?)\&gt;/gi, (match, closeSlash, tagName, attrs) => {
            let cleanAttrs = '';
            if (attrs) {
                const decodedAttrs = attrs.replace(/&quot;/g, '"').replace(/&#x27;/g, "'");
                const attrRegex = /\b(class|dir|style)\s*=\s*["']([^"']*)["']/gi;
                let m;
                while ((m = attrRegex.exec(decodedAttrs)) !== null) {
                    cleanAttrs += ` ${m[1]}="${m[2]}"`;
                }
            }
            return `<${closeSlash || ''}${tagName}${cleanAttrs}>`;
        });

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


        // Italic *text* or _text_
        t = t.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
        t = t.replace(/_([^_\n]+)_/g, '<em>$1</em>');

        // Image: ![alt](src)
        t = t.replace(/!\[([^\]]*)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)/g,
            '<img src="$2" alt="$1" style="max-width:100%;border-radius:6px;margin:8px 0;">');

        // Link: [text](href)
        t = t.replace(/\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)/g,
            '<a href="$2" target="_blank" rel="noopener" class="preview-link">$1</a>');

        return t;
    }
}

/**
 * Copy text content to clipboard with navigator.clipboard and fallback to execCommand.
 */
function copyTextToClipboard(text, onSuccess, onError) {
    function fallbackCopy() {
        try {
            const tempTextArea = document.createElement('textarea');
            tempTextArea.value = text;
            tempTextArea.style.top = '0';
            tempTextArea.style.left = '0';
            tempTextArea.style.position = 'fixed';
            tempTextArea.style.opacity = '0';
            document.body.appendChild(tempTextArea);
            tempTextArea.focus();
            tempTextArea.select();

            const successful = document.execCommand('copy');
            tempTextArea.remove();

            if (successful) {
                if (onSuccess) onSuccess();
            } else {
                throw new Error('execCommand copy returned false');
            }
        } catch (fallbackErr) {
            if (onError) onError(fallbackErr);
        }
    }

    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        navigator.clipboard.writeText(text)
            .then(() => {
                if (onSuccess) onSuccess();
            })
            .catch(() => {
                fallbackCopy();
            });
    } else {
        fallbackCopy();
    }
}
