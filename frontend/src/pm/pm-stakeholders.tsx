import { useEffect, useMemo, useState } from "react";
import type { SyntheticEvent } from "react";

import type {
  Engagement,
  InterviewSession,
  InvitationSummary,
  Stakeholder,
} from "../api/contracts";
import { Button } from "../components/button";
import { FormField, TextInput } from "../components/form-field";
import { LoadingIndicator } from "../components/loading-indicator";
import { WarningNotice } from "../components/notice";
import { EmptyState, SafeFailureNotice, StatusBadge } from "./common";
import type { PmApi } from "./pm-api";
import { failureFromResult, formatDateTime } from "./safe-ui";
import type { SafeUiFailure } from "./safe-ui";

interface PmStakeholdersProps {
  api: PmApi;
  engagement: Engagement;
}

interface StakeholderData {
  stakeholders: Stakeholder[];
  invitations: InvitationSummary[];
  interviews: InterviewSession[];
}

interface IssuedInvitation {
  invitation: InvitationSummary;
  link: string;
}

interface StakeholderBadge {
  label: string;
  tone: "neutral" | "info" | "success" | "warning" | "error";
}

function invitationTone(
  invitation: InvitationSummary,
  interview: InterviewSession | null,
  now: number,
) {
  if (completedInterview(interview)) {
    return "success" as const;
  }
  if (new Date(invitation.expires_at).getTime() <= now) {
    return "warning" as const;
  }
  switch (invitation.status) {
    case "active":
      return "success" as const;
    case "activated":
      return "info" as const;
    case "expired":
      return "warning" as const;
    case "revoked":
      return "error" as const;
  }
}

function interviewFor(
  interviews: InterviewSession[],
  stakeholderId: string,
): InterviewSession | null {
  return (
    interviews
      .filter((interview) => interview.stakeholder_id === stakeholderId)
      .sort((left, right) => right.started_at.localeCompare(left.started_at))[0] ?? null
  );
}

function interviewLabel(interview: InterviewSession | null): string {
  if (interview === null) {
    return "Not started";
  }
  if (completedInterview(interview)) {
    return "Completed";
  }
  return interview.status === "failed" ? "Needs attention" : "In progress";
}

function invitationStatusLabel(
  invitation: InvitationSummary,
  interview: InterviewSession | null,
  now: number,
): string {
  if (completedInterview(interview)) {
    return "Completed";
  }
  const expired = new Date(invitation.expires_at).getTime() <= now;
  if (invitation.status === "activated" && expired) {
    return "Session expired";
  }
  if (invitation.status === "active" && expired) {
    return "Invitation expired";
  }
  switch (invitation.status) {
    case "active":
      return "Ready to open";
    case "activated":
      return "Session active";
    case "expired":
      return invitation.activated_at === null ? "Invitation expired" : "Session expired";
    case "revoked":
      return "Revoked";
  }
}

function completedInterview(interview: InterviewSession | null): boolean {
  return (
    interview?.status === "finalizing" ||
    interview?.status === "finalized" ||
    interview?.status === "ingesting" ||
    interview?.status === "ready"
  );
}

function latestInvitation(
  invitations: InvitationSummary[],
  stakeholderId: string,
): InvitationSummary | null {
  return (
    invitations
      .filter((invitation) => invitation.stakeholder_id === stakeholderId)
      .sort((left, right) => right.created_at.localeCompare(left.created_at))[0] ?? null
  );
}

function linkIsAvailable(invitation: InvitationSummary | null, now: number): boolean {
  return (
    invitation !== null &&
    (invitation.status === "active" || invitation.status === "activated") &&
    new Date(invitation.expires_at).getTime() > now
  );
}

function invitationLink(invitationToken: string): string {
  return new URL(`/s/${encodeURIComponent(invitationToken)}`, window.location.origin).toString();
}

function stakeholderBadge(
  stakeholder: Stakeholder,
  invitation: InvitationSummary | null,
  interview: InterviewSession | null,
  now: number,
): StakeholderBadge {
  if (stakeholder.status === "revoked") {
    return { label: "Stakeholder revoked", tone: "error" };
  }
  if (completedInterview(interview)) {
    return { label: "Interview completed", tone: "success" };
  }
  if (interview?.status === "failed") {
    return { label: "Interview needs attention", tone: "warning" };
  }
  if (invitation === null) {
    return { label: "No invitation", tone: "neutral" };
  }

  const expired = new Date(invitation.expires_at).getTime() <= now;
  if (invitation.status === "revoked") {
    return { label: "Invitation revoked", tone: "error" };
  }
  if (invitation.status === "activated") {
    return expired
      ? { label: "Session expired", tone: "warning" }
      : { label: "Interview in progress", tone: "info" };
  }
  if (invitation.status === "active") {
    return expired
      ? { label: "Invitation expired", tone: "warning" }
      : { label: "Invitation ready", tone: "success" };
  }
  return invitation.activated_at === null
    ? { label: "Invitation expired", tone: "warning" }
    : { label: "Session expired", tone: "warning" };
}

export function PmStakeholders({ api, engagement }: PmStakeholdersProps) {
  const [data, setData] = useState<StakeholderData | null>(null);
  const [addFormVisible, setAddFormVisible] = useState(false);
  const [failure, setFailure] = useState<SafeUiFailure | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("");
  const [department, setDepartment] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [issuingFor, setIssuingFor] = useState<string | null>(null);
  const [issued, setIssued] = useState<IssuedInvitation | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [currentTime, setCurrentTime] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setCurrentTime(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    let current = true;
    async function load(): Promise<void> {
      setIssued(null);
      try {
        const [stakeholders, invitations, interviews] = await Promise.all([
          api.listStakeholders(engagement.engagement_id),
          api.listInvitations(engagement.engagement_id),
          api.listInterviews(engagement.engagement_id),
        ]);
        if (!current) {
          return;
        }
        if (!stakeholders.ok) {
          setFailure(failureFromResult(stakeholders));
          return;
        }
        if (!invitations.ok) {
          setFailure(failureFromResult(invitations));
          return;
        }
        if (!interviews.ok) {
          setFailure(failureFromResult(interviews));
          return;
        }
        setData({
          stakeholders: stakeholders.value,
          invitations: invitations.value,
          interviews: interviews.value,
        });
      } catch {
        if (current) {
          setFailure({
            message: "Stakeholders and interview invitations could not be loaded.",
            correlationId: null,
          });
        }
      }
    }
    void load();
    return () => {
      current = false;
    };
  }, [api, engagement.engagement_id]);

  const stakeholderNames = useMemo(
    () =>
      new Map(
        data?.stakeholders.map((stakeholder) => [
          stakeholder.stakeholder_id,
          stakeholder.display_name,
        ]),
      ),
    [data?.stakeholders],
  );

  async function createStakeholder(event: SyntheticEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const cleanName = displayName.trim();
    if (cleanName.length === 0) {
      setNameError("Enter the stakeholder's display name.");
      return;
    }
    setCreating(true);
    setFailure(null);
    try {
      const result = await api.createStakeholder(engagement.engagement_id, {
        display_name: cleanName,
        role: role.trim() || null,
        department: department.trim() || null,
      });
      if (result.ok) {
        setData((current) =>
          current === null
            ? current
            : { ...current, stakeholders: [...current.stakeholders, result.value] },
        );
        setDisplayName("");
        setRole("");
        setDepartment("");
        setNameError(null);
        setAddFormVisible(false);
      } else {
        setFailure(failureFromResult(result));
      }
    } catch {
      setFailure({ message: "The stakeholder could not be created.", correlationId: null });
    } finally {
      setCreating(false);
    }
  }

  async function issueInvitation(stakeholderId: string): Promise<void> {
    setIssuingFor(stakeholderId);
    setFailure(null);
    setIssued(null);
    setCopied(false);
    try {
      const result = await api.issueInvitation(engagement.engagement_id, stakeholderId);
      if (result.ok) {
        const link = invitationLink(result.value.invitation_token);
        setData((current) =>
          current === null
            ? current
            : {
                ...current,
                invitations: [
                  result.value.invitation,
                  ...current.invitations.filter(
                    (item) => item.invitation_id !== result.value.invitation.invitation_id,
                  ),
                ],
              },
        );
        setIssued({ invitation: result.value.invitation, link });
      } else {
        setFailure(failureFromResult(result));
      }
    } catch {
      setFailure({ message: "The invitation could not be issued.", correlationId: null });
    } finally {
      setIssuingFor(null);
    }
  }

  async function copyInvitation(invitationId: string): Promise<void> {
    setFailure(null);
    let link = issued?.invitation.invitation_id === invitationId ? issued.link : null;
    if (link === null) {
      const result = await api.getInvitationLink(engagement.engagement_id, invitationId);
      if (!result.ok) {
        setFailure(failureFromResult(result));
        return;
      }
      link = invitationLink(result.value.invitation_token);
      setIssued({ invitation: result.value.invitation, link });
    }
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  async function copyIssuedInvitation(): Promise<void> {
    if (issued === null) {
      return;
    }
    await copyInvitation(issued.invitation.invitation_id);
  }

  async function revokeInvitation(invitationId: string): Promise<void> {
    setRevokingId(invitationId);
    setFailure(null);
    try {
      const result = await api.revokeInvitation(engagement.engagement_id, invitationId);
      if (result.ok) {
        setData((current) =>
          current === null
            ? current
            : {
                ...current,
                invitations: current.invitations.map((item) =>
                  item.invitation_id === invitationId ? result.value : item,
                ),
              },
        );
        if (issued?.invitation.invitation_id === invitationId) {
          setIssued(null);
        }
      } else {
        setFailure(failureFromResult(result));
      }
    } catch {
      setFailure({ message: "The invitation could not be revoked.", correlationId: null });
    } finally {
      setRevokingId(null);
    }
  }

  if (data === null && failure === null) {
    return <LoadingIndicator label="Loading stakeholders and invitations…" />;
  }

  return (
    <div className="grid gap-8">
      <div>
        <h4 className="text-xl font-semibold tracking-tight text-foreground">
          Stakeholders and interview invitations
        </h4>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          Manage stakeholders and their interview invitations for this engagement.
        </p>
      </div>

      {failure === null ? null : <SafeFailureNotice failure={failure} />}

      {issued === null ? null : (
        <WarningNotice title="Interview invitation link ready">
          <label htmlFor="issued-invitation-link" className="mt-3 block text-sm font-semibold">
            Interview invitation link
          </label>
          <input
            id="issued-invitation-link"
            readOnly
            value={issued.link}
            className="mt-2 min-h-11 w-full rounded-control border border-warning-border bg-surface px-3 py-2 font-mono text-sm text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            onFocus={(event) => event.currentTarget.select()}
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="small" variant="secondary" onClick={() => void copyIssuedInvitation()}>
              Copy invitation link
            </Button>
          </div>
          {copied ? <p className="mt-2 text-sm font-semibold">Copied to the clipboard.</p> : null}
        </WarningNotice>
      )}

      <section aria-labelledby="stakeholder-list-title" className="grid gap-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <h5 id="stakeholder-list-title" className="text-lg font-semibold text-foreground">
            Engagement stakeholders
          </h5>
          <Button
            size="small"
            variant="secondary"
            aria-controls="add-stakeholder-panel"
            aria-expanded={addFormVisible}
            onClick={() => setAddFormVisible((visible) => !visible)}
          >
            {addFormVisible ? "Close form" : "Add new stakeholder"}
          </Button>
        </div>
        {addFormVisible ? (
          <section
            id="add-stakeholder-panel"
            aria-labelledby="add-stakeholder-title"
            className="rounded-control bg-surface-subtle p-4 sm:p-5"
          >
            <h6 id="add-stakeholder-title" className="text-base font-semibold text-foreground">
              Add stakeholder
            </h6>
            <form
              className="mt-4 grid gap-4 sm:grid-cols-2"
              onSubmit={(event) => void createStakeholder(event)}
            >
              <FormField
                label="Display name"
                labelFor="stakeholder-display-name"
                error={nameError ?? undefined}
              >
                <TextInput
                  id="stakeholder-display-name"
                  value={displayName}
                  maxLength={200}
                  autoComplete="off"
                  invalid={nameError !== null}
                  aria-describedby={
                    nameError === null ? undefined : "stakeholder-display-name-error"
                  }
                  onChange={(event) => {
                    setDisplayName(event.target.value);
                    setNameError(null);
                  }}
                />
              </FormField>
              <FormField label="Role (optional)" labelFor="stakeholder-role">
                <TextInput
                  id="stakeholder-role"
                  value={role}
                  maxLength={200}
                  autoComplete="off"
                  onChange={(event) => setRole(event.target.value)}
                />
              </FormField>
              <FormField label="Department (optional)" labelFor="stakeholder-department">
                <TextInput
                  id="stakeholder-department"
                  value={department}
                  maxLength={200}
                  autoComplete="off"
                  onChange={(event) => setDepartment(event.target.value)}
                />
              </FormField>
              <div className="flex items-end justify-end sm:col-span-2">
                <Button type="submit" disabled={creating || data === null}>
                  {creating ? "Adding…" : "Add stakeholder"}
                </Button>
              </div>
            </form>
          </section>
        ) : null}
        {data === null || data.stakeholders.length === 0 ? (
          <EmptyState>No stakeholders have been added.</EmptyState>
        ) : (
          <ul className="grid gap-3 md:grid-cols-2">
            {data.stakeholders.map((stakeholder) => {
              const invitation = latestInvitation(data.invitations, stakeholder.stakeholder_id);
              const interview = interviewFor(data.interviews, stakeholder.stakeholder_id);
              const isCompleted = completedInterview(interview);
              const canCopy = linkIsAvailable(invitation, currentTime);
              const badge = stakeholderBadge(stakeholder, invitation, interview, currentTime);
              return (
                <li
                  key={stakeholder.stakeholder_id}
                  className="rounded-control border border-border p-4"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="font-semibold text-foreground">{stakeholder.display_name}</p>
                      <p className="mt-1 text-sm text-muted-foreground">
                        {stakeholder.role ?? "Role not provided"} ·{" "}
                        {stakeholder.department ?? "Department not provided"}
                      </p>
                    </div>
                    <StatusBadge label={badge.label} tone={badge.tone} />
                  </div>
                  <Button
                    className="mt-4"
                    size="small"
                    variant="secondary"
                    disabled={
                      issuingFor !== null || stakeholder.status === "revoked" || isCompleted
                    }
                    onClick={() =>
                      void (canCopy && invitation
                        ? copyInvitation(invitation.invitation_id)
                        : issueInvitation(stakeholder.stakeholder_id))
                    }
                  >
                    {issuingFor === stakeholder.stakeholder_id
                      ? "Generating…"
                      : isCompleted
                        ? "Interview completed"
                        : canCopy
                          ? "Copy invitation"
                          : "Generate invitation link"}
                  </Button>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section aria-labelledby="invitation-list-title" className="grid gap-4">
        <h5 id="invitation-list-title" className="text-lg font-semibold text-foreground">
          Interview invitation lifecycle
        </h5>
        {data === null || data.invitations.length === 0 ? (
          <EmptyState>No invitations have been issued.</EmptyState>
        ) : (
          <div className="overflow-x-auto rounded-control border border-border">
            <table className="w-full min-w-180 border-collapse text-left text-sm">
              <thead className="bg-surface-subtle text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-semibold">Stakeholder</th>
                  <th className="px-4 py-3 font-semibold">Invitation</th>
                  <th className="px-4 py-3 font-semibold">Interview</th>
                  <th className="px-4 py-3 font-semibold">Link or session expires</th>
                  <th className="px-4 py-3 font-semibold">Action</th>
                </tr>
              </thead>
              <tbody>
                {data.invitations.map((invitation) => {
                  const interview = interviewFor(data.interviews, invitation.stakeholder_id);
                  const completed = completedInterview(interview);
                  const available = linkIsAvailable(invitation, currentTime);
                  const latest = latestInvitation(data.invitations, invitation.stakeholder_id);
                  const canGenerate = latest?.invitation_id === invitation.invitation_id;
                  return (
                    <tr key={invitation.invitation_id} className="border-t border-border">
                      <td className="px-4 py-3 font-medium text-foreground">
                        {stakeholderNames.get(invitation.stakeholder_id) ?? "Unknown stakeholder"}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge
                          label={invitationStatusLabel(invitation, interview, currentTime)}
                          tone={invitationTone(invitation, interview, currentTime)}
                        />
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {interviewLabel(interview)}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        <time dateTime={invitation.expires_at}>
                          {formatDateTime(invitation.expires_at)}
                        </time>
                      </td>
                      <td className="px-4 py-3">
                        {completed ? (
                          <span className="text-muted-foreground">Interview completed</span>
                        ) : available ? (
                          <div className="flex flex-wrap gap-2">
                            <Button
                              size="small"
                              variant="secondary"
                              onClick={() => void copyInvitation(invitation.invitation_id)}
                            >
                              Copy invitation
                            </Button>
                            {invitation.status === "active" ? (
                              <Button
                                size="small"
                                variant="danger"
                                disabled={revokingId !== null}
                                onClick={() => void revokeInvitation(invitation.invitation_id)}
                              >
                                {revokingId === invitation.invitation_id ? "Revoking…" : "Revoke"}
                              </Button>
                            ) : null}
                          </div>
                        ) : canGenerate ? (
                          <Button
                            size="small"
                            variant="secondary"
                            disabled={issuingFor !== null}
                            onClick={() => void issueInvitation(invitation.stakeholder_id)}
                          >
                            {issuingFor === invitation.stakeholder_id
                              ? "Generating…"
                              : "Generate invitation link"}
                          </Button>
                        ) : (
                          <span className="text-muted-foreground">No action available</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
