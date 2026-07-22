import type { SyntheticEvent } from "react";

import { Button } from "../components/button";
import { FormField, TextInput } from "../components/form-field";
import { useBrowserSessionActions, useBrowserSessionState } from "./session-context";

export function PmActivation() {
  const state = useBrowserSessionState();
  const { activatePm } = useBrowserSessionActions();
  const denied = state.phase === "activation-required" && state.reason === "denied";

  function submit(event: SyntheticEvent<HTMLFormElement, SubmitEvent>): void {
    event.preventDefault();
    const field = event.currentTarget.elements.namedItem("bootstrap-token");
    if (!(field instanceof HTMLInputElement)) {
      return;
    }
    const bootstrapToken = field.value;
    field.value = "";
    void activatePm(bootstrapToken);
  }

  return (
    <form className="grid gap-5" onSubmit={submit}>
      <FormField
        label="Access key"
        labelFor="bootstrap-token"
        hint="Enter the access key provided for this demo."
        {...(denied ? { error: "That access key was not accepted. Check it and try again." } : {})}
      >
        <TextInput
          id="bootstrap-token"
          name="bootstrap-token"
          type="password"
          autoComplete="off"
          minLength={32}
          maxLength={1024}
          required
          spellCheck={false}
          invalid={denied}
          aria-describedby="bootstrap-token-hint"
          aria-errormessage={denied ? "bootstrap-token-error" : undefined}
        />
      </FormField>
      <div className="flex justify-end">
        <Button type="submit">Open workspace</Button>
      </div>
    </form>
  );
}
