// The file the open page holds changes to that are not saved, null when there are none. A leave
// drops them, so the page's leave guard asks first. Sign out asks the same. So does an action
// that leaves the page, before it acts.
let unsaved: string | null = null;
// Texts edited in place, such as prompts, of which several can be open at once: each draft
// that differs from its text, by the id of its row.
const drafts = new Map<string, string>();

export function setUnsaved(file: string | null): void {
  unsaved = file;
}

// `name` is what the draft of row `id` changes, null once it is saved, dropped or gone.
export function noteDraft(id: string, name: string | null): void {
  if (name === null) drafts.delete(id);
  else drafts.set(id, name);
}

export const hasUnsaved = (): boolean => unsaved !== null || drafts.size > 0;

// Whether to go on and drop the changes: there are none, or the person says so. `why` comes
// first, to say what an action does without them.
export function mayDiscard(why = ""): boolean {
  const names = [...(unsaved === null ? [] : [unsaved]), ...new Set(drafts.values())];
  return names.length === 0 || window.confirm(`${why}Discard your changes to ${names.join(", ")}?`);
}
