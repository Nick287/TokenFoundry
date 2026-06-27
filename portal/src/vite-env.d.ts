/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_API_TARGET?: string;
  readonly VITE_DEV_TOKEN?: string;
  readonly VITE_ENTRA_TENANT_ID?: string;
  readonly VITE_EXTERNAL_ID_AUTHORITY?: string;
  readonly VITE_SPA_CLIENT_ID?: string;
  readonly VITE_API_SCOPE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
