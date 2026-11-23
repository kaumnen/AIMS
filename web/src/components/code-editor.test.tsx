import { EditorView } from "@codemirror/view";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeAll, expect, test } from "vitest";

import { CodeEditor, type Language, type LineProblem, Problems } from "./code-editor";

beforeAll(() => {
  const rect = { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => ({ ...rect, toJSON: () => rect }) as DOMRect;
});
afterEach(cleanup);

// The editor beside its Problems list, as a file page lays them out.
function Page({
  text,
  language,
  problems,
}: {
  text: string;
  language: Language;
  problems: LineProblem[];
}) {
  const [goto, setGoto] = useState<{ line: number } | null>(null);
  return (
    <>
      <CodeEditor
        text={text}
        language={language}
        readOnly={false}
        problems={problems}
        label="Contents"
        goto={goto}
      />
      <Problems
        problems={problems}
        {...(language === "yaml" ? { text } : {})}
        onGoto={(line) => setGoto({ line })}
      />
    </>
  );
}

test("a problem names its line as the editor numbers it and moves the editor there", async () => {
  // YAML ends a line at the lone CR, so "c: [" is its line 4; the editor keeps the CR within
  // its line 2, so the gutter beside "c: [" says 3
  const text = "a: 1\nb: 2\rd: 3\nc: [\n";
  render(
    <Page text={text} language="yaml" problems={[{ line: 4, severity: "error", msg: "open" }]} />,
  );
  const view = EditorView.findFromDOM(await screen.findByLabelText("Contents"));
  if (!view) throw new Error("no editor");
  const problem = screen.getByRole("button", { name: "line 3: open" });
  expect(view.state.doc.lineAt(text.indexOf("c: [")).number).toBe(3);
  fireEvent.click(problem);
  expect(view.state.selection.main.head).toBe(text.indexOf("c: ["));
  cleanup();

  // TOML ends lines at LF only: its lines are the editor's
  render(
    <Page
      text={"a = 1\rb\n"}
      language="toml"
      problems={[{ line: 1, severity: "error", msg: "bad" }]}
    />,
  );
  expect(screen.getByRole("button", { name: "line 1: bad" })).toBeTruthy();
});
