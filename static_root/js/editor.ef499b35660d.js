/**
 * editor.js - Splitted Editor Live Markdown Parsing, Scroll Sync, and Event Bindings.
 */

document.addEventListener('DOMContentLoaded', () => {
    const editor = document.getElementById('markdown-input');
    const preview = document.getElementById('html-preview');
    const deleteForm = document.getElementById('delete-document-form');

    // 1. Initial live compile on page load if editor is present
    if (editor && preview) {
        const initialText = editor.value;
        if (initialText.trim() !== '') {
            preview.innerHTML = compileMarkdown(initialText);
        }

        // Live compile as user types
        editor.addEventListener('input', () => {
            preview.innerHTML = compileMarkdown(editor.value);
        });

        // Split-pane scroll synchronization
        editor.addEventListener('scroll', () => {
            const pct = editor.scrollTop / (editor.scrollHeight - editor.clientHeight);
            preview.scrollTop = pct * (preview.scrollHeight - preview.clientHeight);
        });
    }

    // 2. Client-side submit confirmation for deletion
    if (deleteForm) {
        deleteForm.addEventListener('submit', (e) => {
            if (!confirm('Are you sure you want to delete this document from the library?')) {
                e.preventDefault();
            }
        });
    }
});

/**
 * Robust line-by-line Markdown compiler converting raw MD text to semantic HTML.
 * Includes complete block-level parsing (lists, tables, code blocks, blockquotes)
 * and strict inline-level parsing (bold, italics, inline code, hyper-links) with XSS protection.
 */
function compileMarkdown(markdown) {
    if (!markdown) return '';

    // Step 1: Escape HTML tags to ensure complete XSS protection & secure CSP compliance
    let escaped = markdown
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

    const lines = escaped.split('\n');
    let html = '';
    
    // State Tracking
    let inList = false;
    let listType = null; // 'ul' or 'ol'
    let inBlockquote = false;
    let inCodeBlock = false;
    let codeBlockLang = '';
    let inTable = false;

    for (let i = 0; i < lines.length; i++) {
        let line = lines[i];

        // 1. Code Fence blocks
        if (line.trim().startsWith('```')) {
            if (inCodeBlock) {
                html += '</code></pre>\n';
                inCodeBlock = false;
            } else {
                html += closeActiveContainers();
                codeBlockLang = line.trim().substring(3);
                html += `<pre><code class="language-${codeBlockLang || 'txt'}">`;
                inCodeBlock = true;
            }
            continue;
        }

        if (inCodeBlock) {
            html += line + '\n';
            continue;
        }

        // 2. Table blocks (Standard pipes syntax)
        const isTableRow = line.trim().startsWith('|') && line.trim().endsWith('|');
        if (isTableRow) {
            html += closeActiveContainers(false, true, true); // Keep table, close lists/quotes
            
            // Check if it is a separator line (e.g., |---| or |:---:|)
            const isSeparator = line.match(/^[|\s:-]+$/);
            if (isSeparator) {
                if (!inTable) {
                    inTable = true;
                    html += '<table>\n';
                }
                continue;
            }

            const cells = line.split('|')
                .map(c => c.trim())
                .filter((_, idx, arr) => idx > 0 && idx < arr.length - 1);

            if (!inTable) {
                inTable = true;
                html += '<table>\n<thead>\n<tr>\n';
                cells.forEach(c => {
                    html += `<th>${parseInline(c)}</th>\n`;
                });
                html += '</tr>\n</thead>\n<tbody>\n';
            } else {
                html += '<tr>\n';
                cells.forEach(c => {
                    html += `<td>${parseInline(c)}</td>\n`;
                });
                html += '</tr>\n';
            }
            continue;
        } else {
            if (inTable) {
                html += '</tbody>\n</table>\n';
                inTable = false;
            }
        }

        // 3. Blockquotes block (&gt; syntax)
        if (line.trim().startsWith('&gt; ')) {
            html += closeActiveContainers(true, false, false); // close lists, keep quotes
            if (!inBlockquote) {
                html += '<blockquote>\n';
                inBlockquote = true;
            }
            const content = line.trim().substring(5);
            html += `<p>${parseInline(content)}</p>\n`;
            continue;
        } else {
            if (inBlockquote) {
                html += '</blockquote>\n';
                inBlockquote = false;
            }
        }

        // 4. Ordered / Unordered Lists
        const ulMatch = line.match(/^(\s*)([-*+])\s+(.*)$/);
        const olMatch = line.match(/^(\s*)(\d+)\.\s+(.*)$/);

        if (ulMatch) {
            html += closeActiveContainers(false, false, false); // keep lists, close others
            const content = ulMatch[3];
            if (!inList || listType !== 'ul') {
                html += closeActiveContainers(true, false, false); // closeol if active
                html += '<ul>\n';
                inList = true;
                listType = 'ul';
            }
            html += `<li>${parseInline(content)}</li>\n`;
            continue;
        } else if (olMatch) {
            html += closeActiveContainers(false, false, false);
            const content = olMatch[3];
            if (!inList || listType !== 'ol') {
                html += closeActiveContainers(true, false, false); // close ul if active
                html += '<ol>\n';
                inList = true;
                listType = 'ol';
            }
            html += `<li>${parseInline(content)}</li>\n`;
            continue;
        } else {
            if (inList) {
                html += listType === 'ul' ? '</ul>\n' : '</ol>\n';
                inList = false;
                listType = null;
            }
        }

        // 5. Headings
        if (line.trim().startsWith('# ')) {
            html += `<h1 class="compiled-heading">${parseInline(line.trim().substring(2))}</h1>\n`;
            continue;
        }
        if (line.trim().startsWith('## ')) {
            html += `<h2 class="compiled-heading">${parseInline(line.trim().substring(3))}</h2>\n`;
            continue;
        }
        if (line.trim().startsWith('### ')) {
            html += `<h3 class="compiled-heading">${parseInline(line.trim().substring(4))}</h3>\n`;
            continue;
        }

        // 6. Horizontal rule
        if (line.trim() === '---' || line.trim() === '***') {
            html += '<hr>\n';
            continue;
        }

        // 7. Blank lines
        if (line.trim() === '') {
            continue;
        }

        // Default Paragraph
        html += `<p>${parseInline(line)}</p>\n`;
    }

    // Close open containers at document end
    html += closeActiveContainers();

    return html;

    function closeActiveContainers(closeLists = true, closeQuotes = true, closeTables = true) {
        let closed = '';
        if (closeLists && inList) {
            closed += listType === 'ul' ? '</ul>\n' : '</ol>\n';
            inList = false;
            listType = null;
        }
        if (closeQuotes && inBlockquote) {
            closed += '</blockquote>\n';
            inBlockquote = false;
        }
        if (closeTables && inTable) {
            closed += '</tbody>\n</table>\n';
            inTable = false;
        }
        return closed;
    }

    function parseInline(text) {
        let parsed = text;

        // Inline Code: `code`
        parsed = parsed.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');

        // Bold: **text** or __text__
        parsed = parsed.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        parsed = parsed.replace(/__([^_]+)__/g, '<strong>$1</strong>');

        // Italics: *text* or _text_
        parsed = parsed.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        parsed = parsed.replace(/_([^_]+)_/g, '<em>$1</em>');

        // Markdown Link: [text](href)
        parsed = parsed.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" class="preview-link">$1</a>');

        return parsed;
    }
}
