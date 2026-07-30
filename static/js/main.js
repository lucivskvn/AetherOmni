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

    // 9. Curation Pipeline Booklet Retries
    initializeRetryActions();

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
});

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
        const targetId = btn.getAttribute('data-dismiss');
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
    clearBtn.style.position = 'absolute';
    clearBtn.style.right = '12px';
    clearBtn.style.top = '50%';
    clearBtn.style.transform = 'translateY(-50%)';
    clearBtn.style.background = 'none';
    clearBtn.style.border = 'none';
    clearBtn.style.color = 'var(--text-muted)';
    clearBtn.style.cursor = 'pointer';
    clearBtn.style.display = 'none';
    clearBtn.style.alignItems = 'center';
    clearBtn.style.justifyContent = 'center';
    clearBtn.style.padding = '4px';
    clearBtn.style.zIndex = '5';
    clearBtn.style.transition = 'color 0.2s';
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
        wrapper.style.display = window.getComputedStyle(input).display === 'block' ? 'block' : 'inline-block';
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
            if (e.getModifierState && e.getModifierState('CapsLock')) {
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
function initializeFormSubmitSpinners() {
    const forms = document.querySelectorAll(
        '.login-card form, .register-card form, .forgot-card form, .password-change-card form'
    );

    forms.forEach(form => {
        form.addEventListener('submit', () => {
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
            }

            const spinnerSvg = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="spinner" style="margin-right: 8px;"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>`;

            btn.innerHTML = `${spinnerSvg} ${text}`;
            btn.style.pointerEvents = 'none';
            btn.style.opacity = '0.85';
        });
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
        <button type="button" class="alert-close-btn" aria-label="Dismiss message" data-dismiss="${cardId}">
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
window.showClientSideAlert = showClientSideAlert;

function dismissCard(card) {
    if (!card || card.classList.contains('fade-out')) return;
    card.classList.add('fade-out');
    card.addEventListener('transitionend', function handler(e) {
        if (['opacity', 'max-height', 'transform'].includes(e.propertyName)) {
            card.removeEventListener('transitionend', handler);
            card.remove();
            
            // Clean up alert-container if it becomes empty
            const container = document.querySelector('.alert-container');
            if (container && container.querySelectorAll('.alert-card').length === 0) {
                container.remove();
            }
        }
    });

    // Re-apply library table filtering on dynamic live updates
    const filterInput = document.getElementById('table-filter');
    if (filterInput && filterInput.value.trim().length > 0) {
        applyLibraryFilter(filterInput.value);
    }
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
            cancelResetConfirmation();
        });
    }

    // Backdrop click handling
    if (settingsModal) {
        settingsModal.addEventListener('click', (event) => {
            const rect = settingsModal.getBoundingClientRect();
            const isInDialog = (
                rect.top <= event.clientY && event.clientY <= rect.top + rect.height &&
                rect.left <= event.clientX && event.clientX <= rect.left + rect.width
            );
            if (!isInDialog) {
                settingsModal.close();
                cancelResetConfirmation();
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
        const MAX_SIZE = 31457280; // 30MB
        const ALLOWED_EXTENSIONS = ['.pdf', '.png', '.jpg', '.jpeg', '.csv', '.txt'];
        const dt = new DataTransfer();

        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            const ext = '.' + file.name.split('.').pop().toLowerCase();
            if (!ALLOWED_EXTENSIONS.includes(ext)) {
                showClientSideAlert(`Skipped "${file.name}": Unsupported format. (Use PDF, PNG, JPG, JPEG, CSV, TXT)`);
                continue;
            }
            if (file.size > MAX_SIZE) {
                showClientSideAlert(`Skipped "${file.name}": Exceeds maximum size limit of 30MB.`);
                continue;
            }
            dt.items.add(file);
        }

        if (dt.files.length === 0) {
            fileInput.value = '';
            return;
        }

        fileInput.files = dt.files;
        const validFiles = fileInput.files;

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



    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            validateFilesAndSubmit(fileInput.files);
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

    // Reset action back to default export zip URL if submitted via the primary Build Curated Bundle button
    if (exportForm) {
        exportForm.addEventListener('submit', () => {
            const actionInput = exportForm.querySelector('input[name="action"]');
            if (!actionInput) {
                exportForm.action = '/export/';
            }
        });
    }

    if (bulkRestartBtn && exportForm) {
        bulkRestartBtn.addEventListener('click', () => {
            if (confirm('Are you sure you want to restart curation for the selected documents?')) {
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

    // Deletion click handling delegation
    document.addEventListener('click', (event) => {
        const btn = event.target.closest('.btn-delete-doc');
        if (!btn) return;
        const docId = btn.dataset.docId;
        if (docId && confirm('Are you sure you want to delete this document from the library?')) {
            const form = document.getElementById('delete-form');
            if (form) {
                form.action = `/document/${docId}/delete/`;
                form.submit();
            }
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
        if (select && row && row.style.display === 'none') {
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
        row.style.display = matches ? '' : 'none';
        if (matches) visibleCount++;
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
                    <p id="table-filter-empty-text" style="margin: 0; font-size: 13px; color: var(--text-muted); font-weight: 500;"></p>
                </td>
            `;
            document.querySelector('.files-panel table tbody').appendChild(emptyStateRow);
        }
        const emptyTextEl = emptyStateRow.querySelector('#table-filter-empty-text');
        if (emptyTextEl) {
            emptyTextEl.textContent = `No documents match your filter term "${filterValue}".`;
        }
    } else if (emptyStateRow) {
        emptyStateRow.remove();
    }
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

    function runSemanticRAG() {
        if (!ragQuery || !ragBtn || !ragLoader || !ragResults || !ragAnswer || !ragSourcesList) return;
        const query = ragQuery.value.trim();
        if (!query) {
            ragQuery.focus();
            if (typeof window.showClientSideAlert === 'function') {
                window.showClientSideAlert('Please enter a search query first.', 'error');
            }
            return;
        }

        ragLoader.style.display = 'block';
        ragResults.style.display = 'none';
        ragBtn.disabled = true;

        const checkedBoxes = document.querySelectorAll('.doc-selector:checked');
        const docIds = Array.from(checkedBoxes).map(cb => cb.value);
        let url = `/rag-search/?q=${encodeURIComponent(query)}`;
        if (docIds.length > 0) {
            url += `&document_ids=${docIds.join(',')}`;
        }

        fetch(url)
            .then(res => res.json())
            .then(data => {
                ragLoader.style.display = 'none';
                ragBtn.disabled = false;
                
                if (data.error) {
                    showClientSideAlert(data.error);
                    return;
                }

                ragResults.style.display = 'block';
                ragAnswer.innerHTML = data.answer_html;
                
                ragSourcesList.innerHTML = '';
                if (data.sources && data.sources.length > 0) {
                    data.sources.forEach(src => {
                        const li = document.createElement('li');
                        li.style.marginBottom = '6px';
                        li.innerHTML = `<a href="/document/${src.uuid}/" style="color:var(--accent); text-decoration:none; font-weight:600;">${src.title}</a> (Lang: ${src.language}, Chunk: #${src.chunk_index+1})`;
                        ragSourcesList.appendChild(li);
                    });
                } else {
                    ragSourcesList.innerHTML = '<li>No sources linked</li>';
                }
            })
            .catch(err => {
                ragLoader.style.display = 'none';
                ragBtn.disabled = false;
                showClientSideAlert('An error occurred during vector search.');
                console.error(err);
            });
    }
}

/**
 * Initial global Chart instance pointer for smooth live token updates.
 */
let tokensChartInstance = null;

function initializeTokensChart() {
    const tokensChartEl = document.getElementById('tokensChart');
    if (!tokensChartEl) return;

    const ctx = tokensChartEl.getContext('2d');
    const prompt_tokens = parseInt(tokensChartEl.dataset.prompt || '1', 10);
    const candidates_tokens = parseInt(tokensChartEl.dataset.candidates || '0', 10);
    
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
 * /**
 * Asynchronous Background Status Poller (Fallback).
 * Periodically queries `/api/documents/status/` and updates indicators dynamically.
 */
function initializeStatusPoller() {
    const POLL_INTERVAL = 5000; // 5 seconds
    let activePoll = false;

    // Check if we need to poll immediately
    function checkNeedsPolling() {
        // We poll if there are documents processing/pending on dashboard,
        // or if we are on the document detail screen and the current document is not completed/failed.
        const activeBadges = document.querySelectorAll('.badge-processing, .badge-pending');
        const detailTimelineContainer = document.querySelector('.timeline-container');
        
        let onDetailAndProcessing = false;
        if (detailTimelineContainer) {
            // If on details screen, check if timeline has non-completed states active or if we see a processing box
            const activeSteps = detailTimelineContainer.querySelectorAll('.timeline-step.active');
            const hasCompleted = detailTimelineContainer.querySelectorAll('.timeline-step.completed').length;
            // There are 4 steps total. If completed steps < 4 and there is no failure, we poll.
            const isFailed = document.querySelector('.timeline-step.failed') || document.querySelector('[data-doc-status="FAILED"]');
            if (hasCompleted < 4 && !isFailed) {
                onDetailAndProcessing = true;
            }
        }

        return activeBadges.length > 0 || onDetailAndProcessing;
    }

    if (checkNeedsPolling()) {
        runPoller();
        setInterval(runPoller, POLL_INTERVAL);
    }

    function runPoller() {
        if (activePoll) return;
        activePoll = true;

        fetch('/api/documents/status/')
            .then(res => res.json())
            .then(data => {
                activePoll = false;
                updateDashboardStats(data.stats);
                updateDocumentsTable(data.documents);
                updateDocumentDetailScreen(data);
            })
            .catch(err => {
                activePoll = false;
                console.error('Poller error:', err);
            });
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
        console.log("[Realtime] Supabase Realtime credentials not detected or SDK not loaded. Falling back to background AJAX polling.");
        initializeStatusPoller();
        return;
    }

    console.log("[Realtime] Upgrading to Supabase Realtime WebSockets...");
    const client = supabase.createClient(supabaseUrl, supabaseKey);

    // Gap D-7: Subscribe to broadcast events on 'document-updates' channel
    const channel = client
        .channel('document-updates')
        .on(
            'broadcast',
            { event: 'status-changed' },
            (payload) => {
                console.log('[Realtime] Received document update broadcast:', payload);
                triggerUpdate();
            }
        )
        .subscribe((status) => {
            console.log('[Realtime] Subscription status:', status);
        });

    let activeFetch = false;
    function triggerUpdate() {
        if (activeFetch) return;
        activeFetch = true;
        fetch('/api/documents/status/')
            .then(res => res.json())
            .then(data => {
                activeFetch = false;
                updateDashboardStats(data.stats);
                updateDocumentsTable(data.documents);
                updateDocumentDetailScreen(data);
            })
            .catch(err => {
                activeFetch = false;
                console.error('[Realtime] State update fetch error:', err);
            });
    }
    
    // Initial fetch to load stats and populate active state on page load
    triggerUpdate();
}

/**
 * Shared DOM Update Helpers for Polling and Realtime Channels.
 */

function updateDashboardStats(stats) {
    if (!stats) return;

    // 1. Monthly AI compute spend
    const spendVal = document.querySelector('.metrics-row .metric-card:first-child .metric-value');
    if (spendVal) {
        spendVal.textContent = stats.formatted_monthly_spent;
    }

    const budgetSub = document.querySelector('.metrics-row .metric-card:first-child .metric-sub');
    if (budgetSub) {
        // Find progress fill bar and update style
        const fillBar = budgetSub.querySelector('.budget-bar-fill');
        if (fillBar) {
            fillBar.style.width = stats.percent_spent + '%';
        }
    }

    // 2. Extracted Pages count
    const pagesVal = document.querySelector('.metrics-row .metric-card:nth-child(2) .metric-value');
    if (pagesVal) {
        pagesVal.textContent = stats.total_pages;
    }

    // 3. Active pipelines
    const activeCount = document.getElementById('active-tasks-count');
    if (activeCount) {
        activeCount.textContent = stats.PENDING + stats.EXTRACTING + stats.REFINING + stats.EMBEDDING;
    }

    // 4. Token usage text & Chart
    const tokenContainer = document.querySelector('.metrics-row .metric-card:nth-child(4)');
    if (tokenContainer) {
        const tokenValue = tokenContainer.querySelector('.token-value-text');
        if (tokenValue) {
            tokenValue.textContent = formatCompact(stats.total_tokens);
        }
        const tokenSub = tokenContainer.querySelector('.token-sub-text');
        if (tokenSub) {
            tokenSub.innerHTML = `In: ${formatCompact(stats.prompt_tokens)}<br>Out: ${formatCompact(stats.candidates_tokens)}`;
        }
    }

    if (tokensChartInstance && stats.total_tokens > 0) {
        tokensChartInstance.data.datasets[0].data = [stats.prompt_tokens, stats.candidates_tokens];
        tokensChartInstance.update();
    }

    // 5. Budget Exceeded Alert card logic (dynamic show/hide)
    const billingAlert = document.querySelector('.alert-error');
    if (billingAlert && billingAlert.textContent.includes('Monthly API Billing Cap Triggered')) {
        if (!stats.budget_exceeded) {
            billingAlert.remove();
        }
    } else if (stats.budget_exceeded && !billingAlert) {
        // Reload page once to let Django render the warning beautifully
        location.reload();
    }
}

function updateDocumentsTable(documents) {
    if (!documents) return;
    const tbody = document.querySelector('.files-panel table tbody');
    if (!tbody) return;

    documents.forEach(doc => {
        // Find existing row
        let row = tbody.querySelector(`tr[data-doc-id="${doc.id}"]`);
        
        // If rows don't have data-doc-id, try searching by href or checkbox selector ID
        if (!row) {
            const chk = document.getElementById(`chk-doc-${doc.id}`);
            if (chk) {
                row = chk.closest('tr');
            } else if (doc.uuid) {
                // Search by link matching /document/UUID/
                const link = tbody.querySelector(`a[href*="/document/${doc.uuid}/"]`);
                if (link) {
                    row = link.closest('tr');
                }
            } else {
                // Search by link matching /document/ID/
                const link = tbody.querySelector(`a[href*="/document/${doc.id}/"]`);
                if (link) {
                    row = link.closest('tr');
                }
            }
        }

        if (row) {
            // Keep a record of current status on row
            const previousStatus = row.dataset.status;
            row.dataset.docId = doc.id;
            row.dataset.status = doc.status;

            // If status changed to completed/failed, trigger reload once to get fresh links & checkboxes,
            // or update columns beautifully
            if (previousStatus && previousStatus !== doc.status) {
                if (doc.status === 'COMPLETED' || doc.status === 'FAILED') {
                    // Reloading is safest when transition concludes to let Django render permissions/downloads
                    globalThis.location.reload();
                    return;
                }
            }

            // Update Status Column
            const badgeTd = row.querySelector('td:nth-child(5)');
            if (badgeTd) {
                badgeTd.innerHTML = getStatusBadgeHTML(doc.status, doc.status_display);
            }

            // Update Cost & Token details column
            const costTd = row.querySelector('td:nth-child(4)');
            if (costTd) {
                const costVal = costTd.querySelector('div');
                if (costVal) {
                    costVal.textContent = doc.formatted_cost;
                }
                const costSub = costTd.querySelector('.doc-cost-sub');
                if (costSub && typeof doc.input_tokens !== 'undefined' && typeof doc.output_tokens !== 'undefined') {
                    costSub.textContent = `In: ${formatCompact(doc.input_tokens)} / Out: ${formatCompact(doc.output_tokens)}`;
                }
            }
        }
    });
}

function updateDocumentDetailScreen(data) {
    const detailTimelineContainer = document.querySelector('.timeline-container');
    if (!detailTimelineContainer) return;

    // Get document ID from URL /document/ID/
    const match = globalThis.location.pathname.match(/\/document\/(\d+)\//);
    if (!match) return;
    const docId = parseInt(match[1], 10);

    const currentDoc = data.documents.find(d => d.id === docId);
    if (!currentDoc) return;

    const currentStatus = currentDoc.status;
    
    // Update meta values
    const detailCost = document.getElementById('detail-cost');
    if (detailCost) {
        detailCost.textContent = currentDoc.formatted_cost;
    }

    const detailLang = document.getElementById('detail-lang');
    if (detailLang) {
        if (currentStatus === 'FAILED') {
            detailLang.innerHTML = '<span class="text-danger">Failed</span>';
        } else if (currentStatus === 'PENDING') {
            detailLang.innerHTML = '<span class="text-muted">Queued...</span>';
        } else if (currentDoc.language === 'Unknown') {
            detailLang.innerHTML = '<span class="detecting-pulse"><i data-lucide="loader" class="spinner" style="width:12px; height:12px;"></i> Detecting...</span>';
        } else {
            detailLang.textContent = currentDoc.language;
        }
    }

    const detailAuthor = document.getElementById('detail-author');
    if (detailAuthor) {
        if (currentStatus === 'FAILED') {
            detailAuthor.innerHTML = '<span class="text-danger">Failed</span>';
        } else if (currentStatus === 'PENDING') {
            detailAuthor.innerHTML = '<span class="text-muted">Queued...</span>';
        } else if (currentDoc.author === 'Unknown') {
            detailAuthor.innerHTML = '<span class="detecting-pulse"><i data-lucide="loader" class="spinner" style="width:12px; height:12px;"></i> Detecting...</span>';
        } else {
            detailAuthor.textContent = currentDoc.author;
        }
    }

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

    // Check if finished. If so, reload once to load the rich SFT Q&A + Markdown textareas
    const isCurrentlyProcessing = ['PENDING', 'EXTRACTING', 'REFINING', 'EMBEDDING'].includes(currentStatus);
    const hasEditorForm = document.getElementById('editor-form');
    const hasFailureBoard = document.querySelector('.timeline-step.failed') || document.querySelector('[data-doc-status="FAILED"]');
    
    if (!isCurrentlyProcessing && !hasEditorForm && !hasFailureBoard) {
        globalThis.location.reload();
    }
}

function updateTimelineStep(stepEl, isActive, isCompleted) {
    stepEl.classList.remove('active', 'completed');
    const node = stepEl.querySelector('.step-node');
    
    if (isCompleted) {
        stepEl.classList.add('completed');
        if (node) node.innerHTML = '<i data-lucide="check" style="width:14px; height:14px;"></i>';
    } else if (isActive) {
        stepEl.classList.add('active');
        stepEl.setAttribute('aria-current', 'step');
        if (node) node.innerHTML = '<i data-lucide="loader" class="spinner" style="width:14px; height:14px;"></i>';
    } else {
        // Set index
        const labelText = stepEl.querySelector('.step-label').textContent;
        let idx = '2';
        if (labelText.includes('Reasoning')) idx = '3';
        if (labelText.includes('Vector')) idx = '4';
        if (node) node.textContent = idx;
    }

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


/**
 * Handle manual curation retry actions for failed documents.
 */
function initializeRetryActions() {
    document.addEventListener('click', (event) => {
        const btn = event.target.closest('.btn-retry-doc');
        if (!btn) return;
        
        event.preventDefault();
        const docId = btn.dataset.docId;
        if (!docId) return;
        
        // Show spinning loader on button
        const icon = btn.querySelector('[data-lucide]') || btn.querySelector('i');
        if (icon) {
            icon.classList.add('spinner');
        }
        btn.disabled = true;
        
        // Retrieve Django CSRF token
        const csrfTokenEl = document.querySelector('[name=csrfmiddlewaretoken]');
        const csrfToken = csrfTokenEl ? csrfTokenEl.value : '';
        
        fetch(`/document/${docId}/retry/`, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrfToken,
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json'
            }
        })
        .then(response => {
            if (!response.ok) {
                throw new Error('Network response was not ok');
            }
            return response.json();
        })
        .then(data => {
            if (data.status === 'success') {
                // Instantly reload to transition to PENDING/processing state
                window.location.reload();
            } else {
                showClientSideAlert(data.message || 'Failed to re-enqueue booklet.');
                if (icon) {
                    icon.classList.remove('spinner');
                }
                btn.disabled = false;
            }
        })
        .catch(err => {
            console.error('Error re-enqueuing:', err);
            showClientSideAlert('An error occurred while retrying the curation pipeline.');
            if (icon) {
                icon.classList.remove('spinner');
            }
            btn.disabled = false;
        });
    });
}


/**
 * Automatically parses UTC timestamps from data-utc attribute and formats them
 * in the user's browser/system region timezone, appending the timezone name.
 */
function initializeLocalTimezones() {
    const timeElements = document.querySelectorAll('.local-datetime');
    timeElements.forEach(el => {
        const utcStr = el.dataset.utc;
        if (!utcStr) return;
        
        try {
            const date = new Date(utcStr);
            if (isNaN(date.getTime())) return;
            
            // Format using user's system locale and configuration
            const options = {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false
            };
            
            // Format the main date/time part
            const formatter = new Intl.DateTimeFormat(undefined, options);
            const formattedDate = formatter.format(date);
            
            // Resolve local timezone abbreviation or offset (e.g. SGT or GMT+8)
            const tzOptions = { timeZoneName: 'short' };
            const tzFormatter = new Intl.DateTimeFormat(undefined, tzOptions);
            const parts = tzFormatter.formatToParts(date);
            const tzPart = parts.find(p => p.type === 'timeZoneName');
            const tzName = tzPart ? tzPart.value : '';
            
            el.textContent = `${formattedDate} ${tzName}`.trim();
        } catch (e) {
            console.error('Timezone conversion failed:', e);
        }
    });
}


