import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type Tenant } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { ConfirmDialog, Modal } from "../components/Modal";
import { CopyId } from "../components/CopyId";
import { useToast } from "../components/Toast";

export function TenantsPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const toast = useToast();
  const [name, setName] = useState("");
  const [mode, setMode] = useState("RESELL");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Tenant | null>(null);
  const [removing, setRemoving] = useState<Tenant | null>(null);
  // The tenant whose archival switch is being flipped, if any. Deliberately a
  // separate confirm rather than a checkbox inside the edit form: this one
  // decides whether the customer's prompts and source code start landing on
  // disk, and it should not be reachable by the same reflex as a rename.
  const [auditing, setAuditing] = useState<Tenant | null>(null);

  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api.listTenants(principal.token),
  });

  const create = useMutation({
    mutationFn: () => api.createTenant(principal.token, { name, mode }),
    onSuccess: () => {
      setName("");
      setMode("RESELL");
      setAdding(false);
      toast(t("common.created"));
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
  });

  const bindProduct = useMutation({
    mutationFn: (id: string) => api.ensureTenantProduct(principal.token, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tenants"] }),
  });

  const save = useMutation({
    mutationFn: (vars: { id: string; body: { name?: string; mode?: string } }) =>
      api.updateTenant(principal.token, vars.id, vars.body),
    onSuccess: () => {
      setEditing(null);
      toast(t("common.saved"));
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteTenant(principal.token, id),
    onSuccess: () => {
      setRemoving(null);
      toast(t("common.deleted"));
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
  });

  // Separate from `save` because the failure mode is different: the control
  // plane pushes the flag to APIM first and only records it if that succeeds,
  // so a 502 here means archival did NOT change. That has to be visible —
  // silently leaving the switch where it was is how an operator ends up
  // believing a tenant is being audited when nothing is being captured.
  const toggleAudit = useMutation({
    mutationFn: (vars: { id: string; enabled: boolean }) =>
      api.updateTenant(principal.token, vars.id, { audit_enabled: vars.enabled }),
    onSuccess: (_data, vars) => {
      setAuditing(null);
      toast(vars.enabled ? t("tenants.auditOnDone") : t("tenants.auditOffDone"));
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
    onError: () => {
      setAuditing(null);
      toast(t("tenants.auditFailed"), "error");
    },
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

      <div className="list-toolbar">
        <button type="button" className="add-toggle" onClick={() => setAdding((v) => !v)}>
          {adding ? t("common.close") : `+ ${t("tenants.addNew")}`}
        </button>
      </div>

      {adding && (
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
      )}
      {create.isError && <p className="error">{String(create.error)}</p>}
      {/* Persistent, unlike the toast: a gateway rejection is worth reading
          after the 3-second window has gone. */}
      {toggleAudit.isError && <p className="error">{String(toggleAudit.error)}</p>}

      {tenants.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : tenants.isError ? (
        <p className="error">{String(tenants.error)}</p>
      ) : tenants.data && tenants.data.length === 0 ? (
        <div className="card empty-cta">
          <strong>{t("tenants.emptyTitle")}</strong>
          {t("tenants.emptyHint")}
        </div>
      ) : (
        <div className="table-scroll">
        <table className="card">
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("common.id")}</th>
              <th>{t("tenants.mode")}</th>
              <th>{t("tenants.product")}</th>
              <th>{t("tenants.audit")}</th>
              <th>{t("common.status")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {tenants.data?.map((tn) => (
              <tr key={tn.id}>
                <td>{tn.name}</td>
                <td>
                  <CopyId value={tn.id} />
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
                  <span
                    className={`badge badge-${tn.audit_enabled ? "audit-on" : "off"}`}
                  >
                    {tn.audit_enabled ? t("tenants.auditOn") : t("tenants.auditOff")}
                  </span>
                </td>
                <td>
                  <span className={`badge badge-${tn.status}`}>{tn.status}</span>
                </td>
                <td className="row-actions">
                  <button type="button" className="btn-sm" onClick={() => setEditing(tn)}>
                    {t("common.edit")}
                  </button>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={toggleAudit.isPending}
                    onClick={() => setAuditing(tn)}
                  >
                    {tn.audit_enabled
                      ? t("tenants.auditDisable")
                      : t("tenants.auditEnable")}
                  </button>
                  <button
                    type="button"
                    className="btn-sm btn-danger"
                    onClick={() => setRemoving(tn)}
                  >
                    {t("common.delete")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}

      {editing && (
        <EditTenantModal
          tenant={editing}
          busy={save.isPending}
          onClose={() => setEditing(null)}
          onSave={(body) => save.mutate({ id: editing.id, body })}
        />
      )}
      {auditing && (
        <ConfirmDialog
          title={
            auditing.audit_enabled
              ? t("tenants.auditOffTitle")
              : t("tenants.auditOnTitle")
          }
          impact={
            auditing.audit_enabled ? (
              t("tenants.auditOffImpact", { name: auditing.name })
            ) : (
              <>
                {t("tenants.auditOnImpact", { name: auditing.name })}{" "}
                <strong>{t("tenants.auditOnWarning")}</strong>
              </>
            )
          }
          busy={toggleAudit.isPending}
          confirmLabel={
            auditing.audit_enabled
              ? t("tenants.auditDisable")
              : t("tenants.auditEnable")
          }
          busyLabel={t("common.saving")}
          // Solid-danger only in the direction that starts writing customer
          // content to disk. Switching archival off needs no alarm colour.
          danger={!auditing.audit_enabled}
          onConfirm={() =>
            toggleAudit.mutate({
              id: auditing.id,
              enabled: !auditing.audit_enabled,
            })
          }
          onClose={() => setAuditing(null)}
        />
      )}
      {removing && (
        <ConfirmDialog
          title={t("tenants.deleteTitle")}
          impact={
            <>
              {t("tenants.deleteImpact", { name: removing.name })} {t("common.cannotUndo")}
            </>
          }
          busy={del.isPending}
          onConfirm={() => del.mutate(removing.id)}
          onClose={() => setRemoving(null)}
        />
      )}
    </section>
  );
}

function EditTenantModal({
  tenant,
  busy,
  onClose,
  onSave,
}: {
  tenant: Tenant;
  busy: boolean;
  onClose: () => void;
  onSave: (body: { name: string; mode: string }) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(tenant.name);
  const [mode, setMode] = useState<string>(tenant.mode);
  return (
    <Modal title={t("tenants.editTitle")} onClose={onClose}>
      <div className="modal-form">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("tenants.namePlaceholder")} />
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="RESELL">{t("tenants.modeResell")}</option>
          <option value="BYO">{t("tenants.modeByo")}</option>
          <option value="INTERNAL">{t("tenants.modeInternal")}</option>
        </select>
      </div>
      <div className="modal-actions">
        <button type="button" className="btn-sm" onClick={onClose} disabled={busy}>
          {t("common.cancel")}
        </button>
        <button type="button" disabled={!name || busy} onClick={() => onSave({ name, mode })}>
          {busy ? t("common.saving") : t("common.save")}
        </button>
      </div>
    </Modal>
  );
}
