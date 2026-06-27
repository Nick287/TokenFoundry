import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { UsageCard } from "./UsageCard";

// Admin cross-tenant usage view — reads any tenant by explicit id.
export function UsageDashboardPage() {
  const principal = usePrincipal()!;
  const { t } = useTranslation();
  const [tenantId, setTenantId] = useState("");

  const usage = useQuery({
    queryKey: ["admin-usage", tenantId],
    queryFn: () => api.tenantUsage(principal.token, tenantId),
    enabled: tenantId.length > 0,
  });

  return (
    <section>
      <h2>{t("usage.title")}</h2>
      <p className="help-card">{t("help.usage")}</p>
      <div className="card form-row">
        <input
          placeholder={t("usage.tenantId")}
          value={tenantId}
          onChange={(e) => setTenantId(e.target.value)}
        />
      </div>
      {usage.isLoading && tenantId && <p>{t("common.loading")}</p>}
      {usage.isError && <p className="error">{String(usage.error)}</p>}
      {usage.data && <UsageCard usage={usage.data} />}
    </section>
  );
}
