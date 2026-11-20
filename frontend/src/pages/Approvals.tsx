import {
  Check,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import type { Dispatch, Ref, SetStateAction } from "react";
import type { Approval, RunSummary } from "../api";
import {
  EmptyState,
  formatDate,
  PageTitle,
  StatusBadge,
} from "../components/shared";

export type ApprovalSecret = { token: string; payload: string };
export type ApprovalSecrets = Record<string, ApprovalSecret>;

export function Approvals({
  approvals,
  runs,
  secrets,
  submittingApprovalId,
  headingRef,
  onSecrets,
  onRefresh,
  onDecision,
}: {
  approvals: Approval[];
  runs: RunSummary[];
  secrets: ApprovalSecrets;
  submittingApprovalId: string | null;
  headingRef: Ref<HTMLHeadingElement>;
  onSecrets: Dispatch<SetStateAction<ApprovalSecrets>>;
  onRefresh: () => void;
  onDecision: (approval: Approval, decision: "approve" | "reject") => void;
}) {
  const updateSecret = (approvalId: string, field: keyof ApprovalSecret, value: string) => {
    onSecrets((current) => ({
      ...current,
      [approvalId]: {
        token: current[approvalId]?.token ?? "",
        payload: current[approvalId]?.payload ?? "{}",
        [field]: value,
      },
    }));
  };

  return (
    <div className="page-stack">
      <PageTitle
        eyebrow="HUMAN IN THE LOOP"
        title="Approvals"
        description="Review paused workflow actions before they are allowed to execute."
        headingRef={headingRef}
        action={
          <button className="button-secondary" onClick={onRefresh}>
            <RefreshCw aria-hidden="true" size={15} /> Refresh
          </button>
        }
      />
      <div className="notice-card">
        <LockKeyhole aria-hidden="true" size={17} />
        <div>
          <strong>Secure approval required</strong>
          <p>
            The API never returns raw approval tokens or task arguments. Supply
            the original token and exact payload securely received from your
            trusted workflow initiator.
          </p>
        </div>
      </div>
      {approvals.length === 0 ? (
        <div className="panel">
          <EmptyState
            icon={ShieldCheck}
            title="No pending approvals"
            text={
              runs.length
                ? "Pending approvals appear here after you select an active run in Run History."
                : "No workflow runs are waiting for approval."
            }
          />
        </div>
      ) : (
        <div className="approval-grid">
          {approvals.map((approval) => {
            const tokenId = `approval-token-${approval.approval_id}`;
            const payloadId = `approval-payload-${approval.approval_id}`;
            const secret = secrets[approval.approval_id];
            const submitting = submittingApprovalId === approval.approval_id;

            return (
              <article
                className="panel approval-card"
                key={approval.approval_id}
                aria-labelledby={`approval-title-${approval.approval_id}`}
              >
                <div className="approval-card-head">
                  <div aria-hidden="true" className="approval-symbol">
                    <LockKeyhole size={18} />
                  </div>
                  <StatusBadge status={approval.status} />
                </div>
                <div className="eyebrow">
                  APPROVAL REQUEST · {approval.workflow_name ?? "Workflow"}
                </div>
                <h2 id={`approval-title-${approval.approval_id}`}>
                  {approval.task_name ?? approval.task_id}
                </h2>
                <p>
                  Tool: <strong>{approval.tool_name ?? "Unavailable"}</strong> ·
                  Task: <code>{approval.task_id}</code>
                </p>
                <p>
                  Run <code>{approval.run_id}</code> · Expires{" "}
                  {formatDate(approval.expires_at)}
                </p>
                <div className="approval-field">
                  <label htmlFor={tokenId}>Approval token</label>
                  <input
                    id={tokenId}
                    type="password"
                    autoComplete="new-password"
                    spellCheck={false}
                    value={secret?.token ?? ""}
                    onChange={(event) =>
                      updateSecret(
                        approval.approval_id,
                        "token",
                        event.target.value,
                      )
                    }
                    placeholder="Paste token from trusted initiator"
                  />
                </div>
                <div className="approval-field">
                  <label htmlFor={payloadId}>Exact action payload (JSON object)</label>
                  <textarea
                    id={payloadId}
                    rows={4}
                    spellCheck={false}
                    value={secret?.payload ?? "{}"}
                    onChange={(event) =>
                      updateSecret(
                        approval.approval_id,
                        "payload",
                        event.target.value,
                      )
                    }
                  />
                </div>
                <div className="approval-actions">
                  <button
                    className="button-danger"
                    onClick={() => onDecision(approval, "reject")}
                    disabled={!secret?.token || submitting}
                  >
                    <X aria-hidden="true" size={15} />
                    {submitting ? "Submitting..." : "Reject"}
                  </button>
                  <button
                    className="button-primary"
                    onClick={() => onDecision(approval, "approve")}
                    disabled={!secret?.token || submitting}
                  >
                    <Check aria-hidden="true" size={15} />
                    {submitting ? "Submitting..." : "Approve"}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
