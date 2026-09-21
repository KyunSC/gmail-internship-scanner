"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { signIn, signOut, isConfigured } from "@/lib/google-auth";
import { getProfileEmail, runGmailSearch, type GmailMessage } from "@/lib/gmail";
import { ruleBasedAnalyze, type ResultItem } from "@/lib/filter";
import {
  analyze,
  detectCapability,
  recommendModel,
  MODEL_OPTIONS,
  MOBILE_MODEL_ID,
  DEFAULT_WEBLLM_MODEL,
  type Capability,
  type LlmBackend,
  type ModelProgress,
} from "@/lib/llm";
import {
  loadCache,
  saveScan,
  saveResults,
  addSeen,
  removeSeen,
  loadSeen,
  clearCache,
  type ScanSnapshot,
  type EmailHeader,
} from "@/lib/cache";

type CacheState = { scan: ScanSnapshot | null; results: ResultItem[] | null };

const PRIORITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };

const CATEGORY_TONE: Record<string, string> = {
  internship: "text-emerald-400 border-emerald-400/40",
  recruiter: "text-fuchsia-400 border-fuchsia-400/40",
  confirmation: "text-sky-400 border-sky-400/40",
  reply: "text-amber-300 border-amber-300/40",
  status: "text-slate-300 border-slate-300/40",
};

const PRIORITY_TONE: Record<string, string> = {
  high: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  medium: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  low: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};

function headerOf(e: GmailMessage): EmailHeader {
  return { id: e.id, subject: e.subject, from: e.from, date: e.date };
}

function mergeById<T extends { id?: string }>(prev: T[], next: T[]): T[] {
  const byId = new Map<string, T>();
  for (const item of [...prev, ...next]) if (item.id) byId.set(item.id, item);
  return [...byId.values()];
}

export default function Dashboard() {
  const [cache, setCache] = useState<CacheState | null>(null);
  const [loadingCache, setLoadingCache] = useState(false);

  // Auth
  const [email, setEmail] = useState<string | null>(null);
  const [signingIn, setSigningIn] = useState(false);

  // LLM backend
  const [backend, setBackend] = useState<LlmBackend>("webllm");
  const [capability, setCapability] = useState<Capability | null>(null);
  const [webllmModel, setWebllmModel] = useState(DEFAULT_WEBLLM_MODEL);
  const [ollamaUrl, setOllamaUrl] = useState("http://localhost:11434");
  const [ollamaModel, setOllamaModel] = useState("qwen3.5:9b");

  // Scan controls
  const [keyword, setKeyword] = useState("");
  const [days, setDays] = useState(30);
  const [maxEmails, setMaxEmails] = useState(100);
  const [includeRead, setIncludeRead] = useState(false);
  const [fast, setFast] = useState(false);
  const [rescan, setRescan] = useState(false);

  // Run state
  const [log, setLog] = useState("");
  const [busy, setBusy] = useState(false);
  const [modelProgress, setModelProgress] = useState<ModelProgress | null>(null);
  const sessionRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const logRef = useRef<HTMLPreElement | null>(null);

  const appendLog = useCallback((line: string) => {
    setLog((prev) => (prev ? `${prev}\n${line}` : line));
  }, []);

  const refreshCache = useCallback(async (account: string, session: number) => {
    if (sessionRef.current !== session) return;
    setLoadingCache(true);
    try {
      const next = await loadCache(account);
      if (sessionRef.current === session) setCache(next);
    } finally {
      if (sessionRef.current === session) setLoadingCache(false);
    }
  }, []);

  useEffect(() => {
    detectCapability()
      .then((cap) => {
        setCapability(cap);
        setWebllmModel(recommendModel(cap));
        if (!cap.webgpu) {
          // Ollama cannot run on iOS/Android, so falling back to it there strands
          // the user on a backend the device has no way to provide. Rule-based
          // Fast mode is the only path that actually works on a phone.
          if (cap.mobile) setFast(true);
          else setBackend("ollama");
        }
      })
      .catch(() => undefined);
  }, [refreshCache]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  const onSignIn = useCallback(async () => {
    const session = ++sessionRef.current;
    abortRef.current?.abort();
    setEmail(null);
    setCache(null);
    setLog("");
    setBusy(false);
    setModelProgress(null);
    setSigningIn(true);
    try {
      await signIn();
      if (session !== sessionRef.current) return;
      const account = (await getProfileEmail()).trim().toLowerCase();
      if (!account) throw new Error("Missing account identity");
      if (session !== sessionRef.current) return;
      setEmail(account);
      await refreshCache(account, session);
    } catch (err) {
      if (session === sessionRef.current) appendLog(`[sign-in error] ${(err as Error).message}`);
    } finally {
      if (session === sessionRef.current) setSigningIn(false);
    }
  }, [appendLog, refreshCache]);

  const onSignOut = useCallback(() => {
    ++sessionRef.current;
    abortRef.current?.abort();
    signOut();
    setCache(null);
    setLog("");
    setBusy(false);
    setSigningIn(false);
    setLoadingCache(false);
    setModelProgress(null);
    setEmail(null);
  }, []);

  const onScan = useCallback(async () => {
    if (busy || loadingCache) return;
    if (!email) {
      appendLog("[!] Sign in with Google first.");
      return;
    }
    const session = sessionRef.current;
    const ac = new AbortController();
    const current = () => session === sessionRef.current && !ac.signal.aborted;
    const scanLog = (line: string) => { if (current()) appendLog(line); };
    setBusy(true);
    setLog("");
    setModelProgress(null);
    abortRef.current = ac;
    try {
      // 1. Fetch the targeted Gmail queries (server-side filtered at Google).
      const fetched = await runGmailSearch({
        keyword: keyword.trim(),
        days,
        maxResults: maxEmails,
        unreadOnly: !includeRead,
        onLog: scanLog,
      });
      if (ac.signal.aborted) throw new DOMException("Aborted", "AbortError");

      // 2. Incremental: skip emails already analyzed in a previous run.
      const seen = await loadSeen(email);
      if (!current()) return;
      const seenSet = new Set(seen);
      const toAnalyze = rescan ? fetched : fetched.filter((e) => !seenSet.has(e.id));
      const skipped = fetched.length - toAnalyze.length;
      if (skipped > 0) {
        scanLog(`Skipping ${skipped} email(s) already scanned; ${toAnalyze.length} new to analyze.`);
      }

      // 3. Analyze (LLM two-pass, or the no-LLM fast path).
      let analyzed: ResultItem[] = [];
      let successfulIds = toAnalyze.map((e) => e.id);
      let failedIds: string[] = [];
      if (toAnalyze.length === 0) {
        scanLog("No new emails since the last scan.");
      } else if (fast) {
        scanLog("Fast mode: rule-based full-body filter (no LLM).");
        analyzed = ruleBasedAnalyze(toAnalyze);
        scanLog(`${analyzed.length} candidate(s) kept.`);
      } else {
        const outcome = await analyze(toAnalyze, {
          backend,
          webllmModel,
          ollamaUrl,
          ollamaModel,
          // Phones can't hold the full classification prompt in context; rules
          // take over relevance there and the model only extracts fields.
          lowMemory: backend === "webllm" && (capability?.mobile ?? false),
          onLog: scanLog,
          onModelProgress: (p) => { if (current()) setModelProgress(p); },
          signal: ac.signal,
        });
        analyzed = outcome.results;
        successfulIds = outcome.successfulIds;
        failedIds = outcome.failedIds;
      }
      if (!current()) return;
      setModelProgress(null);

      // 4. Merge with prior cache + persist. On a full rescan, the current
      // window's kept-status is decided solely by this run (drop stale entries
      // for fetched emails); incremental runs keep prior results untouched
      // since seen emails are never re-evaluated.
      const previous = await loadCache(email);
      if (!current()) return;
      const prevResults = previous.results ?? [];
      const prevEmails = previous.scan?.emails ?? [];
      const evaluatedIds = new Set(successfulIds);
      const basis = prevResults.filter((r) => !(r.id && evaluatedIds.has(r.id)));
      const mergedResults = mergeById(basis, analyzed);
      const mergedEmails = mergeById(prevEmails, toAnalyze.filter((e) => evaluatedIds.has(e.id)).map(headerOf));
      const snapshot: ScanSnapshot = {
        scan_time: new Date().toLocaleString(),
        emails: mergedEmails,
        kept_ids: mergedResults.map((r) => r.id ?? "").filter(Boolean),
      };
      await saveResults(email, mergedResults);
      if (!current()) return;
      await saveScan(email, snapshot);
      if (!current()) return;
      await removeSeen(email, failedIds);
      if (!current()) return;
      await addSeen(email, successfulIds);
      await refreshCache(email, session);
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        scanLog(`[error] ${(err as Error).message}`);
      } else {
        scanLog("[cancelled]");
      }
    } finally {
      if (session === sessionRef.current) {
        setBusy(false);
        abortRef.current = null;
      }
    }
  }, [
    busy, loadingCache, email, keyword, days, maxEmails, includeRead, fast, rescan, backend,
    webllmModel, ollamaUrl, ollamaModel, cache, appendLog, refreshCache, capability,
  ]);

  const onCancel = useCallback(() => abortRef.current?.abort(), []);

  const onClearCache = useCallback(async () => {
    if (!email || busy) return;
    const session = sessionRef.current;
    await clearCache(email);
    await refreshCache(email, session);
    if (session === sessionRef.current) appendLog("Cache cleared.");
  }, [refreshCache, appendLog, email, busy]);

  const keptIds = useMemo(() => new Set(cache?.scan?.kept_ids ?? []), [cache?.scan?.kept_ids]);

  const sortedResults = useMemo(() => {
    const arr = [...(cache?.results ?? [])];
    arr.sort(
      (a, b) =>
        (PRIORITY_ORDER[a.priority ?? "low"] ?? 2) - (PRIORITY_ORDER[b.priority ?? "low"] ?? 2),
    );
    return arr;
  }, [cache?.results]);

  const scannedEmails = cache?.scan?.emails ?? [];

  return (
    <main className="min-h-screen p-6 md:p-10 max-w-6xl mx-auto">
      <header className="mb-8 flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Internship Scanner</h1>
          <p className="text-sm text-[color:var(--color-muted)] mt-1">
            Runs entirely in your browser — Gmail + LLM analysis never touch a server.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {email ? (
            <>
              <span className="text-xs text-[color:var(--color-muted)]">{email}</span>
              <button onClick={onSignOut} className="btn-ghost">Sign out</button>
            </>
          ) : (
            <button onClick={onSignIn} disabled={signingIn || !isConfigured()} className="btn-primary">
              {signingIn ? "Signing in…" : "Sign in with Google"}
            </button>
          )}
        </div>
      </header>

      {!isConfigured() && (
        <Banner tone="warn">
          No Google OAuth client configured. Set <code>NEXT_PUBLIC_GOOGLE_CLIENT_ID</code> in{" "}
          <code>web/.env.local</code> (see <code>.env.local.example</code>) and rebuild.
        </Banner>
      )}

      <section className="grid md:grid-cols-2 gap-4 mb-6">
        <Panel title="Run scan">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Keyword (optional)">
              <input
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                placeholder="e.g. EXFO"
                className="input col-span-2"
              />
            </Field>
            <Field label="Days back">
              <input
                type="number"
                min={1}
                value={days}
                onChange={(e) => setDays(Number(e.target.value) || 1)}
                className="input"
              />
            </Field>
            <Field label="Max emails / query">
              <input
                type="number"
                min={1}
                value={maxEmails}
                onChange={(e) => setMaxEmails(Number(e.target.value) || 1)}
                className="input"
              />
            </Field>
            <Toggle label="Include read emails" checked={includeRead} onChange={setIncludeRead} />
            <Toggle label="Fast (rule-based, no LLM)" checked={fast} onChange={setFast} />
            <Toggle label="Re-analyze all (ignore cache)" checked={rescan} onChange={setRescan} />
          </div>
          <div className="flex gap-2 mt-4">
            <button onClick={onScan} disabled={busy || !email} className="btn-primary">
              {busy ? "Scanning…" : "Run scan"}
            </button>
            {busy && <button onClick={onCancel} className="btn-ghost">Cancel</button>}
          </div>
        </Panel>

        <Panel title="LLM backend">
          <div className="flex gap-2 mb-3">
            <BackendTab label="In-browser (WebLLM)" active={backend === "webllm"} onClick={() => setBackend("webllm")} />
            <BackendTab label="Local Ollama" active={backend === "ollama"} onClick={() => setBackend("ollama")} />
          </div>

          {backend === "webllm" ? (
            <div className="space-y-3">
              <div className="text-xs text-[color:var(--color-muted)]">
                {capability ? (
                  capability.webgpu ? (
                    <>
                      WebGPU ✓ · {capability.gpuVendor ?? "GPU"} ·{" "}
                      {capability.maxBufferMB ? `${(capability.maxBufferMB / 1000).toFixed(1)} GB max buffer · ` : ""}
                      {capability.deviceMemoryGB ? `~${capability.deviceMemoryGB}GB RAM · ` : ""}
                      {capability.cores ? `${capability.cores} cores` : ""}
                    </>
                  ) : capability.mobile ? (
                    <span className="text-amber-300">
                      WebGPU not available on this device — Ollama can&apos;t run on a phone either, so
                      Fast mode (rule-based, no LLM) has been turned on for you.
                    </span>
                  ) : (
                    <span className="text-amber-300">
                      WebGPU not available here — use Chrome/Edge or Safari 18+, or switch to Local Ollama.
                    </span>
                  )
                ) : (
                  "Detecting hardware…"
                )}
              </div>
              <Field label="Model (auto-selected for your hardware)">
                <select value={webllmModel} onChange={(e) => setWebllmModel(e.target.value)} className="input col-span-2">
                  {MODEL_OPTIONS.map((m) => (
                    <option key={m.id} value={m.id}>{m.label}</option>
                  ))}
                </select>
              </Field>
              {capability?.mobile && webllmModel !== MOBILE_MODEL_ID && (
                <p className="text-[11px] text-amber-300">
                  Phones and tablets kill a tab that goes much past ~1 GB. Anything above the 1B model
                  will likely reload the page mid-scan — switch back if that happens, or use Fast mode.
                </p>
              )}
              {modelProgress && modelProgress.progress < 1 && (
                <div>
                  <div className="text-[11px] text-[color:var(--color-muted)] mb-1 truncate">{modelProgress.text}</div>
                  <div className="h-1.5 bg-[color:var(--color-panel-2)] rounded overflow-hidden">
                    <div className="h-full bg-[color:var(--color-accent)]" style={{ width: `${Math.round(modelProgress.progress * 100)}%` }} />
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Ollama URL">
                <input value={ollamaUrl} onChange={(e) => setOllamaUrl(e.target.value)} className="input col-span-2" />
              </Field>
              <Field label="Model">
                <input value={ollamaModel} onChange={(e) => setOllamaModel(e.target.value)} className="input col-span-2" />
              </Field>
              <p className="text-[11px] text-[color:var(--color-muted)] col-span-2">
                Runs on your own machine — email never leaves your device. If this page is served over HTTPS, start
                Ollama allowing this origin:{" "}
                <code>OLLAMA_ORIGINS=&quot;{typeof window !== "undefined" ? window.location.origin : ""}&quot; ollama serve</code>
              </p>
            </div>
          )}
        </Panel>
      </section>

      <section className="mb-6">
        <Panel title="Live log">
          <pre
            ref={logRef}
            className="text-xs leading-relaxed bg-black/40 border border-[color:var(--color-border)] rounded p-3 h-64 overflow-auto whitespace-pre-wrap"
          >
            {log || <span className="text-[color:var(--color-muted)]">Run a scan to see output here.</span>}
          </pre>
        </Panel>
      </section>

      <section className="mb-6">
        <Panel
          title={`Kept candidates${sortedResults.length ? ` (${sortedResults.length})` : ""}`}
          headerRight={
            <div className="flex gap-3">
              <button onClick={() => { if (email) void refreshCache(email, sessionRef.current); }} className="text-xs text-[color:var(--color-muted)] hover:text-white">
                {loadingCache ? "…" : "Refresh"}
              </button>
              <button onClick={onClearCache} className="text-xs text-[color:var(--color-muted)] hover:text-white">
                Clear cache
              </button>
            </div>
          }
        >
          {sortedResults.length === 0 ? (
            <p className="text-sm text-[color:var(--color-muted)]">No results cached. Run a scan to populate.</p>
          ) : (
            <ul className="space-y-3">
              {sortedResults.map((r, i) => (
                <li
                  key={r.id ?? `${r.subject}-${i}`}
                  className="border border-[color:var(--color-border)] rounded-lg p-3 bg-[color:var(--color-panel-2)]"
                >
                  <div className="flex items-center gap-2 flex-wrap mb-1">
                    <span className={`text-[10px] uppercase tracking-wide px-2 py-0.5 border rounded ${CATEGORY_TONE[r.category ?? "status"] ?? CATEGORY_TONE.status}`}>
                      {r.category ?? "status"}
                    </span>
                    <span className={`text-[10px] uppercase tracking-wide px-2 py-0.5 border rounded ${PRIORITY_TONE[r.priority ?? "low"] ?? PRIORITY_TONE.low}`}>
                      {r.priority ?? "low"}
                    </span>
                    {r.company && <span className="text-xs text-[color:var(--color-muted)]">{r.company}</span>}
                  </div>
                  <div className="font-medium">{r.subject || "(no subject)"}</div>
                  <div className="text-xs text-[color:var(--color-muted)] mt-0.5">{r.from} · {r.date}</div>
                  {r.summary && <p className="text-sm mt-2 text-[color:var(--color-text)]/90">{r.summary}</p>}
                  {r.action_items && r.action_items.length > 0 && (
                    <ul className="mt-2 text-sm list-disc list-inside text-[color:var(--color-text)]/90">
                      {r.action_items.map((a, j) => (
                        <li key={j}>{a}</li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </section>

      <section>
        <Panel title={`All scanned emails${scannedEmails.length ? ` (${scannedEmails.length})` : ""}`}>
          {cache?.scan?.scan_time && (
            <p className="text-xs text-[color:var(--color-muted)] mb-3">Last scan: {cache.scan.scan_time}</p>
          )}
          {scannedEmails.length === 0 ? (
            <p className="text-sm text-[color:var(--color-muted)]">No scan cache yet.</p>
          ) : (
            <ul className="divide-y divide-[color:var(--color-border)]">
              {scannedEmails.map((e) => {
                const kept = keptIds.has(e.id);
                return (
                  <li key={e.id} className="py-2 flex items-start gap-3">
                    <span
                      className={`mt-1 inline-block w-1.5 h-1.5 rounded-full shrink-0 ${kept ? "bg-emerald-400" : "bg-slate-600"}`}
                      title={kept ? "Kept" : "Filtered / aggregator"}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="text-sm truncate">{e.subject}</div>
                      <div className="text-[11px] text-[color:var(--color-muted)] truncate">{e.from} · {e.date}</div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>
      </section>

      <style>{`
        .input { background: var(--color-panel-2); border: 1px solid var(--color-border); border-radius: 6px; padding: 6px 10px; font-size: 13px; color: var(--color-text); outline: none; }
        .input:focus { border-color: var(--color-accent); }
        .btn-primary { background: var(--color-accent); color: #0b0d10; padding: 8px 14px; border-radius: 6px; font-weight: 500; font-size: 13px; }
        .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-ghost { background: transparent; color: var(--color-text); border: 1px solid var(--color-border); padding: 8px 14px; border-radius: 6px; font-size: 13px; }
      `}</style>
    </main>
  );
}

function Banner({ tone, children }: { tone: "warn"; children: React.ReactNode }) {
  const cls = tone === "warn" ? "border-amber-500/40 bg-amber-500/10 text-amber-200" : "";
  return <div className={`rounded-lg border px-4 py-3 text-sm mb-6 ${cls}`}>{children}</div>;
}

function BackendTab({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`text-xs px-3 py-1.5 rounded border ${active ? "border-[color:var(--color-accent)] text-[color:var(--color-text)]" : "border-[color:var(--color-border)] text-[color:var(--color-muted)]"}`}
    >
      {label}
    </button>
  );
}

function Panel({
  title,
  headerRight,
  children,
}: {
  title: string;
  headerRight?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-[color:var(--color-border)] bg-[color:var(--color-panel)] p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-[color:var(--color-muted)]">{title}</h2>
        {headerRight}
      </div>
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-[color:var(--color-muted)]">{label}</span>
      {children}
    </label>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 text-xs cursor-pointer select-none col-span-2">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="accent-[color:var(--color-accent)]" />
      <span>{label}</span>
    </label>
  );
}
