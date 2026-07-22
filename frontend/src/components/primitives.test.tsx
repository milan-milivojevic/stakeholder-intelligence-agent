import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it } from "vitest";

import { Button } from "./button";
import { FormField, TextArea, TextInput } from "./form-field";
import { LoadingIndicator } from "./loading-indicator";
import { ErrorNotice, InfoNotice, SuccessNotice, WarningNotice } from "./notice";
import { Panel, PanelBody, PanelHeader, PanelTitle } from "./panel";

describe("accessible primitives", () => {
  it("supports keyboard button activation", async () => {
    const user = userEvent.setup();
    let activations = 0;
    render(<Button onClick={() => (activations += 1)}>Continue</Button>);

    await user.tab();
    expect(screen.getByRole("button", { name: "Continue" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(activations).toBe(1);
  });

  it("exposes labels, hints, invalid state, and error text", () => {
    render(
      <FormField
        label="Engagement name"
        labelFor="engagement-name"
        hint="Use the client-facing name."
        error="An engagement name is required."
      >
        <TextInput id="engagement-name" invalid />
      </FormField>,
    );

    expect(screen.getByRole("textbox", { name: "Engagement name" })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("An engagement name is required.");
    expect(screen.getByText("Use the client-facing name.")).toBeVisible();
  });

  it("renders the approved explicit status and panel variants without accessibility violations", async () => {
    const { container } = render(
      <Panel aria-label="Verification panel">
        <PanelHeader>
          <PanelTitle>Verification</PanelTitle>
        </PanelHeader>
        <PanelBody>
          <InfoNotice>Context is loading.</InfoNotice>
          <SuccessNotice>Upload completed.</SuccessNotice>
          <WarningNotice>Evidence is incomplete.</WarningNotice>
          <ErrorNotice>Access was denied.</ErrorNotice>
          <FormField label="Question" labelFor="question">
            <TextArea id="question" />
          </FormField>
          <LoadingIndicator label="Loading evidence…" />
        </PanelBody>
      </Panel>,
    );

    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });
});
