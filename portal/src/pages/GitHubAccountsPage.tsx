import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  type DeployConfigStatus,
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

  // Deploy readiness gates the "add account" button: adding an account triggers
  // a cloud deploy that needs the GitHub PATs + repo secrets configured first.
  const deployStatus = useQuery({
    queryKey: ["deploy-status"],
    queryFn: () => api.getDeployStatus(principal.token),
  });
  const ready = deployStatus.data?.ready ?? false;

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

  const resync = useMutation({
    mutationFn: (id: string) => api.resyncGithubCatalog(principal.token, id),
    onSuccess: (r) => {
      const added = r.routes_after - r.routes_before;
      toast(t("github.resyncedOk", { count: added }));
      qc.invalidateQueries({ queryKey: ["model-routes"] });
    },
    onError: (e) => toast(String(e), "error"),
  });

  return (
    <section>
      <h2>{t("github.title")}</h2>
      <p className="help-card">{t("help.github")}</p>

      <DeployConfigSection />

      <div className="list-toolbar">
        <button
          type="button"
          className="add-toggle"
          onClick={() => start.mutate()}
          disabled={start.isPending || !ready}
          title={!ready ? t("github.gateHint") : undefined}
        >
          {start.isPending ? t("github.starting") : `+ ${t("github.addNew")}`}
        </button>
        <span className="count">{accounts.data?.length ?? 0}</span>
      </div>
      {!ready && !deployStatus.isLoading && (
        <p className="hint">{t("github.gateHint")}</p>
      )}
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
                      className="btn-sm"
                      onClick={() => resync.mutate(a.id)}
                      disabled={
                        !a.container_app_fqdn ||
                        a.status !== "ready" ||
                        (resync.isPending && resync.variables === a.id)
                      }
                      title={t("github.resyncHint")}
                    >
                      {resync.isPending && resync.variables === a.id
                        ? t("github.resyncing")
                        : t("github.resync")}
                    </button>
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

// Deploy configuration: paste the two GitHub PATs (bootstrap + deploy). Saving
// stores them in Key Vault and auto-pushes the Azure SP creds into the repo's
// Actions secrets; once that succeeds `ready` flips true and the add-account
// button unlocks. GitHub can't mint PATs via API, so the admin generates them
// and pastes them here. Secret VALUES are never returned — set PATs show a mask.
function DeployConfigSection() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const toast = useToast();
  const [bootstrapPat, setBootstrapPat] = useState("");
  const [deployPat, setDeployPat] = useState("");

  const status = useQuery({
    queryKey: ["deploy-status"],
    queryFn: () => api.getDeployStatus(principal.token),
  });

  const save = useMutation({
    mutationFn: () =>
      api.saveDeployPats(principal.token, {
        // Only send non-empty fields — empty means "leave unchanged".
        ...(bootstrapPat ? { bootstrap_pat: bootstrapPat } : {}),
        ...(deployPat ? { deploy_pat: deployPat } : {}),
      }),
    onSuccess: (s: DeployConfigStatus) => {
      setBootstrapPat("");
      setDeployPat("");
      if (s.pushed) toast(t("github.pushedOk"));
      else if (s.detail) toast(s.detail, "error");
      qc.setQueryData(["deploy-status"], s);
      qc.invalidateQueries({ queryKey: ["deploy-status"] });
    },
  });

  const push = useMutation({
    mutationFn: () => api.pushSpCreds(principal.token),
    onSuccess: (s: DeployConfigStatus) => {
      toast(t("github.pushedOk"));
      qc.setQueryData(["deploy-status"], s);
    },
  });

  const s = status.data;
  const canSave = (!!bootstrapPat || !!deployPat) && !save.isPending;

  return (
    <details className="card deploy-config" open={!s?.ready}>
      <summary>
        <strong>{t("github.deployConfigTitle")}</strong>{" "}
        {status.isLoading ? (
          <span className="badge">{t("common.loading")}</span>
        ) : s?.ready ? (
          <span className="badge badge-active">{t("github.status.ready")}</span>
        ) : (
          <span className="badge badge-suspended">{t("github.notConfigured")}</span>
        )}
      </summary>

      <p className="help-card">{t("help.deployConfig")}</p>

      <form
        className="form-grid"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSave) save.mutate();
        }}
      >
        <label>
          {t("github.bootstrapPat")}
          <input
            type="password"
            autoComplete="off"
            placeholder={s?.bootstrap_pat_set ? t("github.patSet") : t("github.patPlaceholder")}
            value={bootstrapPat}
            onChange={(e) => setBootstrapPat(e.target.value)}
          />
          <span className="hint">{t("github.bootstrapPatHint")}</span>
        </label>
        <label>
          {t("github.deployPat")}
          <input
            type="password"
            autoComplete="off"
            placeholder={s?.deploy_pat_set ? t("github.patSet") : t("github.patPlaceholder")}
            value={deployPat}
            onChange={(e) => setDeployPat(e.target.value)}
          />
          <span className="hint">{t("github.deployPatHint")}</span>
        </label>
        <div className="row-actions">
          <button type="submit" disabled={!canSave}>
            {save.isPending ? t("github.saving") : t("github.savePats")}
          </button>
          {s?.bootstrap_pat_set && s?.sp_creds_present && (
            <button
              type="button"
              className="btn-sm"
              onClick={() => push.mutate()}
              disabled={push.isPending}
            >
              {push.isPending ? t("github.pushing") : t("github.pushSp")}
            </button>
          )}
        </div>
      </form>

      {s && !s.sp_creds_present && <p className="hint">{t("github.spMissing")}</p>}
      {s?.detail && !s.pushed && <p className="error">{s.detail}</p>}
      {(save.isError || push.isError) && (
        <p className="error">{String(save.error ?? push.error)}</p>
      )}
    </details>
  );
}
