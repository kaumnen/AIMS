import { queryOptions } from "@tanstack/react-query";

import type { Schema } from "@/lib/yaml-schema";

import { client, type Schemas } from "./client";

export type FileInfo = Schemas["FileInfo"];
export type Saved = Schemas["Saved"];
export type CheckResult = Schemas["CheckResult"];

export const SCENARIO_FILE = "scenario.yaml";
// Larger files open as a note, not in the editor.
export const MAX_EDIT_BYTES = 1024 * 1024;

export function filesQuery(source: string, name: string) {
  return queryOptions({
    queryKey: ["scenarios", "files", source, name] as const,
    queryFn: async ({ signal }) => {
      const { data, error } = await client.GET("/api/scenarios/{source}/{name}/files", {
        params: { path: { source, name } },
        signal,
      });
      if (error) throw error;
      return data;
    },
    staleTime: 5_000,
  });
}

// A file's text and the tag a save must name; `text` is null for bytes that are not UTF-8.
export interface Opened {
  text: string | null;
  size: number;
  etag: string;
}

const utf8 = new TextDecoder("utf-8", { fatal: true });

export function fileQuery(source: string, name: string, file: string) {
  return queryOptions({
    queryKey: ["scenarios", "file", source, name, file] as const,
    queryFn: async ({ signal }): Promise<Opened> => {
      const { data, error, response } = await client.GET(
        "/api/scenarios/{source}/{name}/files/{file}",
        { params: { path: { source, name, file } }, parseAs: "arrayBuffer", signal },
      );
      if (error) throw error;
      const bytes = data as unknown as ArrayBuffer;
      let text: string | null = null;
      try {
        text = utf8.decode(bytes);
      } catch {
        text = null;
      }
      return { text, size: bytes.byteLength, etag: response.headers.get("ETag") ?? "" };
    },
    staleTime: 0,
    gcTime: 0,
  });
}

export const schemaQuery = queryOptions({
  queryKey: ["scenarios", "schema"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/scenarios/schema", { signal });
    if (error) throw error;
    return data as Schema;
  },
  staleTime: Infinity,
});

export async function checkScenario(text: string, signal?: AbortSignal): Promise<CheckResult> {
  const { data, error } = await client.POST("/api/scenarios/check", {
    body: { text },
    ...(signal ? { signal } : {}),
  });
  if (error) throw error;
  return data;
}

// Write a file: `etag` names the version it replaces, null for a new file.
export async function saveFile(
  source: string,
  name: string,
  file: string,
  text: string,
  etag: string | null,
): Promise<Saved> {
  const { data, error } = await client.PUT("/api/scenarios/{source}/{name}/files/{file}", {
    params: {
      path: { source, name, file },
      header: etag === null ? { "If-None-Match": "*" } : { "If-Match": etag },
    },
    body: text,
    bodySerializer: (body) => new Blob([String(body)]),
    headers: { "Content-Type": "application/octet-stream" },
  });
  if (error) throw error;
  return data;
}

export async function deleteFile(
  source: string,
  name: string,
  file: string,
  etag: string,
): Promise<void> {
  const { error } = await client.DELETE("/api/scenarios/{source}/{name}/files/{file}", {
    params: { path: { source, name, file }, header: { "If-Match": etag } },
  });
  if (error) throw error;
}

export async function createScenario(source: string, name: string): Promise<Saved> {
  const { data, error } = await client.POST("/api/scenarios/{source}/{name}", {
    params: { path: { source, name } },
  });
  if (error) throw error;
  return data;
}

export async function duplicateScenario(
  from: { source: string; name: string },
  to: { source: string; name: string },
): Promise<Saved> {
  const { data, error } = await client.POST("/api/scenarios/{source}/{name}/duplicate", {
    params: { path: from },
    body: to,
  });
  if (error) throw error;
  return data;
}

export type PromptsInfo = Schemas["PromptsInfo"];
export type PromptKey = string[];
export type PromptText = PromptsInfo["agent"][string];

export const PROMPT_SOURCES: Record<PromptText["source"], string> = {
  default: "built in",
  user: "this machine",
  project: "this project",
};

export const promptsQuery = queryOptions({
  queryKey: ["prompts"] as const,
  queryFn: async ({ signal }) => {
    const { data, error } = await client.GET("/api/prompts", { signal });
    if (error) throw error;
    return data;
  },
  staleTime: 30_000,
});

// Set an agent's text override in scenario.yaml, or drop it with `value` null. `etag` is the
// scenario.yaml version the change is made against.
export async function patchPrompt(
  source: string,
  name: string,
  agent: string,
  key: PromptKey,
  value: string | null,
  etag: string,
): Promise<Saved> {
  const body = value === null ? { unset: [{ agent, key }] } : { set: [{ agent, key, value }] };
  const { data, error } = await client.PATCH("/api/scenarios/{source}/{name}/prompts", {
    params: { path: { source, name }, header: { "If-Match": `"${etag}"` } },
    body,
  });
  if (error) throw error;
  return data;
}
