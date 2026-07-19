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
