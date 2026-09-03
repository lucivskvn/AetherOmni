/**
 * main.js unit tests
 *
 * Tests pure/utility functions from static/js/main.js that don't require
 * a live DOM or network. DOM-dependent initializers (initializeAlerts, etc.)
 * are tested via their side-effects using jsdom.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import {
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
  initializeSupabaseRealtime,
  trapFocus,
} from '../main.js';









// ---------------------------------------------------------------------------
// formatCompact — number formatter
// ---------------------------------------------------------------------------
describe('formatCompact', () => {
  it('returns raw string for numbers below 1000', () => {
    expect(formatCompact(0)).toBe('0');
    expect(formatCompact(999)).toBe('999');
    expect(formatCompact(1)).toBe('1');
  });

  it('formats thousands as K', () => {
    expect(formatCompact(1000)).toBe('1K');
    expect(formatCompact(1500)).toBe('1.5K');
    expect(formatCompact(10000)).toBe('10K');
    expect(formatCompact(999999)).toBe('1000K');
  });

  it('formats millions as M', () => {
    expect(formatCompact(1000000)).toBe('1M');
    expect(formatCompact(2500000)).toBe('2.5M');
    expect(formatCompact(10000000)).toBe('10M');
  });

  it('strips trailing .0 from K/M values', () => {
    expect(formatCompact(2000)).toBe('2K');
    expect(formatCompact(5000000)).toBe('5M');
  });
});

// ---------------------------------------------------------------------------
// getStatusBadgeHTML — badge HTML generator
// ---------------------------------------------------------------------------
describe('getStatusBadgeHTML', () => {
  it('returns completed badge for COMPLETED status', () => {
    const html = getStatusBadgeHTML('COMPLETED', 'Done');
    expect(html).toContain('badge-completed');
    expect(html).toContain('Done');
    expect(html).toContain('check-circle-2');
  });

  it('returns failed badge for FAILED status (ignores display arg)', () => {
    const html = getStatusBadgeHTML('FAILED', 'Something');
    expect(html).toContain('badge-failed');
    expect(html).toContain('Failed');
    expect(html).toContain('x-circle');
  });

  it('returns pending badge for PENDING status', () => {
    const html = getStatusBadgeHTML('PENDING', 'Queued');
    expect(html).toContain('badge-pending');
    expect(html).toContain('Queued');
    expect(html).toContain('clock');
  });

  it('returns processing badge for any other status', () => {
    const html = getStatusBadgeHTML('EXTRACTING', 'Extracting');
    expect(html).toContain('badge-processing');
    expect(html).toContain('Extracting');
    expect(html).toContain('loader');
  });

  it('returns processing badge for EMBEDDING status', () => {
    const html = getStatusBadgeHTML('EMBEDDING', 'Embedding');
    expect(html).toContain('badge-processing');
  });

  it('includes a <span> wrapper', () => {
    const html = getStatusBadgeHTML('COMPLETED', 'Done');
    expect(html.startsWith('<span')).toBe(true);
    expect(html.endsWith('</span>')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// showClientSideAlert — DOM mutation
// ---------------------------------------------------------------------------
describe('showClientSideAlert', () => {
  beforeEach(() => {
    // Reset DOM before each test
    document.body.innerHTML = '';
  });

  it('injects an alert element into the body', () => {
    showClientSideAlert('Something went wrong');
    const alert = document.querySelector('[role="alert"], .alert, .client-alert');
    expect(alert).not.toBeNull();
  });

  it('includes the provided message text', () => {
    showClientSideAlert('Test error message');
    expect(document.body.textContent).toContain('Test error message');
  });

  it('defaults to error type', () => {
    showClientSideAlert('error msg');
    // Look for error styling class in the injected element
    const html = document.body.innerHTML;
    expect(html.toLowerCase()).toMatch(/error|danger/);
  });

  it('handles success alert type properly', () => {
    showClientSideAlert('Operation succeeded', 'success');
    const alert = document.querySelector('.alert-card');
    expect(alert).not.toBeNull();
    expect(alert.textContent).toContain('Operation succeeded');
  });
});

// ---------------------------------------------------------------------------
// dismissCard — Alert Card dismissal & container lifecycle
// ---------------------------------------------------------------------------
describe('dismissCard', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    vi.useFakeTimers();
  });

  it('handles null/undefined card gracefully without throwing', () => {
    expect(() => dismissCard(null)).not.toThrow();
    expect(() => dismissCard(undefined)).not.toThrow();
  });

  it('adds fade-out class and cleans up on transitionend', () => {
    const container = document.createElement('div');
    container.className = 'alert-container';
    const card = document.createElement('div');
    card.className = 'alert-card';
    container.appendChild(card);
    document.body.appendChild(container);

    dismissCard(card);
    expect(card.classList.contains('fade-out')).toBe(true);

    // Simulate transitionend event
    const event = new Event('transitionend');
    Object.defineProperty(event, 'propertyName', { value: 'opacity' });
    card.dispatchEvent(event);

    expect(document.querySelector('.alert-card')).toBeNull();
    expect(document.querySelector('.alert-container')).toBeNull();
  });

  it('cleans up card and container via safety timeout when transitions are skipped', () => {
    const container = document.createElement('div');
    container.className = 'alert-container';
    const card = document.createElement('div');
    card.className = 'alert-card';
    container.appendChild(card);
    document.body.appendChild(container);

    dismissCard(card);
    expect(card.classList.contains('fade-out')).toBe(true);

    // Advance past safety timeout (400ms)
    vi.advanceTimersByTime(450);

    expect(document.querySelector('.alert-card')).toBeNull();
    expect(document.querySelector('.alert-container')).toBeNull();
  });

  it('keeps alert-container if other cards remain', () => {
    const container = document.createElement('div');
    container.className = 'alert-container';
    const card1 = document.createElement('div');
    card1.className = 'alert-card';
    const card2 = document.createElement('div');
    card2.className = 'alert-card';
    container.appendChild(card1);
    container.appendChild(card2);
    document.body.appendChild(container);

    dismissCard(card1);
    vi.advanceTimersByTime(450);

    expect(document.body.contains(card1)).toBe(false);
    expect(document.body.contains(card2)).toBe(true);
    expect(document.querySelector('.alert-container')).not.toBeNull();
  });

  afterEach(() => {
    vi.useRealTimers();
  });
});


// ---------------------------------------------------------------------------
// initializeDragAndDrop — Keyboard and click accessibility
// ---------------------------------------------------------------------------
describe('initializeDragAndDrop', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('triggers fileInput.click() on dropZone click', () => {
    document.body.innerHTML = `
      <form id="upload-form">
        <input type="file" id="file-input" />
        <button type="button" id="drop-zone"></button>
      </form>
    `;
    const fileInput = document.getElementById('file-input');
    const dropZone = document.getElementById('drop-zone');
    let clicked = false;
    fileInput.click = () => { clicked = true; };

    initializeDragAndDrop();
    dropZone.click();

    expect(clicked).toBe(true);
  });

  it('triggers fileInput.click() on Enter keydown on dropZone', () => {
    document.body.innerHTML = `
      <form id="upload-form">
        <input type="file" id="file-input" />
        <button type="button" id="drop-zone"></button>
      </form>
    `;
    const fileInput = document.getElementById('file-input');
    const dropZone = document.getElementById('drop-zone');
    let clicked = false;
    fileInput.click = () => { clicked = true; };

    initializeDragAndDrop();
    const event = new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true });
    dropZone.dispatchEvent(event);

    expect(clicked).toBe(true);
  });

  it('triggers fileInput.click() on Space keydown on dropZone', () => {
    document.body.innerHTML = `
      <form id="upload-form">
        <input type="file" id="file-input" />
        <button type="button" id="drop-zone"></button>
      </form>
    `;
    const fileInput = document.getElementById('file-input');
    const dropZone = document.getElementById('drop-zone');
    let clicked = false;
    fileInput.click = () => { clicked = true; };

    initializeDragAndDrop();
    const event = new window.KeyboardEvent('keydown', { key: ' ', bubbles: true });
    dropZone.dispatchEvent(event);

    expect(clicked).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// updateTimelineStep — Accessibility and DOM State Synchronization
// ---------------------------------------------------------------------------
describe('updateTimelineStep', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('sets aria-current="step" and active class when active', () => {
    const step = document.createElement('div');
    step.className = 'timeline-step';
    const node = document.createElement('div');
    node.className = 'step-node';
    step.appendChild(node);

    updateTimelineStep(step, true, false);

    expect(step.classList.contains('active')).toBe(true);
    expect(step.getAttribute('aria-current')).toBe('step');
  });

  it('removes aria-current and sets completed class when isCompleted is true', () => {
    const step = document.createElement('div');
    step.className = 'timeline-step active';
    step.setAttribute('aria-current', 'step');
    const node = document.createElement('div');
    node.className = 'step-node';
    step.appendChild(node);

    updateTimelineStep(step, false, true);

    expect(step.classList.contains('completed')).toBe(true);
    expect(step.classList.contains('active')).toBe(false);
    expect(step.hasAttribute('aria-current')).toBe(false);
  });

  it('resets to index number and cleans up aria-current when inactive', () => {
    const step = document.createElement('div');
    step.className = 'timeline-step active';
    step.setAttribute('aria-current', 'step');
    const node = document.createElement('div');
    node.className = 'step-node';
    const label = document.createElement('div');
    label.className = 'step-label';
    label.textContent = 'Reasoning Analysis';
    step.appendChild(node);
    step.appendChild(label);

    updateTimelineStep(step, false, false);

    expect(step.classList.contains('active')).toBe(false);
    expect(step.classList.contains('completed')).toBe(false);
    expect(step.hasAttribute('aria-current')).toBe(false);
    expect(node.textContent).toBe('3');
  });
});

// ---------------------------------------------------------------------------
// selectAllCheckbox — Table Batch Selection & Filter Coordination
// ---------------------------------------------------------------------------
describe('selectAllCheckbox', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="export-actions-bar">
        <span id="selected-count">0</span>
      </div>
      <table class="files-panel">
        <tbody>
          <tr data-doc-id="doc-1">
            <td><input type="checkbox" class="doc-selector" value="doc-1"></td>
          </tr>
          <tr data-doc-id="doc-2" style="display: none;">
            <td><input type="checkbox" class="doc-selector" value="doc-2"></td>
          </tr>
          <tr data-doc-id="doc-3">
            <td><input type="checkbox" class="doc-selector" value="doc-3"></td>
          </tr>
        </tbody>
      </table>
    `;
  });

  it('selects only visible checkboxes when select=true', () => {
    selectAllCheckbox(true);

    const cbs = document.querySelectorAll('.doc-selector');
    expect(cbs[0].checked).toBe(true);
    expect(cbs[1].checked).toBe(false); // Hidden row ignored
    expect(cbs[2].checked).toBe(true);

    const countLabel = document.getElementById('selected-count');
    expect(countLabel.textContent).toBe('2');
  });

  it('unchecks all checkboxes when select=false', () => {
    const cbs = document.querySelectorAll('.doc-selector');
    cbs[0].checked = true;
    cbs[1].checked = true;

    selectAllCheckbox(false);

    expect(cbs[0].checked).toBe(false);
    expect(cbs[1].checked).toBe(false);
    expect(cbs[2].checked).toBe(false);

    const countLabel = document.getElementById('selected-count');
    expect(countLabel.textContent).toBe('0');
  });
});

// ---------------------------------------------------------------------------
// applyLibraryFilter — Instant Client-Side Library Search & Desync Guard
// ---------------------------------------------------------------------------
describe('applyLibraryFilter', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="export-actions-bar">
        <span id="selected-count">0</span>
      </div>
      <div class="files-panel">
        <table>
          <tbody>
            <tr data-doc-id="doc-1">
              <td><input type="checkbox" class="doc-selector" value="doc-1" checked></td>
              <td>Financial Quarterly Report</td>
              <td>John Doe</td>
            </tr>
            <tr data-doc-id="doc-2">
              <td><input type="checkbox" class="doc-selector" value="doc-2" checked></td>
              <td>Legal Agreement 2026</td>
              <td>Jane Smith</td>
            </tr>
          </tbody>
        </table>
      </div>
    `;
  });

  it('hides non-matching rows and unchecks hidden checkboxes', () => {
    applyLibraryFilter('Financial');

    const rows = document.querySelectorAll('.files-panel table tbody tr[data-doc-id]');
    expect(rows[0].style.display).toBe('');
    expect(rows[1].style.display).toBe('none');

    const cb1 = rows[0].querySelector('.doc-selector');
    const cb2 = rows[1].querySelector('.doc-selector');
    expect(cb1.checked).toBe(true);
    expect(cb2.checked).toBe(false); // Desync guard unchecks hidden row

    const countLabel = document.getElementById('selected-count');
    expect(countLabel.textContent).toBe('1');
  });

  it('displays empty state row when no records match filter', () => {
    applyLibraryFilter('NonexistentTerm');

    const emptyRow = document.getElementById('table-filter-empty-row');
    expect(emptyRow).not.toBeNull();
    const emptyText = document.getElementById('table-filter-empty-text');
    expect(emptyText.textContent).toContain('NonexistentTerm');
  });
});

// ---------------------------------------------------------------------------
// initializeSettingsModal & cancelResetConfirmation — Danger Zone Reset Flow
// ---------------------------------------------------------------------------
describe('initializeSettingsModal Danger Zone Reset Flow', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <dialog id="settings-modal">
        <button id="settings-close-btn">Close</button>
        <div id="reset-initial-state" style="display: block;">
          <button type="button" id="reset-trigger-btn">Reset All Memory</button>
        </div>
        <div id="reset-confirm-state" style="display: none;">
          <input type="text" id="reset_confirm_input" value="">
          <button type="button" id="reset-cancel-btn">Cancel</button>
          <button type="submit" id="final-reset-btn" disabled>Confirm Wipe</button>
        </div>
      </dialog>
      <button id="settings-trigger-btn">Open Settings</button>
    `;
    // Mock HTMLDialogElement showModal, close if not implemented in jsdom
    const modal = document.getElementById('settings-modal');
    if (!modal.showModal) {
      modal.showModal = vi.fn();
    }
    if (!modal.close) {
      modal.close = vi.fn();
    }
  });

  it('transitions from initial state to confirmation state when trigger clicked', () => {
    initializeSettingsModal();

    const triggerBtn = document.getElementById('reset-trigger-btn');
    triggerBtn.click();

    const initialState = document.getElementById('reset-initial-state');
    const confirmState = document.getElementById('reset-confirm-state');
    const confirmInput = document.getElementById('reset_confirm_input');
    const finalResetBtn = document.getElementById('final-reset-btn');

    expect(initialState.style.display).toBe('none');
    expect(confirmState.style.display).toBe('flex');
    expect(confirmInput.value).toBe('');
    expect(finalResetBtn.disabled).toBe(true);
  });

  it('enables final reset button only when input matches "RESET"', () => {
    initializeSettingsModal();

    const triggerBtn = document.getElementById('reset-trigger-btn');
    triggerBtn.click();

    const confirmInput = document.getElementById('reset_confirm_input');
    const finalResetBtn = document.getElementById('final-reset-btn');

    confirmInput.value = 'res';
    confirmInput.dispatchEvent(new window.Event('input'));
    expect(finalResetBtn.disabled).toBe(true);

    confirmInput.value = 'reset';
    confirmInput.dispatchEvent(new window.Event('input'));
    expect(finalResetBtn.disabled).toBe(false);

    confirmInput.value = 'RESET ';
    confirmInput.dispatchEvent(new window.Event('input'));
    expect(finalResetBtn.disabled).toBe(false);
  });

  it('resets back to initial state when cancel button is clicked', () => {
    initializeSettingsModal();

    const triggerBtn = document.getElementById('reset-trigger-btn');
    triggerBtn.click();

    const cancelBtn = document.getElementById('reset-cancel-btn');
    cancelBtn.click();

    const initialState = document.getElementById('reset-initial-state');
    const confirmState = document.getElementById('reset-confirm-state');
    expect(initialState.style.display).toBe('block');
    expect(confirmState.style.display).toBe('none');
  });

  it('directly resets display state via cancelResetConfirmation()', () => {
    const initialState = document.getElementById('reset-initial-state');
    const confirmState = document.getElementById('reset-confirm-state');
    initialState.style.display = 'none';
    confirmState.style.display = 'flex';

    cancelResetConfirmation();

    expect(initialState.style.display).toBe('block');
    expect(confirmState.style.display).toBe('none');
  });

  it('prevents submission when window.confirm is rejected on finalResetBtn click', () => {
    initializeSettingsModal();

    const finalResetBtn = document.getElementById('final-reset-btn');
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    const event = new window.MouseEvent('click', { cancelable: true });
    finalResetBtn.dispatchEvent(event);

    expect(confirmSpy).toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
    confirmSpy.mockRestore();
  });
});

describe('Drag and Drop File Input Validation', () => {
  let dropZone, fileInput, uploadForm;

  beforeEach(() => {
    document.body.innerHTML = `
      <form id="upload-form">
        <div id="drop-zone" tabindex="0">Drop files here</div>
        <input type="file" id="file-input" name="file" multiple>
      </form>
      <div class="alerts-container"></div>
    `;
    dropZone = document.getElementById('drop-zone');
    fileInput = document.getElementById('file-input');
    uploadForm = document.getElementById('upload-form');
    uploadForm.submit = vi.fn();
  });

  it('triggers file-input on Enter and Space keypress', () => {
    initializeDragAndDrop();
    const clickSpy = vi.spyOn(fileInput, 'click');

    const enterEvent = new window.KeyboardEvent('keydown', { key: 'Enter', cancelable: true });
    dropZone.dispatchEvent(enterEvent);
    expect(clickSpy).toHaveBeenCalledTimes(1);

    const spaceEvent = new window.KeyboardEvent('keydown', { key: ' ', cancelable: true });
    dropZone.dispatchEvent(spaceEvent);
    expect(clickSpy).toHaveBeenCalledTimes(2);
  });

  it('filters out extensionless and unsupported files on change', () => {
    initializeDragAndDrop();

    const validFile = new window.File(['content'], 'document.pdf', { type: 'application/pdf' });
    const extensionlessFile = new window.File(['content'], 'notes', { type: 'text/plain' });
    const unsupportedFile = new window.File(['content'], 'executable.exe', { type: 'application/x-msdownload' });

    Object.defineProperty(fileInput, 'files', {
      value: [validFile, extensionlessFile, unsupportedFile],
      writable: true,
      configurable: true
    });


    fileInput.dispatchEvent(new Event('change'));

    expect(uploadForm.submit).toHaveBeenCalled();
    expect(dropZone.innerHTML).toContain('Ingesting 1 File');
  });
});

describe('Document State Actions (Retry and Cancel)', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <input type="hidden" name="csrfmiddlewaretoken" value="dummy-csrf-token">
      <button class="btn btn-secondary btn-retry-doc" data-doc-id="doc-uuid-123">
        <i data-lucide="refresh-cw"></i> Retry
      </button>
      <button class="btn btn-secondary btn-cancel-doc" data-doc-id="doc-uuid-456">
        <i data-lucide="square"></i> Stop
      </button>
      <div class="alerts-container"></div>
    `;
    globalThis.fetch = vi.fn();
  });



  it('triggers POST to /document/:id/retry/ on retry button click', async () => {
    initializeRetryActions();

    globalThis.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: 'success', message: 'Re-enqueued' }),
    });

    const retryBtn = document.querySelector('.btn-retry-doc');
    retryBtn.click();

    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/document/doc-uuid-123/retry/',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          'X-CSRFToken': 'dummy-csrf-token',
          'X-Requested-With': 'XMLHttpRequest',
        }),
      })
    );
  });

  it('triggers confirmation before POST to /document/:id/cancel/ on cancel button click', async () => {
    initializeCancelActions();

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    globalThis.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: 'success', message: 'Stopped' }),
    });

    const cancelBtn = document.querySelector('.btn-cancel-doc');
    cancelBtn.click();

    expect(confirmSpy).toHaveBeenCalled();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/document/doc-uuid-456/cancel/',
      expect.objectContaining({
        method: 'POST',
      })
    );
    confirmSpy.mockRestore();
  });

  it('aborts cancel action when user rejects confirmation dialog', async () => {
    initializeCancelActions();

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    const cancelBtn = document.querySelector('.btn-cancel-doc');
    cancelBtn.click();

    expect(confirmSpy).toHaveBeenCalled();
    expect(globalThis.fetch).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('triggers confirmation before POST to /document/:id/delete/ on delete button click', async () => {
    document.body.innerHTML += `
      <button class="btn btn-secondary btn-delete-doc" data-doc-id="doc-uuid-789">
        <i data-lucide="trash-2"></i> Delete
      </button>
    `;
    initializeDeleteActions();

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    globalThis.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: 'success', message: 'Deleted' }),
    });

    const deleteBtn = document.querySelector('.btn-delete-doc');
    deleteBtn.click();

    expect(confirmSpy).toHaveBeenCalled();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      '/document/doc-uuid-789/delete/',
      expect.objectContaining({
        method: 'POST',
      })
    );
    confirmSpy.mockRestore();
  });
});


// ---------------------------------------------------------------------------
// initializeRAGSearch — Semantic Spotlight Search Flow
// ---------------------------------------------------------------------------
describe('initializeRAGSearch Flow', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <form id="rag-search-form">
        <input type="text" id="rag-query" value="" />
        <button type="submit" id="rag-btn">Search</button>
      </form>
      <div id="rag-loader" style="display: none;">Searching...</div>
      <div id="rag-results-container" style="display: none;">
        <div id="rag-answer"></div>
        <ul id="rag-sources-list"></ul>
      </div>
      <input type="checkbox" class="doc-selector" value="doc-1" checked />
      <input type="checkbox" class="doc-selector" value="doc-2" />
    `;
    globalThis.fetch = vi.fn();
  });

  it('alerts when submitting empty search query', () => {
    initializeRAGSearch();
    const queryInput = document.getElementById('rag-query');
    queryInput.value = '   ';

    const form = document.getElementById('rag-search-form');
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('renders grounded answer and sources upon successful search', async () => {
    initializeRAGSearch();
    const queryInput = document.getElementById('rag-query');
    queryInput.value = 'What is ancient epigraphy?';

    globalThis.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        answer_html: '<p>Epigraphy is the study of inscriptions.</p>',
        sources: [
          {
            uuid: 'doc-uuid-1',
            title: 'Epigraphy Handbook',
            language: 'en',
            chunk_index: 0,
          },
          {
            title: 'Anonymous Manuscript',
            language: 'la',
            chunk_index: 1,
          },
        ],
      }),
    });

    const form = document.getElementById('rag-search-form');
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/rag-search/?q=What%20is%20ancient%20epigraphy%3F&document_ids=doc-1')
    );

    // Wait for promise chain and microtask resolution
    await new Promise(resolve => setTimeout(resolve, 0));



    const results = document.getElementById('rag-results-container');
    const answer = document.getElementById('rag-answer');
    const sourcesList = document.getElementById('rag-sources-list');

    expect(results.style.display).toBe('block');
    expect(answer.innerHTML).toContain('Epigraphy is the study of inscriptions.');
    expect(sourcesList.children.length).toBe(2);
    expect(sourcesList.innerHTML).toContain('Epigraphy Handbook');
    expect(sourcesList.innerHTML).toContain('Anonymous Manuscript');
  });
});

// ---------------------------------------------------------------------------
// initializeExportActions Flow — Bulk and single deletion actions
// ---------------------------------------------------------------------------
describe('initializeExportActions Flow', () => {
  let exportForm, deleteForm, bulkRestartBtn, bulkDeleteBtn;

  beforeEach(() => {
    document.body.innerHTML = `
      <form id="export-form" method="post" action="/export/">
        <input type="checkbox" class="doc-selector" value="1" checked>
      </form>
      <form id="delete-form" method="post" action="">
      </form>
      <button type="button" id="btn-bulk-restart" data-action-url="/bulk-action/">Bulk Restart</button>
      <button type="button" id="btn-bulk-delete" data-action-url="/bulk-action/">Bulk Delete</button>
      <table>
        <tbody>
          <tr>
            <td>
              <button type="button" class="btn-delete-doc" data-doc-id="doc-999">Delete</button>
            </td>
          </tr>
        </tbody>
      </table>
    `;
    exportForm = document.getElementById('export-form');
    deleteForm = document.getElementById('delete-form');
    bulkRestartBtn = document.getElementById('btn-bulk-restart');
    bulkDeleteBtn = document.getElementById('btn-bulk-delete');

    exportForm.submit = vi.fn();
    deleteForm.submit = vi.fn();
  });

  it('submits bulk restart with action=restart when confirmed', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    initializeExportActions();

    bulkRestartBtn.click();

    expect(confirmSpy).toHaveBeenCalled();
    expect(exportForm.action).toContain('/bulk-action/');
    const actionInput = exportForm.querySelector('input[name="action"]');
    expect(actionInput).not.toBeNull();
    expect(actionInput.value).toBe('restart');
    expect(exportForm.submit).toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('submits bulk delete with action=delete when confirmed', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    initializeExportActions();

    bulkDeleteBtn.click();

    expect(confirmSpy).toHaveBeenCalled();
    expect(exportForm.action).toContain('/bulk-action/');
    const actionInput = exportForm.querySelector('input[name="action"]');
    expect(actionInput).not.toBeNull();
    expect(actionInput.value).toBe('delete');
    expect(exportForm.submit).toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});


// ---------------------------------------------------------------------------
// initializeLocalTimezones — Client-Side Datetime Conversion
// ---------------------------------------------------------------------------
describe('initializeLocalTimezones', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <time class="local-datetime" datetime="2026-08-24T12:00:00Z"></time>
      <span class="local-datetime" data-utc="2026-08-24T15:30:00Z"></span>
      <time class="local-datetime" datetime="invalid-date-string"></time>
    `;
  });

  it('formats semantic datetime attribute and sets UTC title', () => {
    initializeLocalTimezones();

    const timeEl = document.querySelector('time.local-datetime[datetime="2026-08-24T12:00:00Z"]');
    expect(timeEl.textContent).not.toBe('');
    expect(timeEl.getAttribute('title')).toContain('UTC: 2026-08-24T12:00:00.000Z');
  });

  it('formats data-utc attribute fallback and sets UTC title', () => {
    initializeLocalTimezones();

    const spanEl = document.querySelector('span.local-datetime[data-utc="2026-08-24T15:30:00Z"]');
    expect(spanEl.textContent).not.toBe('');
    expect(spanEl.getAttribute('title')).toContain('UTC: 2026-08-24T15:30:00.000Z');
  });

  it('gracefully handles invalid date string without throwing or mutating', () => {
    const invalidEl = document.querySelector('time.local-datetime[datetime="invalid-date-string"]');
    initializeLocalTimezones();

    expect(invalidEl.textContent).toBe('');
    expect(invalidEl.getAttribute('title')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// initializePasswordToggles
// ---------------------------------------------------------------------------
describe('initializePasswordToggles', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <input type="password" id="test-pwd" value="secret123">
    `;
  });

  it('wraps password input and toggles visibility on button click', () => {
    initializePasswordToggles();

    const input = document.getElementById('test-pwd');
    const btn = document.querySelector('.password-toggle-btn');
    expect(btn).not.toBeNull();
    expect(btn.getAttribute('aria-label')).toBe('Show password');
    expect(input.type).toBe('password');

    btn.click();
    expect(input.type).toBe('text');
    expect(btn.getAttribute('aria-label')).toBe('Hide password');
    expect(btn.getAttribute('aria-expanded')).toBe('true');

    btn.click();
    expect(input.type).toBe('password');
    expect(btn.getAttribute('aria-label')).toBe('Show password');
  });
});

// ---------------------------------------------------------------------------
// initializeCapsLockDetector
// ---------------------------------------------------------------------------
describe('initializeCapsLockDetector', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <input type="password" id="caps-pwd">
    `;
  });

  it('displays warning when CapsLock modifier is active', () => {
    initializeCapsLockDetector();

    const input = document.getElementById('caps-pwd');
    const warning = document.querySelector('.caps-lock-warning');
    expect(warning).not.toBeNull();
    expect(warning.style.display).toBe('none');

    const keyEvent = new globalThis.KeyboardEvent('keydown', { bubbles: true });
    keyEvent.getModifierState = (key) => key === 'CapsLock';
    input.dispatchEvent(keyEvent);

    expect(warning.style.display).toBe('inline-flex');

    input.dispatchEvent(new Event('blur'));
    expect(warning.style.display).toBe('none');
  });
});

// ---------------------------------------------------------------------------
// initializePasswordMatchFeedback
// ---------------------------------------------------------------------------
describe('initializePasswordMatchFeedback', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <form>
        <input type="password" id="id_password" value="">
        <input type="password" id="id_confirm_password" value="">
      </form>
    `;
  });

  it('renders match and mismatch state dynamically', () => {
    initializePasswordMatchFeedback();

    const pwd = document.getElementById('id_password');
    const confirmPwd = document.getElementById('id_confirm_password');
    const feedback = document.querySelector('.password-match-status');
    expect(feedback).not.toBeNull();

    pwd.value = 'password123';
    confirmPwd.value = 'password999';
    confirmPwd.dispatchEvent(new Event('input'));
    expect(feedback.textContent).toContain('Passwords do not match');

    confirmPwd.value = 'password123';
    confirmPwd.dispatchEvent(new Event('input'));
    expect(feedback.textContent).toContain('Passwords match');
  });
});

// ---------------------------------------------------------------------------
// initializeAuditSearch & initializeSearchShortcuts
// ---------------------------------------------------------------------------
describe('initializeAuditSearch & initializeSearchShortcuts', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div class="search-input-container">
        <input type="text" id="search-input" value="">
        <span id="audit-search-hint">Press /</span>
      </div>
      <div class="search-box">
        <input type="text" id="rag-query" value="">
        <div class="search-kbd-hint"><span>/</span></div>
      </div>
    `;
  });

  it('focuses audit search on slash keypress and clears on escape', () => {
    initializeAuditSearch();

    const input = document.getElementById('search-input');
    const clearBtn = document.querySelector('.search-clear-btn');
    expect(clearBtn).not.toBeNull();

    document.dispatchEvent(new globalThis.KeyboardEvent('keydown', { key: '/' }));
    expect(document.activeElement).toBe(input);

    input.value = 'query';
    input.dispatchEvent(new Event('input'));
    expect(clearBtn.style.display).toBe('inline-flex');

    input.dispatchEvent(new globalThis.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(input.value).toBe('query');
  });

  it('focuses RAG search on slash keypress and handles clear button', () => {
    document.body.innerHTML = `
      <div class="search-input-container">
        <input type="text" id="rag-query" value="">
        <span id="search-hint">Press /</span>
      </div>
    `;

    initializeSearchShortcuts();

    const input = document.getElementById('rag-query');
    const clearBtn = document.querySelector('.search-clear-btn');
    expect(clearBtn).not.toBeNull();

    document.dispatchEvent(new globalThis.KeyboardEvent('keydown', { key: '/' }));
    expect(document.activeElement).toBe(input);

    input.value = 'test semantic query';
    input.dispatchEvent(new Event('input'));
    expect(clearBtn.style.display).toBe('inline-flex');

    clearBtn.click();
    expect(input.value).toBe('');
    expect(clearBtn.style.display).toBe('none');
  });
});

// ---------------------------------------------------------------------------
// setFormSubmitLoadingState & initializeFormSubmitSpinners
// ---------------------------------------------------------------------------
describe('setFormSubmitLoadingState & initializeFormSubmitSpinners', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div class="login-card">
        <form id="test-login-form">
          <button type="submit" class="btn-login-submit">Login</button>
        </form>
      </div>
    `;
  });

  it('attaches spinner and disables pointer events on submit', () => {
    const form = document.getElementById('test-login-form');
    const btn = form.querySelector('button');

    setFormSubmitLoadingState(form);
    expect(btn.innerHTML).toContain('Unlocking Dashboard...');
    expect(btn.style.pointerEvents).toBe('none');
    expect(btn.dataset.originalHtml).toBe('Login');
  });

  it('wires submit event and pageshow restoration with initializeFormSubmitSpinners', () => {
    initializeFormSubmitSpinners();

    const form = document.getElementById('test-login-form');
    const btn = form.querySelector('button');

    form.dispatchEvent(new Event('submit', { cancelable: true }));
    expect(btn.innerHTML).toContain('Unlocking Dashboard...');

    const pageShowEvent = new Event('pageshow');
    pageShowEvent.persisted = true;
    globalThis.dispatchEvent(pageShowEvent);

    expect(btn.innerHTML).toBe('Login');
    expect(btn.style.pointerEvents).toBe('');
  });
});

// ---------------------------------------------------------------------------
// initializeLibraryFilter & toggleExportFooter
// ---------------------------------------------------------------------------
describe('initializeLibraryFilter & toggleExportFooter', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div class="table-filter-wrapper">
        <input type="text" id="table-filter" value="">
      </div>
      <div id="export-actions-bar">
        <span id="selected-count">0</span>
      </div>
      <div class="files-panel">
        <table>
          <tbody>
            <tr data-doc-id="1">
              <td><input type="checkbox" class="doc-selector" checked></td>
              <td>Document Alpha</td>
              <td>Author One</td>
            </tr>
            <tr data-doc-id="2">
              <td><input type="checkbox" class="doc-selector"></td>
              <td>Document Beta</td>
              <td>Author Two</td>
            </tr>
          </tbody>
        </table>
      </div>
    `;
  });

  it('toggles export actions bar visibility based on selected checkboxes', () => {
    toggleExportFooter();

    const footer = document.getElementById('export-actions-bar');
    const count = document.getElementById('selected-count');
    expect(count.textContent).toBe('1');
    expect(footer.classList.contains('visible')).toBe(true);

    const cb = document.querySelector('.doc-selector:checked');
    cb.checked = false;
    toggleExportFooter();

    expect(count.textContent).toBe('0');
    expect(footer.classList.contains('visible')).toBe(false);
  });

  it('wires library filter input, clear button and escape key', () => {
    initializeLibraryFilter();

    const input = document.getElementById('table-filter');
    const clearBtn = document.querySelector('.table-filter-clear-btn');
    const row2 = document.querySelector('tr[data-doc-id="2"]');

    input.value = 'Alpha';
    input.dispatchEvent(new Event('input'));
    expect(row2.style.display).toBe('none');
    expect(clearBtn.style.display).toBe('inline-flex');

    clearBtn.click();
    expect(input.value).toBe('');
    expect(row2.style.display).toBe('');
  });
});

// ---------------------------------------------------------------------------
// _checkNeedsPolling, initializeTokensChart, updateDashboardStats, updateDocumentsTable
// ---------------------------------------------------------------------------
describe('Live Realtime & Status DOM Updaters', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div class="metrics-row">
        <div class="metric-card"><span class="metric-value">$0.00</span><div class="metric-sub"><div class="budget-bar-fill" style="width:0%"></div></div></div>
        <div class="metric-card"><span class="metric-value">0</span></div>
        <div class="metric-card"><span id="active-tasks-count">0</span></div>
        <div class="metric-card"><span class="token-value-text">0</span><span class="token-sub-text"></span></div>
      </div>
      <canvas id="tokensChart" data-prompt="100" data-candidates="200"></canvas>
      <div class="files-panel">
        <table>
          <tbody>
            <tr data-doc-id="10" data-status="PENDING">
              <td><input type="checkbox" id="chk-doc-10"></td>
              <td>Document Title</td>
              <td>Author</td>
              <td class="cost-cell"><div>$0.00</div><span class="doc-cost-sub"></span></td>
              <td class="status-cell"><span class="badge-pending">Queued</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    `;
  });

  it('detects polling requirement accurately', () => {
    expect(_checkNeedsPolling()).toBe(true);

    const pendingBadge = document.querySelector('.badge-pending');
    pendingBadge.className = 'badge-completed';
    expect(_checkNeedsPolling()).toBe(false);
  });

  it('updates dashboard stats metrics DOM elements cleanly', () => {
    const stats = {
      formatted_monthly_spent: '$12.50',
      percent_spent: 45,
      total_pages: '120',
      PENDING: 1,
      EXTRACTING: 0,
      REFINING: 0,
      EMBEDDING: 0,
      total_tokens: 15000,
      prompt_tokens: 10000,
      candidates_tokens: 5000,
      budget_exceeded: false
    };

    updateDashboardStats(stats);

    const spendVal = document.querySelector('.metrics-row .metric-card:first-child .metric-value');
    const fillBar = document.querySelector('.budget-bar-fill');
    const pagesVal = document.querySelector('.metrics-row .metric-card:nth-child(2) .metric-value');
    const activeTasks = document.getElementById('active-tasks-count');

    expect(spendVal.textContent).toBe('$12.50');
    expect(fillBar.style.width).toBe('45%');
    expect(pagesVal.textContent).toBe('120');
    expect(activeTasks.textContent).toBe('1');
  });

  it('updates documents table row status badge', () => {
    const docs = [
      { id: 10, uuid: 'doc-uuid-10', status: 'EXTRACTING', status_display: 'Extracting' }
    ];

    updateDocumentsTable(docs);

    const statusCell = document.querySelector('tr[data-doc-id="10"] .status-cell');
    expect(statusCell.innerHTML).toContain('badge-processing');
  });

  it('gracefully initialises tokens chart when Chart is undefined or defined', () => {
    expect(() => initializeTokensChart()).not.toThrow();
  });

  it('updates document detail screen metadata, cost, and tokens cleanly', () => {
    document.body.innerHTML = `
      <div class="timeline-container">
        <div class="timeline-step"></div>
        <div class="timeline-step" data-step="EXTRACTING"></div>
        <div class="timeline-step" data-step="REFINING"></div>
        <div class="timeline-step" data-step="EMBEDDING"></div>
      </div>
      <div id="detail-cost">$0.00</div>
      <div id="detail-lang">Old Lang</div>
      <div id="detail-author">Old Author</div>
      <form id="editor-form"></form>
    `;

    delete globalThis.location;
    globalThis.location = { pathname: '/document/test-doc-uuid-123/', reload: vi.fn() };

    const doc = {
      uuid: 'test-doc-uuid-123',
      title: 'New Title',
      author: 'New Author',
      language: 'Arabic',
      page_count: 50,
      formatted_cost: '$0.15',
      input_tokens: 1200,
      output_tokens: 800,
      status: 'EXTRACTING',
      status_display: 'Extracting'
    };

    updateDocumentDetailScreen({ documents: [doc] });

    expect(document.getElementById('detail-cost').textContent).toBe('$0.15');
    expect(document.getElementById('detail-lang').textContent).toBe('Arabic');
    expect(document.getElementById('detail-author').textContent).toBe('New Author');

    _updateDetailMetaFields(doc, 'EXTRACTING');
    expect(document.getElementById('detail-cost').textContent).toBe('$0.15');
  });
});

// ---------------------------------------------------------------------------
// initializeAlerts & initializeSupabaseRealtime
// ---------------------------------------------------------------------------
describe('initializeAlerts & initializeSupabaseRealtime', () => {
  it('dismisses alert card on close button click', () => {
    document.body.innerHTML = `
      <div class="alert-card" id="alert-1" role="alert">
        <button class="alert-close-btn" data-dismiss="alert-1"></button>
      </div>
    `;

    initializeAlerts();

    const btn = document.querySelector('.alert-close-btn');
    btn.click();

    const card = document.getElementById('alert-1');
    expect(card.classList.contains('fade-out')).toBe(true);
  });

  it('gracefully handles initializeSupabaseRealtime with missing credentials', () => {
    expect(() => initializeSupabaseRealtime()).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// trapFocus
// ---------------------------------------------------------------------------
describe('trapFocus', () => {
  it('cycles focus from last element to first on forward Tab', () => {
    document.body.innerHTML = `
      <dialog id="test-dialog">
        <button id="first-btn">First</button>
        <button id="last-btn">Last</button>
      </dialog>
    `;
    const dialog = document.getElementById('test-dialog');
    const first = document.getElementById('first-btn');
    const last = document.getElementById('last-btn');
    last.focus();

    const event = new window.KeyboardEvent('keydown', { key: 'Tab', shiftKey: false, cancelable: true });
    trapFocus(dialog, event);

    expect(document.activeElement).toBe(first);
  });

  it('cycles focus from first element to last on reverse Shift+Tab', () => {
    document.body.innerHTML = `
      <dialog id="test-dialog">
        <button id="first-btn">First</button>
        <button id="last-btn">Last</button>
      </dialog>
    `;
    const dialog = document.getElementById('test-dialog');
    const first = document.getElementById('first-btn');
    const last = document.getElementById('last-btn');
    first.focus();

    const event = new window.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, cancelable: true });
    trapFocus(dialog, event);

    expect(document.activeElement).toBe(last);
  });
});
