## 2026-08-10 - Add `:focus-visible` to Toolbar Buttons
**Learning:** Keyboard users navigating the Markdown editor toolbar currently lack visual feedback on which button is focused.
**Action:** Adding a `:focus-visible` CSS rule for `.tb-btn` using the `var(--primary)` color scheme fixes this by displaying a clear outline when a button receives keyboard focus, aligning with existing styles.

## 2026-08-13 - Enhance Submit Form Loading States
**Learning:** Loading states for interactive forms improve perceived performance and give clear UX feedback. Centralizing form submit spinners in `static/js/main.js` using `initializeFormSubmitSpinners` is an effective pattern.

**Action:** When adding new forms, such as `#settings-form` or `.confirm-card form`, ensure they are added to the centralized spinner query selector to provide consistent UX state transitions. Also, remove duplicate inline submit handlers to prevent regressions.

## 2026-08-14 - Fix Empty State Opacity
**Learning:** Low opacity on empty states makes text unreadable and fails minimum color contrast requirements.
**Action:** Set opacity to 1 on empty state text to ensure readability and compliance.

## 2026-08-15 - Add ARIA Labels to OAuth Buttons
**Learning:** Icon-heavy third-party authentication buttons (like GitHub OAuth) lack clear accessible names if they rely solely on SVG content.
**Action:** Add descriptive `aria-label` attributes to OAuth buttons (e.g., `Sign in with GitHub`) to ensure screen readers announce their function accurately.

## 2026-08-15 - Add `:focus-visible` to OAuth Buttons

**Learning:** Keyboard users navigating the authentication forms lack visual feedback when focusing on the GitHub OAuth buttons, as they don't inherit the global focus styles perfectly due to their border radius and specific styles.

## 2026-08-22 - Add `aria-label` and `:focus-visible` to SFT Modal Close Button

**Learning:** Icon-only buttons with hardcoded text symbols like '✕' inside modals lack proper screen reader announcements and often miss global focus styles because they aren't explicitly classed.
**Action:** Replace hardcoded symbols with semantic SVG icons (like Lucide `x`), add `aria-label` and `title` attributes, and explicitly add them to the `:focus-visible` CSS selector rules to ensure keyboard focus visibility.
