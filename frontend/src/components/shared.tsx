import {
  Activity,
  CheckCircle2,
  Workflow as WorkflowIcon,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import {
  Handle,
  Position,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import type { ReactNode, Ref } from "react";
import type { Task } from "../api";

export type TaskNodeData = {
  task: Task;
  index: number;
  status?: string;
};

function TaskNode({ data, selected }: NodeProps<Node<TaskNodeData>>) {
  const status =
    data.status ?? (data.task.requires_approval ? "approval" : "pending");

  return (
    <div
      aria-label={`${data.task.name}, tool ${data.task.tool}, status ${status.replaceAll("_", " ")}`}
      className={`task-node ${selected ? "task-node-selected" : ""} state-${status}`}
    >
      <Handle type="target" position={Position.Top} />
      <div aria-hidden="true" className="task-node-icon">
        <WorkflowIcon size={16} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold text-slate-100">
          {data.task.name}
        </div>
        <div className="mt-1 truncate text-xs text-slate-400">
          {data.task.tool}
        </div>
      </div>
      <span aria-hidden="true" className="task-index">
        {String(data.index + 1).padStart(2, "0")}
      </span>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export const nodeTypes = { task: TaskNode };

export function statusClass(status?: string): string {
  const key = status?.toLowerCase() ?? "pending";
  if (["completed", "success"].includes(key)) return "badge-success";
  if (["failed", "cancelled", "timed_out"].includes(key)) return "badge-danger";
  if (["running", "waiting_approval", "paused_approval"].includes(key)) {
    return "badge-active";
  }
  return "badge-muted";
}

export function StatusBadge({ status }: { status?: string }) {
  const label = status?.replaceAll("_", " ") ?? "unknown";
  return (
    <span className={`status-badge ${statusClass(status)}`}>
      <span aria-hidden="true" className="status-dot" />
      {label}
    </span>
  );
}

export function PageTitle({
  eyebrow,
  title,
  description,
  action,
  headingRef,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
  headingRef?: Ref<HTMLHeadingElement>;
}) {
  return (
    <div className="page-title-row">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1 id="page-title" ref={headingRef} tabIndex={-1}>
          {title}
        </h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  text,
  action,
}: {
  icon: LucideIcon;
  title: string;
  text: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div aria-hidden="true" className="empty-icon">
        <Icon size={19} />
      </div>
      <h3>{title}</h3>
      <p>{text}</p>
      {action}
    </div>
  );
}

export function StatCard({
  label,
  value,
  icon: Icon,
  tone,
  helper,
}: {
  label: string;
  value: number | string;
  icon: LucideIcon;
  tone: string;
  helper: string;
}) {
  return (
    <div className="stat-card">
      <div className="stat-top">
        <span>{label}</span>
        <span aria-hidden="true" className={`stat-icon ${tone}`}>
          <Icon size={16} />
        </span>
      </div>
      <div className="stat-number">{value}</div>
      <div className="stat-helper">{helper}</div>
    </div>
  );
}

export function formatDate(value?: string | null): string {
  if (!value) return "Not available";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function getWorkflowTitle(
  workflows: Array<{ id: string; title?: string | null }>,
  workflowId: string,
): string {
  return workflows.find((workflow) => workflow.id === workflowId)?.title ?? workflowId;
}

export { Activity, CheckCircle2, WorkflowIcon };
