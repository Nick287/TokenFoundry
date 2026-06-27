// Auth context: database-backed login (no Entra).
//
// login(username, password) calls POST /api/login, stores the issued JWT +
// role + tenant in localStorage, and exposes the principal to the app. The
// backend verifies that JWT on every request. A VITE_DEV_TOKEN of form
// "dev:<role>:<tenant>" still short-circuits for local dev without a backend.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { api } from "../api/client";

export type Role = "admin" | "customer";

export interface Principal {
  username: string;
  role: Role;
  tenantId: string | null;
  token: string;
}

interface AuthContextValue {
  principal: Principal | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);
const STORAGE_KEY = "tf_auth";

function decodeDevToken(token: string): Principal | null {
  if (!token.startsWith("dev:")) return null;
  const [, role, tenant] = token.split(":");
  return { username: "dev", role: role as Role, tenantId: tenant || null, token };
}

function loadStored(): Principal | null {
  // Dev token (build-time) wins for local end-to-end without a backend.
  const devToken = import.meta.env.VITE_DEV_TOKEN as string | undefined;
  if (devToken) return decodeDevToken(devToken);
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Principal) : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [principal, setPrincipal] = useState<Principal | null>(loadStored);

  const login = useCallback(async (username: string, password: string) => {
    const res = await api.login(username, password);
    const next: Principal = {
      username,
      role: res.role as Role,
      tenantId: res.tenant_id ?? null,
      token: res.access_token,
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    setPrincipal(next);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setPrincipal(null);
  }, []);

  const value = useMemo(
    () => ({ principal, login, logout }),
    [principal, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

function useAuthContext(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

export function usePrincipal(): Principal | null {
  return useAuthContext().principal;
}

export function useAuth(): AuthContextValue {
  return useAuthContext();
}
