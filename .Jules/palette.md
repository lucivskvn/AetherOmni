# Palette's Journal - Critical UX & Accessibility Learnings

## 2026-07-15 - Lucide Icon Disconnection and Dynamic DOM Wrapping Collision
**Learning:**
1. Dynamic icon libraries (such as Lucide) that replace placeholder elements (like `<i>` tags) with parsed `<svg>` nodes will silently disconnect any JavaScript DOM references stored during initialization. If a toggler later attempts to update the icon's source by modifying the disconnected `<i>` node's attributes and re-calling the icon generator, the visible icon will remain permanently stuck in its initial state.
2. Force-wrapping inputs in forms with fixed layout specifications (such as Django dynamic CSS grid or flexbox forms) inside standard block-level `div` wrappers disrupts centered styling and responsiveness. The injected wrapper must copy or dynamically evaluate the input's computed `display` style to prevent severe alignment breakage on centered forms.

**Action:**
1. Use native inline SVG templates directly embedded in the dynamic component's javascript handlers when building toggleable states. This removes any dependency on external framework lifecycle triggers, ensures perfect DOM continuity, and results in a zero-dependency, ultra-reliable visual experience.
2. Style the injected password-toggle wrappers to adaptively match the input's computed display property (e.g., `inline-block` or `block`) and preserve standard form proportions with absolute layout positioning.

## 2026-07-17 - Dynamic ARIA Attributes for Standard-Compliant Accessibility on Mode Toggles
**Learning:**
1. A static `aria-label` on toggle buttons (such as theme toggles) fails to inform screen-reader users of the active state and what actions clicking will trigger. Incorporating a dynamic `aria-label` coupled with the standard `aria-pressed` state guarantees that screen readers are aware of both the active mode and the next mode transition.
2. Direct CSS display manipulation of inline SVGs prevents rendering delays and visual layout shifts during page hydration compared to relying on third-party dynamic icon replacement libraries (such as Lucide).

**Action:**
1. When designing toggle buttons, always include `aria-pressed` and a contextual `aria-label` that dynamically reflects the state change.
2. Favor native inline SVGs over dynamic icon initialization calls for core interactive actions to guarantee visual stability.
