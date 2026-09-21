// Client-side persistence in IndexedDB, replacing the CLI's .last_scan.json /
// .last_results.json files. Shapes mirror what the old /api/cache route served.

import type { ResultItem } from "@/lib/filter";

export type EmailHeader = { id: string; subject: string; from: string; date: string };
export type ScanSnapshot = { scan_time: string; emails: EmailHeader[]; kept_ids: string[] };

const DB_NAME = "internship-scanner";
const STORE = "kv";
const DB_VERSION = 1;
const SEEN_CAP = 5000; // mirror the CLI's 5000-record seen-set cap

const SCAN_KEY = "scan";
const RESULTS_KEY = "results";
const SEEN_KEY = "seen";

function accountKey(account: string, key: string): string {
  if (!account.trim()) throw new Error("Account identity required");
  return `account:${encodeURIComponent(account.trim().toLowerCase())}:${key}`;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbGet<T>(key: string): Promise<T | null> {
  const db = await openDb();
  try {
    return await new Promise<T | null>((resolve, reject) => {
      const req = db.transaction(STORE, "readonly").objectStore(STORE).get(key);
      req.onsuccess = () => resolve((req.result as T) ?? null);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

async function idbSet(key: string, value: unknown): Promise<void> {
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(value, key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

async function idbDel(keys: string[]): Promise<void> {
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      for (const k of keys) tx.objectStore(STORE).delete(k);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

// ── Scan snapshot + results ──────────────────────────────────────────────────
export function saveScan(account: string, snapshot: ScanSnapshot): Promise<void> {
  return idbSet(accountKey(account, SCAN_KEY), snapshot);
}
export function loadScan(account: string): Promise<ScanSnapshot | null> {
  return idbGet<ScanSnapshot>(accountKey(account, SCAN_KEY));
}
export function saveResults(account: string, results: ResultItem[]): Promise<void> {
  return idbSet(accountKey(account, RESULTS_KEY), results);
}
export function loadResults(account: string): Promise<ResultItem[] | null> {
  return idbGet<ResultItem[]>(accountKey(account, RESULTS_KEY));
}

export async function loadCache(account: string): Promise<{ scan: ScanSnapshot | null; results: ResultItem[] | null }> {
  const [scan, results] = await Promise.all([loadScan(account), loadResults(account)]);
  return { scan, results };
}

export function clearCache(account: string): Promise<void> {
  return idbDel([SCAN_KEY, RESULTS_KEY, SEEN_KEY].map((key) => accountKey(account, key)));
}

// ── Incremental seen-set (skip already-scanned emails) ───────────────────────
export function loadSeen(account: string): Promise<string[]> {
  return idbGet<string[]>(accountKey(account, SEEN_KEY)).then((v) => v ?? []);
}

/** Append new message ids, dedupe, and cap to the most recent SEEN_CAP. */
export async function addSeen(account: string, ids: string[]): Promise<void> {
  const existing = await loadSeen(account);
  const known = new Set(existing);
  const fresh = ids.filter((id) => !known.has(id));
  if (fresh.length === 0) return;
  const merged = [...existing, ...fresh].slice(-SEEN_CAP);
  await idbSet(accountKey(account, SEEN_KEY), merged);
}

/** Failed rescans retain prior display decisions but must remain retryable. */
export async function removeSeen(account: string, ids: string[]): Promise<void> {
  const failed = new Set(ids);
  await idbSet(accountKey(account, SEEN_KEY), (await loadSeen(account)).filter((id) => !failed.has(id)));
}
