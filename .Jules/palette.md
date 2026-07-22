# Palette's Journal - Critical UX & Accessibility Learnings

## 2026-07-15 - Lucide Icon Disconnection and Dynamic DOM Wrapping Collision
**Learning:**
1. Dynamic icon libraries (such as Lucide) that replace placeholder elements (like `<i>` tags) with parsed `<svg>` nodes will silently disconnect any JavaScript DOM references stored during initialization. If a toggler later attempts to update the icon's source by modifying the disconnected `<i>` node's attributes and re-calling the icon generator, the visible icon will remain permanently stuck in its initial state.
2. Force-wrapping inputs in forms with fixed layout specifications (such as Django dynamic CSS grid or flexbox forms) inside standard block-level `div` wrappers disrupts centered styling and responsiveness. The injected wrapper must copy or dynamically evaluate the input's computed `display` style to prevent severe alignment breakage on centered forms.

**Action:**
1. Use native inline SVG templates directly embedded in the dynamic component's javascript handlers when building toggleable states. This removes any dependency on external framework lifecycle triggers, ensures perfect DOM continuity, and results in a zero-dependency, ultra-reliable visual experience.
2. Style the injected password-toggle wrappers to adaptively match the input's computed display property (e.g., `inline-block` or `block`) and preserve standard form proportions with absolute layout positioning.

## 2026-07-16 - Escape-to-Exit Keyboard Behavior in Distraction-Free Fullscreen Workspaces
**Learning:**
1. In distraction-free fullscreen layout modules (such as split-pane curation editors), keyboard accessibility is vital. Users expect standard modal-dismissal key triggers (specifically `Escape`) to act as an exit mechanism.
2. When binding global listeners for standard keys like `Escape`, the handler must strictly check the visual presence/class state of the target container rather than unconditionally intercepting the key. Otherwise, standard native browser behaviors (like canceling selects, auto-completes, or other key inputs) will be disrupted globally on the page.

**Action:**
1. Implement a centralized wrapper function (e.g. `toggleFullscreen(forceState)`) to unify state changes, button title updates, and icon re-rendering.
2. Bind keydown listeners at the document level but restrict execution to conditions where the workspace's state matches `fullscreen`, preventing interference with non-fullscreen forms and inputs.

## 2026-07-18 - Seamless Dynamic Client-Side Toast Notification Pattern
**Learning:**
1. Spawning dynamic, non-disruptive toast alerts is vastly superior to browser-native blocking `alert()` popups. Aligning dynamic client-side alerts with existing Django backend `messages` classes creates a seamless, highly integrated UX and preserves design system visual language.
2. Re-invoking `lucide.createIcons()` on dynamically generated elements guarantees standard SVG icons render properly, but we must verify that its presence is defensively checked (`typeof lucide !== 'undefined' && lucide.createIcons`) to prevent script crash side effects on disconnected pages.

**Action:**
1. Expose a global `window.showClientSideAlert(message, type)` function that mirrors Django's server-rendered alerts by dynamically building container `alert-card` DOM nodes.
2. Check for the existence of `showClientSideAlert` before calling it from sub-scripts to maintain graceful fallback support when scripts load asynchronously.

## 2026-07-19 - Dynamic Input-Clear Controls and Sibling Keyboard Shortcut Hint Coordination
**Learning:**
1. Custom client-side input-clearing controls are superior to inconsistent native browser elements for custom theme layouts, but they must explicitly coordinate with neighboring keyboard shortcut badges (e.g. `<kbd>`) to prevent overlapping text and layout clutter during active typing.
2. Dynamic visibility transitions should be handled cleanly via style attributes (such as opacity and visibility) while preserving standard keyboard and mouse event delegation (like click-and-focus recovery).

**Action:**
1. Programmatically coordinate custom clear button displays and sibling `<kbd>` indicators using conditional listeners triggered on `input`, `focus`, and `blur` events.
2. Ensure clear buttons use standard HTML `<button type="button">` wrappers with explicit `aria-label` attributes to maintain accessible landmarks for screen readers.

## 2026-07-20 - Instant Client-Side Library Table Filtering and Responsive Focus Expanders
**Learning:** In metadata-rich dashboard systems, providing an instant client-side table filter improves discoverability and spatial navigation compared to full-page server-reloads. Seamlessly expanding search input widths during focus increases spatial accessibility for longer terms without breaking header grid constraints, provided we use CSS transition properties and handle keyboard layout alignment gracefully.
**Action:** Implement lightweight, dynamic `input` event listeners coupled with flexible layout elements that expand gracefully on focus and collapse on blur to deliver an engaging search and navigation experience.

## 2026-07-22 - Copy Markdown Toolbar Option & SVG Copy-to-Check Dynamic Transitions
**Learning:**
1. In curation-focused split-pane Markdown workspaces, providing a "Copy Markdown to Clipboard" toolbar option dramatically reduces user friction compared to manual selection.
2. Integrating native inline SVG templates instead of dynamic Lucide icon element replacements protects against icon state desynchronization. Combining clipboard API handlers with animated, non-disruptive, accessible toast notifications (using safely set textContent alerts) provides clear, beautiful, visual verification to users without breaking screen reader compatibility.
**Action:** Always embed inline SVG elements for stateful toggle actions, and pair clipboard success callbacks with existing server-styled toast notification containers to maintain design-system consistency.
