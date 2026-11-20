import {
  CircleHelp,
  RefreshCw,
  Settings,
  ShieldCheck,
} from "lucide-react";
import type { Ref } from "react";
import { PageTitle, StatusBadge } from "../components/shared";

export function SettingsPage({
  apiBase,
  apiOnline,
  headingRef,
  onRefresh,
}: {
  apiBase: string;
  apiOnline: boolean | null;
  headingRef: Ref<HTMLHeadingElement>;
  onRefresh: () => void;
}) {
  const connectionStatus =
    apiOnline === null ? "unknown" : apiOnline ? "connected" : "disconnected";

  return (
    <div className="page-stack">
      <PageTitle
        eyebrow="PREFERENCES"
        title="Settings"
        description="Manage your local FlowPilot connection and workspace preferences."
        headingRef={headingRef}
      />
      <section className="panel settings-card" aria-labelledby="backend-heading">
        <div className="settings-title">
          <div aria-hidden="true" className="settings-icon">
            <Settings size={19} />
          </div>
          <div>
            <h2 id="backend-heading">Backend connection</h2>
            <p>
              Frontend requests are sent directly to your configured FlowPilot
              API.
            </p>
          </div>
          <StatusBadge status={connectionStatus} />
        </div>
        <div className="settings-row">
          <div>
            <strong>API base URL</strong>
            <span>Set with VITE_API_BASE_URL at build time</span>
          </div>
          <code>{apiBase}</code>
        </div>
        <div className="settings-row">
          <div>
            <strong>Connection status</strong>
            <span>Health endpoint: /health</span>
          </div>
          <StatusBadge status={connectionStatus} />
        </div>
        <div className="settings-row">
          <div>
            <strong>Security</strong>
            <span>No API secrets are stored in the frontend.</span>
          </div>
          <ShieldCheck aria-hidden="true" size={18} className="text-emerald-400" />
        </div>
        <button className="button-secondary mt-5" onClick={onRefresh}>
          <RefreshCw aria-hidden="true" size={15} /> Test connection
        </button>
      </section>
      <div className="notice-card">
        <CircleHelp aria-hidden="true" size={17} />
        <div>
          <strong>Trusted local environment</strong>
          <p>
            The MVP backend does not include authentication. Keep the API bound
            to localhost or use an authenticated gateway before exposing it
            beyond a trusted machine.
          </p>
        </div>
      </div>
    </div>
  );
}
