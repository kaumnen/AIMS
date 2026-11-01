import { configure } from "@testing-library/react";
import { afterEach } from "vitest";

// Route chunks load lazily and transform on first use, which a cold CI runner can take over a second to do.
configure({ asyncUtilTimeout: 5000 });

// Base UI reports misuse (a link rendered as a button, say) on the console, once per message.
const misuse: string[] = [];
for (const level of ["error", "warn"] as const) {
  const log = console[level].bind(console);
  console[level] = (...args: unknown[]) => {
    const first = args[0];
    if (typeof first === "string" && first.startsWith("Base UI:")) {
      misuse.push(first.split("\n")[0] ?? first);
    }
    log(...args);
  };
}
afterEach(() => {
  const found = misuse.splice(0);
  if (found.length > 0) throw new Error(found.join("\n"));
});
