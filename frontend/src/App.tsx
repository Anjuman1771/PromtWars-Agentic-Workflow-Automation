import {
  CheckCircle2,
  ChevronDown,
  Command,
  GitBranch,
  LayoutDashboard,
  Settings,
  ShieldCheck,
  X,
  XCircle,
  Clock3,
} from "lucide-react";
import type { Edge, Node } from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  API_BASE,
  api,
  type Approval,
  type RunDetail,
  type RunSummary,
  type Workflow,
  type WorkflowRecord,
} from "./api";
import type { TaskNodeData } from "./components/shared";
import { Approvals, type ApprovalSecrets } from "./pages/Approvals";
import { Dashboard } from "./pages/Dashboard";
import { RunHistory } from "./pages/RunHistory";
import { SettingsPage } from "./pages/SettingsPage";
import { WorkflowBuilder } from "./pages/WorkflowBuilder";

type Page =
  | "Dashboard"
  | "Workflow Builder"
  | "Run History"
  | "Approvals"
  | "Settings";

const navigation: Array<{
  label: Page;
  icon: typeof LayoutDashboard;
}> = [
  { label: "Dashboard", icon: LayoutDashboard },
  { label: "Workflow Builder", icon: GitBranch },
  { label: "Run History", icon: Clock3 },
  { label: "Approvals", icon: ShieldCheck },
  { label: "Settings", icon: Settings },
];

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function App() {
  const [page, setPage] = useState<Page>("Dashboard");
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowRecord[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [workflow, setWorkflow] = useState<Workflow>({
    name: "Untitled workflow",
    tasks: [],
  });
  const [workflowId, setWorkflowId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [approvalSecrets, setApprovalSecrets] = useState<ApprovalSecrets>({});
  const [submittingApprovalId, setSubmittingApprovalId] = useState<string | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [runLoading, setRunLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [liveAnnouncement, setLiveAnnouncement] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const previousPage = useRef(page);
  const selectedRunIdRef = useRef("");
  const announcedRunSnapshot = useRef<{
    runId: string;
    status: string;
    taskStatuses: Record<string, string>;
  } | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");

    const [workflowResult, runResult, healthResult] = await Promise.allSettled([
      api.workflows(),
      api.runs(),
      api.health(),
    ]);
    const failures: string[] = [];

    if (healthResult.status === "fulfilled") {
      setApiOnline(healthResult.value.status === "ok");
    } else {
      setApiOnline(false);
      failures.push(errorMessage(healthResult.reason, "Health check failed."));
    }

    if (workflowResult.status === "fulfilled") {
      setWorkflows(workflowResult.value.items);
    } else {
      failures.push(
        errorMessage(workflowResult.reason, "Could not load workflows."),
      );
    }

    if (runResult.status === "fulfilled") {
      setRuns(runResult.value.items);
      const pausedRuns = runResult.value.items.filter(
        (run) => run.status === "PAUSED_APPROVAL",
      );
      const approvalResults = await Promise.allSettled(
        pausedRuns.map((run) => api.approvals(run.id)),
      );
      const loadedApprovals: Approval[] = [];
      approvalResults.forEach((result) => {
        if (result.status === "fulfilled") {
          loadedApprovals.push(...result.value.items);
        } else {
          failures.push(
            errorMessage(result.reason, "Could not load pending approvals."),
          );
        }
      });
      setApprovals(loadedApprovals);
    } else {
      failures.push(errorMessage(runResult.reason, "Could not load runs."));
    }

    if (failures.length > 0) setError(failures[0]);
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (previousPage.current !== page) {
      previousPage.current = page;
      headingRef.current?.focus();
    }
  }, [page]);

  const selectedTask =
    workflow.tasks.find((task) => task.id === selectedTaskId) ??
    workflow.tasks[0];
  const completedRuns = runs.filter(
    (run) => run.status === "COMPLETED",
  ).length;
  const failedRuns = runs.filter((run) =>
    ["FAILED", "TIMED_OUT"].includes(run.status),
  ).length;

  const reloadRun = useCallback(async (runId: string): Promise<string | null> => {
    const [detailResult, runsResult, approvalsResult] =
      await Promise.allSettled([
        api.run(runId),
        api.runs(),
        api.approvals(runId),
      ]);
    if (selectedRunIdRef.current !== runId) return null;

    const failures: string[] = [];
    let status: string | null = null;
    if (detailResult.status === "fulfilled") {
      const detail = detailResult.value;
      setRunDetail(detail);
      status = detail.run.status;
      const taskStatuses = Object.fromEntries(
        detail.tasks.map((task) => [task.task_id, task.status]),
      );
      const previous = announcedRunSnapshot.current;
      const announcements: string[] = [];
      if (!previous || previous.runId !== runId || previous.status !== status) {
        announcements.push(
          `Run status is ${status.toLowerCase().replaceAll("_", " ")}.`,
        );
      }
      if (previous?.runId === runId) {
        for (const task of detail.tasks) {
          if (previous.taskStatuses[task.task_id] !== task.status) {
            announcements.push(
              `${task.name} is ${task.status.toLowerCase().replaceAll("_", " ")}.`,
            );
          }
        }
      }
      if (announcements.length > 0) {
        setLiveAnnouncement(announcements.join(" "));
      }
      announcedRunSnapshot.current = { runId, status, taskStatuses };
    } else {
      failures.push(errorMessage(detailResult.reason, "Unable to load run."));
    }
    if (runsResult.status === "fulfilled") {
      setRuns(runsResult.value.items);
    } else {
      failures.push(errorMessage(runsResult.reason, "Could not refresh runs."));
    }
    if (approvalsResult.status === "fulfilled") {
      setApprovals((current) => [
        ...current.filter((approval) => approval.run_id !== runId),
        ...approvalsResult.value.items,
      ]);
    } else {
      failures.push(
        errorMessage(approvalsResult.reason, "Could not refresh approvals."),
      );
    }
    setError(failures[0] ?? "");
    return status;
  }, []);

  const openRun = useCallback(
    async (runId: string) => {
      selectedRunIdRef.current = runId;
      announcedRunSnapshot.current = null;
      setSelectedRunId(runId);
      setPage("Run History");
      setRunDetail(null);
      setRunLoading(true);
      setError("");
      try {
        await reloadRun(runId);
      } finally {
        if (selectedRunIdRef.current === runId) setRunLoading(false);
      }
    },
    [reloadRun],
  );

  useEffect(() => {
    if (!selectedRunId) return;
    const source = new EventSource(api.eventsUrl(selectedRunId));
    const update = () => {
      void reloadRun(selectedRunId)
        .then((status) => {
          if (
            status &&
            ["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"].includes(status)
          ) {
            source.close();
          }
        })
        .catch((reason: unknown) =>
          setError(errorMessage(reason, "Live run updates failed.")),
        );
    };
    source.addEventListener("status", update);
    source.addEventListener("approval_required", () => {
      setLiveAnnouncement("A task is waiting for approval.");
      update();
    });
    source.onerror = () => {
      setLiveAnnouncement(
        "Live updates disconnected. The browser will retry automatically.",
      );
    };
    return () => source.close();
  }, [reloadRun, selectedRunId]);

  const generate = async () => {
    if (!prompt.trim()) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.generate(prompt.trim());
      setWorkflow(result.workflow);
      setWorkflowId("");
      setSelectedTaskId(result.workflow.tasks[0]?.id ?? "");
      setNotice(
        `Workflow generated with ${result.provider}. Review it, then save before running.`,
      );
    } catch (reason) {
      setError(errorMessage(reason, "Workflow generation failed."));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await api.saveWorkflow(workflow);
      setWorkflowId(saved.id);
      setNotice("Workflow saved. It has not been run.");
      await refresh();
    } catch (reason) {
      setError(errorMessage(reason, "Could not save workflow."));
    } finally {
      setBusy(false);
    }
  };

  const runWorkflow = async () => {
    if (!workflowId) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.runWorkflow(workflowId);
      selectedRunIdRef.current = result.run_id;
      setSelectedRunId(result.run_id);
      setPage("Run History");
      await openRun(result.run_id);
      await refresh();
    } catch (reason) {
      setError(errorMessage(reason, "Workflow execution failed."));
    } finally {
      setBusy(false);
    }
  };

  const cancelRun = async () => {
    if (!selectedRunId) return;
    setError("");
    try {
      await api.cancelRun(selectedRunId);
      await refresh();
      await openRun(selectedRunId);
    } catch (reason) {
      setError(errorMessage(reason, "Could not cancel run."));
    }
  };

  const decideApproval = async (
    approval: Approval,
    decision: "approve" | "reject",
  ) => {
    const secret = approvalSecrets[approval.approval_id];
    if (!secret) return;

    setSubmittingApprovalId(approval.approval_id);
    setError("");
    setNotice("");
    try {
      let payload: unknown;
      try {
        payload = JSON.parse(secret.payload);
      } catch {
        throw new Error("Action payload must be valid JSON.");
      }
      if (!isJsonObject(payload)) {
        throw new Error("Action payload must be a JSON object.");
      }

      await api.decideApproval(
        approval.approval_id,
        decision,
        secret.token,
        payload,
      );
      setNotice(
        decision === "approve"
          ? "Approval submitted. The engine is validating the decision."
          : "Rejection submitted. The engine is validating the decision.",
      );
      setApprovalSecrets((current) => {
        const next = { ...current };
        delete next[approval.approval_id];
        return next;
      });
      await refresh();
    } catch (reason) {
      setError(errorMessage(reason, "Approval decision failed."));
    } finally {
      setSubmittingApprovalId(null);
    }
  };

  const showBuilder = () => {
    setPage("Workflow Builder");
    setError("");
    setNotice("");
  };

  const nodes = useMemo<Node<TaskNodeData>[]>(
    () =>
      workflow.tasks.map((task, index) => ({
        id: task.id,
        type: "task",
        position: {
          x: 90 + (index % 2) * 280,
          y: 70 + Math.floor(index / 2) * 150,
        },
        data: {
          task,
          index,
          status: runDetail?.tasks.find((item) => item.task_id === task.id)
            ?.status,
        },
      })),
    [workflow.tasks, runDetail],
  );
  const edges = useMemo<Edge[]>(
    () =>
      workflow.tasks.flatMap((task) =>
        (task.dependencies ?? []).map((dependency) => {
          const source =
            typeof dependency === "string" ? dependency : dependency.task_id;
          return {
            id: `${source}-${task.id}`,
            source,
            target: task.id,
            animated: true,
            style: { stroke: "#7568f5", strokeWidth: 1.5 },
          };
        }),
      ),
    [workflow.tasks],
  );

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <aside className="sidebar">
        <div className="brand-lockup">
          <div aria-hidden="true" className="brand-mark">
            <Command size={19} />
          </div>
          <div>
            <div className="brand-name">flowpilot</div>
            <div className="brand-caption">AI WORKFLOW OS</div>
          </div>
        </div>
        <div className="nav-label">WORKSPACE</div>
        <nav className="nav-list" aria-label="Main navigation">
          {navigation.map(({ label, icon: Icon }) => (
            <button
              key={label}
              aria-label={label}
              aria-current={page === label ? "page" : undefined}
              className={`nav-item ${page === label ? "nav-item-active" : ""}`}
              onClick={() => {
                setPage(label);
                setError("");
                setNotice("");
                if (label !== "Approvals") setApprovalSecrets({});
              }}
            >
              <Icon aria-hidden="true" size={17} />
              <span>{label}</span>
              {label === "Approvals" && approvals.length > 0 && (
                <span className="nav-count" aria-label={`${approvals.length} pending`}>
                  {approvals.length}
                </span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="workspace-card">
            <div aria-hidden="true" className="workspace-avatar">FP</div>
            <div className="min-w-0">
              <div className="truncate text-sm font-medium text-slate-200">
                Personal workspace
              </div>
              <div className="mt-1 text-xs text-slate-500">
                Local environment
              </div>
            </div>
            <ChevronDown
              aria-hidden="true"
              size={15}
              className="ml-auto text-slate-500"
            />
          </div>
          <div className="sidebar-footnote">
            <ShieldCheck aria-hidden="true" size={14} /> Secure execution enabled
          </div>
        </div>
      </aside>
      <main className="main-shell">
        <header className="topbar">
          <div className="breadcrumbs">
            <span>Workspace</span>
            <span aria-hidden="true" className="crumb-slash">/</span>
            <strong>{page}</strong>
          </div>
          <div className="topbar-actions">
            <span
              className={`connection-pill ${apiOnline ? "connected" : apiOnline === false ? "disconnected" : ""}`}
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              <span aria-hidden="true" className="status-dot" />
              {apiOnline ? "API connected" : apiOnline === false ? "API offline" : "Connecting"}
            </span>
            <div aria-hidden="true" className="avatar">JD</div>
          </div>
        </header>
        <section
          id="main-content"
          className="page-content"
          tabIndex={-1}
          aria-labelledby="page-title"
        >
          {error && (
            <div className="alert alert-error" role="alert">
              <XCircle aria-hidden="true" size={17} />
              <span>{error}</span>
              <button
                aria-label="Dismiss error"
                onClick={() => setError("")}
              >
                <X aria-hidden="true" size={15} />
              </button>
            </div>
          )}
          {notice && (
            <div className="alert alert-success" role="status" aria-live="polite">
              <CheckCircle2 aria-hidden="true" size={17} />
              <span>{notice}</span>
              <button
                aria-label="Dismiss notice"
                onClick={() => setNotice("")}
              >
                <X aria-hidden="true" size={15} />
              </button>
            </div>
          )}
          {page === "Dashboard" && (
            <Dashboard
              workflows={workflows}
              runs={runs}
              completed={completedRuns}
              failed={failedRuns}
              loading={loading}
              headingRef={headingRef}
              onCreate={showBuilder}
              onOpenRun={openRun}
              onOpenBuilder={showBuilder}
            />
          )}
          {page === "Workflow Builder" && (
            <WorkflowBuilder
              workflow={workflow}
              workflowId={workflowId}
              prompt={prompt}
              busy={busy}
              nodes={nodes}
              edges={edges}
              selectedTask={selectedTask}
              headingRef={headingRef}
              onPrompt={setPrompt}
              onGenerate={generate}
              onSave={save}
              onRun={runWorkflow}
              onSelectTask={setSelectedTaskId}
              onWorkflowName={(name) => {
                setWorkflow((current) => ({ ...current, name }));
                setWorkflowId("");
              }}
            />
          )}
          {page === "Run History" && (
            <RunHistory
              runs={runs}
              workflows={workflows}
              detail={runDetail}
              selected={selectedRunId}
              loading={loading || runLoading}
              headingRef={headingRef}
              onOpen={openRun}
              onRefresh={() => void refresh()}
              onCancel={() => void cancelRun()}
            />
          )}
          {page === "Approvals" && (
            <Approvals
              approvals={approvals}
              runs={runs}
              secrets={approvalSecrets}
              submittingApprovalId={submittingApprovalId}
              headingRef={headingRef}
              onSecrets={setApprovalSecrets}
              onRefresh={() => void refresh()}
              onDecision={(approval, decision) =>
                void decideApproval(approval, decision)
              }
            />
          )}
          {page === "Settings" && (
            <SettingsPage
              apiBase={API_BASE}
              apiOnline={apiOnline}
              headingRef={headingRef}
              onRefresh={() => void refresh()}
            />
          )}
        </section>
        <div className="sr-only" aria-live="polite" aria-atomic="true">
          {liveAnnouncement}
        </div>
      </main>
    </div>
  );
}

export { App };
