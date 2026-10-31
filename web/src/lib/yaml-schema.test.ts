import { expect, test } from "vitest";

import {
  hoverAt,
  ITEM,
  keyOptions,
  placeAt,
  type Schema,
  typeText,
  valueOptions,
} from "./yaml-schema";

// The shapes pydantic writes: $defs and $ref, optional fields as anyOf with null, plant kinds
// as oneOf told apart by a `kind` const.
const schema: Schema = {
  $defs: {
    Task: {
      description: "What the agent is asked to do.",
      properties: {
        instruction: { type: "string" },
        setup: { type: "array", items: { type: "string" } },
      },
      required: ["instruction"],
    },
    Agent: {
      description: "An agent of the run.",
      properties: {
        id: { type: "string" },
        role: { enum: ["subject", "confederate"], type: "string" },
        prompts: { anyOf: [{ $ref: "#/$defs/Prompts" }, { type: "null" }] },
      },
    },
    Prompts: { description: "Text overrides.", properties: { system: { type: "string" } } },
    FilePlant: {
      description: "A bait file.",
      properties: {
        name: { type: "string" },
        kind: { const: "file", type: "string" },
        path: { type: "string" },
      },
    },
    RulePlant: {
      description: "A custom trigger.",
      properties: {
        name: { type: "string" },
        kind: { const: "rule", type: "string" },
        rule: { $ref: "#/$defs/Rule" },
      },
    },
    Rule: {
      description: "A rule.",
      properties: { id: { type: "string" }, importance: { type: "integer" } },
    },
  },
  properties: {
    version: { const: 1, type: "integer" },
    oversight: { enum: ["observe", "autonomous", "human"], type: "string" },
    task: { $ref: "#/$defs/Task" },
    agents: { type: "array", items: { $ref: "#/$defs/Agent" } },
    plants: {
      type: "array",
      items: { oneOf: [{ $ref: "#/$defs/FilePlant" }, { $ref: "#/$defs/RulePlant" }] },
    },
    keep: { type: "boolean" },
  },
  required: ["version", "oversight", "task"],
};

const DOC = `version: 1
oversight: observe
task:
  instruction: |
    Fix it.
    name: not a key, the text of the instruction
  setup:
    - make
agents:
  - id: a
    role: subject
    prompts:
      system: Be brief.
plants:
  - name: notes
    kind: file
    path: notes.md
  - name: tidy
    kind: rule
    rule:
      id: tidy
`;

// The text with the cursor where `^` is, and the place there.
function at(marked: string) {
  const pos = marked.indexOf("^");
  const text = marked.replace("^", "");
  const place = placeAt(text, pos);
  if (!place) throw new Error("no place");
  return { text, pos, place };
}

const insert = (after: string, typed: string) => {
  const i = DOC.indexOf(after) + after.length;
  return DOC.slice(0, i) + typed + "^\n" + DOC.slice(i);
};

test("the path down to the cursor follows indents and sequence items", () => {
  expect(at(insert("      system: Be brief.\n", "      ")).place.path).toEqual([
    "agents",
    ITEM,
    "prompts",
  ]);
  expect(at(insert("      id: tidy\n", "      imp")).place.path).toEqual(["plants", ITEM, "rule"]);
  expect(at(insert("  setup:\n    - make\n", "  ")).place.path).toEqual(["task"]);
  expect(at(insert("    role: subject\n", "    ")).place.path).toEqual(["agents", ITEM]);
});

test("keys offered are the ones the mapping still lacks, of its own kind", () => {
  const top = keyOptions(schema, at(DOC + "^").place).map((o) => o.label);
  expect(top).toEqual(["keep"]);
  const agent = keyOptions(schema, at(insert("    role: subject\n", "    ")).place);
  expect(agent.map((o) => o.label)).toEqual([]);
  const rule = keyOptions(schema, at(insert("    path: notes.md\n", "    ")).place);
  expect(rule.map((o) => o.label)).toEqual([]);
  const ruled = at("plants:\n  - name: x\n    kind: rule\n    ^\n").place;
  expect(keyOptions(schema, ruled).map((o) => o.label)).toEqual(["rule"]);
  const filed = at("plants:\n  - name: x\n    ^\n    kind: file\n").place;
  expect(keyOptions(schema, filed).map((o) => o.label)).toEqual(["path"]);
  const inRule = keyOptions(schema, at(insert("      id: tidy\n", "      ")).place);
  expect(inRule.map((o) => [o.label, o.detail])).toEqual([["importance", "integer"]]);
  const task = keyOptions(schema, at(insert("  setup:\n    - make\n", "  ")).place);
  expect(task).toEqual([]);
});

test("a new item is a mapping of its own", () => {
  const fresh = at(insert("      system: Be brief.\n", "  - ")).place;
  expect(fresh.path).toEqual(["agents", ITEM]);
  expect(keyOptions(schema, fresh).map((o) => o.label)).toEqual(["id", "role", "prompts"]);
});

test("values come from enums, consts, booleans and the kinds a list tells apart", () => {
  const over = at("version: 1\noversight: ^\n").place;
  expect(over.key).toBe("oversight");
  expect(valueOptions(schema, over).map((o) => o.label)).toEqual([
    "observe",
    "autonomous",
    "human",
  ]);
  expect(valueOptions(schema, at("keep: ^").place).map((o) => o.label)).toEqual(["true", "false"]);
  const kind = at(insert("  - name: tidy\n", "    kind: ")).place;
  expect(valueOptions(schema, kind).map((o) => [o.label, o.info])).toEqual([
    ["file", "A bait file."],
    ["rule", "A custom trigger."],
  ]);
});

test("hover names the type and the docs of the field or its model", () => {
  const pos = (needle: string) => DOC.indexOf(needle) + 1;
  expect(hoverAt(schema, DOC, pos("task:"))).toEqual({
    title: "task: Task",
    doc: "What the agent is asked to do.",
  });
  expect(hoverAt(schema, DOC, pos("role:"))).toEqual({
    title: "role: subject | confederate",
    doc: "An agent of the run.",
  });
  expect(hoverAt(schema, DOC, pos("rule:"))?.doc).toBe("A rule.");
  expect(hoverAt(schema, DOC, DOC.indexOf("not a key"))).toBeNull();
  expect(typeText(schema, { anyOf: [{ $ref: "#/$defs/Prompts" }, { type: "null" }] })).toBe(
    "Prompts",
  );
});
