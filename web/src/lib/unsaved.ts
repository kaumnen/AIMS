// The file the open page holds changes to that are not saved, null when there are none. A leave
// drops them, so the page's leave guard asks first. Sign out asks the same. So does an action
// that leaves the page, before it acts.
let unsaved: string | null = null;
// Texts edited in place, such as prompts, of which several can be open at once: each draft
// that differs from its text, by the id of its row.
const drafts = new Map<string, string>();
// What the page marked as it went under the sign-in form or the outage screen. Hidden, it lets
// go of its marks but keeps its drafts.
let held: string[] = [];

const names = () => [
  ...new Set([...(unsaved === null ? [] : [unsaved]), ...drafts.values(), ...held]),
];

export function setUnsaved(file: string | null): void {
  unsaved = file;
}

// `name` is what the draft of row `id` changes, null once it is saved, dropped or gone.
export function noteDraft(id: string, name: string | null): void {
  if (name === null) drafts.delete(id);
  else drafts.set(id, name);
}

// Keeps what is marked now until it is let go, for a page about to hide.
export function holdUnsaved(on: boolean): void {
  held = on ? names() : [];
}

export const hasUnsaved = (): boolean => names().length > 0;

// Whether to go on and drop the changes: there are none, or the person says so. `why` comes
// first, to say what an action does without them.
export function mayDiscard(why = ""): boolean {
  const all = names();
  return all.length === 0 || window.confirm(`${why}Discard your changes to ${all.join(", ")}?`);
}
