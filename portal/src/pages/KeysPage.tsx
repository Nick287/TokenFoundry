import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type VirtualKey, type VirtualKeySecret } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { ConfirmDialog } from "../components/Modal";
import { CopyId } from "../components/CopyId";
import { useToast } from "../components/Toast";

export function KeysPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const toast = useToast();
  const [projectId, setProjectId] = useState("");
  // Per-key gateway limits. tpm = arbitrary tokens/min; quotaTier = preset amount
  // (APIM can't take an arbitrary quota via expression); period = reset window.
  const [tpm, setTpm] = useState("");
  const [quotaTier, setQuotaTier] = useState("none");
  const [quotaPeriod, setQuotaPeriod] = useState("Daily");
  const [issued, setIssued] = useState<VirtualKeySecret | null>(null);
  const [copied, setCopied] = useState(false);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<VirtualKey | null>(null);

  const QUOTA_TIERS = ["none", "small", "medium", "large"] as const;
  const QUOTA_PERIODS = ["Hourly", "Daily", "Weekly", "Monthly", "Yearly"] as const;

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.listProjects(principal.token),
  });

  const keys = useQuery({
    queryKey: ["keys"],
    queryFn: () => api.listKeys(principal.token),
  });

  const create = useMutation({
    mutationFn: () => {
      const hasQuota = quotaTier !== "none";
      return api.createKey(principal.token, {
        project_id: projectId,
        tokens_per_minute: tpm ? Number(tpm) : null,
        token_quota_tier: hasQuota ? quotaTier : null,
        token_quota_period: hasQuota ? quotaPeriod : null,
      });
    },
    onSuccess: (k) => {
      setIssued(k);
      setCopied(false);
      setProjectId("");
      setTpm("");
      setQuotaTier("none");
      setQuotaPeriod("Daily");
      setAdding(false);
      toast(t("common.created"));
      qc.invalidateQueries({ queryKey: ["keys"] });
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!projectId || create.isPending) return;
    create.mutate();
  }

  const del = useMutation({
    mutationFn: (id: string) => api.deleteKey(principal.token, id),
    onSuccess: () => {
      setRemoving(null);
      toast(t("common.deleted"));
      qc.invalidateQueries({ queryKey: ["keys"] });
    },
  });

  async function onCopy() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.key_value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard blocked (insecure context / permissions) — leave the value
      // visible so the operator can select it manually.
    }
  }

  return (
    <section>
      <h2>{t("keys.title")}</h2>
      <p className="help-card">{t("help.keys")}</p>

      <div className="list-toolbar">
        <button type="button" className="add-toggle" onClick={() => setAdding((v) => !v)}>
          {adding ? t("common.close") : `+ ${t("keys.addNew")}`}
        </button>
      </div>

      {adding && (
      <form className="card form-row" onSubmit={onSubmit}>
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">{t("keys.selectProject")}</option>
          {projects.data?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.id})
            </option>
          ))}
        </select>
        <input
          type="number"
          min="1"
          max="100000000"
          placeholder={t("keys.tokensPerMinute")}
          value={tpm}
          onChange={(e) => setTpm(e.target.value)}
        />
        <select value={quotaTier} onChange={(e) => setQuotaTier(e.target.value)}>
          {QUOTA_TIERS.map((tier) => (
            <option key={tier} value={tier}>
              {t(`keys.quotaTiers.${tier}`)}
            </option>
          ))}
        </select>
        {quotaTier !== "none" && (
          <select
            value={quotaPeriod}
            onChange={(e) => setQuotaPeriod(e.target.value)}
          >
            {QUOTA_PERIODS.map((period) => (
              <option key={period} value={period}>
                {t(`keys.periods.${period}`)}
              </option>
            ))}
          </select>
        )}
        <button type="submit" disabled={!projectId || create.isPending}>
          {create.isPending ? t("keys.provisioning") : t("keys.issue")}
        </button>
      </form>
      )}
      {create.isError && <p className="error">{String(create.error)}</p>}

      {issued && (
        <div className="card secret">
          <h3>{t("keys.issuedTitle")}</h3>
          <code className="key-value">{issued.key_value}</code>
          <div className="form-row">
            <button type="button" className="btn-sm" onClick={onCopy}>
              {copied ? t("keys.copied") : t("keys.copy")}
            </button>
            <button
              type="button"
              className="btn-sm"
              onClick={() => setIssued(null)}
            >
              {t("keys.dismiss")}
            </button>
          </div>
          <p className="hint">{t("keys.issuedHint")}</p>
        </div>
      )}

      <h3>{t("keys.issuedKeys")}</h3>
      {keys.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : keys.isError ? (
        <p className="error">{t("common.loadFailed")}</p>
      ) : keys.data && keys.data.length > 0 ? (
        <div className="table-scroll">
        <table className="card">
          <thead>
            <tr>
              <th>{t("keys.keyId")}</th>
              <th>{t("keys.project")}</th>
              <th>{t("common.status")}</th>
              <th>{t("keys.limitsCol")}</th>
              <th>{t("keys.created")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {keys.data.map((k) => {
              const proj = projects.data?.find((p) => p.id === k.project_id);
              return (
                <tr key={k.id}>
                  <td>
                    <CopyId value={k.id} />
                  </td>
                  <td>
                    {proj ? (
                      <>
                        {proj.name}{" "}
                        <code className="id-cell">({k.project_id})</code>
                      </>
                    ) : (
                      <code className="id-cell">{k.project_id}</code>
                    )}
                  </td>
                  <td>
                    <span className={`badge badge-${k.status}`}>{k.status}</span>
                  </td>
                  <td>
                    {k.tokens_per_minute == null && k.token_quota_tier == null ? (
                      <span className="cell-zero">{t("keys.unlimited")}</span>
                    ) : (
                      <>
                        {k.tokens_per_minute != null && (
                          <div>{t("keys.tpmShort", { n: k.tokens_per_minute })}</div>
                        )}
                        {k.token_quota_tier != null && (
                          <div className="hint">
                            {t(`keys.quotaTiers.${k.token_quota_tier}`)}
                            {k.token_quota_period
                              ? ` / ${t(`keys.periods.${k.token_quota_period}`)}`
                              : ""}
                          </div>
                        )}
                      </>
                    )}
                  </td>
                  <td>{new Date(k.created_at).toLocaleString()}</td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="btn-sm btn-danger"
                      onClick={() => setRemoving(k)}
                    >
                      {t("common.delete")}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        </div>
      ) : (
        <div className="card empty-cta">
          <strong>{t("keys.emptyTitle")}</strong>
          {t("keys.emptyHint")}
        </div>
      )}

      {removing && (
        <ConfirmDialog
          title={t("keys.deleteTitle")}
          impact={
            <>
              {t("keys.deleteImpact", { id: removing.id })} {t("common.cannotUndo")}
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
