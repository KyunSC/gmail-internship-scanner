// Gmail fetch + body extraction, ported from cli/scanner.py (search_emails,
// _extract_body, _normalize_body, _strip_footer, _html_to_text, run_gmail_search).
// Runs entirely in the browser against the Gmail REST API with the user's
// in-browser OAuth token — no backend.

import { getAccessToken } from "@/lib/google-auth";

export type GmailMessage = {
  id: string;
  subject: string;
  from: string;
  date: string;
  body: string;
};

const API = "https://gmail.googleapis.com/gmail/v1/users/me";
const BODY_MAX_CHARS = 5000; // mirror scanner.py BODY_MAX_CHARS

// ── base64url → UTF-8 (mirror base64.urlsafe_b64decode + errors="replace") ────
function b64urlToUtf8(data: string): string {
  try {
    const b64 = data.replace(/-/g, "+").replace(/_/g, "/");
    const pad = b64.length % 4 ? "=".repeat(4 - (b64.length % 4)) : "";
    const bin = atob(b64 + pad);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return "";
  }
}

// ── HTML → text (mirror _TextExtractor: drop script/style, join text " ") ─────
function htmlToText(html: string): string {
  try {
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("script, style").forEach((el) => el.remove());
    const parts: string[] = [];
    const walker = doc.createTreeWalker(doc.body ?? doc, NodeFilter.SHOW_TEXT);
    let n: Node | null;
    while ((n = walker.nextNode())) {
      if (n.nodeValue) parts.push(n.nodeValue);
    }
    return parts.join(" ");
  } catch {
    return html;
  }
}

function htmlUnescape(s: string): string {
  const ta = document.createElement("textarea");
  ta.innerHTML = s;
  return ta.value;
}

// Footer boilerplate markers (mirror _FOOTER_MARKERS). Everything from a marker
// to the end is the sender's unsubscribe/identity footer, never a job listing.
const FOOTER_MARKERS = ["This email was intended for"];

function stripFooter(text: string): string {
  let cut = text.length;
  for (const marker of FOOTER_MARKERS) {
    const i = text.indexOf(marker);
    if (i !== -1) cut = Math.min(cut, i);
  }
  return text.slice(0, cut).trim();
}

// Zero-width + bidi control marks: U+200B–200F, U+202A–202E, U+2060, U+FEFF.
const ZERO_WIDTH_RE = /[​-‏‪-‮⁠﻿]/g;
const URL_RE = /https?:\/\/\S+/g;
const WS_RE = /\s+/g;
const DECOR_RE = /([^\w\s])\1{3,}/g; // collapse decorative runs (===, ---, ***)

function normalizeBody(text: string): string {
  let t = htmlUnescape(text);
  t = t.replace(ZERO_WIDTH_RE, "");
  t = t.replace(URL_RE, "");
  t = t.replace(WS_RE, " ");
  t = t.replace(DECOR_RE, "$1$1$1");
  return stripFooter(t.trim());
}

type GmailPart = {
  mimeType?: string;
  body?: { data?: string };
  parts?: GmailPart[];
};

// Walk MIME parts; return whichever of plain/html yields more substantive text
// after cleanup (mirror _extract_body — LinkedIn ships boilerplate-only plain).
function extractBody(payload: GmailPart): string {
  const plain: string[] = [];
  const htmlParts: string[] = [];
  const walk = (part: GmailPart) => {
    const data = part.body?.data;
    if (data) {
      const decoded = b64urlToUtf8(data);
      if (part.mimeType === "text/plain") plain.push(decoded);
      else if (part.mimeType === "text/html") htmlParts.push(decoded);
    }
    for (const sub of part.parts ?? []) walk(sub);
  };
  walk(payload);
  const plainText = plain.length ? normalizeBody(plain.join("\n")) : "";
  const htmlText = htmlParts.length ? normalizeBody(htmlToText(htmlParts.join("\n"))) : "";
  return htmlText.length > plainText.length ? htmlText : plainText;
}

// ── REST helpers ─────────────────────────────────────────────────────────────
async function gfetch(path: string): Promise<Response> {
  const token = await getAccessToken();
  return fetch(`${API}${path}`, { headers: { Authorization: `Bearer ${token}` } });
}

async function listMessageIds(query: string, maxResults: number): Promise<string[]> {
  const res = await gfetch(`/messages?q=${encodeURIComponent(query)}&maxResults=${maxResults}`);
  if (!res.ok) {
    if (res.status === 401) throw new Error("Gmail authorization expired — sign in again.");
    throw new Error(`Gmail search failed (${res.status})`);
  }
  const data = (await res.json()) as { messages?: Array<{ id: string }> };
  return (data.messages ?? []).map((m) => m.id);
}

async function getMessage(id: string): Promise<GmailMessage | null> {
  const res = await gfetch(`/messages/${id}?format=full`);
  if (!res.ok) return null;
  const msg = (await res.json()) as {
    payload?: GmailPart & { headers?: Array<{ name: string; value: string }> };
  };
  const payload = msg.payload ?? {};
  const headers: Record<string, string> = {};
  for (const h of payload.headers ?? []) headers[h.name] = h.value;
  let body = extractBody(payload);
  if (body.length > BODY_MAX_CHARS) body = body.slice(0, BODY_MAX_CHARS) + " …[truncated]";
  return {
    id,
    subject: headers["Subject"] ?? "(no subject)",
    from: headers["From"] ?? "unknown",
    date: headers["Date"] ?? "",
    body,
  };
}

// Run a callback over items with bounded concurrency.
async function mapLimit<T>(items: T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  let i = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (i < items.length) {
      const idx = i++;
      await fn(items[idx]);
    }
  });
  await Promise.all(workers);
}

// ── Query construction (mirror run_gmail_search) ─────────────────────────────
const INTERN_TERMS = "intern OR internship OR coop OR co-op OR stage OR stagiaire OR student";

function formatCutoff(days: number): string {
  const d = new Date(Date.now() - days * 86_400_000);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}/${m}/${day}`;
}

function buildQueries(keyword: string, days: number, unreadOnly: boolean): string[] {
  let queries: string[];
  if (keyword.trim()) {
    queries = [`${keyword} (${INTERN_TERMS})`];
  } else {
    const cutoff = formatCutoff(days);
    queries = [
      `(${INTERN_TERMS}) after:${cutoff}`,
      `subject:(application OR applied OR interview OR offer) after:${cutoff}`,
      `from:(recruiter OR talent OR hiring OR careers OR hr) after:${cutoff}`,
    ];
  }
  return unreadOnly ? queries.map((q) => `is:unread ${q}`) : queries;
}

export type SearchOptions = {
  keyword?: string;
  days?: number;
  unreadOnly?: boolean;
  maxResults?: number;
  onLog?: (line: string) => void;
};

/** The signed-in user's Gmail address (for display). */
export async function getProfileEmail(): Promise<string> {
  const res = await gfetch(`/profile`);
  if (!res.ok) throw new Error(`Gmail profile failed (${res.status})`);
  const data = (await res.json()) as { emailAddress?: string };
  return data.emailAddress ?? "";
}

/** Run the targeted searches, dedupe by id, and fetch full bodies. */
export async function runGmailSearch(opts: SearchOptions = {}): Promise<GmailMessage[]> {
  const { keyword = "", days = 30, unreadOnly = true, maxResults = 100, onLog } = opts;
  const queries = buildQueries(keyword, days, unreadOnly);

  const ids = new Set<string>();
  for (const q of queries) {
    onLog?.(`Searching: ${q}`);
    try {
      for (const id of await listMessageIds(q, maxResults)) ids.add(id);
    } catch (e) {
      onLog?.(`  [!] ${(e as Error).message}`);
    }
  }

  const idList = [...ids];
  onLog?.(`Fetching ${idList.length} email(s)…`);
  const byId = new Map<string, GmailMessage>();
  let done = 0;
  await mapLimit(idList, 5, async (id) => {
    const m = await getMessage(id).catch(() => null);
    done += 1;
    if (m) byId.set(id, m);
    if (done % 10 === 0 || done === idList.length) onLog?.(`  Fetched ${done}/${idList.length}`);
  });

  return [...byId.values()];
}
