import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";

export function TenantsPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [mode, setMode] = useState("RESELL");

  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api.listTenants(principal.token),
  });

  const create = useMutation({
    mutationFn: () => api.createTenant(principal.token, { name, mode }),
    onSuccess: () => {
      setName("");
      setMode("RESELL");
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
  });

  const bindProduct = useMutation({
    mutationFn: (id: string) => api.ensureTenantProduct(principal.token, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tenants"] }),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!name || create.isPending) return;
    create.mutate();
  }

  return (
    <section>
      <h2>{t("tenants.title")}</h2>
      <p className="help-card">{t("help.tenants")}</p>

      <form className="card form-row" onSubmit={onSubmit}>
        <input
          placeholder={t("tenants.namePlaceholder")}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="RESELL">{t("tenants.modeResell")}</option>
          <option value="BYO">{t("tenants.modeByo")}</option>
          <option value="INTERNAL">{t("tenants.modeInternal")}</option>
        </select>
        <button type="submit" disabled={!name || create.isPending}>
          {create.isPending ? t("tenants.creating") : t("tenants.create")}
        </button>
      </form>
      {create.isError && <p className="error">{String(create.error)}</p>}

      {tenants.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : tenants.isError ? (
        <p className="error">{String(tenants.error)}</p>
      ) : (
        <table className="card">
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("common.id")}</th>
              <th>{t("tenants.mode")}</th>
              <th>{t("tenants.product")}</th>
              <th>{t("common.status")}</th>
            </tr>
          </thead>
          <tbody>
            {tenants.data && tenants.data.length === 0 && (
              <tr>
                <td colSpan={5} className="hint">
                  {t("tenants.empty")}
                </td>
              </tr>
            )}
            {tenants.data?.map((tn) => (
              <tr key={tn.id}>
                <td>{tn.name}</td>
                <td>
                  <code className="id-cell">{tn.id}</code>
                </td>
                <td>{tn.mode}</td>
                <td>
                  {tn.apim_product_ids.length > 0 ? (
                    <code className="id-cell">{tn.apim_product_ids[0]}</code>
                  ) : (
                    <button
                      className="btn-sm"
                      disabled={bindProduct.isPending}
                      onClick={() => bindProduct.mutate(tn.id)}
                    >
                      {t("tenants.bindProduct")}
                    </button>
                  )}
                </td>
                <td>
                  <span className={`badge badge-${tn.status}`}>{tn.status}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
