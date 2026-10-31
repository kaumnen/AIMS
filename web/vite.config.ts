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
    tanstackRouter({
      target: "react",
      autoCodeSplitting: true,
      routeFileIgnorePattern: "\\.test\\.tsx?$",
    }),
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
    // What first paint needs goes in a few named chunks, not dozens of shared ones; lazy
    // pages keep their own. scripts/budget.ts checks the sizes, so no size warning here.
    chunkSizeWarningLimit: Infinity,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "react",
              test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/,
              tags: ["$initial"],
              priority: 2,
            },
            { name: "vendor", test: /node_modules[\\/]/, tags: ["$initial"], priority: 1 },
            { name: "app", tags: ["$initial"] },
          ],
        },
      },
    },
  },
  test: {
    environment: "happy-dom",
    setupFiles: ["src/test/setup.ts"],
    testTimeout: 15_000,
  },
});
