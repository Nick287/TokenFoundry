import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";

// Projects live under a tenant and carry the pj_ id used to issue virtual keys.
export function ProjectsPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const [tenantId, setTenantId] = useState("");
  const [name, setName] = useState("");
  const [costCenter, setCostCenter] = useState("");

  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api.listTenants(principal.token),
  });

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api.listProjects(principal.token),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createProject(principal.token, {
        tenant_id: tenantId,
        name,
        cost_center: costCenter || null,
      }),
    onSuccess: () => {
      setName("");
      setCostCenter("");
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  return (
    <section>
      <h2>{t("projects.title")}</h2>
      <p className="help-card">{t("help.projects")}</p>

      <div className="card form-row">
        <select value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
          <option value="">{t("projects.selectTenant")}</option>
          {tenants.data?.map((tn) => (
            <option key={tn.id} value={tn.id}>
              {tn.name} ({tn.id})
            </option>
          ))}
        </select>
        <input
          placeholder={t("projects.namePlaceholder")}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          placeholder={t("projects.costCenter")}
          value={costCenter}
          onChange={(e) => setCostCenter(e.target.value)}
        />
        <button
          disabled={!tenantId || !name || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? t("projects.creating") : t("projects.create")}
        </button>
      </div>
      {create.isError && <p className="error">{String(create.error)}</p>}

      {projects.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : (
        <table className="card">
          <thead>
            <tr>
              <th>{t("common.id")}</th>
              <th>{t("common.name")}</th>
              <th>{t("projects.tenant")}</th>
              <th>{t("projects.costCenterCol")}</th>
            </tr>
          </thead>
          <tbody>
            {projects.data?.map((p) => (
              <tr key={p.id}>
                <td>
                  <code>{p.id}</code>
                </td>
                <td>{p.name}</td>
                <td>
                  <code>{p.tenant_id}</code>
                </td>
                <td>{p.cost_center ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
