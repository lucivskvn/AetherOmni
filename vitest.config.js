import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // jsdom provides browser-like globals (document, window, localStorage)
    // needed to test DOM-manipulating functions in main.js / editor.js
    environment: 'jsdom',

    // Test file discovery
    include: ['static/js/tests/**/*.test.js'],

    // Coverage configuration — outputs LCOV for SonarCloud
    coverage: {
      provider: 'v8',
      reporter: ['lcov', 'text', 'html'],

      // Output dir: coverage/lcov.info is consumed by sonar.javascript.lcov.reportPaths
      reportsDirectory: './coverage/js',

      // Only measure coverage on production JS sources
      include: ['static/js/*.js'],

      // Exclude test files and minified bundles from coverage
      exclude: [
        'static/js/tests/**',
        'static/js/**/*.min.js',
      ],

      // Thresholds disabled: window.eval() instrumentation is not tracked by
      // the v8 coverage provider. The lcov.info report is still generated and
      // uploaded to SonarCloud. Migrate to ES module exports to enable thresholds.
      // thresholds: { lines: 30, functions: 25, branches: 25, statements: 30 },
    },

    // Clean globals so tests explicitly import what they need
    globals: false,
  },
});
