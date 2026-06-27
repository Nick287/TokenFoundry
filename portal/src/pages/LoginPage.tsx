import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/AuthProvider";

export function LoginPage() {
  const { login } = useAuth();
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(username, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("login.failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <h1>🔨 {t("brand")}</h1>
      <p>{t("tagline")}</p>
      <form onSubmit={onSubmit} className="login-form">
        <label>
          {t("login.username")}
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label>
          {t("login.password")}
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? t("login.signingIn") : t("login.signIn")}
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  );
}
