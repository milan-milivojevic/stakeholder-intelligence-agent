import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";

import type { SafeRunEvent } from "../api/contracts";
import { Button } from "../components/button";
import { ErrorNotice, InfoNotice, SuccessNotice, WarningNotice } from "../components/notice";
import type {
  Citation,
  EvidenceDrillDownResponse,
  InsightExecutionMetrics,
  InsightReport,
  ResearchedTopicOutcome,
  SourceArtifactSummary,
} from "./contracts";
import { EmptyState, StatusBadge } from "./common";
import type { PmApi } from "./pm-api";
import { formatDateTime } from "./safe-ui";
import { describeSourceLocation } from "./source-location";

export interface EvidenceSelectionItem {
  evidenceId: string;
  sourceLabel: string;
  locationLabel: string;
}

export interface EvidenceSelection {
  title: string;
  items: EvidenceSelectionItem[];
}

interface ReportViewProps {
  report: InsightReport;
  metrics: InsightExecutionMetrics;
  events: SafeRunEvent[];
  onOpenEvidence: (selection: EvidenceSelection) => void;
}

function eventActionLabel(action: string): string {
  const labels: Record<string, string> = {
    run_queued: "Analysis queued",
    run_planning: "Research planning started",
    run_researching: "Research started",
    run_editing: "Report drafting started",
    run_validating: "Report validation started",
    run_complete: "Analysis completed",
    run_partial: "Analysis partially completed",
    run_insufficient_evidence: "More evidence needed",
    run_failed: "Analysis failed safely",
    research_plan_saved: "Research plan saved",
    research_topic_started: "Research started",
    research_topic_completed: "Research completed",
    scoped_retrieval_completed: "Source search completed",
    research_artifacts_saved: "Research findings saved",
    research_package_loaded: "Research package prepared for the report",
  };
  return labels[action] ?? action.replaceAll("_", " ");
}

function eventActorLabel(actor: string): string {
  const labels: Record<string, string> = {
    insight_service: "Insight service",
    insight_orchestrator: "Research coordinator",
    "topic-researcher": "Researcher",
    researcher: "Researcher",
    "report-editor": "Report editor",
    editor: "Report editor",
  };
  return labels[actor] ?? actor.replaceAll("_", " ");
}

function eventArtifactLabel(artifactName: string): string {
  if (artifactName === "research_plan.md") {
    return "Research plan";
  }
  if (artifactName.endsWith("/findings.md")) {
    return "Research findings";
  }
  return artifactName.replaceAll("_", " ").replace(/\.[^.]+$/u, "");
}

export function ExecutionTimeline({
  events,
  topics = [],
}: {
  events: SafeRunEvent[];
  topics?: ResearchedTopicOutcome[];
}) {
  const topicTitles = new Map(topics.map((topic) => [topic.topic_id, topic.title]));
  const unknownTopicIds = [
    ...new Set(events.flatMap((event) => (event.topic_id === null ? [] : [event.topic_id]))),
  ];

  return (
    <ol className="grid gap-3">
      {events.map((item) => {
        const topicPosition = item.topic_id === null ? -1 : unknownTopicIds.indexOf(item.topic_id);
        const topicTitle =
          item.topic_id === null
            ? null
            : (topicTitles.get(item.topic_id) ?? `Research topic ${String(topicPosition + 1)}`);
        const excerptCount = item.evidence_ids.length;
        const sourceCount = new Set(item.source_ids).size;
        return (
          <li key={item.event_id} className="rounded-control border border-border p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="font-semibold text-foreground">
                  {topicTitle ?? eventActorLabel(item.actor)}
                </p>
                <p className="mt-1 text-sm text-muted-foreground">
                  {eventActionLabel(item.action)}
                </p>
                {sourceCount === 0 && excerptCount === 0 ? null : (
                  <p className="mt-1 text-xs text-muted-foreground">
                    {String(sourceCount)} {sourceCount === 1 ? "source" : "sources"} ·{" "}
                    {String(excerptCount)} relevant {excerptCount === 1 ? "excerpt" : "excerpts"}
                  </p>
                )}
              </div>
              {item.to_status === null ? null : <StatusBadge label={item.to_status} tone="info" />}
            </div>
            {item.artifact_name === null ? null : (
              <p className="mt-3 text-xs text-muted-foreground">
                <strong>Output:</strong> {eventArtifactLabel(item.artifact_name)}
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function sourceItemsFor(report: InsightReport, evidenceIds: string[]): EvidenceSelectionItem[] {
  const wanted = new Set(evidenceIds);
  return report.citations
    .filter((citation) => wanted.has(citation.evidence_id))
    .map((citation) => ({
      evidenceId: citation.evidence_id,
      sourceLabel: citation.display_label,
      locationLabel: describeSourceLocation(citation.source_location),
    }));
}

function SupportingSourcesButton({
  report,
  evidenceIds,
  title,
  onOpenEvidence,
}: {
  report: InsightReport;
  evidenceIds: string[];
  title: string;
  onOpenEvidence: (selection: EvidenceSelection) => void;
}) {
  const items = sourceItemsFor(report, evidenceIds);
  if (items.length === 0) {
    return null;
  }
  return (
    <Button
      className="mt-3"
      size="small"
      variant="quiet"
      onClick={() => onOpenEvidence({ title, items })}
    >
      {items.length === 1
        ? "View supporting source"
        : `View supporting sources (${String(items.length)})`}
    </Button>
  );
}

function ReportStatus({ report }: { report: InsightReport }) {
  if (report.status === "complete") {
    return (
      <SuccessNotice title="Analysis complete">
        The answer is supported by permitted sources from this engagement.
      </SuccessNotice>
    );
  }
  if (report.status === "partial") {
    return (
      <WarningNotice title="Analysis partially complete">
        Supported findings are available, but some questions remain evidence-limited.
      </WarningNotice>
    );
  }
  return (
    <InfoNotice title="More evidence needed">
      The analysis completed without asserting unsupported conclusions. Review the evidence gaps and
      recommended next steps.
    </InfoNotice>
  );
}

function SummaryCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-control border border-border bg-surface-subtle p-4">
      <p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{title}</p>
      <div className="mt-2 text-sm leading-6 text-foreground">{children}</div>
    </section>
  );
}

function DisclosureSection({
  title,
  summary,
  children,
}: {
  title: string;
  summary: string;
  children: ReactNode;
}) {
  return (
    <details className="group rounded-control border border-border bg-surface">
      <summary className="cursor-pointer list-none px-4 py-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
        <span className="flex items-start justify-between gap-4">
          <span>
            <span className="block font-semibold text-foreground">{title}</span>
            <span className="mt-1 block text-sm leading-6 text-muted-foreground">{summary}</span>
          </span>
          <span
            aria-hidden="true"
            className="mt-0.5 text-lg text-muted-foreground group-open:rotate-45"
          >
            +
          </span>
        </span>
      </summary>
      <div className="border-t border-border px-4 py-5">{children}</div>
    </details>
  );
}

function listOrEmpty(items: string[], empty: string) {
  return items.length === 0 ? (
    <EmptyState>{empty}</EmptyState>
  ) : (
    <ul className="grid list-disc gap-2 pl-5 text-sm leading-6 text-foreground">
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

interface CitationGroup {
  sourceLabel: string;
  locationLabel: string;
  citations: Citation[];
}

function groupedCitations(citations: Citation[]): CitationGroup[] {
  const groups = new Map<string, CitationGroup>();
  for (const citation of citations) {
    const locationLabel = describeSourceLocation(citation.source_location);
    const key = `${citation.display_label}\u0000${locationLabel}`;
    const existing = groups.get(key);
    if (existing === undefined) {
      groups.set(key, {
        sourceLabel: citation.display_label,
        locationLabel,
        citations: [citation],
      });
    } else {
      existing.citations.push(citation);
    }
  }
  return [...groups.values()];
}

export function ReportView({ report, metrics, events, onOpenEvidence }: ReportViewProps) {
  const sourceCount = new Set(report.citations.map((citation) => citation.display_label)).size;
  const approvalFinding =
    report.findings.find((finding) => /approv|depend|decision/iu.test(finding.statement)) ??
    report.findings.at(0);
  const primaryResponsibility = report.responsibilities.at(0);
  const primaryRisk = report.operational_risks.at(0);
  const primaryGap = report.evidence_gaps.at(0);
  const citationGroups = groupedCitations(report.citations);

  return (
    <article
      aria-labelledby="insight-report-title"
      className="grid gap-6 rounded-panel border border-border bg-surface p-5 sm:p-6"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-sm font-semibold tracking-wide text-brand uppercase">
            Insight summary
          </p>
          <h5 id="insight-report-title" className="mt-2 text-lg font-semibold text-foreground">
            Evidence-grounded report
          </h5>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">{report.question}</p>
        </div>
        <p className="shrink-0 rounded-full bg-surface-subtle px-3 py-1.5 text-sm font-semibold text-muted-foreground">
          {String(sourceCount)} {sourceCount === 1 ? "source" : "sources"} used
        </p>
      </div>

      <ReportStatus report={report} />

      <section aria-labelledby="executive-summary-title">
        <h6 id="executive-summary-title" className="font-semibold text-foreground">
          What you need to know
        </h6>
        <p className="mt-2 text-sm leading-7 whitespace-pre-wrap text-foreground">
          {report.executive_summary}
        </p>
      </section>

      <section aria-labelledby="key-insights-title">
        <h6 id="key-insights-title" className="font-semibold text-foreground">
          Key insights
        </h6>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <SummaryCard title="Primary responsibility">
            {primaryResponsibility === undefined ? (
              "No supported responsibility was identified."
            ) : (
              <>
                <strong>{primaryResponsibility.attribution}:</strong>{" "}
                {primaryResponsibility.responsibility}
              </>
            )}
          </SummaryCard>
          <SummaryCard title="Main operational risk">
            {primaryRisk === undefined ? (
              "No supported operational risk was identified."
            ) : (
              <>
                <strong>{primaryRisk.risk}</strong> {primaryRisk.impact}
              </>
            )}
          </SummaryCard>
          <SummaryCard title="Key supported finding">
            {approvalFinding?.statement ?? "No supported finding was identified."}
          </SummaryCard>
          <SummaryCard title="Primary evidence gap">
            {primaryGap === undefined ? (
              "No material evidence gap was identified."
            ) : (
              <>
                <strong>{primaryGap.topic}:</strong> {primaryGap.description}
              </>
            )}
          </SummaryCard>
        </div>
      </section>

      <section
        aria-labelledby="recommended-actions-title"
        className="rounded-control border border-info-border bg-info-surface p-4 sm:p-5"
      >
        <h6 id="recommended-actions-title" className="font-semibold text-foreground">
          Recommended actions
        </h6>
        {report.follow_up_recommendations.length === 0 ? (
          <p className="mt-3 text-sm text-muted-foreground">
            No follow-up recommendations were reported.
          </p>
        ) : (
          <ol className="mt-3 grid gap-3">
            {report.follow_up_recommendations.map((item, index) => (
              <li
                key={`${item.recommendation}-${String(index)}`}
                className="rounded-control bg-surface p-4"
              >
                <div className="flex gap-3">
                  <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-brand text-sm font-bold text-brand-contrast">
                    {String(index + 1)}
                  </span>
                  <div>
                    <p className="font-semibold text-foreground">{item.recommendation}</p>
                    <p className="mt-2 text-sm leading-6 text-muted-foreground">{item.rationale}</p>
                    <SupportingSourcesButton
                      report={report}
                      evidenceIds={item.evidence_ids}
                      title="Sources supporting this recommendation"
                      onOpenEvidence={onOpenEvidence}
                    />
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section
        aria-labelledby="detailed-analysis-title"
        className="grid gap-3 border-t border-border pt-6"
      >
        <div>
          <h6 id="detailed-analysis-title" className="font-semibold text-foreground">
            Detailed analysis
          </h6>
          <p className="mt-1 text-sm text-muted-foreground">Open only the sections you need.</p>
        </div>

        <DisclosureSection
          title="Responsibilities"
          summary={`${String(report.responsibilities.length)} supported ownership finding${report.responsibilities.length === 1 ? "" : "s"}`}
        >
          {report.responsibilities.length === 0 ? (
            <EmptyState>No supported responsibility findings were asserted.</EmptyState>
          ) : (
            <ul className="grid gap-3">
              {report.responsibilities.map((item) => (
                <li key={item.claim_id} className="rounded-control border border-border p-4">
                  <p className="font-semibold text-foreground">{item.responsibility}</p>
                  <p className="mt-2 text-sm leading-6">
                    <strong>Owner:</strong> {item.attribution}
                  </p>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">
                    <strong>Uncertainty:</strong> {item.uncertainty}
                  </p>
                  <SupportingSourcesButton
                    report={report}
                    evidenceIds={item.evidence_ids}
                    title="Sources supporting this responsibility"
                    onOpenEvidence={onOpenEvidence}
                  />
                </li>
              ))}
            </ul>
          )}
        </DisclosureSection>

        <DisclosureSection
          title="Risks and approval dependencies"
          summary={`${String(report.operational_risks.length)} risk${report.operational_risks.length === 1 ? "" : "s"} and ${String(report.findings.length)} supported finding${report.findings.length === 1 ? "" : "s"}`}
        >
          <div className="grid gap-5">
            {report.operational_risks.length === 0 ? (
              <EmptyState>No supported operational risks were asserted.</EmptyState>
            ) : (
              <ul className="grid gap-3">
                {report.operational_risks.map((item) => (
                  <li
                    key={item.claim_id}
                    className="rounded-control border border-warning-border bg-warning-surface p-4"
                  >
                    <p className="font-semibold text-warning-foreground">{item.risk}</p>
                    <p className="mt-2 text-sm leading-6">
                      <strong>Impact:</strong> {item.impact}
                    </p>
                    <p className="mt-1 text-sm leading-6">
                      <strong>Responsibility context:</strong> {item.responsibility_context}
                    </p>
                    <p className="mt-1 text-sm leading-6 text-muted-foreground">
                      <strong>Uncertainty:</strong> {item.uncertainty}
                    </p>
                    <SupportingSourcesButton
                      report={report}
                      evidenceIds={item.evidence_ids}
                      title="Sources supporting this risk"
                      onOpenEvidence={onOpenEvidence}
                    />
                  </li>
                ))}
              </ul>
            )}
            <div>
              <h6 className="font-semibold text-foreground">Supported findings</h6>
              {report.findings.length === 0 ? (
                <EmptyState>No supported findings were asserted.</EmptyState>
              ) : (
                <ul className="mt-3 grid gap-3">
                  {report.findings.map((finding) => (
                    <li key={finding.claim_id} className="rounded-control border border-border p-4">
                      <p className="text-sm leading-6 text-foreground">{finding.statement}</p>
                      <SupportingSourcesButton
                        report={report}
                        evidenceIds={finding.evidence_ids}
                        title="Sources supporting this finding"
                        onOpenEvidence={onOpenEvidence}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </DisclosureSection>

        <DisclosureSection
          title="Stakeholder alignment"
          summary="Supported signals and documented disagreements"
        >
          <div className="grid gap-5 md:grid-cols-2">
            <section>
              <h6 className="font-semibold text-foreground">Buy-in signals</h6>
              {report.buy_in_signals.length === 0 ? (
                <EmptyState>No stakeholder buy-in signal was supported.</EmptyState>
              ) : (
                <ul className="mt-3 grid gap-3">
                  {report.buy_in_signals.map((signal, index) => (
                    <li
                      key={`${signal.topic}-${String(index)}`}
                      className="rounded-control border border-border p-4"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <p className="font-semibold text-foreground">{signal.topic}</p>
                        <StatusBadge
                          label={signal.category}
                          tone={
                            signal.category === "confirmed_support"
                              ? "success"
                              : signal.category === "expressed_concern"
                                ? "warning"
                                : "neutral"
                          }
                        />
                      </div>
                      <p className="mt-2 text-sm leading-6">{signal.explanation}</p>
                      <SupportingSourcesButton
                        report={report}
                        evidenceIds={signal.evidence_ids}
                        title="Sources supporting this stakeholder signal"
                        onOpenEvidence={onOpenEvidence}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section>
              <h6 className="font-semibold text-foreground">Contradictions</h6>
              {report.contradictions.length === 0 ? (
                <EmptyState>No two-sided supported contradiction was found.</EmptyState>
              ) : (
                <ul className="mt-3 grid gap-3">
                  {report.contradictions.map((item, index) => (
                    <li
                      key={`${item.topic}-${String(index)}`}
                      className="rounded-control border border-border p-4"
                    >
                      <p className="font-semibold text-foreground">{item.topic}</p>
                      <blockquote className="mt-3 rounded-control bg-surface-subtle p-3 text-sm leading-6">
                        {item.side_a.statement}
                      </blockquote>
                      <blockquote className="mt-2 rounded-control bg-surface-subtle p-3 text-sm leading-6">
                        {item.side_b.statement}
                      </blockquote>
                      <p className="mt-3 text-sm leading-6">{item.interpretation}</p>
                      <SupportingSourcesButton
                        report={report}
                        evidenceIds={item.evidence_ids}
                        title="Sources supporting this comparison"
                        onOpenEvidence={onOpenEvidence}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </DisclosureSection>

        <DisclosureSection
          title="Evidence gaps and open questions"
          summary={`${String(report.evidence_gaps.length)} gap${report.evidence_gaps.length === 1 ? "" : "s"} and ${String(report.open_questions.length)} open question${report.open_questions.length === 1 ? "" : "s"}`}
        >
          <div className="grid gap-5 md:grid-cols-2">
            <section>
              <h6 className="font-semibold text-foreground">Evidence gaps</h6>
              {report.evidence_gaps.length === 0 ? (
                <EmptyState>No material evidence gaps were reported.</EmptyState>
              ) : (
                <ul className="mt-3 grid gap-3">
                  {report.evidence_gaps.map((gap, index) => (
                    <li
                      key={`${gap.topic}-${String(index)}`}
                      className="rounded-control border border-warning-border bg-warning-surface p-4"
                    >
                      <p className="font-semibold text-warning-foreground">{gap.topic}</p>
                      <p className="mt-2 text-sm leading-6">{gap.description}</p>
                      <p className="mt-2 text-sm leading-6">
                        <strong>Impact:</strong> {gap.impact}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section>
              <h6 className="font-semibold text-foreground">Open questions</h6>
              <div className="mt-3">
                {listOrEmpty(report.open_questions, "No open questions were reported.")}
              </div>
            </section>
          </div>
        </DisclosureSection>
      </section>

      <section aria-labelledby="sources-title" className="grid gap-3 border-t border-border pt-6">
        <div>
          <h6 id="sources-title" className="font-semibold text-foreground">
            Sources
          </h6>
          <p className="mt-1 text-sm text-muted-foreground">
            Human-readable source locations used by this analysis.
          </p>
        </div>
        {citationGroups.length === 0 ? (
          <EmptyState>
            No cited sources are available because no supported evidence was asserted.
          </EmptyState>
        ) : (
          <ul className="grid gap-3 md:grid-cols-2">
            {citationGroups.map((group) => {
              const items = group.citations.map((citation) => ({
                evidenceId: citation.evidence_id,
                sourceLabel: citation.display_label,
                locationLabel: describeSourceLocation(citation.source_location),
              }));
              return (
                <li
                  key={`${group.sourceLabel}-${group.locationLabel}`}
                  className="rounded-control border border-border p-4"
                >
                  <p className="font-semibold text-foreground">{group.sourceLabel}</p>
                  <p className="mt-1 text-sm text-muted-foreground">{group.locationLabel}</p>
                  <Button
                    className="mt-3"
                    size="small"
                    variant="secondary"
                    onClick={() => onOpenEvidence({ title: group.sourceLabel, items })}
                  >
                    {items.length === 1
                      ? "View source excerpt"
                      : `View source excerpts (${String(items.length)})`}
                  </Button>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <DisclosureSection
        title="Technical details"
        summary="Execution timeline, research topics, and audit metadata"
      >
        <div className="grid gap-6">
          <section>
            <h6 className="font-semibold text-foreground">Execution timeline</h6>
            <div className="mt-3">
              {events.length === 0 ? (
                <EmptyState>No operational events were published.</EmptyState>
              ) : (
                <ExecutionTimeline events={events} topics={report.researched_topics} />
              )}
            </div>
          </section>
          <section>
            <h6 className="font-semibold text-foreground">Researched topics</h6>
            <ul className="mt-3 grid gap-3">
              {report.researched_topics.map((topic) => (
                <li key={topic.topic_id} className="rounded-control bg-surface-subtle p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <p className="font-semibold text-foreground">{topic.title}</p>
                    <StatusBadge
                      label={topic.status}
                      tone={
                        topic.status === "completed"
                          ? "success"
                          : topic.status === "failed"
                            ? "error"
                            : "warning"
                      }
                    />
                  </div>
                  <p className="mt-2 text-sm leading-6 text-foreground">{topic.summary}</p>
                </li>
              ))}
            </ul>
          </section>
          <section>
            <h6 className="font-semibold text-foreground">Execution metrics</h6>
            <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
              <div>
                <dt className="font-semibold text-muted-foreground">Completed</dt>
                <dd className="mt-1">{formatDateTime(metrics.completed_at)}</dd>
              </div>
              <div>
                <dt className="font-semibold text-muted-foreground">Topics</dt>
                <dd className="mt-1">{metrics.topic_count}</dd>
              </div>
              <div>
                <dt className="font-semibold text-muted-foreground">Researcher calls</dt>
                <dd className="mt-1">{metrics.researcher_calls}</dd>
              </div>
              <div>
                <dt className="font-semibold text-muted-foreground">Tool calls</dt>
                <dd className="mt-1">{metrics.tool_calls}</dd>
              </div>
            </dl>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">
              {report.run_metadata.status_detail}
            </p>
          </section>
        </div>
      </DisclosureSection>
    </article>
  );
}

function ArtifactItem({
  artifact,
  api,
  engagementId,
  runId,
  evidenceId,
}: {
  artifact: SourceArtifactSummary;
  api: PmApi;
  engagementId: string;
  runId: string;
  evidenceId: string;
}) {
  const downloadable = artifact.download_path !== null;
  return (
    <li className="rounded-control border border-border p-3">
      <p className="font-semibold text-foreground">{artifact.artifact_kind.replaceAll("_", " ")}</p>
      <p className="mt-1 text-xs text-muted-foreground">{artifact.media_type}</p>
      {downloadable ? (
        <a
          className="mt-3 inline-flex min-h-9 items-center justify-center rounded-control border border-border-strong bg-surface px-3 py-1.5 text-sm font-semibold text-foreground transition-colors hover:border-brand hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          href={api.artifactDownloadPath(engagementId, runId, evidenceId, artifact.artifact_id)}
          download
        >
          Download source file
        </a>
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">
          This source file is available for review but cannot be downloaded.
        </p>
      )}
    </li>
  );
}

export function EvidenceDrawer({
  selection,
  selectedIndex,
  value,
  loading,
  errorMessage,
  api,
  engagementId,
  runId,
  onSelectIndex,
  onClose,
}: {
  selection: EvidenceSelection;
  selectedIndex: number;
  value: EvidenceDrillDownResponse | null;
  loading: boolean;
  errorMessage: string | null;
  api: PmApi;
  engagementId: string;
  runId: string;
  onSelectIndex: (index: number) => void;
  onClose: () => void;
}) {
  const titleRef = useRef<HTMLHeadingElement>(null);
  const closeRef = useRef(onClose);
  const currentItem = selection.items[selectedIndex];

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    titleRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeRef.current();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  const artifacts = value === null ? [] : [value.original, ...value.related_artifacts];
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex justify-end bg-slate-950/45"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        aria-labelledby="evidence-drawer-title"
        aria-modal="true"
        role="dialog"
        className="h-full w-full max-w-2xl overflow-y-auto border-l border-border bg-surface shadow-2xl"
      >
        <div className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b border-border bg-surface px-5 py-4 sm:px-6">
          <div>
            <p className="text-sm font-semibold tracking-wide text-brand uppercase">
              Source supporting this analysis
            </p>
            <h5
              id="evidence-drawer-title"
              ref={titleRef}
              tabIndex={-1}
              className="mt-1 text-lg font-semibold text-foreground outline-none"
            >
              {selection.title}
            </h5>
          </div>
          <Button size="small" variant="quiet" onClick={onClose}>
            Close
          </Button>
        </div>

        <div className="grid gap-5 p-5 sm:p-6">
          <section className="rounded-control bg-surface-subtle p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="font-semibold text-foreground">{currentItem?.sourceLabel}</p>
                <p className="mt-1 text-sm text-muted-foreground">{currentItem?.locationLabel}</p>
              </div>
              <p className="text-sm font-semibold text-muted-foreground">
                Excerpt {String(selectedIndex + 1)} of {String(selection.items.length)}
              </p>
            </div>
            {selection.items.length > 1 ? (
              <div className="mt-4 flex gap-2">
                <Button
                  size="small"
                  variant="secondary"
                  disabled={selectedIndex === 0 || loading}
                  onClick={() => onSelectIndex(selectedIndex - 1)}
                >
                  Previous excerpt
                </Button>
                <Button
                  size="small"
                  variant="secondary"
                  disabled={selectedIndex === selection.items.length - 1 || loading}
                  onClick={() => onSelectIndex(selectedIndex + 1)}
                >
                  Next excerpt
                </Button>
              </div>
            ) : null}
          </section>

          {loading ? (
            <p role="status" className="text-sm text-muted-foreground">
              Loading source excerpt…
            </p>
          ) : null}
          {errorMessage === null ? null : (
            <ErrorNotice title="Source could not be opened">{errorMessage}</ErrorNotice>
          )}

          {value === null || loading ? null : (
            <>
              <section>
                <h6 className="font-semibold text-foreground">Original excerpt</h6>
                <blockquote className="mt-2 rounded-control border border-info-border bg-info-surface p-4 text-sm leading-7 whitespace-pre-wrap text-foreground">
                  {value.evidence.original_excerpt}
                </blockquote>
              </section>

              {value.evidence.english_interpretation === null ? null : (
                <section>
                  <h6 className="font-semibold text-foreground">English interpretation</h6>
                  <p className="mt-2 text-sm leading-7 text-foreground">
                    {value.evidence.english_interpretation}
                  </p>
                </section>
              )}

              <section className="grid gap-3">
                <h6 className="font-semibold text-foreground">Available source files</h6>
                <ul className="grid gap-3 md:grid-cols-2">
                  {artifacts.map((artifact) => (
                    <ArtifactItem
                      key={artifact.artifact_id}
                      artifact={artifact}
                      api={api}
                      engagementId={engagementId}
                      runId={runId}
                      evidenceId={value.evidence.evidence_id}
                    />
                  ))}
                </ul>
              </section>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
