import { useEffect, useState } from "react";
import type { SyntheticEvent } from "react";

import type { BrowserSessionView, Engagement } from "../api/contracts";
import { Button } from "../components/button";
import { FormField, TextArea, TextInput } from "../components/form-field";
import { LoadingIndicator } from "../components/loading-indicator";
import { InfoNotice } from "../components/notice";
import { classNames } from "../lib/class-names";
import { SafeFailureNotice } from "./common";
import { PmDocuments } from "./pm-documents";
import { PmInsights } from "./pm-insights";
import { PmInterviews } from "./pm-interviews";
import { PmStakeholders } from "./pm-stakeholders";
import type { PmApi } from "./pm-api";
import { failureFromResult } from "./safe-ui";
import type { SafeUiFailure } from "./safe-ui";

type WorkspaceTab = "stakeholders" | "documents" | "interviews" | "insights" | "history";

const tabs: readonly { id: WorkspaceTab; label: string }[] = [
  { id: "stakeholders", label: "Stakeholders and invitations" },
  { id: "documents", label: "Documents" },
  { id: "interviews", label: "Interviews" },
  { id: "insights", label: "Insight research" },
  { id: "history", label: "Insight history" },
];

interface PmWorkspaceProps {
  api: PmApi;
  session: BrowserSessionView;
}

interface WorkspaceLoad {
  engagements: Engagement[];
}

function EngagementChooser({
  api,
  engagements,
  onSelected,
}: {
  api: PmApi;
  engagements: Engagement[];
  onSelected: (engagement: Engagement, created: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [failure, setFailure] = useState<SafeUiFailure | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  async function create(event: SyntheticEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const cleanName = name.trim();
    if (cleanName.length === 0) {
      setNameError("Enter an engagement name.");
      return;
    }
    setCreating(true);
    setFailure(null);
    try {
      const result = await api.createEngagement({
        name: cleanName,
        description: description.trim() || null,
      });
      if (result.ok) {
        setName("");
        setDescription("");
        onSelected(result.value, true);
      } else {
        setFailure(failureFromResult(result));
      }
    } catch {
      setFailure({ message: "The engagement could not be created.", correlationId: null });
    } finally {
      setCreating(false);
    }
  }

  async function select(engagementId: string): Promise<void> {
    setPendingId(engagementId);
    setFailure(null);
    try {
      const result = await api.selectEngagement(engagementId);
      if (result.ok) {
        onSelected(result.value, false);
      } else {
        setFailure(failureFromResult(result));
      }
    } catch {
      setFailure({ message: "The engagement could not be opened.", correlationId: null });
    } finally {
      setPendingId(null);
    }
  }

  return (
    <div className="grid gap-7 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,0.8fr)]">
      <section aria-labelledby="available-engagements-title" className="grid content-start gap-4">
        <div>
          <h4 id="available-engagements-title" className="text-base font-semibold text-foreground">
            Available engagements
          </h4>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            Open an existing active engagement to restore its server-owned scope.
          </p>
        </div>
        {engagements.length === 0 ? (
          <InfoNotice title="No engagements yet">
            Create the first engagement. No database editing is required.
          </InfoNotice>
        ) : (
          <ul className="grid gap-3">
            {engagements.map((engagement) => (
              <li
                key={engagement.engagement_id}
                className="flex flex-col gap-3 rounded-control border border-border p-4 sm:flex-row sm:items-start sm:justify-between"
              >
                <div className="min-w-0">
                  <p className="font-semibold text-foreground">{engagement.name}</p>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">
                    {engagement.description ?? "No description provided."}
                  </p>
                </div>
                <Button
                  size="small"
                  variant="secondary"
                  disabled={pendingId !== null}
                  onClick={() => void select(engagement.engagement_id)}
                >
                  {pendingId === engagement.engagement_id ? "Opening…" : "Open"}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section
        aria-labelledby="create-engagement-title"
        className="rounded-control bg-surface-subtle p-4 sm:p-5"
      >
        <h4 id="create-engagement-title" className="text-base font-semibold text-foreground">
          Create engagement
        </h4>
        <form className="mt-4 grid gap-4" onSubmit={(event) => void create(event)}>
          <FormField
            label="Engagement name"
            labelFor="engagement-name"
            error={nameError ?? undefined}
          >
            <TextInput
              id="engagement-name"
              value={name}
              maxLength={200}
              autoComplete="off"
              invalid={nameError !== null}
              aria-describedby={nameError === null ? undefined : "engagement-name-error"}
              onChange={(event) => {
                setName(event.target.value);
                setNameError(null);
              }}
            />
          </FormField>
          <FormField label="Description (optional)" labelFor="engagement-description">
            <TextArea
              id="engagement-description"
              value={description}
              maxLength={4000}
              onChange={(event) => setDescription(event.target.value)}
            />
          </FormField>
          {failure === null ? null : <SafeFailureNotice failure={failure} />}
          <div className="flex justify-end">
            <Button type="submit" disabled={creating}>
              {creating ? "Creating…" : "Create and open"}
            </Button>
          </div>
        </form>
      </section>
    </div>
  );
}

export function PmWorkspace({ api, session }: PmWorkspaceProps) {
  const [load, setLoad] = useState<WorkspaceLoad | null>(null);
  const [loadFailure, setLoadFailure] = useState<SafeUiFailure | null>(null);
  const [selected, setSelected] = useState<Engagement | null>(null);
  const [chooserVisible, setChooserVisible] = useState(false);
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("stakeholders");

  useEffect(() => {
    let current = true;
    async function prepare(): Promise<void> {
      try {
        const [listResult, selectedResult] = await Promise.all([
          api.listEngagements(),
          session.engagement_id === null
            ? Promise.resolve(null)
            : api.getEngagement(session.engagement_id),
        ]);
        if (!current) {
          return;
        }
        if (!listResult.ok) {
          setLoadFailure(failureFromResult(listResult));
          return;
        }
        if (selectedResult !== null && !selectedResult.ok) {
          setLoadFailure(failureFromResult(selectedResult));
          setLoad({ engagements: listResult.value });
          return;
        }
        const initialSelection = selectedResult?.value ?? null;
        setLoad({ engagements: listResult.value });
        setSelected(initialSelection);
      } catch {
        if (current) {
          setLoadFailure({
            message: "The project manager workspace could not be loaded.",
            correlationId: null,
          });
        }
      }
    }
    void prepare();
    return () => {
      current = false;
    };
  }, [api, session.engagement_id]);

  function selectEngagement(engagement: Engagement, created: boolean): void {
    setSelected(engagement);
    setChooserVisible(false);
    setActiveTab("stakeholders");
    setLoad((current) => {
      const previous = current?.engagements ?? [];
      const engagements = created
        ? [
            engagement,
            ...previous.filter((item) => item.engagement_id !== engagement.engagement_id),
          ]
        : previous;
      return { engagements };
    });
  }

  return (
    <div className="grid min-w-0 gap-5">
      {load === null && loadFailure === null ? (
        <LoadingIndicator label="Loading engagements…" />
      ) : null}
      {loadFailure === null ? null : <SafeFailureNotice failure={loadFailure} />}

      {load === null ? null : selected === null || chooserVisible ? (
        <EngagementChooser api={api} engagements={load.engagements} onSelected={selectEngagement} />
      ) : (
        <div className="grid min-w-0 gap-5">
          <section
            className="flex flex-col gap-2 pb-3 sm:flex-row sm:items-center sm:justify-between"
            aria-label="Active engagement"
          >
            <div className="min-w-0">
              <p className="text-sm font-semibold tracking-wide text-muted-foreground uppercase">
                Engagement
              </p>
              <div className="min-w-0">
                <p className="truncate text-xl font-semibold text-foreground">{selected.name}</p>
              </div>
            </div>
            <Button className="shrink-0" size="small" onClick={() => setChooserVisible(true)}>
              Change engagement
            </Button>
          </section>

          <div className="min-w-0 border-b border-border">
            <div
              className="flex max-w-full min-w-0 gap-1 overflow-x-auto"
              role="tablist"
              aria-label="Engagement workspace sections"
            >
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  id={`pm-tab-${tab.id}`}
                  type="button"
                  role="tab"
                  aria-selected={activeTab === tab.id}
                  aria-controls={`pm-panel-${tab.id}`}
                  className={classNames(
                    "min-h-11 shrink-0 border-b-2 px-3 py-2 text-sm font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
                    activeTab === tab.id
                      ? "border-brand text-brand"
                      : "border-transparent text-muted-foreground hover:text-foreground",
                  )}
                  onClick={() => setActiveTab(tab.id)}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>

          <div
            id={`pm-panel-${activeTab}`}
            role="tabpanel"
            aria-labelledby={`pm-tab-${activeTab}`}
            tabIndex={0}
            className="min-w-0 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-focus"
          >
            {activeTab === "stakeholders" ? (
              <PmStakeholders key={selected.engagement_id} api={api} engagement={selected} />
            ) : null}
            {activeTab === "documents" ? (
              <PmDocuments key={selected.engagement_id} api={api} engagement={selected} />
            ) : null}
            {activeTab === "interviews" ? (
              <PmInterviews key={selected.engagement_id} api={api} engagement={selected} />
            ) : null}
            {activeTab === "insights" ? (
              <PmInsights
                key={`${selected.engagement_id}-research`}
                api={api}
                engagement={selected}
                view="research"
              />
            ) : null}
            {activeTab === "history" ? (
              <PmInsights
                key={`${selected.engagement_id}-history`}
                api={api}
                engagement={selected}
                view="history"
              />
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
