import ts from "@typescript/typescript6";
import { plugin } from "bun";

// openapi-typescript needs the TypeScript 5/6 JS API, which TypeScript 7 no longer ships.
plugin({
  name: "typescript6",
  setup(build) {
    build.module("typescript", () => ({ exports: { ...ts, default: ts }, loader: "object" }));
  },
});
