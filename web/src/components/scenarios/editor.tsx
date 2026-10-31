import {
  autocompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { yaml } from "@codemirror/lang-yaml";
import {
  bracketMatching,
  HighlightStyle,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import { type Diagnostic, setDiagnostics } from "@codemirror/lint";
import { highlightSelectionMatches, searchKeymap } from "@codemirror/search";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import {
  drawSelection,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  hoverTooltip,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { useEffect, useLayoutEffect, useRef } from "react";

import { reflow } from "@/lib/scenario";
import { hoverAt, keyOptions, placeAt, type Schema, valueOptions } from "@/lib/yaml-schema";

// Problems found in the text, by line (1-based); a line of null marks the whole file.
export interface LineProblem {
  line: number | null;
  msg: string;
  severity: "error" | "warning";
}

const theme = EditorView.theme({
  "&": {
    height: "100%",
    fontSize: "13px",
    color: "var(--foreground)",
    backgroundColor: "var(--background)",
  },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.55" },
  ".cm-content": { caretColor: "var(--foreground)" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--foreground)" },
  "&.cm-focused": { outline: "none" },
  ".cm-gutters": {
    backgroundColor: "var(--background)",
    color: "var(--muted-foreground)",
    border: "none",
    borderRight: "1px solid var(--border)",
  },
  ".cm-activeLine, .cm-activeLineGutter": {
    backgroundColor: "color-mix(in oklab, var(--foreground) 4%, transparent)",
  },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    backgroundColor: "color-mix(in oklab, var(--pen-1) 22%, transparent) !important",
  },
  ".cm-tooltip": {
    backgroundColor: "var(--popover)",
    color: "var(--popover-foreground)",
    border: "1px solid var(--border)",
    borderRadius: "4px",
  },
  ".cm-tooltip-autocomplete ul li[aria-selected]": {
    backgroundColor: "var(--accent)",
    color: "var(--accent-foreground)",
  },
  ".cm-completionDetail": { color: "var(--muted-foreground)", marginLeft: "0.75em" },
  ".cm-completionInfo, .cm-hover": {
    maxWidth: "32rem",
    padding: "6px 8px",
    whiteSpace: "pre-wrap",
    fontFamily: "var(--font-sans)",
  },
  ".cm-hover-title": { fontFamily: "var(--font-mono)", marginBottom: "4px" },
  ".cm-diagnostic": { fontFamily: "var(--font-sans)", borderLeftColor: "var(--foreground)" },
  ".cm-diagnostic-warning": { borderLeftColor: "var(--muted-foreground)" },
  ".cm-lintRange-error, .cm-lintRange-warning": {
    backgroundImage: "none",
    textDecoration: "underline wavy var(--foreground)",
    textUnderlineOffset: "3px",
  },
  ".cm-lintRange-warning": { textDecorationColor: "var(--muted-foreground)" },
});

const highlight = HighlightStyle.define([
  { tag: [tags.propertyName, tags.definition(tags.propertyName)], color: "var(--pen-1)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--foreground)" },
  { tag: [tags.number, tags.bool, tags.null, tags.atom], color: "var(--pen-2)" },
  { tag: [tags.comment, tags.meta], color: "var(--muted-foreground)", fontStyle: "italic" },
  { tag: [tags.keyword, tags.typeName, tags.labelName], color: "var(--pen-3)" },
  { tag: [tags.punctuation, tags.separator], color: "var(--muted-foreground)" },
]);

function completions(schema: Schema) {
  return (ctx: CompletionContext): CompletionResult | null => {
    const word = ctx.matchBefore(/[\w.-]*/);
    if (!ctx.explicit && (word === null || word.from === word.to)) return null;
    const place = placeAt(ctx.state.doc.toString(), ctx.pos);
    if (!place) return null;
    const asValue = place.key !== null;
    const found = asValue ? valueOptions(schema, place) : keyOptions(schema, place);
    if (found.length === 0) return null;
    const options: Completion[] = found.map((o) => ({
      label: o.label,
      detail: o.detail,
      ...(o.info ? { info: reflow(o.info) } : {}),
      type: asValue ? "enum" : "property",
      apply: asValue ? o.label : `${o.label}: `,
    }));
    return { from: place.from, options, validFor: /^[\w.-]*$/ };
  };
}

function hover(schema: Schema) {
  return hoverTooltip((view, pos) => {
    const found = hoverAt(schema, view.state.doc.toString(), pos);
    if (!found) return null;
    return {
      pos,
      above: true,
      create: () => {
        const dom = document.createElement("div");
        dom.className = "cm-hover";
        const title = document.createElement("div");
        title.className = "cm-hover-title";
        title.textContent = found.title;
        dom.append(title);
        if (found.doc) dom.append(document.createTextNode(reflow(found.doc)));
        return { dom };
      },
    };
  });
}

function toDiagnostics(state: EditorState, problems: readonly LineProblem[]): Diagnostic[] {
  return problems
    .filter((p) => p.line !== null && p.line >= 1 && p.line <= state.doc.lines)
    .map((p) => {
      const line = state.doc.line(p.line ?? 1);
      return { from: line.from, to: line.to, severity: p.severity, message: p.msg };
    });
}

// A CodeMirror editor over `text`. YAML files get highlighting and, with `schema`, completion
// and hover docs. Mod-s calls `onSave`.
export function CodeEditor({
  text,
  yamlFile,
  schema,
  readOnly,
  problems,
  onChange,
  onSave,
  label,
  goto,
}: {
  text: string;
  yamlFile: boolean;
  schema: Schema | undefined;
  readOnly: boolean;
  problems: readonly LineProblem[];
  onChange: (text: string) => void;
  onSave: () => void;
  label: string;
  // a line to show and put the cursor on; a new object each time it is asked
  goto?: { line: number } | null;
}) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const handlers = useRef({ onChange, onSave });
  useLayoutEffect(() => {
    handlers.current = { onChange, onSave };
  });
  // the editor is made once, from the first text and label; later props reach it below
  const first = useRef({ text, label });
  const language = useRef(new Compartment());
  const editable = useRef(new Compartment());

  useEffect(() => {
    if (!host.current) return;
    const extensions: Extension[] = [
      lineNumbers(),
      highlightActiveLineGutter(),
      history(),
      drawSelection(),
      indentOnInput(),
      bracketMatching(),
      highlightActiveLine(),
      highlightSelectionMatches(),
      syntaxHighlighting(highlight),
      theme,
      EditorState.tabSize.of(2),
      keymap.of([
        {
          key: "Mod-s",
          run: () => {
            handlers.current.onSave();
            return true;
          },
        },
        indentWithTab,
        ...defaultKeymap,
        ...historyKeymap,
        ...searchKeymap,
      ]),
      EditorView.contentAttributes.of({ "aria-label": first.current.label }),
      EditorView.updateListener.of((u) => {
        if (u.docChanged) handlers.current.onChange(u.state.doc.toString());
      }),
      language.current.of([]),
      editable.current.of([]),
    ];
    view.current = new EditorView({
      state: EditorState.create({ doc: first.current.text, extensions }),
      parent: host.current,
    });
    return () => {
      view.current?.destroy();
      view.current = null;
    };
  }, []);

  useEffect(() => {
    const v = view.current;
    if (!v || v.state.doc.toString() === text) return;
    v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: text } });
  }, [text]);

  useEffect(() => {
    const v = view.current;
    if (!v) return;
    const lang: Extension[] = yamlFile ? [yaml()] : [];
    if (yamlFile && schema) {
      lang.push(autocompletion({ override: [completions(schema)] }), hover(schema));
    }
    v.dispatch({ effects: language.current.reconfigure(lang) });
  }, [yamlFile, schema]);

  useEffect(() => {
    view.current?.dispatch({
      effects: editable.current.reconfigure([
        EditorState.readOnly.of(readOnly),
        EditorView.editable.of(!readOnly),
      ]),
    });
  }, [readOnly]);

  useEffect(() => {
    const v = view.current;
    if (!v) return;
    v.dispatch(setDiagnostics(v.state, toDiagnostics(v.state, problems)));
  }, [problems]);

  useEffect(() => {
    const v = view.current;
    if (!v || !goto) return;
    const line = v.state.doc.line(Math.min(Math.max(goto.line, 1), v.state.doc.lines));
    v.dispatch({ selection: { anchor: line.from }, scrollIntoView: true });
    v.focus();
  }, [goto]);

  return <div ref={host} className="h-full min-h-0 overflow-hidden" />;
}
