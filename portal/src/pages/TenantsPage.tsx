import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
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
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
  });

  const bindProduct = useMutation({
    mutationFn: (id: string) => api.ensureTenantProduct(principal.token, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tenants"] }),
  });

  return (
    <section>
      <h2>{t("tenants.title")}</h2>
      <p className="help-card">{t("help.tenants")}</p>

      <div className="card form-row">
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
        <button disabled={!name || create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? t("tenants.creating") : t("tenants.create")}
        </button>
      </div>
      {create.isError && <p className="error">{String(create.error)}</p>}

      {tenants.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : tenants.isError ? (
        <p className="error">{String(tenants.error)}</p>
      ) : (
        <table className="card">
          <thead>
            <tr>
              <th>{t("common.id")}</th>
              <th>{t("common.name")}</th>
              <th>{t("tenants.mode")}</th>
              <th>{t("tenants.product")}</th>
              <th>{t("common.status")}</th>
            </tr>
          </thead>
          <tbody>
            {tenants.data?.map((tn) => (
              <tr key={tn.id}>
                <td>
                  <code>{tn.id}</code>
                </td>
                <td>{tn.name}</td>
                <td>{tn.mode}</td>
                <td>
                  {tn.apim_product_ids.length > 0 ? (
                    <code>{tn.apim_product_ids[0]}</code>
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
