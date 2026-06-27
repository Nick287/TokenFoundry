import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";

// Self-service model onboarding: add Claude / Gemini / Kimi / DeepSeek as a
// client-facing alias. Provider determines the API format (Anthropic vs
// OpenAI-compatible); the backend wires it into the Unified Model API.
export function ModelRoutesPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const [form, setForm] = useState({
    name: "",
    provider: "anthropic",
    backend_url: "",
    backend_secret: "",
    auth_mode: "KV_SECRET",
    price_in_per_1k: "",
    price_out_per_1k: "",
    markup_pct: "",
  });

  const routes = useQuery({
    queryKey: ["routes"],
    queryFn: () => api.listRoutes(principal.token),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createRoute(principal.token, {
        name: form.name,
        provider: form.provider,
        backend_url: form.backend_url || null,
        backend_secret: form.backend_secret || null,
        auth_mode: form.auth_mode,
        price_in_per_1k: Number(form.price_in_per_1k) || 0,
        price_out_per_1k: Number(form.price_out_per_1k) || 0,
        markup_pct: Number(form.markup_pct) || 0,
      }),
    onSuccess: () => {
      setForm({ ...form, name: "", backend_url: "", backend_secret: "" });
      qc.invalidateQueries({ queryKey: ["routes"] });
    },
  });

  const upd = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value });

  return (
    <section>
      <h2>{t("models.title")}</h2>
      <p className="help-card">{t("help.models")}</p>
      <div className="card form-grid">
        <input placeholder={t("models.alias")} value={form.name} onChange={upd("name")} />
        <select value={form.provider} onChange={upd("provider")}>
          <option value="anthropic">Anthropic (Claude)</option>
          <option value="openai">OpenAI-compatible (Kimi / DeepSeek / AOAI)</option>
          <option value="google">Google (Gemini, OpenAI-compat)</option>
        </select>
        <input placeholder={t("models.backendUrl")} value={form.backend_url} onChange={upd("backend_url")} />
        <input
          placeholder={t("models.backendKey")}
          value={form.backend_secret}
          onChange={upd("backend_secret")}
          type="password"
        />
        <input placeholder={t("models.priceIn")} value={form.price_in_per_1k} onChange={upd("price_in_per_1k")} />
        <input placeholder={t("models.priceOut")} value={form.price_out_per_1k} onChange={upd("price_out_per_1k")} />
        <input placeholder={t("models.markup")} value={form.markup_pct} onChange={upd("markup_pct")} />
        <button disabled={!form.name || create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? t("models.adding") : t("models.add")}
        </button>
      </div>
      {create.isError && <p className="error">{String(create.error)}</p>}

      {routes.data && (
        <table className="card">
          <thead>
            <tr>
              <th>{t("models.aliasCol")}</th>
              <th>{t("models.provider")}</th>
              <th>{t("models.scope")}</th>
              <th>{t("models.markupCol")}</th>
            </tr>
          </thead>
          <tbody>
            {routes.data.map((r) => (
              <tr key={r.id}>
                <td>
                  <code>{r.name}</code>
                </td>
                <td>{r.provider}</td>
                <td>{r.owner_scope}</td>
                <td>{(r.markup_pct * 100).toFixed(0)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
