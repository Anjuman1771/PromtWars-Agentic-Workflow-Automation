const API_BASE =
  import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";

export type Workflow = {
  id?: string;
  name?: string | null;
  description?: string | null;
  tasks: Task[];
};

export type Task = {
  id: string;
  name: string;
  tool: string;
  input_parameters?: Record<string, unknown>;
  dependencies?: Array<string | { task_id: string }>;
  requires_approval?: boolean;
  outputs?: string[];
};

export type RunSummary = {
  id: string;
  workflow_id: string;
  status: string;
  started_at?: string;
  completed_at?: string | null;
  current_step_count?: number;
};

export type RunDetail = {
  run: RunSummary & { error_message?: string | null };
  tasks: Array<{
    task_id: string;
    name: string;
    tool_name: string;
    status: string;
    error?: string | null;
    output?: unknown;
    reconciliation_required: boolean;
  }>;
};

export type Approval = {
  approval_id: string;
  run_id: string;
  workflow_name?: string | null;
  task_id: string;
  task_name?: string | null;
  tool_name?: string | null;
  status: string;
  expires_at: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body.error?.message ?? message;
    } catch {
      // Keep the generic HTTP message for non-JSON responses.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string }>("/health"),
  workflows: () => request<{ items: Array<Record<string, unknown>> }>("/workflows"),
  workflow: (id: string) => request<Record<string, unknown>>(`/workflows/${id}`),
  generate: (prompt: string) =>
    request<{ workflow: Workflow; provider: string }>("/workflows/generate", {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  saveWorkflow: (workflow: Workflow) =>
    request<Record<string, unknown>>("/workflows", {
      method: "POST",
      body: JSON.stringify(workflow),
    }),
  runWorkflow: (id: string) =>
    request<Record<string, unknown>>(`/workflows/${id}/run`, { method: "POST" }),
  runs: () => request<{ items: RunSummary[] }>("/runs"),
  run: (id: string) => request<RunDetail>(`/runs/${id}`),
  cancelRun: (id: string) =>
    request<Record<string, unknown>>(`/runs/${id}/cancel`, { method: "POST" }),
  approvals: (runId: string) =>
    request<{ items: Approval[] }>(`/runs/${runId}/approvals`),
  decideApproval: (
    id: string,
    decision: "approve" | "reject",
    token: string,
    actionPayload: Record<string, unknown>,
  ) =>
    request<Record<string, unknown>>(`/approvals/${id}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ token, action_payload: actionPayload }),
    }),
  eventsUrl: (runId: string) => `${API_BASE}/runs/${runId}/events`,
};

export { API_BASE };
