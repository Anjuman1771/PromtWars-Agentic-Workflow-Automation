import {
  Activity,
  ArrowRight,
  Bot,
  Check,
  CheckCircle2,
  Plus,
  Sparkles,
  Workflow as WorkflowIcon,
  XCircle,
} from "lucide-react";
import type { RunSummary, WorkflowRecord } from "../api";
import {
  EmptyState,
  formatDate,
  getWorkflowTitle,
  PageTitle,
  StatCard,
  StatusBadge,
} from "../components/shared";

export function Dashboard({
  workflows,
  runs,
  completed,
  failed,
  loading,
  headingRef,
  onCreate,
  onOpenRun,
  onOpenBuilder,
}: {
  workflows: WorkflowRecord[];
  runs: RunSummary[];
  completed: number;
  failed: number;
  loading: boolean;
  headingRef: React.Ref<HTMLHeadingElement>;
  onCreate: () => void;
  onOpenRun: (id: string) => void;
  onOpenBuilder: () => void;
}) {
  return (
    <div className="page-stack">
      <PageTitle
        eyebrow="OVERVIEW"
        title="Good to see you"
        description="Your workflow operations at a glance."
        headingRef={headingRef}
        action={
          <button className="button-primary" onClick={onCreate}>
            <Plus aria-hidden="true" size={16} /> New workflow
          </button>
        }
      />
      <div className="hero-card">
        <div aria-hidden="true" className="hero-glow" />
        <div className="hero-copy">
          <div className="hero-kicker">
            <Sparkles aria-hidden="true" size={14} /> WORKFLOW AUTOMATION
          </div>
          <h2>
            Make your work
            <br />
            flow <span>intelligently.</span>
          </h2>
          <p>
            Design, orchestrate, and monitor AI-powered workflows — all in one
            place.
          </p>
          <button className="button-light" onClick={onOpenBuilder}>
            Open workflow builder <ArrowRight aria-hidden="true" size={15} />
          </button>
        </div>
        <div aria-hidden="true" className="hero-art">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="orbit-center">
            <Bot size={34} />
          </div>
          <div className="orbit-node orbit-node-a">
            <WorkflowIcon size={15} />
          </div>
          <div className="orbit-node orbit-node-b">
            <Check size={15} />
          </div>
          <div className="orbit-node orbit-node-c">
            <Sparkles size={15} />
          </div>
        </div>
        <div className="hero-bottom">
          <span>
            <span aria-hidden="true" className="live-dot" /> READY TO ORCHESTRATE
          </span>
          <span>POWERED BY FLOWPILOT ENGINE</span>
        </div>
      </div>
      <div className="stats-grid" role="group" aria-label="Workflow metrics">
        <StatCard
          label="Total workflows"
          value={loading ? "—" : workflows.length}
          icon={WorkflowIcon}
          tone="violet"
          helper="Saved in workspace"
        />
        <StatCard
          label="Total runs"
          value={loading ? "—" : runs.length}
          icon={Activity}
          tone="blue"
          helper="Execution history"
        />
        <StatCard
          label="Successful runs"
          value={loading ? "—" : completed}
          icon={CheckCircle2}
          tone="green"
          helper="Completed successfully"
        />
        <StatCard
          label="Failed runs"
          value={loading ? "—" : failed}
          icon={XCircle}
          tone="rose"
          helper="Needs attention"
        />
      </div>
      <div className="section-heading">
        <div>
          <h2>Recent activity</h2>
          <p>Your latest workflow executions</p>
        </div>
        <button className="button-ghost" onClick={onOpenBuilder}>
          Build a workflow <ArrowRight aria-hidden="true" size={14} />
        </button>
      </div>
      <div className="table-card">
        {runs.length === 0 ? (
          <EmptyState
            icon={Activity}
            title="No runs yet"
            text="Your workflow activity will appear here once you run an automation."
            action={
              <button className="button-secondary" onClick={onCreate}>
                <Plus aria-hidden="true" size={15} /> Create a workflow
              </button>
            }
          />
        ) : (
          <div className="activity-list">
            {runs.slice(0, 5).map((run) => (
              <button
                className="activity-row"
                key={run.id}
                onClick={() => onOpenRun(run.id)}
              >
                <div aria-hidden="true" className="activity-icon">
                  <WorkflowIcon size={16} />
                </div>
                <div className="activity-main">
                  <div className="activity-name">
                    {getWorkflowTitle(workflows, run.workflow_id)}
                  </div>
                  <div className="activity-meta">
                    {run.id} · {formatDate(run.started_at)}
                  </div>
                </div>
                <StatusBadge status={run.status} />
                <ArrowRight aria-hidden="true" size={15} className="text-slate-600" />
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
