// Browser-only Google OAuth via Google Identity Services (GIS) token client.
//
// There is no backend: the token never leaves the user's browser. The OAuth
// client_id is *meant* to be public (no client secret is used in this flow).
// Scope is gmail.readonly ONLY — the app can fetch + analyze but can never
// modify the mailbox.
//
// Set NEXT_PUBLIC_GOOGLE_CLIENT_ID at build time (see web/.env.local.example).

const GIS_SRC = "https://accounts.google.com/gsi/client";
const GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly";

export const GOOGLE_CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "";

// ── Minimal typings for the GIS oauth2 token client ──────────────────────────
type TokenResponse = {
  access_token?: string;
  expires_in?: number | string;
  error?: string;
  error_description?: string;
};

type TokenClient = {
  requestAccessToken: (opts?: { prompt?: "" | "none" | "consent" }) => void;
};

type GisOAuth2 = {
  initTokenClient: (config: {
    client_id: string;
    scope: string;
    callback: (resp: TokenResponse) => void;
    error_callback?: (err: { type?: string; message?: string }) => void;
  }) => TokenClient;
  revoke: (token: string, done?: () => void) => void;
};

declare global {
  interface Window {
    google?: { accounts?: { oauth2?: GisOAuth2 } };
  }
}

// ── Module state (in-memory only; cleared on tab close) ──────────────────────
let tokenClient: TokenClient | null = null;
let accessToken: string | null = null;
let tokenExpiry = 0; // epoch ms
let gisLoaded: Promise<void> | null = null;
let pending: { resolve: (t: string) => void; reject: (e: Error) => void } | null = null;

function loadGis(): Promise<void> {
  if (gisLoaded) return gisLoaded;
  gisLoaded = new Promise<void>((resolve, reject) => {
    if (typeof window === "undefined") return reject(new Error("Not in a browser"));
    if (window.google?.accounts?.oauth2) return resolve();
    const s = document.createElement("script");
    s.src = GIS_SRC;
    s.async = true;
    s.defer = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("Failed to load Google Identity Services"));
    document.head.appendChild(s);
  });
  return gisLoaded;
}

async function ensureClient(): Promise<TokenClient> {
  await loadGis();
  const oauth2 = window.google?.accounts?.oauth2;
  if (!oauth2) throw new Error("Google Identity Services unavailable");
  if (!tokenClient) {
    tokenClient = oauth2.initTokenClient({
      client_id: GOOGLE_CLIENT_ID,
      scope: GMAIL_READONLY,
      callback: (resp: TokenResponse) => {
        const p = pending;
        pending = null;
        if (resp.error || !resp.access_token) {
          p?.reject(new Error(resp.error_description || resp.error || "Authorization failed"));
          return;
        }
        accessToken = resp.access_token;
        tokenExpiry = Date.now() + Number(resp.expires_in ?? 3600) * 1000;
        p?.resolve(accessToken);
      },
      error_callback: (err) => {
        const p = pending;
        pending = null;
        p?.reject(new Error(err?.message || "Authorization popup closed or blocked"));
      },
    });
  }
  return tokenClient;
}

/** True if a client_id was configured at build time. */
export function isConfigured(): boolean {
  return GOOGLE_CLIENT_ID.length > 0;
}

/** True if we hold an access token that is still valid (60s safety margin). */
export function hasValidToken(): boolean {
  return !!accessToken && Date.now() < tokenExpiry - 60_000;
}

function requestToken(prompt: "" | "none" | "consent"): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    if (!isConfigured()) {
      reject(new Error("Missing NEXT_PUBLIC_GOOGLE_CLIENT_ID — see web/.env.local.example"));
      return;
    }
    ensureClient()
      .then((client) => {
        pending = { resolve, reject };
        client.requestAccessToken({ prompt });
      })
      .catch(reject);
  });
}

/** Interactive sign-in (shows the Google account/consent popup as needed). */
export function signIn(): Promise<string> {
  return requestToken("");
}

/**
 * Return a valid access token, reusing the cached one when possible and
 * silently refreshing otherwise. Falls back to an interactive prompt if the
 * silent refresh fails (e.g. first call of the session).
 */
export async function getAccessToken(): Promise<string> {
  if (hasValidToken()) return accessToken as string;
  try {
    return await requestToken("none");
  } catch {
    return requestToken("");
  }
}

/** Revoke the current token and clear local state. */
export function signOut(): void {
  const oauth2 = window.google?.accounts?.oauth2;
  if (accessToken && oauth2) oauth2.revoke(accessToken);
  accessToken = null;
  tokenExpiry = 0;
}
