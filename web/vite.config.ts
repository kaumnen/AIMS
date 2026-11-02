/// <reference types="vitest/config" />
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { brotliCompress, constants, gzip } from "node:zlib";

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

// A brotli and a gzip copy of each text asset, written next to it once the build is on disk;
// the server sends one by Accept-Encoding, so no request pays to compress. Browsers ask for
// brotli only over https and on localhost, so plain http gets the gzip copy.
function precompress(): Plugin {
  const br = promisify(brotliCompress);
  const gz = promisify(gzip);
  let assets = "";
  return {
    name: "aims-precompress",
    apply: "build",
    configResolved(config) {
      assets = join(resolve(config.root, config.build.outDir), config.build.assetsDir);
    },
    async closeBundle() {
      const files = (await readdir(assets)).filter((f) => /\.(js|css|svg|json)$/.test(f));
      const copy = async (file: string) => {
        const raw = await readFile(join(assets, file));
        const copies = await Promise.all([
          br(raw, {
            params: {
              [constants.BROTLI_PARAM_MODE]: constants.BROTLI_MODE_TEXT,
              [constants.BROTLI_PARAM_QUALITY]: constants.BROTLI_MAX_QUALITY,
              [constants.BROTLI_PARAM_SIZE_HINT]: raw.length,
            },
          }),
          gz(raw, { level: constants.Z_BEST_COMPRESSION }),
        ]);
        const writes = [".br", ".gz"].map((ext, i) => {
          const bytes = copies[i] as Buffer;
          return bytes.length < raw.length ? writeFile(join(assets, file + ext), bytes) : null;
        });
        await Promise.all(writes);
      };
      await Promise.all(files.map(copy));
    },
  };
}

export default defineConfig({
  plugins: [
    themeBoot(),
    precompress(),
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
