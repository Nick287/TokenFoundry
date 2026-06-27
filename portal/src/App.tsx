// App shell + RBAC routing.
//
// One frontend, two personas. Admin sees the operator console (tenants,
// projects, keys, model routes); customers see only their own usage. Route
// guards reflect the role in the token — but they are NOT the security
// boundary: the backend re-enforces tenant isolation on every request.

import { useTranslation } from "react-i18next";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { LANG_KEY } from "./i18n";
import { useAuth, usePrincipal } from "./auth/AuthProvider";
import { CustomerUsagePage } from "./pages/CustomerUsagePage";
import { KeysPage } from "./pages/KeysPage";
import { LoginPage } from "./pages/LoginPage";
import { ModelRoutesPage } from "./pages/ModelRoutesPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { TenantsPage } from "./pages/TenantsPage";
import { UsageDashboardPage } from "./pages/UsageDashboardPage";
import { UsersPage } from "./pages/UsersPage";

function LanguageSwitcher() {
  const { i18n } = useTranslation();
  const set = (lng: string) => {
    i18n.changeLanguage(lng);
    localStorage.setItem(LANG_KEY, lng);
  };
  return (
    <div className="lang-switch">
      <button
        type="button"
        className={i18n.resolvedLanguage === "en" ? "active" : ""}
        onClick={() => set("en")}
      >
        EN
      </button>
      <button
        type="button"
        className={i18n.resolvedLanguage === "zh" ? "active" : ""}
        onClick={() => set("zh")}
      >
        中文
      </button>
    </div>
  );
}

export function App() {
  const principal = usePrincipal();
  const { logout } = useAuth();
  const { t } = useTranslation();

  if (!principal) return <LoginPage />;

  const isAdmin = principal.role === "admin";

  return (
    <div className="layout">
      <header className="topbar">
        <span className="brand">🔨 {t("brand")}</span>
        <nav>
          {isAdmin ? (
            <>
              <NavLink to="/tenants">{t("nav.tenants")}</NavLink>
              <NavLink to="/projects">{t("nav.projects")}</NavLink>
              <NavLink to="/keys">{t("nav.keys")}</NavLink>
              <NavLink to="/routes">{t("nav.models")}</NavLink>
              <NavLink to="/usage">{t("nav.usage")}</NavLink>
              <NavLink to="/users">{t("nav.users")}</NavLink>
            </>
          ) : (
            <>
              <NavLink to="/me">{t("nav.myUsage")}</NavLink>
              <NavLink to="/account">{t("nav.myAccount")}</NavLink>
            </>
          )}
        </nav>
        <LanguageSwitcher />
        <span className="who">
          {principal.role}
          {principal.tenantId ? ` · ${principal.tenantId}` : ""}
          <button type="button" className="logout" onClick={logout}>
            {t("nav.signOut")}
          </button>
        </span>
      </header>

      <main className="content">
        <Routes>
          {isAdmin ? (
            <>
              <Route path="/tenants" element={<TenantsPage />} />
              <Route path="/projects" element={<ProjectsPage />} />
              <Route path="/keys" element={<KeysPage />} />
              <Route path="/routes" element={<ModelRoutesPage />} />
              <Route path="/usage" element={<UsageDashboardPage />} />
              <Route path="/users" element={<UsersPage />} />
              <Route path="*" element={<Navigate to="/tenants" replace />} />
            </>
          ) : (
            <>
              <Route path="/me" element={<CustomerUsagePage />} />
              <Route path="/account" element={<UsersPage />} />
              <Route path="*" element={<Navigate to="/me" replace />} />
            </>
          )}
        </Routes>
      </main>
    </div>
  );
}
