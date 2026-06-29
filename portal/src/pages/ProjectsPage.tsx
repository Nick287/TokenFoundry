import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type Project } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { ConfirmDialog, Modal } from "../components/Modal";
import { CopyId } from "../components/CopyId";
import { useToast } from "../components/Toast";

// Projects live under a tenant and carry the pj_ id used to issue virtual keys.
export function ProjectsPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const toast = useToast();
  const [tenantId, setTenantId] = useState("");
  const [name, setName] = useState("");
  const [costCenter, setCostCenter] = useState("");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null);
  const [removing, setRemoving] = useState<Project | null>(null);

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
      setAdding(false);
      toast(t("common.created"));
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const save = useMutation({
    mutationFn: (vars: { id: string; body: { name?: string; cost_center?: string | null } }) =>
      api.updateProject(principal.token, vars.id, vars.body),
    onSuccess: () => {
      setEditing(null);
      toast(t("common.saved"));
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteProject(principal.token, id),
    onSuccess: () => {
      setRemoving(null);
      toast(t("common.deleted"));
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

      <div className="list-toolbar">
        <button type="button" className="add-toggle" onClick={() => setAdding((v) => !v)}>
          {adding ? t("common.close") : `+ ${t("projects.addNew")}`}
        </button>
      </div>

      {adding && (
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
      )}
      {create.isError && <p className="error">{String(create.error)}</p>}

      {projects.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : projects.data && projects.data.length === 0 ? (
        <div className="card empty-cta">
          <strong>{t("projects.emptyTitle")}</strong>
          {t("projects.emptyHint")}
        </div>
      ) : (
        <div className="table-scroll">
        <table className="card">
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("common.id")}</th>
              <th>{t("projects.tenant")}</th>
              <th>{t("projects.costCenterCol")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {projects.data?.map((p) => {
              const tn = tenants.data?.find((x) => x.id === p.tenant_id);
              return (
                <tr key={p.id}>
                  <td>{p.name}</td>
                  <td>
                    <CopyId value={p.id} />
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
                  <td className="row-actions">
                    <button type="button" className="btn-sm" onClick={() => setEditing(p)}>
                      {t("common.edit")}
                    </button>
                    <button
                      type="button"
                      className="btn-sm btn-danger"
                      onClick={() => setRemoving(p)}
                    >
                      {t("common.delete")}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        </div>
      )}

      {editing && (
        <EditProjectModal
          project={editing}
          busy={save.isPending}
          onClose={() => setEditing(null)}
          onSave={(body) => save.mutate({ id: editing.id, body })}
        />
      )}
      {removing && (
        <ConfirmDialog
          title={t("projects.deleteTitle")}
          impact={
            <>
              {t("projects.deleteImpact", { name: removing.name })} {t("common.cannotUndo")}
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

function EditProjectModal({
  project,
  busy,
  onClose,
  onSave,
}: {
  project: Project;
  busy: boolean;
  onClose: () => void;
  onSave: (body: { name: string; cost_center: string | null }) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(project.name);
  const [cc, setCc] = useState(project.cost_center ?? "");
  return (
    <Modal title={t("projects.editTitle")} onClose={onClose}>
      <div className="modal-form">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("projects.namePlaceholder")} />
        <input value={cc} onChange={(e) => setCc(e.target.value)} placeholder={t("projects.costCenter")} />
      </div>
      <div className="modal-actions">
        <button type="button" className="btn-sm" onClick={onClose} disabled={busy}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          disabled={!name || busy}
          onClick={() => onSave({ name, cost_center: cc || null })}
        >
          {busy ? t("common.saving") : t("common.save")}
        </button>
      </div>
    </Modal>
  );
}
