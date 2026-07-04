import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  type DeviceStart,
  type GitHubAccount,
  type GitHubDeployStatus,
} from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { ConfirmDialog, Modal } from "../components/Modal";
import { useToast } from "../components/Toast";

// "Adding a model" becomes "adding a GitHub account": the user runs a GitHub
// device-flow login, and the control plane deploys a dedicated GitModel hub for
// that account's Copilot subscription and joins it to the APIM pools. This page
// drives the device flow (show user_code -> poll) and then shows the deploy
// status walking pending -> deploying -> ready (or failed).

const TRANSITIONAL: GitHubDeployStatus[] = ["pending", "deploying", "deleting"];

// Map a deploy status onto the shared badge tones (styles.css).
function statusBadgeClass(status: GitHubDeployStatus): string {
  if (status === "ready") return "badge badge-active";
  if (status === "failed") return "badge badge-suspended";
  return "badge"; // pending / deploying / deleting — neutral
}

export function GitHubAccountsPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const toast = useToast();
  const [flow, setFlow] = useState<DeviceStart | null>(null);
  const [removing, setRemoving] = useState<GitHubAccount | null>(null);

  // The accounts list drives the table AND the deploying->ready transition:
  // poll every 3s while any account is in a transitional state.
  const accounts = useQuery({
    queryKey: ["github-accounts"],
    queryFn: () => api.listGithubAccounts(principal.token),
    refetchInterval: (q) =>
      (q.state.data ?? []).some((a) => TRANSITIONAL.includes(a.status)) ? 3000 : false,
  });

  const start = useMutation({
    mutationFn: () => api.startGithubDevice(principal.token),
    onSuccess: (d) => {
      setFlow(d);
      qc.invalidateQueries({ queryKey: ["github-accounts"] });
    },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteGithubAccount(principal.token, id),
    onSuccess: () => {
      setRemoving(null);
      toast(t("common.deleted"));
      qc.invalidateQueries({ queryKey: ["github-accounts"] });
    },
  });

  return (
    <section>
      <h2>{t("github.title")}</h2>
      <p className="help-card">{t("help.github")}</p>

      <div className="list-toolbar">
        <button
          type="button"
          className="add-toggle"
          onClick={() => start.mutate()}
          disabled={start.isPending}
        >
          {start.isPending ? t("github.starting") : `+ ${t("github.addNew")}`}
        </button>
        <span className="count">{accounts.data?.length ?? 0}</span>
      </div>
      {start.isError && <p className="error">{String(start.error)}</p>}

      {accounts.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : accounts.isError ? (
        <p className="error">{t("common.loadFailed")}</p>
      ) : accounts.data && accounts.data.length === 0 ? (
        <div className="card empty-cta">
          <strong>{t("github.emptyTitle")}</strong>
          {t("github.emptyHint")}
        </div>
      ) : (
        <div className="table-scroll">
          <table className="card">
            <thead>
              <tr>
                <th>{t("github.accountCol")}</th>
                <th>{t("github.statusCol")}</th>
                <th>{t("github.endpointCol")}</th>
                <th>{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {(accounts.data ?? []).map((a) => (
                <tr key={a.id}>
                  <td>
                    {a.github_login ? <strong>{a.github_login}</strong> : <code>{a.id}</code>}
                  </td>
                  <td>
                    <span className={statusBadgeClass(a.status)}>
                      {t(`github.status.${a.status}`)}
                    </span>
                    {a.status === "failed" && a.error_detail && (
                      <div className="hint error-detail">{a.error_detail}</div>
                    )}
                  </td>
                  <td>
                    {a.container_app_fqdn ? (
                      <a href={`https://${a.container_app_fqdn}`} target="_blank" rel="noreferrer">
                        {a.container_app_fqdn}
                      </a>
                    ) : (
                      <span className="cell-zero">—</span>
                    )}
                  </td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-sm btn-danger"
                      onClick={() => setRemoving(a)}
                      disabled={a.status === "deleting"}
                    >
                      {t("common.delete")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {flow && (
        <DeviceFlowModal
          flow={flow}
          onClose={() => {
            setFlow(null);
            qc.invalidateQueries({ queryKey: ["github-accounts"] });
          }}
        />
      )}
      {removing && (
        <ConfirmDialog
          title={t("github.deleteTitle")}
          impact={
            <>
              {t("github.deleteImpact", { login: removing.github_login ?? removing.id })}{" "}
              {t("common.cannotUndo")}
            </>
          }
          busy={del.isPending}
          onConfirm={() => del.mutate(removing.id)}
          onClose={() => setRemoving(null)}
        />
      )}
    </section>
  );
}

// Device-flow modal: shows the user_code + verification link, polls the backend
// on the flow's interval, and advances awaiting -> deploying -> ready/failed.
function DeviceFlowModal({ flow, onClose }: { flow: DeviceStart; onClose: () => void }) {
  const principal = usePrincipal()!;
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  // Poll device/poll while still awaiting authorization; once the backend flips
  // past PENDING (deploying/failed) it echoes the status and we stop polling.
  const poll = useQuery({
    queryKey: ["gh-device-poll", flow.account_id],
    queryFn: () => api.pollGithubDevice(principal.token, flow.account_id),
    refetchInterval: (q) =>
      !q.state.data || q.state.data.status === "pending"
        ? Math.max(flow.interval, 1) * 1000
        : false,
  });

  const status = poll.data?.status ?? "pending";

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(flow.user_code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard blocked (insecure context) — the code is shown for manual copy
    }
  }

  return (
    <Modal title={t("github.authorizeTitle")} onClose={onClose}>
      {status === "pending" ? (
        <div className="device-flow">
          <p>{t("github.userCodeHint")}</p>
          <div className="device-code">
            <code>{flow.user_code}</code>
            <button type="button" className="btn-sm" onClick={onCopy}>
              {copied ? t("keys.copied") : t("keys.copy")}
            </button>
          </div>
          <a
            className="btn-sm"
            href={flow.verification_uri}
            target="_blank"
            rel="noreferrer"
          >
            {t("github.openGithub")}
          </a>
          <p className="hint">{t("github.awaitingHint")}</p>
        </div>
      ) : status === "failed" ? (
        <div className="device-flow">
          <p className="error">{poll.data?.detail ?? t("github.failedHint")}</p>
        </div>
      ) : (
        <div className="device-flow">
          <p>
            <span className={statusBadgeClass(status)}>{t(`github.status.${status}`)}</span>
          </p>
          <p className="hint">
            {status === "ready" ? t("github.readyHint") : t("github.deployingHint")}
          </p>
        </div>
      )}
      <div className="modal-actions">
        <button type="button" className="btn-sm" onClick={onClose}>
          {status === "ready" ? t("common.close") : t("github.runInBackground")}
        </button>
      </div>
    </Modal>
  );
}
