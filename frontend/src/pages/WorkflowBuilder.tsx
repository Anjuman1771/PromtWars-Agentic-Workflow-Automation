import {
  CheckCircle2,
  CircleHelp,
  Command,
  FileText,
  GitBranch,
  LockKeyhole,
  LoaderCircle,
  Play,
  Save,
  Sparkles,
  Workflow as WorkflowIcon,
} from "lucide-react";
import {
  Background,
  Controls,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import type { Ref } from "react";
import type { Task, Workflow } from "../api";
import {
  EmptyState,
  PageTitle,
  type TaskNodeData,
  nodeTypes,
} from "../components/shared";

export function WorkflowBuilder({
  workflow,
  workflowId,
  prompt,
  busy,
  nodes,
  edges,
  selectedTask,
  headingRef,
  onPrompt,
  onGenerate,
  onSave,
  onRun,
  onSelectTask,
  onWorkflowName,
}: {
  workflow: Workflow;
  workflowId: string;
  prompt: string;
  busy: boolean;
  nodes: Node<TaskNodeData>[];
  edges: Edge[];
  selectedTask?: Task;
  headingRef: Ref<HTMLHeadingElement>;
  onPrompt: (value: string) => void;
  onGenerate: () => void;
  onSave: () => void;
  onRun: () => void;
  onSelectTask: (id: string) => void;
  onWorkflowName: (name: string) => void;
}) {
  return (
    <div className="page-stack builder-page">
      <PageTitle
        eyebrow="DESIGN STUDIO"
        title="Workflow builder"
        description="Describe your process and let FlowPilot map it into an executable workflow."
        headingRef={headingRef}
      />
      <div className="builder-toolbar">
        <div className="builder-document">
          <div aria-hidden="true" className="builder-document-icon">
            <FileText size={17} />
          </div>
          <div>
            <label className="sr-only" htmlFor="workflow-name">
              Workflow name
            </label>
            <input
              id="workflow-name"
              maxLength={200}
              value={workflow.name ?? ""}
              onChange={(event) => onWorkflowName(event.target.value)}
            />
            <span>
              {workflowId ? "Saved workflow" : "Unsaved draft"} ·{" "}
              {workflow.tasks.length} tasks
            </span>
          </div>
        </div>
        <div className="toolbar-actions">
          <button
            className="button-secondary"
            onClick={onSave}
            disabled={busy || workflow.tasks.length === 0}
          >
            <Save aria-hidden="true" size={15} /> Save workflow
          </button>
          <button
            className="button-primary"
            onClick={onRun}
            disabled={busy || !workflowId}
          >
            <Play aria-hidden="true" size={15} /> Run workflow
          </button>
        </div>
      </div>
      <div className="builder-grid">
        <section className="panel prompt-panel" aria-labelledby="prompt-heading">
          <div className="panel-heading">
            <div>
              <span aria-hidden="true" className="step-number">01</span>
              <h2 id="prompt-heading">Describe your workflow</h2>
            </div>
            <span className="panel-tag">
              <Sparkles aria-hidden="true" size={12} /> AI powered
            </span>
          </div>
          <p className="panel-description" id="prompt-help">
            Tell us what you want to automate. Be specific about the steps and
            tools involved.
          </p>
          <label className="sr-only" htmlFor="workflow-prompt">
            Workflow prompt
          </label>
          <textarea
            id="workflow-prompt"
            aria-describedby="prompt-help prompt-character-count"
            maxLength={20_000}
            value={prompt}
            onChange={(event) => onPrompt(event.target.value)}
            placeholder="e.g. Fetch a webpage, extract key information, and transform it into a concise summary..."
          />
          <div className="prompt-footer">
            <span id="prompt-character-count" aria-live="polite" aria-atomic="true">
              {prompt.length} / 20,000 characters
            </span>
            <button
              className="button-primary button-full"
              onClick={onGenerate}
              disabled={busy || !prompt.trim()}
            >
              {busy ? (
                <LoaderCircle aria-hidden="true" className="spin" size={15} />
              ) : (
                <Sparkles aria-hidden="true" size={15} />
              )}
              {busy ? "Generating..." : "Generate workflow"}
            </button>
          </div>
          <section className="metadata-block" aria-label="Workflow details">
            <div className="meta-heading">WORKFLOW DETAILS</div>
            <div className="meta-line">
              <span>Name</span>
              <strong>{workflow.name || "Untitled"}</strong>
            </div>
            <div className="meta-line">
              <span>Tasks</span>
              <strong>{workflow.tasks.length}</strong>
            </div>
            <div className="meta-line">
              <span>Requires approval</span>
              <strong>
                {workflow.tasks.some((task) => task.requires_approval)
                  ? "Yes"
                  : "Per task"}
              </strong>
            </div>
          </section>
        </section>
        <section className="panel canvas-panel" aria-labelledby="canvas-heading">
          <div className="canvas-heading">
            <div>
              <span aria-hidden="true" className="step-number">02</span>
              <h2 id="canvas-heading">Workflow canvas</h2>
            </div>
            <span className="canvas-hint">Select a node to inspect</span>
          </div>
          <div
            className="flow-canvas"
            role="region"
            aria-label="Workflow task graph"
          >
            {nodes.length === 0 ? (
              <EmptyState
                icon={GitBranch}
                title="Your canvas is ready"
                text="Generate a workflow to visualize its tasks and dependencies."
              />
            ) : (
              <ReactFlow
                ariaLabelConfig={{
                  "node.a11yDescription.default":
                    "Task node. Use arrow keys to navigate and Enter to select.",
                  "controls.fitView.ariaLabel": "Fit workflow to view",
                  "controls.zoomIn.ariaLabel": "Zoom in",
                  "controls.zoomOut.ariaLabel": "Zoom out",
                }}
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                onNodeClick={(_, node) => onSelectTask(node.id)}
                fitView
                fitViewOptions={{ padding: 0.25 }}
                nodesDraggable={false}
                nodesConnectable={false}
                nodesFocusable
                edgesFocusable
              >
                <Background color="#273044" gap={22} size={1} />
                <Controls />
              </ReactFlow>
            )}
          </div>
          <div className="canvas-legend" role="group" aria-label="Task status legend">
            <span><i aria-hidden="true" className="legend-dot done" /> Completed</span>
            <span><i aria-hidden="true" className="legend-dot active" /> Running</span>
            <span><i aria-hidden="true" className="legend-dot approval" /> Approval</span>
            <span><i aria-hidden="true" className="legend-dot pending" /> Pending</span>
          </div>
        </section>
        <section className="panel inspector-panel" aria-labelledby="inspector-heading">
          <div className="panel-heading">
            <div>
              <span aria-hidden="true" className="step-number">03</span>
              <h2 id="inspector-heading">Task inspector</h2>
            </div>
            <CircleHelp aria-hidden="true" size={15} className="text-slate-500" />
          </div>
          {selectedTask ? (
            <>
              <div
                className="inspector-task"
                aria-live="polite"
                aria-atomic="true"
              >
                <div aria-hidden="true" className="inspector-task-icon">
                  <WorkflowIcon size={17} />
                </div>
                <div className="min-w-0">
                  <strong>{selectedTask.name}</strong>
                  <span>{selectedTask.id}</span>
                </div>
              </div>
              <div className="inspector-field">
                <span>TOOL</span>
                <div className="field-box">
                  <Command aria-hidden="true" size={14} />
                  {selectedTask.tool}
                </div>
              </div>
              <div className="inspector-field">
                <span>DEPENDENCIES</span>
                <div className="field-box">
                  {(selectedTask.dependencies ?? [])
                    .map((dependency) =>
                      typeof dependency === "string"
                        ? dependency
                        : dependency.task_id,
                    )
                    .join(", ") || "No dependencies"}
                </div>
              </div>
              <div className="inspector-field">
                <span>INPUT PARAMETERS</span>
                <pre className="json-preview">
                  {JSON.stringify(selectedTask.input_parameters ?? {}, null, 2)}
                </pre>
              </div>
              <div className="approval-flag">
                <LockKeyhole aria-hidden="true" size={15} />
                <div>
                  <strong>Human approval</strong>
                  <span>
                    Approval {selectedTask.requires_approval ? "required" : "not required"} before execution.
                  </span>
                </div>
                <span
                  aria-hidden="true"
                  className={`toggle-pill ${selectedTask.requires_approval ? "toggle-on" : ""}`}
                >
                  <i aria-hidden="true" />
                </span>
              </div>
            </>
          ) : (
            <EmptyState
              icon={WorkflowIcon}
              title="No task selected"
              text="Generate a workflow to inspect task details."
            />
          )}
        </section>
      </div>
      {workflowId && (
        <div className="saved-callout" role="status">
          <CheckCircle2 aria-hidden="true" size={16} /> Saved workflows are ready
          to run. Generation never starts execution automatically.
        </div>
      )}
    </div>
  );
}
