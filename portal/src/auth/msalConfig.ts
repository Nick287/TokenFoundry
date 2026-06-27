// MSAL configuration — dual identity sources per the plan.
//
//   admin     -> Microsoft Entra ID           (platform operators)
//   customer  -> Microsoft Entra External ID   (CIAM, tenant users)
//
// Both feed a single SPA; the role + tenant come from the token claims and are
// re-enforced server-side. Env vars are injected at build time (Vite import.meta.env).

import type { Configuration } from "@azure/msal-browser";

const adminAuthority = `https://login.microsoftonline.com/${
  import.meta.env.VITE_ENTRA_TENANT_ID ?? "common"
}`;

const customerAuthority =
  import.meta.env.VITE_EXTERNAL_ID_AUTHORITY ?? adminAuthority;

export type Persona = "admin" | "customer";

export function msalConfig(persona: Persona): Configuration {
  return {
    auth: {
      clientId: import.meta.env.VITE_SPA_CLIENT_ID ?? "00000000-0000-0000-0000-000000000000",
      authority: persona === "admin" ? adminAuthority : customerAuthority,
      redirectUri: window.location.origin,
    },
    cache: {
      cacheLocation: "sessionStorage",
      storeAuthStateInCookie: false,
    },
  };
}

export const apiScopes: string[] = [
  import.meta.env.VITE_API_SCOPE ?? "api://token-foundry/.default",
];
