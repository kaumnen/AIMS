// Whether a key press is a page shortcut: not text typed into a field, not a key a widget
// already handled, and not inside a dialog, menu or popup (those mount outside the shell).
export function shortcut(e: KeyboardEvent): boolean {
  if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return false;
  const t = e.target;
  if (!(t instanceof HTMLElement) || t === document.body) return true;
  if (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return false;
  return t.closest("[data-shell]") !== null;
}
