# Palette UX Journal

## 2026-07-11 - Proactive Real-Time Workspace Diagnostics
**Learning:** In LLM document curation and fine-tuning annotation pipelines, users are highly sensitive to text length constraints and boundaries. Simply offering raw editing textareas without inline contextual metadata leads to uncertainty during saving. Integrating an immediate, high-contrast, yet non-intrusive character and word counter directly in the editor status bar satisfies this user behavior pattern of progressive validation.
**Action:** Always complement text workspace editing panes with live character and word stats alongside proper `aria-live` containers to ensure immediate feedback and high screen-reader accessibility.
