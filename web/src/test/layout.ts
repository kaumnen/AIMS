import { afterAll, beforeAll } from "vitest";

// happy-dom lays nothing out, so a virtual list would see an empty window.
export function stubHeight(px = 600): void {
  const size = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
  beforeAll(() => {
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true,
      get: () => px,
    });
  });
  afterAll(() => {
    if (size) Object.defineProperty(HTMLElement.prototype, "clientHeight", size);
  });
}
