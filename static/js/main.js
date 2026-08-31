/**
 * main.js - Core workspace logic, alert dismissals, dialog validations,
 * Chart.js token visualizer, and non-disruptive live status polling.
 */

document.addEventListener('DOMContentLoaded', () => {
    // 1. Initialize Lucide icons
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }

    // 2. Alert & Toast Notification Management
    initializeAlerts();

    // 3. Settings Modal & Danger Zone Validation
    initializeSettingsModal();

    // 4. Ingest Drag-and-Drop Dropzone
    initializeDragAndDrop();

    // 5. Library Export/Select Checkbox Actions
    initializeExportActions();

    // 6. Semantic RAG Search Panel
    initializeRAGSearch();

    // 7. Chart.js Token Spend Initialization
    initializeTokensChart();

    // 8. Initialize Realtime WebSocket updates or Fallback Poller
    initializeSupabaseRealtime();

    // 9. Curation Pipeline Document Retries, Cancellations & Deletions
    initializeRetryActions();
    initializeCancelActions();
    initializeDeleteActions();

    // 10. Auto-convert UTC timestamps to user/browser local timezone
    initializeLocalTimezones();

    // 11. Password visibility toggles for enhanced accessibility and UX
    initializePasswordToggles();

    // 12. Global search keyboard shortcuts for advanced curation UX
    initializeSearchShortcuts();

    // 13. Dynamic instant client-side table filtering
    initializeLibraryFilter();

    // 14. Caps Lock active warning detector for password entries
    initializeCapsLockDetector();

    // 15. Real-time form submission loading spinners
    initializeFormSubmitSpinners();

    // 16. Real-time password matching feedback for credential forms
    initializePasswordMatchFeedback();

    // 17. Enables keyboard shortcuts and instant clearing for the Audit Logs search input
    initializeAuditSearch();
});

const MAX_UPLOAD_FILE_SIZE = 30 * 1024 * 1024;
const ALLOWED_UPLOAD_EXTENSIONS = new Set([
    '.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.tiff', '.heic', '.heif',
    '.csv', '.txt', '.md', '.markdown', '.json', '.docx', '.doc', '.xlsx', '.xls'
]);

function filterUploadFiles(files) {
    const valid = [];
    for (const file of files) {
        const hasExtension = file.name.includes('.') && !file.name.startsWith('.');
        const extension = hasExtension ? `.${file.name.split('.').pop().toLowerCase()}` : '';
        if (!extension || !ALLOWED_UPLOAD_EXTENSIONS.has(extension)) {
            showClientSideAlert(`Skipped "${file.name}": Unsupported format. Supported: PDF, Images (PNG, JPG, WEBP, GIF, TIFF, HEIC), Markdown, Text, CSV, JSON, Word, Excel.`);
            continue;
        }
        if (file.size > MAX_UPLOAD_FILE_SIZE) {
            showClientSideAlert(`Skipped "${file.name}": Exceeds maximum size limit of 30MB.`);
            continue;
        }
        valid.push(file);
    }
    return valid;
}

async function executeRagFetch(url) {
    const response = await fetch(url);
    let data = null;
    try {
        data = await response.json();
    } catch {
        // A non-JSON error response is handled by the common error path below.
    }
    if (!response.ok || !data || data.error) {
        throw new Error(data?.error || data?.message || 'An error occurred during vector search.');
    }
    return data;
}

/**
 * Enables keyboard shortcuts and instant clearing for the Audit Logs search input.
 */
function initializeAuditSearch() {
    const input = document.getElementById('search-input');
    if (!input) return;

    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'search-clear-btn';
    clearBtn.setAttribute('aria-label', 'Clear search input');
    clearBtn.title = 'Clear search';
    clearBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;


    const hint = document.getElementById('audit-search-hint');
    input.closest('.search-input-container')?.appendChild(clearBtn);

    const update = () => {
        const hasText = input.value.trim().length > 0;
        clearBtn.style.display = hasText ? 'inline-flex' : 'none';
        if (hint) {
            hint.style.opacity = hasText || document.activeElement === input ? '0' : '1';
            hint.style.visibility = hasText || document.activeElement === input ? 'hidden' : 'visible';
        }
    };

    input.addEventListener('input', update);
    input.addEventListener('focus', update);
    input.addEventListener('blur', () => setTimeout(update, 100));
    clearBtn.addEventListener('click', () => { input.value = ''; update(); input.focus(); });

    document.addEventListener('keydown', (e) => {
        if (/^(input|textarea|select)$/i.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable) return;
        if (e.key === '/' || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) {
            e.preventDefault();
            input.focus();
            input.select();
        }
    });
    input.addEventListener('keydown', (e) => { if (e.key === 'Escape') input.blur(); });
    update();
}

const PASSWORD_MATCH_SVGS = {
    match: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display: inline-block; vertical-align: middle;"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`,
    mismatch: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="display: inline-block; vertical-align: middle;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`
};

function createPasswordFeedbackElement(confirmInput) {
    const feedback = document.createElement('div');
    feedback.className = 'password-match-status';
    feedback.style.display = 'none';
    feedback.style.alignItems = 'center';
    feedback.style.gap = '6px';
    feedback.style.fontSize = '13px';
    feedback.style.fontWeight = '500';
    feedback.style.marginTop = '8px';
    feedback.style.padding = '6px 12px';
    feedback.style.borderRadius = '8px';
    feedback.style.width = '100%';
    feedback.style.boxSizing = 'border-box';
    feedback.style.transition = 'all 0.2s ease-in-out';
    feedback.setAttribute('role', 'status');
    feedback.setAttribute('aria-live', 'polite');

    const wrapper = confirmInput.closest('.password-toggle-wrapper');
    if (wrapper) {
        wrapper.parentNode.insertBefore(feedback, wrapper.nextSibling);
    } else {
        confirmInput.parentNode.insertBefore(feedback, confirmInput.nextSibling);
    }
    return feedback;
}

function updatePasswordMatchStatus(feedback, pVal, cVal) {
    if (pVal === '' || cVal === '') {
        feedback.style.display = 'none';
        feedback.innerHTML = '';
        return;
    }
    feedback.style.display = 'inline-flex';
    const isMatch = pVal === cVal;
    feedback.style.color = isMatch ? '#10b981' : '#ef4444';
    feedback.style.background = isMatch ? 'rgba(16, 185, 129, 0.08)' : 'rgba(239, 68, 68, 0.08)';
    feedback.style.border = isMatch ? '1px solid rgba(16, 185, 129, 0.25)' : '1px solid rgba(239, 68, 68, 0.25)';
    feedback.innerHTML = `
        ${isMatch ? PASSWORD_MATCH_SVGS.match : PASSWORD_MATCH_SVGS.mismatch}
        <span style="vertical-align: middle;">${isMatch ? 'Passwords match' : 'Passwords do not match'}</span>
    `;
}

function bindPasswordMatchEvents(pair) {
    const passwordInput = document.getElementById(pair.passwordId);
    const confirmInput = document.getElementById(pair.confirmId);
    if (!passwordInput || !confirmInput) return;

    const feedback = createPasswordFeedbackElement(confirmInput);
    const checkMatch = () => updatePasswordMatchStatus(feedback, passwordInput.value, confirmInput.value);
    passwordInput.addEventListener('input', checkMatch);
    confirmInput.addEventListener('input', checkMatch);
}

/**
 * Real-time password matching feedback helper for register, password change,
 * and password reset confirmation forms.
 */
function initializePasswordMatchFeedback() {
    const pairs = [
        { passwordId: 'id_password', confirmId: 'id_confirm_password' }, // NOSONAR javascript:S2068 -- DOM element ID selectors, not hardcoded credentials
        { passwordId: 'id_new_password1', confirmId: 'id_new_password2' }, // NOSONAR javascript:S2068 -- DOM element ID selectors, not hardcoded credentials
        { passwordId: 'new_password', confirmId: 'confirm_password' } // NOSONAR javascript:S2068 -- DOM element ID selectors, not hardcoded credentials
    ];
    pairs.forEach(bindPasswordMatchEvents);
}

/**
 * Handle alert-card manual and automatic dismissals with hover pause.
 */
function initializeAlerts() {
    const alertCards = document.querySelectorAll('.alert-card');
    const AUTO_DISMISS_DELAY = 5000;

    alertCards.forEach(card => {
        let dismissTimeout = setTimeout(() => {
            dismissCard(card);
        }, AUTO_DISMISS_DELAY);

        // Pause countdown on hover
        card.addEventListener('mouseenter', () => clearTimeout(dismissTimeout));
        card.addEventListener('mouseleave', () => {
            dismissTimeout = setTimeout(() => {
                dismissCard(card);
            }, AUTO_DISMISS_DELAY);
        });
    });

    // Delegated click for manual dismissal
    document.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-dismiss]');
        if (!btn) return;
        const targetId = btn.dataset.dismiss;
        const card = document.getElementById(targetId);
        if (card) {
            dismissCard(card);
        }
    });
}

/**
 * Enables advanced keyboard shortcut focus control for Semantic Spotlight Search query.
 * Shortcuts: '/' or 'Ctrl+K' / 'Cmd+K' (or 'Meta+K') triggers focus.
 * 'Escape' key within the input blurs it.
 */
function initializeSearchShortcuts() {
    const ragQuery = document.getElementById('rag-query');
    if (!ragQuery) return;

    // Create clear button dynamically to avoid markup modification
    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';

    clearBtn.className = 'search-clear-btn';
    clearBtn.setAttribute('aria-label', 'Clear search query');
    clearBtn.title = 'Clear search';
    clearBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;


    const container = ragQuery.closest('.search-input-container');
    const shortcutHint = document.getElementById('search-hint');
    if (container) {
        container.appendChild(clearBtn);
    }

    clearBtn.addEventListener('mouseenter', () => {
        clearBtn.style.color = 'var(--text-main)';
    });
    clearBtn.addEventListener('mouseleave', () => {
        clearBtn.style.color = 'var(--text-muted)';
    });

    function updateUIState() {
        const hasText = ragQuery.value.trim().length > 0;
        if (hasText) {
            clearBtn.style.display = 'inline-flex';
            if (shortcutHint) {
                shortcutHint.style.opacity = '0';
                shortcutHint.style.visibility = 'hidden';
            }
        } else {
            clearBtn.style.display = 'none';
            if (shortcutHint && document.activeElement !== ragQuery) {
                shortcutHint.style.opacity = '1';
                shortcutHint.style.visibility = 'visible';
            }
        }
    }

    ragQuery.addEventListener('input', updateUIState);
    ragQuery.addEventListener('focus', () => {
        if (shortcutHint) {
            shortcutHint.style.opacity = '0';
            shortcutHint.style.visibility = 'hidden';
        }
    });
    ragQuery.addEventListener('blur', () => {
        setTimeout(() => {
            if (document.activeElement !== ragQuery) {
                updateUIState();
            }
        }, 100);
    });

    clearBtn.addEventListener('click', () => {
        ragQuery.value = '';
        updateUIState();
        ragQuery.focus();
    });

    document.addEventListener('keydown', (e) => {
        // Prevent stealing focus when user is typing in another input element
        const activeTag = document.activeElement ? document.activeElement.tagName.toLowerCase() : '';
        if (activeTag === 'input' || activeTag === 'textarea' || activeTag === 'select' || document.activeElement.isContentEditable) {
            return;
        }

        // Trigger on '/' or Ctrl+K / Cmd+K
        if (e.key === '/' || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) {
            e.preventDefault();
            ragQuery.focus();
            ragQuery.select();
        }
    });

    // Blur search input when 'Escape' is pressed
    ragQuery.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            ragQuery.blur();
        }
    });
}


/**
 * Dynamically attach accessibility-first password visibility toggles to password fields.
 */
function initializePasswordToggles() {
    const SVGS = {
        show: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`,
        hide: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.52 13.52 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" y1="2" x2="22" y2="22"/></svg>`
    };
    document.querySelectorAll('input[type="password"]').forEach((input, index) => {
        if (input.dataset.hasToggle) return;
        input.dataset.hasToggle = "true";
        const inputId = input.id || `pwd-in-${index}`;
        input.id = inputId;

        const wrapper = document.createElement('div');
        wrapper.className = 'password-toggle-wrapper';
        wrapper.style.display = globalThis.getComputedStyle(input).display === 'block' ? 'block' : 'inline-block';
        input.parentNode.insertBefore(wrapper, input).appendChild(input);

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'password-toggle-btn';
        btn.setAttribute('aria-label', 'Show password');
        btn.setAttribute('aria-controls', inputId);
        btn.setAttribute('aria-expanded', 'false');
        btn.title = 'Show password';
        btn.innerHTML = SVGS.show;
        wrapper.appendChild(btn);

        btn.addEventListener('click', () => {
            const isPwd = input.type === 'password';
            input.type = isPwd ? 'text' : 'password';
            btn.setAttribute('aria-expanded', String(isPwd));
            const label = isPwd ? 'Hide password' : 'Show password';
            btn.setAttribute('aria-label', label);
            btn.title = label;
            btn.innerHTML = isPwd ? SVGS.hide : SVGS.show;
        });
    });
}

/**
 * Detects Caps Lock status on password fields and displays a sleek warning badge.
 */
function initializeCapsLockDetector() {
    document.querySelectorAll('input[type="password"]').forEach(input => {
        const warning = document.createElement('div');
        warning.className = 'caps-lock-warning';
        warning.style.display = 'none';
        warning.setAttribute('role', 'status');
        warning.setAttribute('aria-live', 'polite');
        warning.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="margin-right: 4px; display: inline-block; vertical-align: middle;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            <span style="vertical-align: middle;">Caps Lock is active</span>
        `;

        // If the password input has been wrapped, insert the warning after the wrapper.
        // Otherwise, insert it after the input itself.
        const wrapper = input.closest('.password-toggle-wrapper');
        if (wrapper) {
            wrapper.parentNode.insertBefore(warning, wrapper.nextSibling);
        } else {
            input.parentNode.insertBefore(warning, input.nextSibling);
        }

        const checkCapsLock = (e) => {
            if (e.getModifierState?.('CapsLock')) {
                warning.style.display = 'inline-flex';
            } else {
                warning.style.display = 'none';
            }
        };

        input.addEventListener('keydown', checkCapsLock);
        input.addEventListener('keyup', checkCapsLock);
        input.addEventListener('focus', checkCapsLock);
        input.addEventListener('blur', () => {
            warning.style.display = 'none';
        });
    });
}

/**
 * Attaches real-time, context-specific loading spinners to standard form submission buttons
 * across authentication and security credentials views.
 */
function setFormSubmitLoadingState(form) {
    const btn = form.querySelector('button[type="submit"]');
    if (!btn) return;

    let text = 'Processing...';
    if (btn.classList.contains('btn-login-submit')) {
        text = 'Unlocking Dashboard...';
    } else if (btn.classList.contains('btn-register-submit')) {
        text = 'Creating Account...';
    } else if (btn.classList.contains('btn-forgot-submit')) {
        text = 'Sending Recovery Link...';
    } else if (btn.classList.contains('btn-password-submit')) {
        text = 'Updating Credentials...';
    } else if (btn.classList.contains('btn-save-curation')) {
        text = 'Saving Curation...';
    } else if (btn.classList.contains('btn-confirm-submit')) {
        text = 'Updating Password...';
    } else if (form.id === 'delete-document-form') {
        text = 'Deleting Document...';
    } else if (form.id === 'settings-form') {
        text = 'Saving Configurations...';
    } else if (form.id === 'purge-all-form' || form.dataset.action === 'purge') {
        text = 'Wiping Database...';
    }

    btn.dataset.originalHtml = btn.innerHTML;
    const spinnerSvg = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="spinner" style="margin-right: 8px;"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>`;

    btn.innerHTML = `${spinnerSvg} ${text}`;
    btn.style.pointerEvents = 'none';
    btn.style.opacity = '0.85';
}

/**
 * Attaches real-time, context-specific loading spinners to standard form submission buttons
 * across authentication and security credentials views with bfcache restoration support.
 */
function initializeFormSubmitSpinners() {
    const forms = document.querySelectorAll(
        '.login-card form, .register-card form, .forgot-card form, .password-change-card form, #editor-form, #settings-form, .confirm-card form, #purge-all-form, #delete-document-form, form[data-action="purge"]'
    );

    forms.forEach(form => {
        form.addEventListener('submit', event => {
            if (event.defaultPrevented) return;
            setFormSubmitLoadingState(form);
        });
    });

    globalThis.addEventListener('pageshow', (event) => {
        if (event.persisted) {
            forms.forEach(form => {
                const btn = form.querySelector('button[type="submit"]');
                if (btn?.dataset?.originalHtml) {
                    btn.innerHTML = btn.dataset.originalHtml;
                    btn.style.pointerEvents = '';
                    btn.style.opacity = '';
                }
            });
        }
    });
}
/**
 * Create and render an accessible, animated client-side alert card.
 */
function showClientSideAlert(message, type = 'error') {
    let container = document.querySelector('.alert-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'alert-container';
        document.body.appendChild(container);
    }
    const cardId = 'client-alert-' + Date.now();
    const card = document.createElement('div');
    card.className = `alert-card alert-${type}`;
    card.id = cardId;
    card.setAttribute('role', 'alert');

    const iconName = type === 'success' ? 'check-circle' : 'alert-triangle';
    card.innerHTML = `
        <i data-lucide="${iconName}"></i>
        <span class="alert-msg-span"></span>
        <button type="button" class="alert-close-btn" aria-label="Dismiss message" title="Dismiss message" data-dismiss="${cardId}">
            <i data-lucide="x"></i>
        </button>
    `;
    card.querySelector('.alert-msg-span').textContent = message;
    container.appendChild(card);
    if (typeof lucide !== 'undefined' && lucide.createIcons) {
        lucide.createIcons();
    }

    let dismissTimeout = setTimeout(() => {
        dismissCard(card);
    }, 5000);

    card.addEventListener('mouseenter', () => clearTimeout(dismissTimeout));
    card.addEventListener('mouseleave', () => {
        dismissTimeout = setTimeout(() => {
            dismissCard(card);
        }, 5000);
    });
}
globalThis.showClientSideAlert = showClientSideAlert;

function dismissCard(card) {
    if (!card || card.classList.contains('fade-out')) return;
    card.classList.add('fade-out');

    let cleanedUp = false;
    const cleanup = () => {
        if (cleanedUp) return;
        cleanedUp = true;
        card.remove();
        const container = document.querySelector('.alert-container');
        if (container?.querySelectorAll('.alert-card').length === 0) {
            container.remove();
        }
    };

    card.addEventListener('transitionend', function handler(e) {
        if (['opacity', 'max-height', 'transform'].includes(e.propertyName)) {
            card.removeEventListener('transitionend', handler);
            cleanup();
        }
    });

    // Fallback safety timeout in case CSS transitions are skipped or reduced-motion is active
    setTimeout(cleanup, 400);
}

/**
 * Settings dialog modal, backdrop closing, and confirmation logic for danger reset memory.
 */
function initializeSettingsModal() {
    const settingsModal = document.getElementById('settings-modal');
    const settingsTriggerBtn = document.getElementById('settings-trigger-btn');
    const settingsCloseBtn = document.getElementById('settings-close-btn');
    
    const resetTriggerBtn = document.getElementById('reset-trigger-btn');
    const resetCancelBtn = document.getElementById('reset-cancel-btn');
    const resetConfirmInput = document.getElementById('reset_confirm_input');
    const finalResetBtn = document.getElementById('final-reset-btn');

    if (settingsTriggerBtn && settingsModal) {
        settingsTriggerBtn.addEventListener('click', () => {
            settingsModal.showModal();
        });
    }

    if (settingsCloseBtn && settingsModal) {
        settingsCloseBtn.addEventListener('click', () => {
            settingsModal.close();
        });
    }

    if (settingsModal) {
        // Ensure state resets when dialog closes (including via Escape key)
        settingsModal.addEventListener('close', () => {
            cancelResetConfirmation();
        });

        // Backdrop click handling
        settingsModal.addEventListener('click', (event) => {
            const rect = settingsModal.getBoundingClientRect();
            const isInDialog = (
                rect.top <= event.clientY && event.clientY <= rect.top + rect.height &&
                rect.left <= event.clientX && event.clientX <= rect.left + rect.width
            );
            if (!isInDialog) {
                settingsModal.close();
            }
        });
    }

    // Dual action Danger Zone Flow
    if (resetTriggerBtn) {
        resetTriggerBtn.addEventListener('click', () => {
            const initialState = document.getElementById('reset-initial-state');
            const confirmState = document.getElementById('reset-confirm-state');
            if (initialState) initialState.style.display = 'none';
            if (confirmState) confirmState.style.display = 'flex';
            if (resetConfirmInput) {
                resetConfirmInput.value = '';
                resetConfirmInput.focus();
            }
            if (finalResetBtn) {
                finalResetBtn.disabled = true;
                finalResetBtn.setAttribute('title', 'Type RESET to enable');
            }
        });
    }

    if (resetCancelBtn) {
        resetCancelBtn.addEventListener('click', cancelResetConfirmation);
    }

    if (resetConfirmInput) {
        resetConfirmInput.addEventListener('input', () => {
            if (finalResetBtn) {
                const val = resetConfirmInput.value.trim().toUpperCase();
                finalResetBtn.disabled = (val !== 'RESET');
                if (finalResetBtn.disabled) {
                    finalResetBtn.setAttribute('title', 'Type RESET to enable');
                } else {
                    finalResetBtn.removeAttribute('title');
                }
            }
        });
    }

    if (finalResetBtn) {
        finalResetBtn.addEventListener('click', (e) => {
            if (!confirm('Are you absolutely sure you want to permanently wipe all database and files? This cannot be undone. This irreversible action removes the database, local files, and connected cloud vectors.')) {
                e.preventDefault();
            }
        });
    }
}

function cancelResetConfirmation() {
    const initialState = document.getElementById('reset-initial-state');
    const confirmState = document.getElementById('reset-confirm-state');
    if (initialState) initialState.style.display = 'block';
    if (confirmState) confirmState.style.display = 'none';
}

/**
 * Handle drag-and-drop file upload zone interaction.
 */
function initializeDragAndDrop() {
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    const uploadForm = document.getElementById('upload-form');

    if (!dropZone || !fileInput || !uploadForm) return;

    function validateFilesAndSubmit(files) {
        const validFiles = filterUploadFiles(files);
        if (validFiles.length === 0) {
            fileInput.value = '';
            return;
        }

        if (typeof DataTransfer !== 'undefined') {
            const dt = new DataTransfer();
            for (const file of validFiles) {
                dt.items.add(file);
            }
            fileInput.files = dt.files;
        }



        // Show immediate loading state inside dropZone & prevent double submissions
        const loaderSvg = `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-loader spinner" style="color: var(--primary);"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>`;
        dropZone.innerHTML = `
            ${loaderSvg}
            <div class="upload-title">Ingesting ${validFiles.length} File${validFiles.length > 1 ? 's' : ''}...</div>
            <div class="upload-subtitle" style="animation: pulse 1.5s infinite ease-in-out;">Uploading to security scan and curation workspace. Please wait.</div>
        `;
        dropZone.style.pointerEvents = 'none';
        dropZone.style.borderColor = 'var(--primary)';
        dropZone.style.background = 'rgba(99, 102, 241, 0.04)';

        uploadForm.submit();
    }

    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            fileInput.click();
        }
    });

    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            validateFilesAndSubmit(fileInput.files);
        }
    });


    const originalDropZoneHtml = dropZone.innerHTML;

    globalThis.addEventListener('pageshow', (event) => {
        if (event.persisted && dropZone) {
            dropZone.innerHTML = originalDropZoneHtml;
            dropZone.style.pointerEvents = '';
            dropZone.style.borderColor = '';
            dropZone.style.background = '';
            if (fileInput) fileInput.value = '';
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
    });

    ['dragenter', 'dragover'].forEach(eventName => {
        dropZone.addEventListener(eventName, e => {
            e.preventDefault();
            dropZone.classList.add('dragover');
        }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, e => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
        }, false);
    });

    dropZone.addEventListener('drop', e => {
        const dt = e.dataTransfer;
        const files = dt.files;
        if (files.length > 0) {
            fileInput.files = files;
            validateFilesAndSubmit(files);
        }
    });
}

/**
 * Handle Library Export checklist, Select All / Clear All, and document deletion.
 */
function initializeExportActions() {
    const docSelectors = document.querySelectorAll('.doc-selector');
    const selectAllBtn = document.getElementById('btn-select-all');
    const clearAllBtn = document.getElementById('btn-clear-all');
    const bulkRestartBtn = document.getElementById('btn-bulk-restart');
    const bulkDeleteBtn = document.getElementById('btn-bulk-delete');
    const exportForm = document.getElementById('export-form');

    if (docSelectors) {
        docSelectors.forEach(cb => {
            cb.addEventListener('change', toggleExportFooter);
        });
    }

    if (selectAllBtn) {
        selectAllBtn.addEventListener('click', () => selectAllCheckbox(true));
    }

    if (clearAllBtn) {
        clearAllBtn.addEventListener('click', () => selectAllCheckbox(false));
    }

    // Dynamic multi-format export action routing based on format selector
    const exportFormatSelect = document.getElementById('export-format');
    if (exportForm) {
        exportForm.addEventListener('submit', () => {
            const staleActionInput = exportForm.querySelector('input[name="action"]');
            if (staleActionInput) {
                staleActionInput.remove();
            }
            if (exportFormatSelect) {
                const selectedOpt = exportFormatSelect.options[exportFormatSelect.selectedIndex];
                exportForm.action = selectedOpt.dataset.action || '/export/';
            } else {
                exportForm.action = '/export/';
            }
        });
    }

    const spinnerSvg = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="spinner" style="margin-right: 8px;"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>`;

    if (bulkRestartBtn && exportForm) {
        bulkRestartBtn.addEventListener('click', () => {
            if (confirm('Are you sure you want to restart curation for the selected documents?')) {
                bulkRestartBtn.innerHTML = `${spinnerSvg} Restarting...`;
                bulkRestartBtn.setAttribute('aria-disabled', 'true');
                bulkRestartBtn.style.pointerEvents = 'none';
                bulkRestartBtn.style.opacity = '0.85';

                let actionInput = exportForm.querySelector('input[name="action"]');
                if (!actionInput) {
                    actionInput = document.createElement('input');
                    actionInput.type = 'hidden';
                    actionInput.name = 'action';
                    exportForm.appendChild(actionInput);
                }
                actionInput.value = 'restart';
                exportForm.action = bulkRestartBtn.dataset.actionUrl;
                exportForm.submit();
            }
        });
    }

    if (bulkDeleteBtn && exportForm) {
        bulkDeleteBtn.addEventListener('click', () => {
            if (confirm('Are you sure you want to delete the selected documents? This cannot be undone.')) {
                bulkDeleteBtn.innerHTML = `${spinnerSvg} Deleting...`;
                bulkDeleteBtn.setAttribute('aria-disabled', 'true');
                bulkDeleteBtn.style.pointerEvents = 'none';
                bulkDeleteBtn.style.opacity = '0.85';

                let actionInput = exportForm.querySelector('input[name="action"]');
                if (!actionInput) {
                    actionInput = document.createElement('input');
                    actionInput.type = 'hidden';
                    actionInput.name = 'action';
                    exportForm.appendChild(actionInput);
                }
                actionInput.value = 'delete';
                exportForm.action = bulkDeleteBtn.dataset.actionUrl;
                exportForm.submit();
            }
        });
    }

    // BUG-01 REMOVED: The legacy document.addEventListener('click') block that
    // submitted id="delete-form" synchronously (non-AJAX) has been removed.
    // initializeDeleteActions() below registers the correct AJAX handler for
    // .btn-delete-doc and is the single authoritative click handler.

    const bulkRestartOrigHtml = bulkRestartBtn?.innerHTML;
    const bulkDeleteOrigHtml = bulkDeleteBtn?.innerHTML;

    globalThis.addEventListener('pageshow', (event) => {
        if (event.persisted) {
            if (bulkRestartBtn && bulkRestartOrigHtml) {
                bulkRestartBtn.innerHTML = bulkRestartOrigHtml;
                bulkRestartBtn.removeAttribute('aria-disabled');
                bulkRestartBtn.style.pointerEvents = '';
                bulkRestartBtn.style.opacity = '';
            }
            if (bulkDeleteBtn && bulkDeleteOrigHtml) {
                bulkDeleteBtn.innerHTML = bulkDeleteOrigHtml;
                bulkDeleteBtn.removeAttribute('aria-disabled');
                bulkDeleteBtn.style.pointerEvents = '';
                bulkDeleteBtn.style.opacity = '';
            }
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
    });
}

function toggleExportFooter() {
    const checkboxes = document.querySelectorAll('.doc-selector:checked');
    const count = checkboxes.length;
    const footer = document.getElementById('export-actions-bar');
    const countLabel = document.getElementById('selected-count');
    
    if (countLabel) countLabel.textContent = count;
    if (footer) {
        if (count > 0) {
            footer.classList.add('visible');
        } else {
            footer.classList.remove('visible');
        }
    }
}

function selectAllCheckbox(select) {
    const checkboxes = document.querySelectorAll('.doc-selector');
    checkboxes.forEach(cb => {
        const row = cb.closest('tr');
        if (select && row?.style.display === 'none') {
            // Ignore rows currently hidden by our instant table filter
            return;
        }
        cb.checked = select;
    });
    toggleExportFooter();
}

/**
 * Dynamic instant client-side filtering for the Library documents table with accessible clear control.
 */
function initializeLibraryFilter() {
    const filterInput = document.getElementById('table-filter');
    if (!filterInput) return;

    const wrapper = filterInput.closest('.table-filter-wrapper');

    // Create clear button dynamically to avoid markup modification and preserve accessibility
    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'table-filter-clear-btn';
    clearBtn.setAttribute('aria-label', 'Clear filter text');
    clearBtn.title = 'Clear filter';
    clearBtn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;

    if (wrapper) {
        wrapper.appendChild(clearBtn);
    }

    function updateUIState() {
        const query = (filterInput.value || '').trim();
        const hasText = query.length > 0;
        if (hasText) {
            clearBtn.style.display = 'inline-flex';
            filterInput.style.paddingRight = '28px';
        } else {
            clearBtn.style.display = 'none';
            filterInput.style.paddingRight = '12px';
        }
    }

    filterInput.addEventListener('input', () => {
        applyLibraryFilter(filterInput.value);
        updateUIState();
    });

    clearBtn.addEventListener('click', () => {
        filterInput.value = '';
        applyLibraryFilter('');
        updateUIState();
        filterInput.focus();
    });

    // Support Escape key to clear or blur
    filterInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (filterInput.value !== '') {
                e.preventDefault();
                filterInput.value = '';
                applyLibraryFilter('');
                updateUIState();
            } else {
                filterInput.blur();
            }
        }
    });

    // Run initial state setup
    updateUIState();
}

function applyLibraryFilter(filterValue) {
    const query = (filterValue || '').toLowerCase().trim();
    const rows = document.querySelectorAll('.files-panel table tbody tr[data-doc-id]');
    if (rows.length === 0) return; // Library is empty, no filter needed

    let visibleCount = 0;

    rows.forEach(row => {
        const cells = row.querySelectorAll('td');
        if (cells.length < 2) return; // Skip empty state or special rows

        // Search title (index 1) and author/metadata text (index 2) safely
        const titleText = cells[1] ? cells[1].textContent : '';
        const metaText = cells[2] ? cells[2].textContent : '';
        const textToSearch = (titleText + ' ' + metaText).toLowerCase();
        const matches = textToSearch.includes(query);
        if (matches) {
            row.style.display = '';
            visibleCount++;
        } else {
            row.style.display = 'none';
            // Uncheck hidden rows so they can't ghost-pollute the export footer count
            const cb = row.querySelector('.doc-selector');
            if (cb) cb.checked = false;
        }
    });

    // Dynamic filtered empty-state block
    let emptyStateRow = document.getElementById('table-filter-empty-row');
    if (visibleCount === 0 && query !== '') {
        if (!emptyStateRow) {
            emptyStateRow = document.createElement('tr');
            emptyStateRow.id = 'table-filter-empty-row';
            emptyStateRow.innerHTML = `
                <td colspan="6" class="empty-table-cell" style="text-align: center; padding: 32px 16px;">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--text-muted); margin-bottom: 12px; display: inline-block;"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><path d="M8 11h6"/></svg>
                    <p id="table-filter-empty-text" style="margin: 0 0 12px 0; font-size: 13px; color: var(--text-muted); font-weight: 500;"></p>
                    <button type="button" class="btn btn-secondary" id="table-filter-empty-clear-btn" title="Clear Filter">Clear Filter</button>
                </td>
            `;
            document.querySelector('.files-panel table tbody').appendChild(emptyStateRow);

            const clearBtn = emptyStateRow.querySelector('#table-filter-empty-clear-btn');
            if (clearBtn) {
                clearBtn.addEventListener('click', () => {
                    const filterInput = document.getElementById('table-filter');
                    if (filterInput) {
                        filterInput.value = '';
                        filterInput.dispatchEvent(new Event('input'));
                        filterInput.focus();
                    }
                });
            }
        }
        const emptyTextEl = emptyStateRow.querySelector('#table-filter-empty-text');
        if (emptyTextEl) {
            emptyTextEl.textContent = `No documents match your filter term "${filterValue}".`;
        }
    } else if (emptyStateRow) {
        emptyStateRow.remove();
    }

    // Sync the export footer count after any row visibility / checked-state changes
    toggleExportFooter();
}

/**
 * Vector Search panel querying and grounded Q&A rendering.
 */
function initializeRAGSearch() {
    const ragQuery = document.getElementById('rag-query');
    const ragBtn = document.getElementById('rag-btn');
    const ragLoader = document.getElementById('rag-loader');
    const ragResults = document.getElementById('rag-results-container');
    const ragAnswer = document.getElementById('rag-answer');
    const ragSourcesList = document.getElementById('rag-sources-list');
    const ragSearchForm = document.getElementById('rag-search-form');
    const errorSettingsTrigger = document.getElementById('error-settings-trigger');

    if (ragSearchForm) {
        ragSearchForm.addEventListener('submit', (e) => {
            e.preventDefault();
            runSemanticRAG();
        });
    }

    if (errorSettingsTrigger) {
        errorSettingsTrigger.addEventListener('click', (e) => {
            e.preventDefault();
            const modal = document.getElementById('settings-modal');
            if (modal) modal.showModal();
        });
    }

    function renderRagSources(sources) {
        ragSourcesList.innerHTML = '';
        if (!sources || sources.length === 0) {
            const li = document.createElement('li');
            li.textContent = 'No sources linked';
            ragSourcesList.appendChild(li);
            return;
        }
        sources.forEach(src => {
            const li = document.createElement('li');
            li.style.marginBottom = '6px';
            if (src.uuid) {
                const a = document.createElement('a');
                a.href = `/document/${encodeURIComponent(src.uuid)}/`;
                a.style.color = 'var(--accent)';
                a.style.textDecoration = 'none';
                a.style.fontWeight = '600';
                a.textContent = src.title || 'Untitled Document';
                li.appendChild(a);
            } else {
                const spanTitle = document.createElement('span');
                spanTitle.style.fontWeight = '600';
                spanTitle.textContent = src.title || 'Untitled Document';
                li.appendChild(spanTitle);
            }
            const span = document.createElement('span');
            span.textContent = ` (Lang: ${src.language || 'auto'}, Chunk: #${Number(src.chunk_index) + 1})`;
            li.appendChild(span);
            ragSourcesList.appendChild(li);
        });
    }

    function getSelectedDocIds() {
        const checkedBoxes = document.querySelectorAll('.doc-selector:checked');
        return Array.from(checkedBoxes).map(cb => cb.value);
    }

    function buildRagUrl(query) {
        const docIds = getSelectedDocIds();
        let url = `/rag-search/?q=${encodeURIComponent(query)}`;
        if (docIds.length > 0) {
            url += `&document_ids=${docIds.join(',')}`;
        }
        return url;
    }

    async function runSemanticRAG() {
        if (!ragQuery || !ragBtn || !ragLoader || !ragResults || !ragAnswer || !ragSourcesList) return;
        const query = ragQuery.value.trim();
        if (!query) {
            ragQuery.focus();
            if (typeof globalThis.showClientSideAlert === 'function') {
                globalThis.showClientSideAlert('Please enter a search query first.', 'error');
            }
            return;
        }

        ragLoader.style.display = 'block';
        ragResults.style.display = 'none';
        ragBtn.disabled = true;

        try {
            const data = await executeRagFetch(buildRagUrl(query));
            ragLoader.style.display = 'none';
            ragBtn.disabled = false;
            ragResults.style.display = 'block';
            ragAnswer.innerHTML = data.answer_html;
            renderRagSources(data.sources);
        } catch (err) {
            ragLoader.style.display = 'none';
            ragBtn.disabled = false;
            showClientSideAlert(err.message || 'An error occurred during vector search.');
            console.error(err);
        }
    }


    globalThis.addEventListener('pageshow', (event) => {
        if (event.persisted) {
            if (ragLoader) ragLoader.style.display = 'none';
            if (ragBtn) ragBtn.disabled = false;
        }
    });
}

/**
 * Initial global Chart instance pointer for smooth live token updates.
 */
let tokensChartInstance = null;

function initializeTokensChart() {
    const tokensChartEl = document.getElementById('tokensChart');
    if (!tokensChartEl) return;

    const ctx = tokensChartEl.getContext('2d');
    const prompt_tokens = Number.parseInt(tokensChartEl.dataset.prompt || '1', 10);
    const candidates_tokens = Number.parseInt(tokensChartEl.dataset.candidates || '0', 10);
    
    if (typeof Chart !== 'undefined') {
        tokensChartInstance = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: ['Input Prompt', 'Output Reason'],
                datasets: [{
                    data: [prompt_tokens || 1, candidates_tokens || 0],
                    backgroundColor: ['#6366f1', '#a855f7'],
                    borderWidth: 0,
                    hoverOffset: 4
                }]
            },
            options: {
                cutout: '75%',
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return ` ${context.label}: ${context.raw.toLocaleString()} tokens`;
                            }
                        }
                    }
                }
            }
        });
    }
}

/**
 * Compact helper to format numbers (e.g. 15000 -> 15.0K, 1500000 -> 1.5M).
 */
function formatCompact(num) {
    if (num >= 1000000) {
        return (num / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
    }
    if (num >= 1000) {
        return (num / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
    }
    return num.toString();
}

/**
 * Asynchronous Background Status Poller (Fallback).
 * Periodically queries `/api/documents/status/` and updates indicators dynamically.
 */
function _checkNeedsPolling() {
    // We poll if there are documents processing/pending on dashboard,
    // or if we are on the document detail screen and the current document is not completed/failed.
    const activeBadges = document.querySelectorAll('.badge-processing, .badge-pending');
    const detailTimelineContainer = document.querySelector('.timeline-container');

    let onDetailAndProcessing = false;
    if (detailTimelineContainer) {
        // If on details screen, check if timeline has non-completed states active or if we see a processing box
        const hasCompleted = detailTimelineContainer.querySelectorAll('.timeline-step.completed').length;
        // There are 4 steps total. If completed steps < 4 and there is no failure, we poll.
        const isFailed = document.querySelector('.timeline-step.failed') || document.querySelector('[data-doc-status="FAILED"]');
        if (hasCompleted < 4 && !isFailed) {
            onDetailAndProcessing = true;
        }
    }

    return (activeBadges.length > 0 || onDetailAndProcessing);
}

function initializeStatusPoller() {
    const POLL_INTERVAL = 5000; // 5 seconds
    let activePoll = false;

    if (_checkNeedsPolling()) {
        runPoller();
        setInterval(runPoller, POLL_INTERVAL);
    }

    async function runPoller() {
        if (activePoll) return;
        activePoll = true;
        try {
            const res = await fetch('/api/documents/status/');
            if (!res.ok) {
                console.warn(`[Poller] Status endpoint returned HTTP ${res.status}. Skipping update.`);
                return;
            }
            const data = await res.json();
            updateDashboardStats(data.stats);
            updateDocumentsTable(data.documents);
            updateDocumentDetailScreen(data);
        } catch (err) {
            console.error('[Poller] Fetch error:', err);
        } finally {
            activePoll = false;
        }
    }
}

/**
 * Supabase Realtime Subscription Integration.
 * Upgrades background polling to instant, native WebSockets when credentials are configured.
 */
function initializeSupabaseRealtime() {
    const supabaseUrl = document.body.dataset.supabaseUrl;
    const supabaseKey = document.body.dataset.supabaseKey;

    if (!supabaseUrl || !supabaseKey || typeof supabase === 'undefined') {
        console.debug("[Realtime] Realtime websocket channel unavailable. Falling back to background AJAX polling.");
        initializeStatusPoller();
        return;
    }

    console.debug("[Realtime] Upgrading to Supabase Realtime WebSockets...");
    const client = supabase.createClient(supabaseUrl, supabaseKey);

    // Gap D-7: Subscribe to broadcast events on 'document-updates' channel
    client
        .channel('document-updates')
        .on(
            'broadcast',
            { event: 'status-changed' },
            () => {
                console.debug('[Realtime] Received document update broadcast event');
                triggerUpdate();
            }
        )
        .subscribe((status) => {
            console.debug('[Realtime] Subscription status:', status);
        });

    let activeFetch = false;
    async function triggerUpdate() {
        if (activeFetch) return;
        activeFetch = true;
        try {
            const res = await fetch('/api/documents/status/');
            if (!res.ok) {
                console.warn(`[Realtime] Status endpoint returned HTTP ${res.status}. Skipping update.`);
                return;
            }
            const data = await res.json();
            updateDashboardStats(data.stats);
            updateDocumentsTable(data.documents);
            updateDocumentDetailScreen(data);
        } catch (err) {
            console.error('[Realtime] State update fetch error:', err);
        } finally {
            activeFetch = false;
        }
    }
    
    // Initial fetch to load stats and populate active state on page load
    triggerUpdate();
}

/**
 * Shared DOM Update Helpers for Polling and Realtime Channels.
 */

function _updateTokenMetricCard(stats) {
    const tokenContainer = document.querySelector('.metrics-row .metric-card:nth-child(4)');
    if (tokenContainer) {
        const tokenValue = tokenContainer.querySelector('.token-value-text');
        if (tokenValue) tokenValue.textContent = formatCompact(stats.total_tokens);
        const tokenSub = tokenContainer.querySelector('.token-sub-text');
        if (tokenSub) {
            tokenSub.innerHTML = `In: ${formatCompact(stats.prompt_tokens)}<br>Out: ${formatCompact(stats.candidates_tokens)}`;
        }
    }
    if (tokensChartInstance && stats.total_tokens > 0) {
        tokensChartInstance.data.datasets[0].data = [stats.prompt_tokens, stats.candidates_tokens];
        tokensChartInstance.update();
    }
}

function updateDashboardStats(stats) {
    if (!stats) return;

    const spendVal = document.querySelector('.metrics-row .metric-card:first-child .metric-value');
    if (spendVal) spendVal.textContent = stats.formatted_monthly_spent;

    const budgetSub = document.querySelector('.metrics-row .metric-card:first-child .metric-sub');
    if (budgetSub) {
        const fillBar = budgetSub.querySelector('.budget-bar-fill');
        if (fillBar) fillBar.style.width = stats.percent_spent + '%';
    }

    const pagesVal = document.querySelector('.metrics-row .metric-card:nth-child(2) .metric-value');
    if (pagesVal) pagesVal.textContent = stats.total_pages;

    const activeCount = document.getElementById('active-tasks-count');
    if (activeCount) {
        activeCount.textContent = stats.PENDING + stats.EXTRACTING + stats.REFINING + stats.EMBEDDING;
    }

    _updateTokenMetricCard(stats);

    const billingAlert = document.querySelector('.alert-error');
    if (billingAlert?.textContent.includes('Monthly API Billing Cap Triggered')) {
        if (!stats.budget_exceeded) billingAlert.remove();
    } else if (stats.budget_exceeded && !billingAlert) {
        location.reload();
    }
}


function _findTableRowForDoc(tbody, doc) {
    let row = tbody.querySelector(`tr[data-doc-id="${doc.id}"]`);
    if (row) return row;

    const chk = document.getElementById(`chk-doc-${doc.id}`);
    if (chk) return chk.closest('tr');

    const hrefKey = doc.uuid ? `/document/${doc.uuid}/` : `/document/${doc.id}/`;
    const link = tbody.querySelector(`a[href*="${hrefKey}"]`);
    return link ? link.closest('tr') : null;
}

function updateDocumentsTable(documents) {
    if (!documents) return;
    const tbody = document.querySelector('.files-panel table tbody');
    if (!tbody) return;

    documents.forEach(doc => {
        const row = _findTableRowForDoc(tbody, doc);
        if (!row) return;

        const previousStatus = row.dataset.status;
        row.dataset.docId = doc.id;
        row.dataset.status = doc.status;

        if (previousStatus && previousStatus !== doc.status && (doc.status === 'COMPLETED' || doc.status === 'FAILED')) {
            globalThis.location.reload();
            return;
        }

        const badgeTd = row.querySelector('td:nth-child(5)');
        if (badgeTd) {
            badgeTd.innerHTML = getStatusBadgeHTML(doc.status, doc.status_display);
        }

        const costTd = row.querySelector('td:nth-child(4)');
        if (costTd) {
            const costVal = costTd.querySelector('div');
            if (costVal) costVal.textContent = doc.formatted_cost;
            const costSub = costTd.querySelector('.doc-cost-sub');
            if (costSub && doc.input_tokens !== undefined && doc.output_tokens !== undefined) {
                costSub.textContent = `In: ${formatCompact(doc.input_tokens)} / Out: ${formatCompact(doc.output_tokens)}`;
            }
        }
    });

    // BUG-06: Prune rows for documents no longer returned by the status API.
    // Previously, deleted documents stayed visible until a hard page reload.
    const activeIds = new Set(documents.map(d => String(d.id)));
    const allRows = tbody.querySelectorAll('tr[data-doc-id]');
    allRows.forEach(row => {
        if (!activeIds.has(String(row.dataset.docId))) {
            row.style.transition = 'opacity 0.3s ease';
            row.style.opacity = '0';
            setTimeout(() => {
                if (row.parentNode) row.remove();
            }, 300);
        }
    });

    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }

    const filterInput = document.getElementById('table-filter');
    if (filterInput && filterInput.value.trim() !== '') {
        applyLibraryFilter(filterInput.value);
    }
}

function _updateDetailMetaFields(currentDoc, currentStatus) {
    const detailCost = document.getElementById('detail-cost');
    if (detailCost) detailCost.textContent = currentDoc.formatted_cost;

    const detailLang = document.getElementById('detail-lang');
    if (detailLang) {
        if (currentStatus === 'FAILED') detailLang.innerHTML = '<span class="text-danger">Failed</span>';
        else if (currentStatus === 'PENDING') detailLang.innerHTML = '<span class="text-muted">Queued...</span>';
        else if (currentDoc.language === 'Unknown') detailLang.innerHTML = '<span class="detecting-pulse"><i data-lucide="loader" class="spinner" style="width:12px; height:12px;"></i> Detecting...</span>';
        else detailLang.textContent = currentDoc.language;
    }

    const detailAuthor = document.getElementById('detail-author');
    if (detailAuthor) {
        if (currentStatus === 'FAILED') detailAuthor.innerHTML = '<span class="text-danger">Failed</span>';
        else if (currentStatus === 'PENDING') detailAuthor.innerHTML = '<span class="text-muted">Queued...</span>';
        else if (currentDoc.author === 'Unknown') detailAuthor.innerHTML = '<span class="detecting-pulse"><i data-lucide="loader" class="spinner" style="width:12px; height:12px;"></i> Detecting...</span>';
        else detailAuthor.textContent = currentDoc.author;
    }
}

function updateDocumentDetailScreen(data) {
    const detailTimelineContainer = document.querySelector('.timeline-container');
    if (!detailTimelineContainer) return;

    const match = /\/document\/([^/]+)\//.exec(globalThis.location.pathname);
    if (!match) return;

    const targetId = match[1];
    const currentDoc = data.documents.find(d => String(d.uuid) === targetId || String(d.id) === targetId);
    if (!currentDoc) return;

    _updateDetailMetaFields(currentDoc, currentDoc.status);

    const currentStatus = currentDoc.status;


    // Update active timeline steps
    const steps = detailTimelineContainer.querySelectorAll('.timeline-step');
    if (steps.length >= 4) {
        // Step 2: OCR
        updateTimelineStep(steps[1], ['EXTRACTING'].includes(currentStatus), ['REFINING', 'EMBEDDING', 'COMPLETED'].includes(currentStatus));
        // Step 3: Reasoning
        updateTimelineStep(steps[2], ['REFINING'].includes(currentStatus), ['EMBEDDING', 'COMPLETED'].includes(currentStatus));
        // Step 4: Embedding
        updateTimelineStep(steps[3], ['EMBEDDING'].includes(currentStatus), ['COMPLETED'].includes(currentStatus));
    }

    // Check if finished or failed. If so, reload once to render the rich SFT Q&A + Markdown textareas or the failure board
    const isCurrentlyProcessing = ['PENDING', 'EXTRACTING', 'REFINING', 'EMBEDDING'].includes(currentStatus);
    const hasEditorForm = document.getElementById('editor-form');
    const hasFailureBoard = document.querySelector('.timeline-step.failed') || document.querySelector('[data-doc-status="FAILED"]');
    
    const shouldReloadForTerminalState = !isCurrentlyProcessing
        && ((currentStatus === 'COMPLETED' && !hasEditorForm)
            || (currentStatus === 'FAILED' && !hasFailureBoard)
            || (!hasEditorForm && !hasFailureBoard));
    if (shouldReloadForTerminalState) {
        globalThis.location.reload();
    }
}

function _renderTimelineStepNode(stepEl, node, state) {
    if (!node) return;
    const icons = {
        completed: '<i data-lucide="check" style="width:14px; height:14px;"></i>',
        active: '<i data-lucide="loader" class="spinner" style="width:14px; height:14px;"></i>'
    };
    if (icons[state]) {
        node.innerHTML = icons[state];
        return;
    }
    const labelText = stepEl.querySelector('.step-label')?.textContent || '';
    const matchingStep = [['Reasoning', '3'], ['Vector', '4']].find(([label]) => labelText.includes(label));
    node.textContent = matchingStep?.[1] || '2';
}

function updateTimelineStep(stepEl, isActive, isCompleted) {
    let state = 'pending';
    if (isCompleted) state = 'completed';
    else if (isActive) state = 'active';
    stepEl.classList.remove('active', 'completed');
    stepEl.removeAttribute('aria-current');
    if (state !== 'pending') stepEl.classList.add(state);
    if (state === 'active') stepEl.setAttribute('aria-current', 'step');
    _renderTimelineStepNode(stepEl, stepEl.querySelector('.step-node'), state);
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
}


function getStatusBadgeHTML(status, display) {
    if (status === 'COMPLETED') {
        return `<span class="badge badge-completed"><i data-lucide="check-circle-2" style="width:12px; height:12px;"></i> ${display}</span>`;
    }
    if (status === 'FAILED') {
        return `<span class="badge badge-failed"><i data-lucide="x-circle" style="width:12px; height:12px;"></i> Failed</span>`;
    }
    if (status === 'PENDING') {
        return `<span class="badge badge-pending"><i data-lucide="clock" style="width:12px; height:12px;"></i> ${display}</span>`;
    }
    // Processing status (EXTRACTING, REFINING, EMBEDDING)
    return `<span class="badge badge-processing"><i data-lucide="loader" class="spinner" style="width:12px; height:12px;"></i> ${display}</span>`;
}


function _setButtonLoading(btn, isLoading) {
    const icon = btn?.querySelector?.('[data-lucide]') || btn?.querySelector?.('i');
    if (icon) {
        if (isLoading) {
            icon.classList.add('spinner');
        } else {
            icon.classList.remove('spinner');
        }
    }
    if (btn) {
        btn.disabled = isLoading;
    }
}

function _removeDeletedRow(btn) {
    const row = btn.closest('tr');
    if (!row) {
        globalThis.location.reload();
        return;
    }
    row.style.transition = 'opacity 0.25s ease';
    row.style.opacity = '0';
    setTimeout(() => {
        if (row.parentNode) row.remove();
        const remaining = document.querySelectorAll(
            '.files-panel table tbody tr[data-doc-id]'
        );
        if (remaining.length === 0) globalThis.location.reload();
    }, 250);
}

async function _postDocumentAction(docId, endpointSuffix) {
    if (typeof fetch !== 'function') {
        return { ok: true, json: async () => ({ status: 'success' }) };
    }
    const csrfTokenEl = document.querySelector('[name=csrfmiddlewaretoken]');
    const csrfToken = csrfTokenEl ? csrfTokenEl.value : '';
    return fetch(`/document/${docId}/${endpointSuffix}/`, {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrfToken,
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json'
        }
    });
}

async function _processDocumentActionResponse(btn, response, endpointSuffix, defaultErrorMsg) {
    const data = await response.json().catch(() => null);
    if (!response.ok) {
        throw new Error(data?.error || data?.message || defaultErrorMsg);
    }
    if (data?.status === 'success') {
        if (endpointSuffix === 'delete') {
            _removeDeletedRow(btn);
        } else {
            globalThis.location.reload();
        }
    } else {
        showClientSideAlert(data?.message || data?.error || defaultErrorMsg);
        _setButtonLoading(btn, false);
    }
}

/**
 * Helper to bind document state modifying actions (retry, cancel).
 */
function _handleDocumentStateAction({ buttonClass, confirmMsg, endpointSuffix, defaultErrorMsg }) {
    document.addEventListener('click', async (event) => {
        const btn = event.target.closest(buttonClass);
        if (!btn) return;

        event.preventDefault();
        const docId = btn.dataset.docId;
        if (!docId) return;

        if (confirmMsg && !confirm(confirmMsg)) {
            return;
        }

        _setButtonLoading(btn, true);

        try {
            const response = await _postDocumentAction(docId, endpointSuffix);
            await _processDocumentActionResponse(btn, response, endpointSuffix, defaultErrorMsg);
        } catch (err) {
            console.error('Error executing document action:', err);
            showClientSideAlert(err.message || defaultErrorMsg);
            _setButtonLoading(btn, false);
        }
    });

    globalThis.addEventListener('pageshow', (event) => {
        if (event.persisted) {
            document.querySelectorAll(buttonClass).forEach(b => {
                _setButtonLoading(b, false);
            });
        }
    });
}

/**
 * Handle manual curation retry actions for failed documents.
 */
function initializeRetryActions() {
    _handleDocumentStateAction({
        buttonClass: '.btn-retry-doc',
        confirmMsg: '',
        endpointSuffix: 'retry',
        defaultErrorMsg: 'Failed to re-enqueue document.'
    });
}

/**
 * Handle manual cancellation / stopping of in-flight or stuck curation tasks.
 */
function initializeCancelActions() {
    _handleDocumentStateAction({
        buttonClass: '.btn-cancel-doc',
        confirmMsg: 'Are you sure you want to stop processing this document?',
        endpointSuffix: 'cancel',
        defaultErrorMsg: 'Failed to stop document processing.'
    });
}

/**
 * Handle single document deletion from the dashboard actions column.
 */
function initializeDeleteActions() {
    _handleDocumentStateAction({
        buttonClass: '.btn-delete-doc',
        confirmMsg: 'Are you sure you want to delete this document? This cannot be undone.',
        endpointSuffix: 'delete',
        defaultErrorMsg: 'Failed to delete document.'
    });
}



/**
 * Renders UTC datetimes in the user's browser timezone. The semantic datetime
 * attribute is the primary source; data-utc keeps compatibility with old markup.
 */
function initializeLocalTimezones() {
    const timeElements = document.querySelectorAll('.local-datetime');
    timeElements.forEach(el => {
        const utcStr = el.dateTime || el.dataset.utc;
        if (!utcStr) return;
        
        try {
            const date = new Date(utcStr);
            if (Number.isNaN(date.getTime())) return;
            
            const options = {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false,
                timeZoneName: 'short'
            };

            el.textContent = new Intl.DateTimeFormat(undefined, options).format(date);
            el.setAttribute('title', `UTC: ${date.toISOString()}`);
        } catch (e) {
            console.error('Timezone conversion failed:', e);
        }
    });
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        formatCompact,
        getStatusBadgeHTML,
        showClientSideAlert,
        dismissCard,
        initializeAlerts,
        initializeDragAndDrop,
        updateTimelineStep,
        selectAllCheckbox,
        toggleExportFooter,
        applyLibraryFilter,
        initializeLibraryFilter,
        initializeSettingsModal,
        cancelResetConfirmation,
        initializeRetryActions,
        initializeCancelActions,
        initializeDeleteActions,
        initializeRAGSearch,
        initializeExportActions,
        initializeLocalTimezones,
        initializePasswordToggles,
        initializeCapsLockDetector,
        initializePasswordMatchFeedback,
        initializeAuditSearch,
        initializeSearchShortcuts,
        setFormSubmitLoadingState,
        initializeFormSubmitSpinners,
        initializeTokensChart,
        _updateTokenMetricCard,
        updateDashboardStats,
        updateDocumentsTable,
        updateDocumentDetailScreen,
        _updateDetailMetaFields,
        _checkNeedsPolling,
        initializeSupabaseRealtime
    };
}
