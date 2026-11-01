import { afterEach, expect, test } from "vitest";

import { shortcut } from "./keys";

afterEach(() => {
  document.body.replaceChildren();
});

function press(target: EventTarget, init: KeyboardEventInit = {}) {
  let seen: boolean | undefined;
  const listen = (e: Event) => {
    seen = shortcut(e as KeyboardEvent);
  };
  window.addEventListener("keydown", listen);
  target.dispatchEvent(new KeyboardEvent("keydown", { key: "j", bubbles: true, ...init }));
  window.removeEventListener("keydown", listen);
  return seen;
}

test("a key on the page or in the shell is a shortcut", () => {
  document.body.innerHTML = `<div data-shell><button>b</button></div>`;
  expect(press(document.body)).toBe(true);
  expect(press(document.querySelector("button")!)).toBe(true);
});

test("typing, a modifier, a handled key or a popup outside the shell is not", () => {
  document.body.innerHTML = `
    <div data-shell><input /><textarea></textarea><div contenteditable="true"></div>
      <button id="in"></button></div>
    <div role="dialog"><button id="out"></button></div>`;
  const typed = ["input", "textarea", "[contenteditable]", "#out"];
  expect(typed.map((sel) => press(document.querySelector(sel)!))).toEqual(typed.map(() => false));
  const button = document.querySelector("#in")!;
  expect(press(button, { metaKey: true })).toBe(false);
  expect(press(button, { ctrlKey: true })).toBe(false);
  expect(press(button, { altKey: true })).toBe(false);
  button.addEventListener("keydown", (e) => e.preventDefault(), { once: true });
  expect(press(button, { cancelable: true })).toBe(false);
});
