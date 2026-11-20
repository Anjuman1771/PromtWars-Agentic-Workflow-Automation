import {
  Activity,
  ArrowRight,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleHelp,
  Clock3,
  Command,
  FileText,
  GitBranch,
  LayoutDashboard,
  LoaderCircle,
  LockKeyhole,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings,
  ShieldCheck,
  Sparkles,
  Workflow as WorkflowIcon,
  X,
  XCircle,
} from "lucide-react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE, api, type Approval, type RunDetail, type RunSummary, type Task, type Workflow } from "./api";

type Page = "Dashboard" | "Workflow Builder" | "Run History" | "Approvals" | "Settings";
type TaskNodeData = { task: Task; index: number; status?: string };
const navItems: Array<{ label: Page; icon: typeof LayoutDashboard }> = [
  { label: "Dashboard", icon: LayoutDashboard },
  { label: "Workflow Builder", icon: GitBranch },
  { label: "Run History", icon: Clock3 },
  { label: "Approvals", icon: ShieldCheck },
  { label: "Settings", icon: Settings },
];

function TaskNode({ data, selected }: NodeProps<Node<TaskNodeData>>) {
  const status = data.status ?? (data.task.requires_approval ? "approval" : "pending");
  return (
    <div className={`task-node ${selected ? "task-node-selected" : ""} state-${status}`}>
      <Handle type="target" position={Position.Top} />
      <div className="task-node-icon"><WorkflowIcon size={16} /></div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold text-slate-100">{data.task.name}</div>
        <div className="mt-1 truncate text-xs text-slate-400">{data.task.tool}</div>
      </div>
      <span className="task-index">{String(data.index + 1).padStart(2, "0")}</span>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { task: TaskNode };

function statusClass(status?: string) {
  const key = status?.toLowerCase() ?? "pending";
  if (["completed", "success"].includes(key)) return "badge-success";
  if (["failed", "cancelled", "timed_out"].includes(key)) return "badge-danger";
  if (["running", "waiting_approval", "paused_approval"].includes(key)) return "badge-active";
  return "badge-muted";
}

function StatusBadge({ status }: { status?: string }) {
  return <span className={`status-badge ${statusClass(status)}`}><span className="status-dot" />{status?.replaceAll("_", " ") ?? "unknown"}</span>;
}

function App() {
  const [page, setPage] = useState<Page>("Dashboard");
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [workflows, setWorkflows] = useState<Array<Record<string, unknown>>>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [workflow, setWorkflow] = useState<Workflow>({ name: "Untitled workflow", tasks: [] });
  const [workflowId, setWorkflowId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [approvalSecrets, setApprovalSecrets] = useState<Record<string, { token: string; payload: string }>>({});
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [workflowResponse, runResponse, health] = await Promise.all([
        api.workflows(),
        api.runs(),
        api.health(),
      ]);
      setWorkflows(workflowResponse.items);
      setRuns(runResponse.items);
      const approvalResponses = await Promise.all(
        runResponse.items
          .filter((run) => run.status === "PAUSED_APPROVAL")
          .map((run) => api.approvals(run.id)),
      );
      setApprovals(approvalResponses.flatMap((response) => response.items));
      setApiOnline(health.status === "ok");
    } catch (reason) {
      setApiOnline(false);
      setError(reason instanceof Error ? reason.message : "Could not connect to FlowPilot API.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const selectedTask = workflow.tasks.find((task) => task.id === selectedTaskId) ?? workflow.tasks[0];
  const completedRuns = runs.filter((run) => run.status === "COMPLETED").length;
  const failedRuns = runs.filter((run) => ["FAILED", "TIMED_OUT"].includes(run.status)).length;

  const openRun = useCallback(async (runId: string) => {
    setSelectedRunId(runId);
    setPage("Run History");
    try {
      const detail = await api.run(runId);
      setRunDetail(detail);
      const pending = await api.approvals(runId);
      setApprovals((current) => [
        ...current.filter((approval) => approval.run_id !== runId),
        ...pending.items,
      ]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load run.");
    }
  }, []);

  useEffect(() => {
    if (!selectedRunId) return;
    const source = new EventSource(api.eventsUrl(selectedRunId));
    const update = () => {
      void Promise.all([api.run(selectedRunId), api.runs(), api.approvals(selectedRunId)])
        .then(([detail, runResponse, approvalResponse]) => {
          setRunDetail(detail);
          setRuns(runResponse.items);
          setApprovals((current) => [
            ...current.filter((approval) => approval.run_id !== selectedRunId),
            ...approvalResponse.items,
          ]);
          if (["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"].includes(detail.run.status)) {
            source.close();
          }
        })
        .catch(() => setError("Live run updates disconnected. Refresh to retry."));
    };
    source.addEventListener("status", update);
    source.addEventListener("approval_required", update);
    source.onerror = () => {
      setError("Live updates disconnected; reconnecting automatically with the latest persisted state.");
    };
    return () => source.close();
  }, [selectedRunId]);

  const generate = async () => {
    if (!prompt.trim()) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api.generate(prompt.trim());
      setWorkflow(result.workflow);
      setWorkflowId("");
      setSelectedTaskId(result.workflow.tasks[0]?.id ?? "");
      setNotice(`Workflow generated with ${result.provider}. Review it, then save before running.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Workflow generation failed.");
    } finally { setBusy(false); }
  };

  const save = async () => {
    setBusy(true); setError(""); setNotice("");
    try {
      const saved = await api.saveWorkflow(workflow);
      const id = String(saved.id);
      setWorkflowId(id);
      setNotice("Workflow saved. It has not been run.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save workflow.");
    } finally { setBusy(false); }
  };

  const runWorkflow = async () => {
    if (!workflowId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api.runWorkflow(workflowId);
      setSelectedRunId(String(result.run_id));
      setRunDetail(await api.run(String(result.run_id)));
      const approvalsResponse = await api.approvals(String(result.run_id));
      setApprovals(approvalsResponse.items);
      setPage("Run History");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Workflow execution failed.");
    } finally { setBusy(false); }
  };

  const showBuilder = () => { setPage("Workflow Builder"); setError(""); setNotice(""); };

  const nodes = useMemo<Node<TaskNodeData>[]>(() => workflow.tasks.map((task, index) => {
    const state = runDetail?.tasks.find((item) => item.task_id === task.id)?.status;
    return {
      id: task.id,
      type: "task",
      position: { x: 90 + (index % 2) * 280, y: 70 + Math.floor(index / 2) * 150 },
      data: { task, index, status: state },
    };
  }), [workflow.tasks, runDetail]);
  const edges = useMemo<Edge[]>(() => workflow.tasks.flatMap((task) =>
    (task.dependencies ?? []).map((dependency) => ({
      id: `${typeof dependency === "string" ? dependency : dependency.task_id}-${task.id}`,
      source: typeof dependency === "string" ? dependency : dependency.task_id,
      target: task.id,
      animated: true,
      style: { stroke: "#7568f5", strokeWidth: 1.5 },
    })),
  ), [workflow.tasks]);

  const sidebar = (
    <aside className="sidebar">
      <div className="brand-lockup">
        <div className="brand-mark"><Command size={19} /></div>
        <div><div className="brand-name">flowpilot</div><div className="brand-caption">AI WORKFLOW OS</div></div>
      </div>
      <div className="nav-label">WORKSPACE</div>
      <nav className="nav-list" aria-label="Main navigation">
        {navItems.map(({ label, icon: Icon }) => (
          <button key={label} aria-label={label} title={label} className={`nav-item ${page === label ? "nav-item-active" : ""}`} onClick={() => { setPage(label); setError(""); }}>
            <Icon size={17} /><span>{label}</span>
            {label === "Approvals" && approvals.length > 0 && <span className="nav-count">{approvals.length}</span>}
          </button>
        ))}
      </nav>
      <div className="sidebar-bottom">
        <div className="workspace-card">
          <div className="workspace-avatar">FP</div>
          <div className="min-w-0"><div className="truncate text-sm font-medium text-slate-200">Personal workspace</div><div className="mt-1 text-xs text-slate-500">Local environment</div></div>
          <ChevronDown size={15} className="ml-auto text-slate-500" />
        </div>
        <div className="sidebar-footnote"><ShieldCheck size={14} /> Secure execution enabled</div>
      </div>
    </aside>
  );

  return (
    <div className="app-shell">
      {sidebar}
      <main className="main-shell">
        <header className="topbar">
          <div className="breadcrumbs"><span>Workspace</span><span className="crumb-slash">/</span><strong>{page}</strong></div>
          <div className="topbar-actions">
            <span className={`connection-pill ${apiOnline ? "connected" : apiOnline === false ? "disconnected" : ""}`}><span className="status-dot" />{apiOnline ? "API connected" : apiOnline === false ? "API offline" : "Connecting"}</span>
            <div className="avatar">JD</div>
          </div>
        </header>
        <section className="page-content">
          {error && <div className="alert alert-error" role="alert"><XCircle size={17} /><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError("")}><X size={15} /></button></div>}
          {notice && <div className="alert alert-success" role="status"><CheckCircle2 size={17} /><span>{notice}</span><button aria-label="Dismiss notice" onClick={() => setNotice("")}><X size={15} /></button></div>}
          {page === "Dashboard" && <Dashboard workflows={workflows} runs={runs} completed={completedRuns} failed={failedRuns} loading={loading} onCreate={showBuilder} onOpenRun={openRun} onOpenBuilder={showBuilder} />}
          {page === "Workflow Builder" && (
            <Builder
              workflow={workflow} workflowId={workflowId} prompt={prompt} busy={busy} nodes={nodes} edges={edges}
              selectedTask={selectedTask} onPrompt={setPrompt} onGenerate={generate} onSave={save} onRun={runWorkflow}
              onSelectTask={setSelectedTaskId} onWorkflowName={(name) => setWorkflow((current) => ({ ...current, name }))}
            />
          )}
          {page === "Run History" && <RunHistory runs={runs} workflows={workflows} detail={runDetail} selected={selectedRunId} loading={loading} onOpen={openRun} onCancel={async () => { if (!selectedRunId) return; try { await api.cancelRun(selectedRunId); await refresh(); await openRun(selectedRunId); } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not cancel run."); } }} />}
          {page === "Approvals" && <Approvals approvals={approvals} runs={runs} secrets={approvalSecrets} onSecrets={setApprovalSecrets} onRefresh={refresh} onDecision={async (approval, decision) => { const secret = approvalSecrets[approval.approval_id]; if (!secret) return; try { await api.decideApproval(approval.approval_id, decision, secret.token, JSON.parse(secret.payload)); setNotice(decision === "approve" ? "Approval submitted. The engine is validating the decision." : "Rejection submitted. The engine is validating the decision."); setApprovalSecrets((current) => { const next = { ...current }; delete next[approval.approval_id]; return next; }); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Approval decision failed."); } }} />}
          {page === "Settings" && <SettingsPage apiBase={API_BASE} apiOnline={apiOnline} onRefresh={refresh} />}
        </section>
      </main>
    </div>
  );
}

function PageTitle({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
  return <div className="page-title-row"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{action}</div>;
}

function Dashboard({ workflows, runs, completed, failed, loading, onCreate, onOpenRun, onOpenBuilder }: {
  workflows: Array<Record<string, unknown>>; runs: RunSummary[]; completed: number; failed: number; loading: boolean;
  onCreate: () => void; onOpenRun: (id: string) => void; onOpenBuilder: () => void;
}) {
  return <div className="page-stack">
    <PageTitle eyebrow="OVERVIEW" title="Good to see you" description="Your workflow operations at a glance." action={<button className="button-primary" onClick={onCreate}><Plus size={16} /> New workflow</button>} />
    <div className="hero-card">
      <div className="hero-glow" /><div className="hero-copy"><div className="hero-kicker"><Sparkles size={14} /> WORKFLOW AUTOMATION</div><h2>Make your work<br />flow <span>intelligently.</span></h2><p>Design, orchestrate, and monitor AI-powered workflows — all in one place.</p><button className="button-light" onClick={onOpenBuilder}>Open workflow builder <ArrowRight size={15} /></button></div>
      <div className="hero-art" aria-hidden="true"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="orbit-center"><Bot size={34} /></div><div className="orbit-node orbit-node-a"><WorkflowIcon size={15} /></div><div className="orbit-node orbit-node-b"><Check size={15} /></div><div className="orbit-node orbit-node-c"><Sparkles size={15} /></div></div>
      <div className="hero-bottom"><span><span className="live-dot" /> READY TO ORCHESTRATE</span><span>POWERED BY FLOWPILOT ENGINE</span></div>
    </div>
    <div className="stats-grid">
      <StatCard label="Total workflows" value={loading ? "—" : workflows.length} icon={WorkflowIcon} tone="violet" helper="Saved in workspace" />
      <StatCard label="Total runs" value={loading ? "—" : runs.length} icon={Activity} tone="blue" helper="Execution history" />
      <StatCard label="Successful runs" value={loading ? "—" : completed} icon={CheckCircle2} tone="green" helper="Completed successfully" />
      <StatCard label="Failed runs" value={loading ? "—" : failed} icon={XCircle} tone="rose" helper="Needs attention" />
    </div>
    <div className="section-heading"><div><h2>Recent activity</h2><p>Your latest workflow executions</p></div><button className="button-ghost" onClick={onOpenBuilder}>Build a workflow <ArrowRight size={14} /></button></div>
    <div className="table-card">
      {runs.length === 0 ? <EmptyState icon={Activity} title="No runs yet" text="Your workflow activity will appear here once you run an automation." action={<button className="button-secondary" onClick={onCreate}><Plus size={15} /> Create a workflow</button>} /> :
        <div className="activity-list">{runs.slice(0, 5).map((run) => <button className="activity-row" key={run.id} onClick={() => onOpenRun(run.id)}><div className="activity-icon"><WorkflowIcon size={16} /></div><div className="activity-main"><div className="activity-name">{String(workflows.find((workflow) => workflow.id === run.workflow_id)?.title ?? run.workflow_id)}</div><div className="activity-meta">{run.id} · {formatDate(run.started_at)}</div></div><StatusBadge status={run.status} /><ArrowRight size={15} className="text-slate-600" /></button>)}</div>}
    </div>
  </div>;
}

function StatCard({ label, value, icon: Icon, tone, helper }: { label: string; value: number | string; icon: typeof Activity; tone: string; helper: string }) {
  return <div className="stat-card"><div className="stat-top"><span>{label}</span><span className={`stat-icon ${tone}`}><Icon size={16} /></span></div><div className="stat-number">{value}</div><div className="stat-helper">{helper}</div></div>;
}

function Builder({ workflow, workflowId, prompt, busy, nodes, edges, selectedTask, onPrompt, onGenerate, onSave, onRun, onSelectTask, onWorkflowName }: {
  workflow: Workflow; workflowId: string; prompt: string; busy: boolean; nodes: Node<TaskNodeData>[]; edges: Edge[];
  selectedTask?: Task; onPrompt: (value: string) => void; onGenerate: () => void; onSave: () => void; onRun: () => void;
  onSelectTask: (id: string) => void; onWorkflowName: (name: string) => void;
}) {
  return <div className="page-stack builder-page">
    <PageTitle eyebrow="DESIGN STUDIO" title="Workflow builder" description="Describe your process and let FlowPilot map it into an executable workflow." />
    <div className="builder-toolbar"><div className="builder-document"><div className="builder-document-icon"><FileText size={17} /></div><div><input aria-label="Workflow name" value={workflow.name ?? ""} onChange={(event) => onWorkflowName(event.target.value)} /><span>{workflowId ? "Saved workflow" : "Unsaved draft"} · {workflow.tasks.length} tasks</span></div></div><div className="toolbar-actions"><button className="button-secondary" onClick={onSave} disabled={busy || workflow.tasks.length === 0}><Save size={15} /> Save workflow</button><button className="button-primary" onClick={onRun} disabled={busy || !workflowId}><Play size={15} /> Run workflow</button></div></div>
    <div className="builder-grid">
      <section className="panel prompt-panel"><div className="panel-heading"><div><span className="step-number">01</span><h2>Describe your workflow</h2></div><span className="panel-tag"><Sparkles size={12} /> AI powered</span></div><p className="panel-description">Tell us what you want to automate. Be specific about the steps and tools involved.</p><label htmlFor="workflow-prompt" className="sr-only">Workflow prompt</label><textarea id="workflow-prompt" value={prompt} onChange={(event) => onPrompt(event.target.value)} placeholder="e.g. Fetch a webpage, extract key information, and transform it into a concise summary..." /><div className="prompt-footer"><span>{prompt.length} characters</span><button className="button-primary button-full" onClick={onGenerate} disabled={busy || !prompt.trim()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}{busy ? "Generating..." : "Generate workflow"}</button></div>
        <div className="metadata-block"><div className="meta-heading">WORKFLOW DETAILS</div><div className="meta-line"><span>Name</span><strong>{workflow.name || "Untitled"}</strong></div><div className="meta-line"><span>Tasks</span><strong>{workflow.tasks.length}</strong></div><div className="meta-line"><span>Requires approval</span><strong>{workflow.tasks.some((task) => task.requires_approval) ? "Yes" : "Per task"}</strong></div></div>
      </section>
      <section className="panel canvas-panel"><div className="canvas-heading"><div><span className="step-number">02</span><h2>Workflow canvas</h2></div><span className="canvas-hint">Select a node to inspect</span></div><div className="flow-canvas">{nodes.length === 0 ? <EmptyState icon={GitBranch} title="Your canvas is ready" text="Generate a workflow to visualize its tasks and dependencies." /> :
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodeClick={(_, node) => onSelectTask(node.id)} fitView fitViewOptions={{ padding: 0.25 }} nodesDraggable={false} nodesConnectable={false} proOptions={{ hideAttribution: true }}><Background color="#273044" gap={22} size={1} /><Controls /></ReactFlow>}</div><div className="canvas-legend"><span><i className="legend-dot done" /> Completed</span><span><i className="legend-dot active" /> Running</span><span><i className="legend-dot approval" /> Approval</span><span><i className="legend-dot pending" /> Pending</span></div></section>
      <section className="panel inspector-panel"><div className="panel-heading"><div><span className="step-number">03</span><h2>Task inspector</h2></div><CircleHelp size={15} className="text-slate-500" /></div>{selectedTask ? <><div className="inspector-task"><div className="inspector-task-icon"><WorkflowIcon size={17} /></div><div className="min-w-0"><strong>{selectedTask.name}</strong><span>{selectedTask.id}</span></div></div><div className="inspector-field"><span>TOOL</span><div className="field-box"><Command size={14} />{selectedTask.tool}</div></div><div className="inspector-field"><span>DEPENDENCIES</span><div className="field-box">{(selectedTask.dependencies ?? []).length ? selectedTask.dependencies?.map((dependency) => typeof dependency === "string" ? dependency : dependency.task_id).join(", ") : "No dependencies"}</div></div><div className="inspector-field"><span>INPUT PARAMETERS</span><pre className="json-preview">{JSON.stringify(selectedTask.input_parameters ?? {}, null, 2)}</pre></div><div className="approval-flag"><LockKeyhole size={15} /><div><strong>Human approval</strong><span>Approval {selectedTask.requires_approval ? "required" : "not required"} before execution.</span></div><span className={`toggle-pill ${selectedTask.requires_approval ? "toggle-on" : ""}`}><i /></span></div></> : <EmptyState icon={WorkflowIcon} title="No task selected" text="Generate a workflow to inspect task details." />}</section>
    </div>
    {workflowId && <div className="saved-callout"><CheckCircle2 size={16} /> Saved workflows are ready to run. Generation never starts execution automatically.</div>}
  </div>;
}

function RunHistory({ runs, workflows, detail, selected, loading, onOpen, onCancel }: { runs: RunSummary[]; workflows: Array<Record<string, unknown>>; detail: RunDetail | null; selected: string; loading: boolean; onOpen: (id: string) => void; onCancel: () => void }) {
  const progress = detail?.tasks.length ? Math.round((detail.tasks.filter((task) => task.status === "COMPLETED").length / detail.tasks.length) * 100) : 0;
  return <div className="page-stack"><PageTitle eyebrow="OBSERVABILITY" title="Run history" description="Monitor workflow executions and inspect task-level outcomes." action={<button className="button-secondary" onClick={() => void onCancel()} disabled={!selected || !["RUNNING", "PAUSED_APPROVAL"].includes(detail?.run.status ?? "")}><X size={15} /> Cancel run</button>} />
    <div className="history-layout"><div className="panel history-panel"><div className="panel-heading"><div><h2>Executions</h2><span className="panel-subtitle">{runs.length} total runs</span></div><button className="icon-button" aria-label="Refresh runs" onClick={() => runs[0] && onOpen(runs[0].id)}><RefreshCw size={15} /></button></div>{loading && <div className="loading-line"><LoaderCircle className="spin" size={16} /> Loading runs</div>}{runs.length === 0 ? <EmptyState icon={Clock3} title="No executions yet" text="Run a saved workflow to see its history here." /> : runs.map((run) => <button key={run.id} className={`run-list-item ${selected === run.id ? "run-list-selected" : ""}`} onClick={() => onOpen(run.id)}><div className="run-list-icon"><Activity size={15} /></div><div className="run-list-copy"><strong>{String(workflows.find((workflow) => workflow.id === run.workflow_id)?.title ?? run.workflow_id)}</strong><span>{run.id}</span><small>{formatDate(run.started_at)}</small></div><StatusBadge status={run.status} /></button>)}</div>
      <div className="panel run-detail-panel">{detail ? <><div className="run-detail-head"><div><div className="eyebrow">RUN DETAIL</div><h2>{String(workflows.find((workflow) => workflow.id === detail.run.workflow_id)?.title ?? detail.run.workflow_id)}</h2><code>{detail.run.id}</code></div><StatusBadge status={detail.run.status} /></div><div className="progress-summary"><div className="progress-label"><span>Progress</span><strong>{progress}%</strong></div><div className="progress-track"><div style={{ width: `${progress}%` }} /></div><div className="progress-caption">{detail.tasks.filter((task) => task.status === "COMPLETED").length} of {detail.tasks.length} tasks completed</div></div><div className="task-results">{detail.tasks.map((task) => <div className="task-result" key={task.task_id}><div className={`task-result-icon ${statusClass(task.status)}`}><WorkflowIcon size={15} /></div><div className="task-result-main"><div className="task-result-top"><strong>{task.name}</strong><StatusBadge status={task.status} /></div><span>{task.tool_name} · {task.task_id}</span>{task.error && <div className="task-error"><XCircle size={13} />{task.error}</div>}{task.reconciliation_required && <div className="task-error"><CircleHelp size={13} />Manual reconciliation required</div>}{task.output !== null && task.output !== undefined && <pre className="output-preview">{JSON.stringify(task.output, null, 2)}</pre>}</div></div>)}</div><div className="run-timestamps"><span>Started {formatDate(detail.run.started_at)}</span><span>{detail.run.completed_at ? `Finished ${formatDate(detail.run.completed_at)}` : "Live updates enabled"}</span></div></> : <EmptyState icon={Activity} title="Select a run" text="Choose an execution from the list to inspect its status and task outputs." />}</div></div>
  </div>;
}

function Approvals({ approvals, runs, secrets, onSecrets, onRefresh, onDecision }: { approvals: Approval[]; runs: RunSummary[]; secrets: Record<string, { token: string; payload: string }>; onSecrets: (value: Record<string, { token: string; payload: string }>) => void; onRefresh: () => void; onDecision: (approval: Approval, decision: "approve" | "reject") => void }) {
  return <div className="page-stack"><PageTitle eyebrow="HUMAN IN THE LOOP" title="Approvals" description="Review paused workflow actions before they are allowed to execute." action={<button className="button-secondary" onClick={() => void onRefresh()}><RefreshCw size={15} /> Refresh</button>} /><div className="notice-card"><LockKeyhole size={17} /><div><strong>Secure approval required</strong><p>The API never returns raw approval tokens or task arguments. Supply the original token and exact payload securely received from your trusted workflow initiator.</p></div></div>{approvals.length === 0 ? <div className="panel"><EmptyState icon={ShieldCheck} title="No pending approvals" text={runs.length ? "Pending approvals appear here after you select an active run in Run History." : "No workflow runs are waiting for approval."} /></div> : <div className="approval-grid">{approvals.map((approval) => <article className="panel approval-card" key={approval.approval_id}><div className="approval-card-head"><div className="approval-symbol"><LockKeyhole size={18} /></div><StatusBadge status={approval.status} /></div><div className="eyebrow">APPROVAL REQUEST · {approval.workflow_name ?? "Workflow"}</div><h2>{approval.task_name ?? approval.task_id}</h2><p>Tool: <strong>{approval.tool_name ?? "Unavailable"}</strong> · Task: <code>{approval.task_id}</code></p><p>Run <code>{approval.run_id}</code> · Expires {formatDate(approval.expires_at)}</p><label>Approval token<input value={secrets[approval.approval_id]?.token ?? ""} onChange={(event) => onSecrets({ ...secrets, [approval.approval_id]: { token: event.target.value, payload: secrets[approval.approval_id]?.payload ?? "{}" } })} placeholder="Paste token from trusted initiator" /></label><label>Exact action payload<textarea rows={4} value={secrets[approval.approval_id]?.payload ?? "{}"} onChange={(event) => onSecrets({ ...secrets, [approval.approval_id]: { token: secrets[approval.approval_id]?.token ?? "", payload: event.target.value } })} /></label><div className="approval-actions"><button className="button-danger" onClick={() => onDecision(approval, "reject")} disabled={!secrets[approval.approval_id]?.token}><X size={15} /> Reject</button><button className="button-primary" onClick={() => onDecision(approval, "approve")} disabled={!secrets[approval.approval_id]?.token}><Check size={15} /> Approve</button></div></article>)}</div>}</div>;
}

function SettingsPage({ apiBase, apiOnline, onRefresh }: { apiBase: string; apiOnline: boolean | null; onRefresh: () => void }) {
  return <div className="page-stack"><PageTitle eyebrow="PREFERENCES" title="Settings" description="Manage your local FlowPilot connection and workspace preferences." /><div className="panel settings-card"><div className="settings-title"><div className="settings-icon"><Settings size={19} /></div><div><h2>Backend connection</h2><p>Frontend requests are sent directly to your configured FlowPilot API.</p></div><StatusBadge status={apiOnline ? "connected" : "disconnected"} /></div><div className="settings-row"><div><strong>API base URL</strong><span>Set with VITE_API_BASE_URL at build time</span></div><code>{apiBase}</code></div><div className="settings-row"><div><strong>Connection status</strong><span>Health endpoint: /health</span></div><StatusBadge status={apiOnline ? "connected" : "disconnected"} /></div><div className="settings-row"><div><strong>Security</strong><span>No API secrets are stored in the frontend.</span></div><ShieldCheck size={18} className="text-emerald-400" /></div><button className="button-secondary mt-5" onClick={onRefresh}><RefreshCw size={15} /> Test connection</button></div><div className="notice-card"><CircleHelp size={17} /><div><strong>Trusted local environment</strong><p>The MVP backend does not include authentication. Keep the API bound to localhost or use an authenticated gateway before exposing it beyond a trusted machine.</p></div></div></div>;
}

function EmptyState({ icon: Icon, title, text, action }: { icon: typeof Activity; title: string; text: string; action?: React.ReactNode }) {
  return <div className="empty-state"><div className="empty-icon"><Icon size={19} /></div><h3>{title}</h3><p>{text}</p>{action}</div>;
}

function formatDate(value?: string | null) {
  if (!value) return "Not available";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export { App };
