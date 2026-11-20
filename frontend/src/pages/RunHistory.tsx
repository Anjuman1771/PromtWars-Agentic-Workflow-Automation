import {
  Activity,
  Clock3,
  LoaderCircle,
  RefreshCw,
  Workflow as WorkflowIcon,
  X,
  XCircle,
  CircleHelp,
} from "lucide-react";
import type { RunDetail, RunSummary, WorkflowRecord } from "../api";
import {
  EmptyState,
  formatDate,
  getWorkflowTitle,
  PageTitle,
  statusClass,
  StatusBadge,
} from "../components/shared";

export function RunHistory({
  runs,
  workflows,
  detail,
  selected,
  loading,
  headingRef,
  onOpen,
  onRefresh,
  onCancel,
}: {
  runs: RunSummary[];
  workflows: WorkflowRecord[];
  detail: RunDetail | null;
  selected: string;
  loading: boolean;
  headingRef: React.Ref<HTMLHeadingElement>;
  onOpen: (id: string) => void;
  onRefresh: () => void;
  onCancel: () => void;
}) {
  const completed = detail?.tasks.filter((task) => task.status === "COMPLETED").length ?? 0;
  const progress = detail?.tasks.length
    ? Math.round((completed / detail.tasks.length) * 100)
    : 0;

  return (
    <div className="page-stack">
      <PageTitle
        eyebrow="OBSERVABILITY"
        title="Run history"
        description="Monitor workflow executions and inspect task-level outcomes."
        headingRef={headingRef}
        action={
          <button
            className="button-secondary"
            onClick={onCancel}
            disabled={
              !selected ||
              !["RUNNING", "PAUSED_APPROVAL"].includes(detail?.run.status ?? "")
            }
          >
            <X aria-hidden="true" size={15} /> Cancel run
          </button>
        }
      />
      <div className="history-layout">
        <section className="panel history-panel" aria-labelledby="executions-heading">
          <div className="panel-heading">
            <div>
              <h2 id="executions-heading">Executions</h2>
              <span className="panel-subtitle">{runs.length} total runs</span>
            </div>
            <button
              className="icon-button"
              aria-label="Refresh runs and workflows"
              onClick={onRefresh}
            >
              <RefreshCw aria-hidden="true" size={15} />
            </button>
          </div>
          {loading && (
            <div className="loading-line" role="status" aria-live="polite">
              <LoaderCircle aria-hidden="true" className="spin" size={16} />
              Loading runs
            </div>
          )}
          {runs.length === 0 ? (
            <EmptyState
              icon={Clock3}
              title="No executions yet"
              text="Run a saved workflow to see its history here."
            />
          ) : (
            <div className="run-list" role="list" aria-label="Workflow runs">
              {runs.map((run) => (
                <div role="listitem" key={run.id}>
                  <button
                    aria-current={selected === run.id ? "true" : undefined}
                    className={`run-list-item ${selected === run.id ? "run-list-selected" : ""}`}
                    onClick={() => onOpen(run.id)}
                  >
                    <div aria-hidden="true" className="run-list-icon">
                      <Activity size={15} />
                    </div>
                    <div className="run-list-copy">
                      <strong>{getWorkflowTitle(workflows, run.workflow_id)}</strong>
                      <span>{run.id}</span>
                      <small>{formatDate(run.started_at)}</small>
                    </div>
                    <StatusBadge status={run.status} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>
        <section className="panel run-detail-panel" aria-label="Selected run details">
          {detail ? (
            <>
              <div className="run-detail-head">
                <div>
                  <div className="eyebrow">RUN DETAIL</div>
                  <h2>{getWorkflowTitle(workflows, detail.run.workflow_id)}</h2>
                  <code>{detail.run.id}</code>
                </div>
                <StatusBadge status={detail.run.status} />
              </div>
              <div className="progress-summary">
                <div className="progress-label">
                  <span>Progress</span>
                  <strong>{progress}%</strong>
                </div>
                <div
                  className="progress-track"
                  role="progressbar"
                  aria-label="Workflow task completion"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={progress}
                >
                  <div style={{ width: `${progress}%` }} />
                </div>
                <div className="progress-caption">
                  {completed} of {detail.tasks.length} tasks completed
                </div>
              </div>
              <section className="task-results" aria-label="Task results">
                {detail.tasks.map((task) => (
                  <article className="task-result" key={task.task_id}>
                    <div
                      aria-hidden="true"
                      className={`task-result-icon ${statusClass(task.status)}`}
                    >
                      <WorkflowIcon size={15} />
                    </div>
                    <div className="task-result-main">
                      <div className="task-result-top">
                        <strong>{task.name}</strong>
                        <StatusBadge status={task.status} />
                      </div>
                      <span>
                        {task.tool_name} · {task.task_id}
                      </span>
                      {task.error && (
                        <div className="task-error" role="status">
                          <XCircle aria-hidden="true" size={13} />
                          {task.error}
                        </div>
                      )}
                      {task.reconciliation_required && (
                        <div className="task-error" role="alert">
                          <CircleHelp aria-hidden="true" size={13} />
                          Manual reconciliation required
                        </div>
                      )}
                      {task.output !== null && task.output !== undefined && (
                        <pre className="output-preview" tabIndex={0} aria-label={`Output for ${task.name}`}>
                          {JSON.stringify(task.output, null, 2)}
                        </pre>
                      )}
                    </div>
                  </article>
                ))}
              </section>
              <div className="run-timestamps">
                <span>Started {formatDate(detail.run.started_at)}</span>
                <span>
                  {detail.run.completed_at
                    ? `Finished ${formatDate(detail.run.completed_at)}`
                    : "Live updates enabled"}
                </span>
              </div>
            </>
          ) : (
            <EmptyState
              icon={Activity}
              title="Select a run"
              text="Choose an execution from the list to inspect its status and task outputs."
            />
          )}
        </section>
      </div>
    </div>
  );
}
