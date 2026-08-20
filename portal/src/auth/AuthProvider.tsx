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
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { UNAUTHORIZED_EVENT, api } from "../api/client";

export type Role = "admin" | "customer";

export interface Principal {
  username: string;
  role: Role;
  tenantId: string | null;
  token: string;
}

interface AuthContextValue {
  principal: Principal | null;
  /** True when we were signed in and the session ran out, so the login page can
   *  say why it is showing rather than looking like a random sign-out. */
  sessionExpired: boolean;
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

/**
 * True when a JWT's `exp` has already passed.
 *
 * Checked at load so an overnight-expired token sends you straight to the login
 * page. Without it the stored principal still looks valid, App renders the
 * whole dashboard, and only once each request comes back 401 does anything
 * happen — which is exactly the "I can click around but nothing loads" state.
 *
 * Unparseable is treated as NOT expired: the server is the authority on a
 * token's validity, and a decoding quirk here must not lock anyone out. A
 * genuinely bad token still gets rejected on the first call.
 */
function isExpired(token: string): boolean {
  const parts = token.split(".");
  if (parts.length !== 3) return false; // dev token or opaque — let the server judge
  try {
    const payload = JSON.parse(
      atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")),
    ) as { exp?: number };
    return typeof payload.exp === "number" && payload.exp * 1000 <= Date.now();
  } catch {
    return false;
  }
}

function loadStored(): Principal | null {
  // Dev token (build-time) wins for local end-to-end without a backend.
  const devToken = import.meta.env.VITE_DEV_TOKEN as string | undefined;
  if (devToken) return decodeDevToken(devToken);
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Principal;
    if (isExpired(stored.token)) {
      localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return stored;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [principal, setPrincipal] = useState<Principal | null>(loadStored);
  const [sessionExpired, setSessionExpired] = useState(false);

  const login = useCallback(async (username: string, password: string) => {
    const res = await api.login(username, password);
    const next: Principal = {
      username,
      role: res.role as Role,
      tenantId: res.tenant_id ?? null,
      token: res.access_token,
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    setSessionExpired(false);
    setPrincipal(next);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setSessionExpired(false);
    setPrincipal(null);
  }, []);

  // Any 401 from the API means this token is no longer accepted — drop it and
  // fall back to the login page (App renders LoginPage whenever principal is
  // null). Covers the case the load-time check cannot: a session that expires
  // while the tab is open.
  useEffect(() => {
    const onUnauthorized = () => {
      localStorage.removeItem(STORAGE_KEY);
      // Only call it "expired" if we thought we were signed in; a 401 with no
      // principal is just a failed login and needs no extra explanation.
      setPrincipal((current) => {
        if (current) setSessionExpired(true);
        return null;
      });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const value = useMemo(
    () => ({ principal, sessionExpired, login, logout }),
    [principal, sessionExpired, login, logout],
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
