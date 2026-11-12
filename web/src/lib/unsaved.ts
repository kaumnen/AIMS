// The file the open page holds changes to that are not saved, null when there are none. A leave
// drops them, so the page's leave guard asks first. Sign out asks the same.
let unsaved: string | null = null;

export function setUnsaved(file: string | null): void {
  unsaved = file;
}

export const hasUnsaved = (): boolean => unsaved !== null;

// Whether to go on and drop the changes: there are none, or the person says so.
export function mayDiscard(): boolean {
  return unsaved === null || window.confirm(`Discard your changes to ${unsaved}?`);
}
