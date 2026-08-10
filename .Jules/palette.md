## 2026-08-10 - Add `:focus-visible` to Toolbar Buttons
**Learning:** Keyboard users navigating the Markdown editor toolbar currently lack visual feedback on which button is focused.
**Action:** Adding a `:focus-visible` CSS rule for `.tb-btn` using the `var(--primary)` color scheme fixes this by displaying a clear outline when a button receives keyboard focus, aligning with existing styles.
