import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";

export function UsersPage() {
  const principal = usePrincipal()!;
  const qc = useQueryClient();
  const { t } = useTranslation();
  const isAdmin = principal.role === "admin";

  // --- create-user form (admin) ---
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("customer");
  const [tenantId, setTenantId] = useState("");

  // --- change-my-password (everyone) ---
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwMsg, setPwMsg] = useState<string | null>(null);

  const users = useQuery({
    queryKey: ["users"],
    queryFn: () => api.listUsers(principal.token),
    enabled: isAdmin,
  });
  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api.listTenants(principal.token),
    enabled: isAdmin,
  });

  const create = useMutation({
    mutationFn: () =>
      api.createUser(principal.token, {
        username,
        password,
        role,
        tenant_id: role === "customer" ? tenantId : null,
      }),
    onSuccess: () => {
      setUsername("");
      setPassword("");
      setTenantId("");
      qc.invalidateQueries({ queryKey: ["users"] });
    },
  });

  const update = useMutation({
    mutationFn: (vars: { id: string; body: Record<string, unknown> }) =>
      api.updateUser(principal.token, vars.id, vars.body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });

  const del = useMutation({
    mutationFn: (id: string) => api.deleteUser(principal.token, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });

  const changePw = useMutation({
    mutationFn: () => api.changeMyPassword(principal.token, oldPw, newPw),
    onSuccess: () => {
      setOldPw("");
      setNewPw("");
      setPwMsg(t("users.changed"));
    },
    onError: (e) => setPwMsg(String(e)),
  });

  function onReset(id: string) {
    const pw = window.prompt(t("users.resetPrompt"));
    if (pw) api.resetPassword(principal.token, id, pw).catch(() => undefined);
  }

  return (
    <section>
      <h2>{isAdmin ? t("users.title") : t("nav.myAccount")}</h2>
      <p className="help-card">{t("help.users")}</p>

      {/* Change my own password — available to everyone */}
      <div className="card">
        <h3>{t("users.myPassword")}</h3>
        <div className="form-row">
          <input
            type="password"
            placeholder={t("users.oldPassword")}
            value={oldPw}
            onChange={(e) => setOldPw(e.target.value)}
            autoComplete="current-password"
          />
          <input
            type="password"
            placeholder={t("users.newPassword")}
            value={newPw}
            onChange={(e) => setNewPw(e.target.value)}
            autoComplete="new-password"
          />
          <button
            disabled={!oldPw || !newPw || changePw.isPending}
            onClick={() => changePw.mutate()}
          >
            {t("users.change")}
          </button>
        </div>
        {pwMsg && <p className="hint">{pwMsg}</p>}
      </div>

      {/* Admin-only: create + manage users */}
      {isAdmin && (
        <>
          <div className="card form-row">
            <input
              placeholder={t("users.username")}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
            <input
              type="password"
              placeholder={t("users.password")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="customer">{t("users.roleCustomer")}</option>
              <option value="admin">{t("users.roleAdmin")}</option>
            </select>
            {role === "customer" && (
              <select value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
                <option value="">{t("users.selectTenant")}</option>
                {tenants.data?.map((tn) => (
                  <option key={tn.id} value={tn.id}>
                    {tn.name} ({tn.id})
                  </option>
                ))}
              </select>
            )}
            <button
              disabled={
                !username ||
                !password ||
                (role === "customer" && !tenantId) ||
                create.isPending
              }
              onClick={() => create.mutate()}
            >
              {create.isPending ? t("users.creating") : t("users.create")}
            </button>
          </div>
          {create.isError && <p className="error">{String(create.error)}</p>}
          {(update.isError || del.isError) && (
            <p className="error">{String(update.error || del.error)}</p>
          )}

          {users.isLoading ? (
            <p>{t("common.loading")}</p>
          ) : (
            <table className="card">
              <thead>
                <tr>
                  <th>{t("users.username")}</th>
                  <th>{t("users.role")}</th>
                  <th>{t("users.tenant")}</th>
                  <th>{t("users.status")}</th>
                  <th>{t("users.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {users.data?.map((u) => {
                  const isSelf = u.username === principal.username;
                  return (
                    <tr key={u.id}>
                      <td>
                        {u.username}
                        {isSelf ? " ·" : ""}
                      </td>
                      <td>{u.role}</td>
                      <td>{u.tenant_id ? <code>{u.tenant_id}</code> : "—"}</td>
                      <td>
                        <span className={`badge badge-${u.disabled ? "suspended" : "active"}`}>
                          {u.disabled ? t("users.disabled") : t("users.active")}
                        </span>
                      </td>
                      <td className="row-actions">
                        <button className="btn-sm" onClick={() => onReset(u.id)}>
                          {t("users.reset")}
                        </button>
                        {!isSelf && (
                          <>
                            <button
                              className="btn-sm"
                              onClick={() =>
                                update.mutate({ id: u.id, body: { disabled: !u.disabled } })
                              }
                            >
                              {u.disabled ? t("users.enable") : t("users.disable")}
                            </button>
                            <button
                              className="btn-sm"
                              onClick={() =>
                                update.mutate({
                                  id: u.id,
                                  body: {
                                    role: u.role === "admin" ? "customer" : "admin",
                                    tenant_id:
                                      u.role === "admin" ? (tenants.data?.[0]?.id ?? null) : null,
                                  },
                                })
                              }
                            >
                              {u.role === "admin" ? t("users.makeCustomer") : t("users.makeAdmin")}
                            </button>
                            <button
                              className="btn-sm btn-danger"
                              onClick={() => {
                                if (window.confirm(t("users.deleteConfirm"))) del.mutate(u.id);
                              }}
                            >
                              {t("users.delete")}
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
