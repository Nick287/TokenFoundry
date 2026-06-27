import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type VirtualKeySecret } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";

export function KeysPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const [projectId, setProjectId] = useState("");
  const [budget, setBudget] = useState("");
  const [issued, setIssued] = useState<VirtualKeySecret | null>(null);
  const [copied, setCopied] = useState(false);

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.listProjects(principal.token),
  });

  const keys = useQuery({
    queryKey: ["keys"],
    queryFn: () => api.listKeys(principal.token),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createKey(principal.token, {
        project_id: projectId,
        monthly_budget_usd: budget ? Number(budget) : null,
        budget_action: "block",
      }),
    onSuccess: (k) => {
      setIssued(k);
      setCopied(false);
      setProjectId("");
      setBudget("");
      qc.invalidateQueries({ queryKey: ["keys"] });
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!projectId || create.isPending) return;
    create.mutate();
  }

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
          placeholder={t("keys.budget")}
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
        />
        <button type="submit" disabled={!projectId || create.isPending}>
          {create.isPending ? t("keys.provisioning") : t("keys.issue")}
        </button>
      </form>
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
        <table className="card">
          <thead>
            <tr>
              <th>{t("keys.keyId")}</th>
              <th>{t("keys.project")}</th>
              <th>{t("common.status")}</th>
              <th>{t("keys.budgetCol")}</th>
              <th>{t("keys.created")}</th>
            </tr>
          </thead>
          <tbody>
            {keys.data.map((k) => {
              const proj = projects.data?.find((p) => p.id === k.project_id);
              return (
                <tr key={k.id}>
                  <td>
                    <code className="id-cell">{k.id}</code>
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
                    {k.monthly_budget_usd != null
                      ? `$${k.monthly_budget_usd.toFixed(2)}`
                      : t("keys.unlimited")}
                  </td>
                  <td>{new Date(k.created_at).toLocaleString()}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <p className="hint">{t("keys.empty")}</p>
      )}
    </section>
  );
}
