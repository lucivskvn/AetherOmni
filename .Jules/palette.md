## 2026-08-05 - Fix Dialog Close State Sync
**Learning:** Native `<dialog>` elements can be closed via the Escape key, which bypasses custom JavaScript click handlers attached to a close button or backdrop. This can leave dynamic modals (like a settings screen with nested confirmation flows) in a "dirty" state when re-opened.
**Action:** Always bind cleanup or reset logic to the generic `'close'` event listener on the `<dialog>` itself, rather than (or in addition to) the specific click triggers.
