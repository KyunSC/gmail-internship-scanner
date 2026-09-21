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
let pending: { promise: Promise<string>; reject: (e: Error) => void } | null = null;
let generation = 0;

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

/** True if a client_id was configured at build time. */
export function isConfigured(): boolean {
  return GOOGLE_CLIENT_ID.length > 0;
}

/** True if we hold an access token that is still valid (60s safety margin). */
export function hasValidToken(): boolean {
  return !!accessToken && Date.now() < tokenExpiry - 60_000;
}

function requestToken(prompt: "" | "none" | "consent"): Promise<string> {
  if (pending) return pending.promise;
  const epoch = generation;
  let resolve!: (token: string) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<string>((ok, fail) => { resolve = ok; reject = fail; });
  const request = { promise, reject };
  pending = request;
  const fail = (error: Error) => {
    if (pending !== request) return;
    pending = null;
    reject(error);
  };
  void (async () => {
    try {
      if (!isConfigured()) throw new Error("Missing NEXT_PUBLIC_GOOGLE_CLIENT_ID");
      await loadGis();
      if (generation !== epoch || pending !== request) return;
      const oauth2 = window.google?.accounts?.oauth2;
      if (!oauth2) throw new Error("Google Identity Services unavailable");
      tokenClient = oauth2.initTokenClient({
        client_id: GOOGLE_CLIENT_ID,
        scope: GMAIL_READONLY,
        callback: (resp) => {
          if (generation !== epoch || pending !== request) return;
          if (resp.error || !resp.access_token) {
            fail(new Error(resp.error_description || resp.error || "Authorization failed"));
            return;
          }
          accessToken = resp.access_token;
          tokenExpiry = Date.now() + Number(resp.expires_in ?? 3600) * 1000;
          pending = null;
          resolve(accessToken);
        },
        error_callback: (err) => fail(new Error(err.message || "Authorization popup closed or blocked")),
      });
      tokenClient.requestAccessToken({ prompt });
    } catch (error) {
      gisLoaded = null;
      fail(error instanceof Error ? error : new Error(String(error)));
    }
  })();
  return promise;
}

// Requests and scans use this to reject work from a previous signed-in session.
export function authGeneration(): number { return generation; }

/** Interactive sign-in (shows the Google account/consent popup as needed). */
export function signIn(): Promise<string> {
  return requestToken("");
}

/**
 * Return a valid access token, reusing the cached one when possible and
 * silently refreshing otherwise. On failure all callers reject together; the
 * user can sign in again and subsequent requests can retry.
 */
export function getAccessToken(): Promise<string> {
  if (hasValidToken()) return Promise.resolve(accessToken as string);
  return requestToken("none");
}

/** Revoke the current token and clear local state. */
export function signOut(): void {
  const revokedToken = accessToken;
  generation += 1;
  pending?.reject(new Error("Signed out"));
  pending = null;
  tokenClient = null;
  accessToken = null;
  tokenExpiry = 0;
  // Local sign-out must complete even if revocation is unavailable.
  const oauth2 = window.google?.accounts?.oauth2;
  if (revokedToken && oauth2) {
    try { oauth2.revoke(revokedToken); } catch { /* already signed out locally */ }
  }
}
