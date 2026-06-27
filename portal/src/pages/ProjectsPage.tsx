import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
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
      setTenantId("");
      setName("");
      setCostCenter("");
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!tenantId || !name || create.isPending) return;
    create.mutate();
  }

  return (
    <section>
      <h2>{t("projects.title")}</h2>
      <p className="help-card">{t("help.projects")}</p>

      <form className="card form-row" onSubmit={onSubmit}>
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
        <button type="submit" disabled={!tenantId || !name || create.isPending}>
          {create.isPending ? t("projects.creating") : t("projects.create")}
        </button>
      </form>
      {create.isError && <p className="error">{String(create.error)}</p>}

      {projects.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : (
        <table className="card">
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("common.id")}</th>
              <th>{t("projects.tenant")}</th>
              <th>{t("projects.costCenterCol")}</th>
            </tr>
          </thead>
          <tbody>
            {projects.data && projects.data.length === 0 && (
              <tr>
                <td colSpan={4} className="hint">
                  {t("projects.empty")}
                </td>
              </tr>
            )}
            {projects.data?.map((p) => {
              const tn = tenants.data?.find((x) => x.id === p.tenant_id);
              return (
                <tr key={p.id}>
                  <td>{p.name}</td>
                  <td>
                    <code className="id-cell">{p.id}</code>
                  </td>
                  <td>
                    {tn ? (
                      <>
                        {tn.name}{" "}
                        <code className="id-cell">({p.tenant_id})</code>
                      </>
                    ) : (
                      <code className="id-cell">{p.tenant_id}</code>
                    )}
                  </td>
                  <td>{p.cost_center ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
