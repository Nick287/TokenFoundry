import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { api } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { UsageCard } from "./UsageCard";

// Customer self-service: usage for the CALLER's own tenant only. The tenant is
// derived server-side from the token — this page never sends a tenant id.
export function CustomerUsagePage() {
  const principal = usePrincipal()!;
  const { t } = useTranslation();

  const usage = useQuery({
    queryKey: ["my-usage"],
    queryFn: () => api.myUsage(principal.token),
  });

  return (
    <section>
      <h2>{t("usage.myTitle")}</h2>
      <p className="help-card">{t("help.myUsage")}</p>
      <p className="hint">
        {t("usage.tenant")}: {principal.tenantId ?? "—"}
      </p>
      {usage.isLoading && <p>{t("common.loading")}</p>}
      {usage.isError && <p className="error">{String(usage.error)}</p>}
      {usage.data && <UsageCard usage={usage.data} />}
    </section>
  );
}
