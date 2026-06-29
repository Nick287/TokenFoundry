import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ModelRoute } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { ConfirmDialog, Modal } from "../components/Modal";
import { useToast } from "../components/Toast";

// Self-service model onboarding: add Claude / Gemini / Kimi / DeepSeek as a
// client-facing alias. With 32+ routes the list is the hero — search + filter
// on top, the create form tucked behind +Add so it doesn't own the screen.
export function ModelRoutesPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
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
  const [editing, setEditing] = useState<ModelRoute | null>(null);
  const [removing, setRemoving] = useState<ModelRoute | null>(null);

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
      setForm({ ...form, name: "", backend_url: "", backend_secret: "", price_in_per_1k: "", price_out_per_1k: "", markup_pct: "" });
      setAdding(false);
      toast(t("common.created"));
      qc.invalidateQueries({ queryKey: ["routes"] });
    },
  });

  const save = useMutation({
    mutationFn: (vars: { id: string; body: Record<string, unknown> }) =>
      api.updateRoute(principal.token, vars.id, vars.body),
    onSuccess: () => {
      setEditing(null);
      toast(t("common.saved"));
      qc.invalidateQueries({ queryKey: ["routes"] });
    },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteRoute(principal.token, id),
    onSuccess: () => {
      setRemoving(null);
      toast(t("common.deleted"));
      qc.invalidateQueries({ queryKey: ["routes"] });
    },
  });

  const upd = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!form.name || create.isPending) return;
    create.mutate();
  }

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (routes.data ?? []).filter(
      (r) =>
        (!q || r.name.toLowerCase().includes(q)) &&
        (!providerFilter || r.provider === providerFilter),
    );
  }, [routes.data, query, providerFilter]);

  return (
    <section>
      <h2>{t("models.title")}</h2>
      <p className="help-card">{t("help.models")}</p>

      <div className="list-toolbar">
        <input
          placeholder={t("models.searchPlaceholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)}>
          <option value="">{t("models.allProviders")}</option>
          <option value="anthropic">Anthropic</option>
          <option value="openai">OpenAI</option>
          <option value="google">Google</option>
        </select>
        <button type="button" className="add-toggle" onClick={() => setAdding((v) => !v)}>
          {adding ? t("common.close") : `+ ${t("models.addNew")}`}
        </button>
        <span className="count">
          {filtered.length === (routes.data?.length ?? 0)
            ? filtered.length
            : `${filtered.length} / ${routes.data?.length ?? 0}`}
        </span>
      </div>

      {adding && (
        <form className="card form-grid" onSubmit={onSubmit}>
          <input placeholder={t("models.alias")} value={form.name} onChange={upd("name")} />
          <select value={form.provider} onChange={upd("provider")}>
            <option value="anthropic">Anthropic (Claude)</option>
            <option value="openai">OpenAI-compatible (Kimi / DeepSeek / AOAI)</option>
            <option value="google">Google (Gemini, OpenAI-compat)</option>
          </select>
          <input placeholder={t("models.backendUrl")} value={form.backend_url} onChange={upd("backend_url")} />
          <input placeholder={t("models.backendKey")} value={form.backend_secret} onChange={upd("backend_secret")} type="password" />
          <input placeholder={t("models.priceIn")} value={form.price_in_per_1k} onChange={upd("price_in_per_1k")} />
          <input placeholder={t("models.priceOut")} value={form.price_out_per_1k} onChange={upd("price_out_per_1k")} />
          <input placeholder={t("models.markup")} value={form.markup_pct} onChange={upd("markup_pct")} />
          <button type="submit" disabled={!form.name || create.isPending}>
            {create.isPending ? t("models.adding") : t("models.add")}
          </button>
        </form>
      )}
      {create.isError && <p className="error">{String(create.error)}</p>}

      {routes.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : routes.data && routes.data.length === 0 ? (
        <div className="card empty-cta">
          <strong>{t("models.emptyTitle")}</strong>
          {t("models.emptyHint")}
        </div>
      ) : (
        <div className="table-scroll">
          <table className="card">
            <thead>
              <tr>
                <th>{t("models.aliasCol")}</th>
                <th>{t("models.provider")}</th>
                <th>{t("models.scope")}</th>
                <th>{t("models.priceInCol")}</th>
                <th>{t("models.priceOutCol")}</th>
                <th>{t("models.markupCol")}</th>
                <th>{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr><td colSpan={7} className="hint">{t("models.noMatch")}</td></tr>
              )}
              {filtered.map((r) => (
                <tr key={r.id}>
                  <td><code>{r.name}</code></td>
                  <td>{r.provider}</td>
                  <td>{r.owner_scope}</td>
                  <td>{r.price_in_per_1k ? `$${r.price_in_per_1k}` : <span className="cell-zero">—</span>}</td>
                  <td>{r.price_out_per_1k ? `$${r.price_out_per_1k}` : <span className="cell-zero">—</span>}</td>
                  <td>{r.markup_pct ? `${(r.markup_pct * 100).toFixed(0)}%` : <span className="cell-zero">0%</span>}</td>
                  <td className="row-actions">
                    <button type="button" className="btn-sm" onClick={() => setEditing(r)}>{t("common.edit")}</button>
                    <button type="button" className="btn-sm btn-danger" onClick={() => setRemoving(r)}>{t("common.delete")}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <EditRouteModal route={editing} busy={save.isPending} onClose={() => setEditing(null)} onSave={(body) => save.mutate({ id: editing.id, body })} />
      )}
      {removing && (
        <ConfirmDialog
          title={t("models.deleteTitle")}
          impact={<>{t("models.deleteImpact", { name: removing.name })} {t("common.cannotUndo")}</>}
          busy={del.isPending}
          onConfirm={() => del.mutate(removing.id)}
          onClose={() => setRemoving(null)}
        />
      )}
    </section>
  );
}

function EditRouteModal({ route, busy, onClose, onSave }: {
  route: ModelRoute; busy: boolean; onClose: () => void; onSave: (body: Record<string, unknown>) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(route.name);
  const [markup, setMarkup] = useState(String(route.markup_pct));
  return (
    <Modal title={t("models.editTitle")} onClose={onClose}>
      <div className="modal-form">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("models.alias")} />
        <input value={markup} onChange={(e) => setMarkup(e.target.value)} placeholder={t("models.markup")} />
      </div>
      <div className="modal-actions">
        <button type="button" className="btn-sm" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
        <button type="button" disabled={!name || busy} onClick={() => onSave({ name, markup_pct: Number(markup) || 0 })}>
          {busy ? t("common.saving") : t("common.save")}
        </button>
      </div>
    </Modal>
  );
}
