# Palette's Journal - Critical UX & Accessibility Learnings

## 2026-07-15 - Lucide Icon Disconnection and Dynamic DOM Wrapping Collision
**Learning:**
1. Dynamic icon libraries (such as Lucide) that replace placeholder elements (like `<i>` tags) with parsed `<svg>` nodes will silently disconnect any JavaScript DOM references stored during initialization. If a toggler later attempts to update the icon's source by modifying the disconnected `<i>` node's attributes and re-calling the icon generator, the visible icon will remain permanently stuck in its initial state.
2. Force-wrapping inputs in forms with fixed layout specifications (such as Django dynamic CSS grid or flexbox forms) inside standard block-level `div` wrappers disrupts centered styling and responsiveness. The injected wrapper must copy or dynamically evaluate the input's computed `display` style to prevent severe alignment breakage on centered forms.

**Action:**
1. Use native inline SVG templates directly embedded in the dynamic component's javascript handlers when building toggleable states. This removes any dependency on external framework lifecycle triggers, ensures perfect DOM continuity, and results in a zero-dependency, ultra-reliable visual experience.
2. Style the injected password-toggle wrappers to adaptively match the input's computed display property (e.g., `inline-block` or `block`) and preserve standard form proportions with absolute layout positioning.
