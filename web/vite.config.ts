/// <reference types="vitest/config" />
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import babel from "@rolldown/plugin-babel";
import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// The stored theme, set before first paint. CSP refuses inline scripts and the server serves
// only assets/, so the script ships there under a hashed name as a blocking <head> script.
function themeBoot(): Plugin {
  const src = "src/theme-boot.js";
  const source = readFileSync(new URL(src, import.meta.url), "utf8");
  const file = `assets/theme-${createHash("sha256").update(source).digest("hex").slice(0, 8)}.js`;
  let build = false;
  return {
    name: "aims-theme-boot",
    configResolved(config) {
      build = config.command === "build";
    },
    transformIndexHtml: () => [
      { tag: "script", attrs: { src: `/${build ? file : src}` }, injectTo: "head" },
    ],
    generateBundle() {
      this.emitFile({ type: "asset", fileName: file, source });
    },
  };
}

export default defineConfig({
  plugins: [
    themeBoot(),
    tanstackRouter({ target: "react", autoCodeSplitting: true }),
    react(),
    babel({ presets: [reactCompilerPreset()] }),
    tailwindcss(),
  ],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    outDir: "../src/aims/ui/dist",
    emptyOutDir: true,
  },
  test: {
    environment: "happy-dom",
    setupFiles: ["src/test/setup.ts"],
    testTimeout: 15_000,
  },
});
