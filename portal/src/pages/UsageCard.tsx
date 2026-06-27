import { useTranslation } from "react-i18next";

import type { UsageSummary } from "../api/client";

export function UsageCard({ usage }: { usage: UsageSummary }) {
  const { t } = useTranslation();
  return (
    <div className="card metrics">
      <div className="metric">
        <span className="metric-label">{t("usage.promptTokens")}</span>
        <span className="metric-value">{usage.total_prompt_tok.toLocaleString()}</span>
      </div>
      <div className="metric">
        <span className="metric-label">{t("usage.completionTokens")}</span>
        <span className="metric-value">
          {usage.total_completion_tok.toLocaleString()}
        </span>
      </div>
      <div className="metric">
        <span className="metric-label">{t("usage.cost")}</span>
        <span className="metric-value">${usage.total_cost_usd.toFixed(2)}</span>
      </div>
      <div className="metric">
        <span className="metric-label">{t("usage.billed")}</span>
        <span className="metric-value">${usage.total_billed_usd.toFixed(2)}</span>
      </div>
    </div>
  );
}
