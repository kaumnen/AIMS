import { fireEvent, screen, waitFor, type within } from "@testing-library/react";
import { expect } from "vitest";

// Base UI's Select picks an option on pointer down, pointer up and click.
export async function pick(
  form: ReturnType<typeof within>,
  name: string,
  option: string | RegExp,
): Promise<void> {
  fireEvent.click(form.getByRole("combobox", { name }));
  const item = await screen.findByRole("option", { name: option });
  fireEvent.pointerDown(item, { pointerType: "mouse" });
  fireEvent.pointerUp(item, { pointerType: "mouse" });
  fireEvent.click(item);
  await waitFor(() => expect(form.getByRole("combobox", { name }).textContent).toMatch(option));
}
