/**
 * main.js unit tests
 *
 * Tests pure/utility functions from static/js/main.js that don't require
 * a live DOM or network. DOM-dependent initializers (initializeAlerts, etc.)
 * are tested via their side-effects using jsdom.
 */
import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';

// Inject main.js into the jsdom window scope
beforeAll(() => {
  // Stub external dependencies before evaluation
  window.lucide = { createIcons: () => {} };
  window.Chart = class { constructor() {} destroy() {} };
  window.supabaseClient = null;

  const src = readFileSync(resolve('static/js/main.js'), 'utf-8');
  // window.eval places function declarations on window (globalThis)
  window.eval(src);
});

// ---------------------------------------------------------------------------
// formatCompact — number formatter
// ---------------------------------------------------------------------------
describe('formatCompact', () => {
  it('returns raw string for numbers below 1000', () => {
    expect(globalThis.formatCompact(0)).toBe('0');
    expect(globalThis.formatCompact(999)).toBe('999');
    expect(globalThis.formatCompact(1)).toBe('1');
  });

  it('formats thousands as K', () => {
    expect(globalThis.formatCompact(1000)).toBe('1K');
    expect(globalThis.formatCompact(1500)).toBe('1.5K');
    expect(globalThis.formatCompact(10000)).toBe('10K');
    expect(globalThis.formatCompact(999999)).toBe('1000K');
  });

  it('formats millions as M', () => {
    expect(globalThis.formatCompact(1000000)).toBe('1M');
    expect(globalThis.formatCompact(2500000)).toBe('2.5M');
    expect(globalThis.formatCompact(10000000)).toBe('10M');
  });

  it('strips trailing .0 from K/M values', () => {
    expect(globalThis.formatCompact(2000)).toBe('2K');
    expect(globalThis.formatCompact(5000000)).toBe('5M');
  });
});

// ---------------------------------------------------------------------------
// getStatusBadgeHTML — badge HTML generator
// ---------------------------------------------------------------------------
describe('getStatusBadgeHTML', () => {
  it('returns completed badge for COMPLETED status', () => {
    const html = globalThis.getStatusBadgeHTML('COMPLETED', 'Done');
    expect(html).toContain('badge-completed');
    expect(html).toContain('Done');
    expect(html).toContain('check-circle-2');
  });

  it('returns failed badge for FAILED status (ignores display arg)', () => {
    const html = globalThis.getStatusBadgeHTML('FAILED', 'Something');
    expect(html).toContain('badge-failed');
    expect(html).toContain('Failed');
    expect(html).toContain('x-circle');
  });

  it('returns pending badge for PENDING status', () => {
    const html = globalThis.getStatusBadgeHTML('PENDING', 'Queued');
    expect(html).toContain('badge-pending');
    expect(html).toContain('Queued');
    expect(html).toContain('clock');
  });

  it('returns processing badge for any other status', () => {
    const html = globalThis.getStatusBadgeHTML('EXTRACTING', 'Extracting');
    expect(html).toContain('badge-processing');
    expect(html).toContain('Extracting');
    expect(html).toContain('loader');
  });

  it('returns processing badge for EMBEDDING status', () => {
    const html = globalThis.getStatusBadgeHTML('EMBEDDING', 'Embedding');
    expect(html).toContain('badge-processing');
  });

  it('includes a <span> wrapper', () => {
    const html = globalThis.getStatusBadgeHTML('COMPLETED', 'Done');
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
    globalThis.showClientSideAlert('Something went wrong');
    const alert = document.querySelector('[role="alert"], .alert, .client-alert');
    expect(alert).not.toBeNull();
  });

  it('includes the provided message text', () => {
    globalThis.showClientSideAlert('Test error message');
    expect(document.body.textContent).toContain('Test error message');
  });

  it('defaults to error type', () => {
    globalThis.showClientSideAlert('error msg');
    // Look for error styling class in the injected element
    const html = document.body.innerHTML;
    expect(html.toLowerCase()).toMatch(/error|danger/);
  });
});
