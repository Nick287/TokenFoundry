import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type VirtualKeySecret } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";

export function KeysPage() {
  const principal = usePrincipal()!;
  const { t } = useTranslation();
  const [projectId, setProjectId] = useState("");
  const [budget, setBudget] = useState("");
  const [issued, setIssued] = useState<VirtualKeySecret | null>(null);

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.listProjects(principal.token),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createKey(principal.token, {
        project_id: projectId,
        monthly_budget_usd: budget ? Number(budget) : null,
        budget_action: "block",
      }),
    onSuccess: (k) => setIssued(k),
  });

  return (
    <section>
      <h2>{t("keys.title")}</h2>
      <p className="help-card">{t("help.keys")}</p>

      <div className="card form-row">
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
        <button
          disabled={!projectId || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? t("keys.provisioning") : t("keys.issue")}
        </button>
      </div>
      {create.isError && <p className="error">{String(create.error)}</p>}

      {issued && (
        <div className="card secret">
          <h3>{t("keys.issuedTitle")}</h3>
          <code className="key-value">{issued.key_value}</code>
          <p className="hint">{t("keys.issuedHint")}</p>
        </div>
      )}
    </section>
  );
}
