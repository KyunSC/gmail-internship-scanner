"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type EmailHeader = { id: string; subject: string; from: string; date: string };

type ResultItem = {
  id?: string;
  subject?: string;
  from?: string;
  date?: string;
  company?: string | null;
  category?: string;
  summary?: string;
  action_items?: string[];
  priority?: string;
};

type CacheResponse = {
  scan: {
    scan_time?: string;
    emails?: EmailHeader[];
    kept_ids?: string[];
  } | null;
  results: ResultItem[] | null;
};

const ANSI_RE = /\x1b\[[0-9;]*m/g;
const stripAnsi = (s: string) => s.replace(ANSI_RE, "");

const PRIORITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };

const CATEGORY_TONE: Record<string, string> = {
  internship: "text-emerald-400 border-emerald-400/40",
  interview: "text-sky-400 border-sky-400/40",
  rejection: "text-rose-400 border-rose-400/40",
  status: "text-amber-300 border-amber-300/40",
};

const PRIORITY_TONE: Record<string, string> = {
  high: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  medium: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  low: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};

export default function Dashboard() {
  const [cache, setCache] = useState<CacheResponse | null>(null);
  const [loadingCache, setLoadingCache] = useState(true);
  const [clearingCache, setClearingCache] = useState(false);

  // Scan controls
  const [keyword, setKeyword] = useState("");
  const [days, setDays] = useState(30);
  const [maxEmails, setMaxEmails] = useState(100);
  const [includeRead, setIncludeRead] = useState(false);
  const [fast, setFast] = useState(false);

  // Clean controls
  const [cleanFromCache, setCleanFromCache] = useState(true);
  const [cleanApply, setCleanApply] = useState(false);

  // Compare controls
  const [compareDays, setCompareDays] = useState(30);
  const [compareMaxEmails, setCompareMaxEmails] = useState(100);
  const [compareAll, setCompareAll] = useState(false);
  const [compareApply, setCompareApply] = useState(false);

  // Stream state
  const [log, setLog] = useState("");
  const [busy, setBusy] = useState<null | "scan" | "clean" | "compare">(null);
  const abortRef = useRef<AbortController | null>(null);
  const logRef = useRef<HTMLPreElement | null>(null);

  const refreshCache = useCallback(async () => {
    setLoadingCache(true);
    try {
      const res = await fetch("/api/cache", { cache: "no-store" });
      if (!res.ok) throw new Error(await res.text());
      setCache((await res.json()) as CacheResponse);
    } catch (err) {
      console.error(err);
    } finally {
      setLoadingCache(false);
    }
  }, []);

  const clearCache = useCallback(async () => {
    if (busy || clearingCache) return;
    if (
      !window.confirm(
        "Clear the scan cache and results? The next scan will re-analyze every email from scratch.",
      )
    )
      return;
    setClearingCache(true);
    try {
      const res = await fetch("/api/cache", { method: "DELETE" });
      if (!res.ok) throw new Error(await res.text());
      const { removed } = (await res.json()) as { removed: string[] };
      setLog(
        removed.length
          ? `Cleared cache: ${removed.join(", ")}\n`
          : "Cache already empty — nothing to clear.\n",
      );
    } catch (err) {
      setLog(`[client error] ${(err as Error).message}\n`);
    } finally {
      setClearingCache(false);
      refreshCache();
    }
  }, [busy, clearingCache, refreshCache]);

  useEffect(() => {
    refreshCache();
  }, [refreshCache]);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [log]);

  const runStream = useCallback(
    async (
      kind: "scan" | "clean" | "compare",
      url: string,
      body: Record<string, unknown>,
    ) => {
      if (busy) return;
      setBusy(kind);
      setLog("");
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: ac.signal,
        });
        if (!res.body) throw new Error("No response body");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          const chunk = stripAnsi(decoder.decode(value, { stream: true }));
          setLog((prev) => prev + chunk);
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          setLog((prev) => prev + `\n[client error] ${(err as Error).message}\n`);
        }
      } finally {
        setBusy(null);
        abortRef.current = null;
        refreshCache();
      }
    },
    [busy, refreshCache],
  );

  const onScan = () =>
    runStream("scan", "/api/scan", {
      keyword: keyword.trim(),
      days,
      maxEmails,
      all: includeRead,
      fast,
    });

  const onClean = () =>
    runStream("clean", "/api/clean", {
      days,
      apply: cleanApply,
      fromCache: cleanFromCache,
    });

  const onCompare = () =>
    runStream("compare", "/api/compare", {
      days: compareDays,
      maxEmails: compareMaxEmails,
      all: compareAll,
      apply: compareApply,
    });

  const onCancel = () => abortRef.current?.abort();

  const keptIds = useMemo(
    () => new Set(cache?.scan?.kept_ids ?? []),
    [cache?.scan?.kept_ids],
  );

  const sortedResults = useMemo(() => {
    const arr = [...(cache?.results ?? [])];
    arr.sort(
      (a, b) =>
        (PRIORITY_ORDER[a.priority ?? "low"] ?? 2) -
        (PRIORITY_ORDER[b.priority ?? "low"] ?? 2),
    );
    return arr;
  }, [cache?.results]);

  const scannedEmails = cache?.scan?.emails ?? [];

  return (
    <main className="min-h-screen p-6 md:p-10 max-w-6xl mx-auto">
      <header className="mb-8">
        <h1 className="text-3xl font-semibold tracking-tight">
          Internship Scanner
        </h1>
        <p className="text-sm text-[color:var(--color-muted)] mt-1">
          Local Gmail scan via Ollama. Dashboard for{" "}
          <code className="text-xs">scanner.py</code>.
        </p>
      </header>

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
            <Field label="Max emails">
              <input
                type="number"
                min={1}
                value={maxEmails}
                onChange={(e) => setMaxEmails(Number(e.target.value) || 1)}
                className="input"
              />
            </Field>
            <Toggle
              label="Include read emails (--all)"
              checked={includeRead}
              onChange={setIncludeRead}
            />
            <Toggle
              label="Fast (rule-based, no LLM)"
              checked={fast}
              onChange={setFast}
            />
          </div>
          <div className="flex gap-2 mt-4">
            <button
              onClick={onScan}
              disabled={!!busy}
              className="btn-primary"
            >
              {busy === "scan" ? "Scanning…" : "Run scan"}
            </button>
            {busy === "scan" && (
              <button onClick={onCancel} className="btn-ghost">
                Cancel
              </button>
            )}
          </div>
        </Panel>

        <Panel title="Clean inbox">
          <p className="text-xs text-[color:var(--color-muted)] mb-3">
            Marks unread aggregator emails (LinkedIn / Glassdoor / Jobright /
            ZipRecruiter / Indeed) as read unless the scanner kept them.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Days back">
              <input
                type="number"
                min={1}
                value={days}
                onChange={(e) => setDays(Number(e.target.value) || 1)}
                className="input"
              />
            </Field>
            <Toggle
              label="Use cached scan (fast)"
              checked={cleanFromCache}
              onChange={setCleanFromCache}
            />
            <Toggle
              label="Apply changes (otherwise dry-run)"
              checked={cleanApply}
              onChange={setCleanApply}
            />
          </div>
          <div className="flex gap-2 mt-4">
            <button
              onClick={onClean}
              disabled={!!busy}
              className={cleanApply ? "btn-danger" : "btn-primary"}
            >
              {busy === "clean"
                ? "Cleaning…"
                : cleanApply
                  ? "Apply cleanup"
                  : "Dry-run cleanup"}
            </button>
            {busy === "clean" && (
              <button onClick={onCancel} className="btn-ghost">
                Cancel
              </button>
            )}
          </div>
        </Panel>
      </section>

      <section className="mb-6">
        <Panel title="Compare LLM vs rule-based">
          <p className="text-xs text-[color:var(--color-muted)] mb-3">
            Runs <code>compare.py</code>: fetches emails once, runs both the LLM
            scanner and the pure rule-based filter on the same set, and prints
            the diff. Diagnostic tool for tuning the prompt or verifying changes.
            Apply marks aggregator emails as read only when both pipelines agree
            they&apos;re not internships.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Days back">
              <input
                type="number"
                min={1}
                value={compareDays}
                onChange={(e) => setCompareDays(Number(e.target.value) || 1)}
                className="input"
              />
            </Field>
            <Field label="Max emails">
              <input
                type="number"
                min={1}
                value={compareMaxEmails}
                onChange={(e) => setCompareMaxEmails(Number(e.target.value) || 1)}
                className="input"
              />
            </Field>
            <Toggle
              label="Include read emails (--all)"
              checked={compareAll}
              onChange={setCompareAll}
            />
            <Toggle
              label="Apply cleanup on agreement (otherwise dry-run)"
              checked={compareApply}
              onChange={setCompareApply}
            />
          </div>
          <div className="flex gap-2 mt-4">
            <button
              onClick={onCompare}
              disabled={!!busy}
              className={compareApply ? "btn-danger" : "btn-primary"}
            >
              {busy === "compare"
                ? "Comparing…"
                : compareApply
                  ? "Run compare + apply"
                  : "Run compare (dry-run)"}
            </button>
            {busy === "compare" && (
              <button onClick={onCancel} className="btn-ghost">
                Cancel
              </button>
            )}
          </div>
        </Panel>
      </section>

      <section className="mb-6">
        <Panel title="Live log">
          <pre
            ref={logRef}
            className="text-xs leading-relaxed bg-black/40 border border-[color:var(--color-border)] rounded p-3 h-64 overflow-auto whitespace-pre-wrap"
          >
            {log || (
              <span className="text-[color:var(--color-muted)]">
                Run a scan or cleanup to see output here.
              </span>
            )}
          </pre>
        </Panel>
      </section>

      <section className="mb-6">
        <Panel
          title={`Kept candidates${sortedResults.length ? ` (${sortedResults.length})` : ""}`}
          headerRight={
            <button
              onClick={refreshCache}
              className="text-xs text-[color:var(--color-muted)] hover:text-white"
            >
              {loadingCache ? "…" : "Refresh"}
            </button>
          }
        >
          {sortedResults.length === 0 ? (
            <p className="text-sm text-[color:var(--color-muted)]">
              No results cached. Run a scan to populate.
            </p>
          ) : (
            <ul className="space-y-3">
              {sortedResults.map((r, i) => (
                <li
                  key={r.id ?? `${r.subject}-${i}`}
                  className="border border-[color:var(--color-border)] rounded-lg p-3 bg-[color:var(--color-panel-2)]"
                >
                  <div className="flex items-center gap-2 flex-wrap mb-1">
                    <span
                      className={`text-[10px] uppercase tracking-wide px-2 py-0.5 border rounded ${CATEGORY_TONE[r.category ?? "status"] ?? CATEGORY_TONE.status}`}
                    >
                      {r.category ?? "status"}
                    </span>
                    <span
                      className={`text-[10px] uppercase tracking-wide px-2 py-0.5 border rounded ${PRIORITY_TONE[r.priority ?? "low"] ?? PRIORITY_TONE.low}`}
                    >
                      {r.priority ?? "low"}
                    </span>
                    {r.company && (
                      <span className="text-xs text-[color:var(--color-muted)]">
                        {r.company}
                      </span>
                    )}
                  </div>
                  <div className="font-medium">{r.subject || "(no subject)"}</div>
                  <div className="text-xs text-[color:var(--color-muted)] mt-0.5">
                    {r.from} · {r.date}
                  </div>
                  {r.summary && (
                    <p className="text-sm mt-2 text-[color:var(--color-text)]/90">
                      {r.summary}
                    </p>
                  )}
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
        <Panel
          title={`All scanned emails${scannedEmails.length ? ` (${scannedEmails.length})` : ""}`}
          headerRight={
            <button
              onClick={clearCache}
              disabled={!!busy || clearingCache}
              className="text-xs text-[color:var(--color-danger)] hover:opacity-80 disabled:opacity-40"
              title="Delete the scan cache so the next scan re-analyzes everything"
            >
              {clearingCache ? "Clearing…" : "Clear cache"}
            </button>
          }
        >
          {cache?.scan?.scan_time && (
            <p className="text-xs text-[color:var(--color-muted)] mb-3">
              Last scan: {cache.scan.scan_time}
            </p>
          )}
          {scannedEmails.length === 0 ? (
            <p className="text-sm text-[color:var(--color-muted)]">
              No scan cache yet.
            </p>
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
                      <div className="text-[11px] text-[color:var(--color-muted)] truncate">
                        {e.from} · {e.date}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>
      </section>

      <style>{`
        .input {
          background: var(--color-panel-2);
          border: 1px solid var(--color-border);
          border-radius: 6px;
          padding: 6px 10px;
          font-size: 13px;
          color: var(--color-text);
          outline: none;
        }
        .input:focus {
          border-color: var(--color-accent);
        }
        .btn-primary {
          background: var(--color-accent);
          color: #0b0d10;
          padding: 8px 14px;
          border-radius: 6px;
          font-weight: 500;
          font-size: 13px;
        }
        .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-danger {
          background: var(--color-danger);
          color: #0b0d10;
          padding: 8px 14px;
          border-radius: 6px;
          font-weight: 500;
          font-size: 13px;
        }
        .btn-danger:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-ghost {
          background: transparent;
          color: var(--color-text);
          border: 1px solid var(--color-border);
          padding: 8px 14px;
          border-radius: 6px;
          font-size: 13px;
        }
      `}</style>
    </main>
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
        <h2 className="text-sm font-semibold uppercase tracking-wider text-[color:var(--color-muted)]">
          {title}
        </h2>
        {headerRight}
      </div>
      {children}
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-[color:var(--color-muted)]">{label}</span>
      {children}
    </label>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-xs cursor-pointer select-none col-span-2">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-[color:var(--color-accent)]"
      />
      <span>{label}</span>
    </label>
  );
}
