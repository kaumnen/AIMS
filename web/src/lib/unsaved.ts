// The file the open page holds changes to that are not saved, null when there are none. A leave
// drops them, so the page's leave guard asks first. Sign out asks the same. So does an action
// that leaves the page, before it acts.
let unsaved: string | null = null;

export function setUnsaved(file: string | null): void {
  unsaved = file;
}

export const hasUnsaved = (): boolean => unsaved !== null;

// Whether to go on and drop the changes: there are none, or the person says so. `why` comes
// first, to say what an action does without them.
export function mayDiscard(why = ""): boolean {
  return unsaved === null || window.confirm(`${why}Discard your changes to ${unsaved}?`);
}
