## 2026-08-10 - Add `:focus-visible` to Toolbar Buttons
**Learning:** Keyboard users navigating the Markdown editor toolbar currently lack visual feedback on which button is focused.
**Action:** Adding a `:focus-visible` CSS rule for `.tb-btn` using the `var(--primary)` color scheme fixes this by displaying a clear outline when a button receives keyboard focus, aligning with existing styles.
## 2026-08-13 - Enhance Submit Form Loading States
**Learning:** Loading states for interactive forms improve perceived performance and give clear UX feedback. Centralizing form submit spinners in `static/js/main.js` using `initializeFormSubmitSpinners` is an effective pattern.
**Action:** When adding new forms, such as `#settings-form` or `.confirm-card form`, ensure they are added to the centralized spinner query selector to provide consistent UX state transitions. Also, remove duplicate inline submit handlers to prevent regressions.
