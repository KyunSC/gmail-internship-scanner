// Pluggable LLM layer, ported from cli/scanner.py (analyze_with_ollama,
// _analyze_batch, _extract_results_array, _normalize_result, _estimate_ctx_size).
//
// Backend-agnostic pipeline: pre-filter → per-email two-pass union → post-filter.
// Only the "generate" call differs between the two backends:
//   - webllm: in-browser inference via WebGPU (@mlc-ai/web-llm)
//   - ollama: the user's own http://localhost:11434 (email stays on-device)

import type { GmailMessage } from "@/lib/gmail";
import {
  hasInternshipSignal,
  postFilterResults,
  coerceResult,
  ruleBasedAnalyze,
  relevantExcerpt,
  type ResultItem,
} from "@/lib/filter";

export type LlmBackend = "webllm" | "ollama";

const LLM_PASS_TEMPERATURES = [0.0, 0.5]; // deterministic baseline + diversity pass
const MAX_TOKENS = 4096;

// Low-memory (phone) profile. A 1B model can't follow the full classification
// prompt, and a 4096-token context blows the mobile memory budget on KV cache
// alone — so rules keep ownership of relevance and the model only fills in the
// two fields rules can't produce. Short prompt, small context, single pass.
const LOW_MEM_CTX = 1024;
const LOW_MEM_MAX_TOKENS = 192;
const LOW_MEM_BODY_CHARS = 1200;
const DEFAULT_OLLAMA_URL = "http://localhost:11434";
const DEFAULT_OLLAMA_MODEL = "qwen3.5:9b";
export const DEFAULT_WEBLLM_MODEL = "Qwen2.5-3B-Instruct-q4f16_1-MLC";

// ── Prompt (verbatim from PROMPT_DEFAULT) ────────────────────────────────────
function buildPrompt(emailText: string): string {
  return `You are analyzing a student's inbox for internship and job-related emails.

CLASSIFICATION PRIORITY:
- Combine signals from the SUBJECT, BODY, and SENDER. No single field is decisive on
  its own. Strong signals: subject keywords (intern/co-op/stage/student), body content
  describing a student role, sender being a recruiter or career address.
- For job-alert digest emails (LinkedIn, Glassdoor, Jobright), scan the ENTIRE body for
  any internship/co-op/stage/student listing that is in the Montreal area OR remote/hybrid
  — not just the headline. Surface the email if ANY listing in it qualifies, even if it
  appears in a recommendations section.

For each email, determine if it is one of:
- Internship / co-op / stage / stagiaire job postings (STUDENT positions only)
- Recruiter outreach or hiring manager contact about an internship
- Application confirmations or status updates for an internship application
- Interview invitations or follow-ups for an internship

STRICT RULES — these MUST be followed:
1. ONLY include actual internship/co-op/stage/stagiaire/student positions. At least ONE
   of the following signals must apply:
   (a) Subject mentions "intern", "internship", "stage", "stagiaire", "co-op", "coop",
       or "student"
   (b) Body clearly describes a student/intern/co-op position
   (c) The email is recruiter outreach or application status for an internship from
       a company or career address
   If NONE of these signals apply, EXCLUDE.
2. EXCLUDE all full-time roles, including "junior", "senior", "mid-level", "lead",
   "associate", "engineer", "developer", "analyst", or "specialist" positions when they
   are NOT explicitly labeled as an internship.
3. Application confirmations, recruiter messages, and status updates from companies
   directly (e.g. PCL, WSP, Staples, Indeed Apply confirmations) about YOUR internship
   applications ARE relevant and should be included.
4. IGNORE newsletters, promotional emails, Quora digests, and unrelated content.
5. LOCATION — only include job LISTINGS located in the Montreal area (Montreal,
   Greater Montreal, Laval, Longueuil, Québec/QC) OR that are explicitly REMOTE or
   HYBRID. EXCLUDE listings clearly located elsewhere (Toronto, Ottawa, Vancouver,
   Calgary, USA, etc.) that are not remote/hybrid. EXCEPTION: application
   confirmations, recruiter outreach, interview invitations, and status updates about
   the user's OWN applications are always relevant — never drop those for location.

OUTPUT FORMAT — return a JSON object with EXACTLY this shape:
{
  "results": [
    {
      "email_index": <integer index from the email block>,
      "subject": "<copy the subject verbatim>",
      "from": "<copy the from verbatim>",
      "date": "<copy the date verbatim>",
      "company": "<extract the hiring company name from THIS email's body — do not invent>",
      "category": "internship",
      "summary": "<short summary of THIS email only>",
      "action_items": ["..."],
      "priority": "high"
    }
  ]
}

CRITICAL: Each result must reflect the corresponding email's content. Do NOT mix
information across emails in the batch. Do NOT use placeholder/example values
verbatim. If you cannot determine the company from the email body, set company to null.

Use EXACTLY these field names — do NOT rename "results" to "jobs"/"emails", and do NOT
rename "subject" to "job_title". Keep the field names verbatim.

Allowed values for "category": "internship", "recruiter", "confirmation", "reply", "status".
Allowed values for "priority": "high", "medium", "low".

If NO emails in this batch are relevant, return: {"results": []}

Emails:
${emailText}`;
}

/**
 * Low-memory prompt: relevance is already decided by the rule filter, so this
 * asks only for the two fields rules cannot produce. Kept deliberately short —
 * every token here competes with the KV cache for the mobile memory budget.
 */
function buildExtractPrompt(e: GmailMessage): string {
  const body = relevantExcerpt(e.from ?? "", e.body ?? "", LOW_MEM_BODY_CHARS);
  return `Extract two fields from this internship email. Reply with JSON only, no other text.

{"company": "<name of the hiring company, copied exactly from the text below, or null>", "summary": "<one sentence, at most 20 words, describing this email>"}

Rules:
- Copy the company name character-for-character from the text. Never invent one.
- If no company name appears in the text, use null.
- The summary must describe THIS email only.

Subject: ${sanitizeForPrompt(e.subject ?? "")}
From: ${sanitizeForPrompt(e.from ?? "")}
Body: ${sanitizeForPrompt(body)}`;
}

function sanitizeForPrompt(s: string): string {
  return s ? s.replace(/[“”]/g, '"').replace(/[‘’]/g, "'") : s;
}

function buildEmailText(emails: GmailMessage[], offset: number): string {
  let t = "";
  emails.forEach((e, i) => {
    t += `\n--- Email ${offset + i} ---\n`;
    t += `Subject: ${sanitizeForPrompt(e.subject)}\n`;
    t += `From: ${sanitizeForPrompt(e.from)}\n`;
    t += `Date: ${e.date}\n`;
    t += `Body: ${sanitizeForPrompt(e.body ?? "")}\n`;
  });
  return t;
}

// ── Response parsing (mirror _extract_results_array + _normalize_result) ─────
function extractResultsArray(parsed: unknown): unknown[] {
  if (Array.isArray(parsed)) return parsed;
  if (!parsed || typeof parsed !== "object") return [];
  const obj = parsed as Record<string, unknown>;
  for (const key of ["results", "jobs", "emails", "items", "data", "matches"]) {
    if (Array.isArray(obj[key])) return obj[key] as unknown[];
  }
  for (const val of Object.values(obj)) {
    if (Array.isArray(val) && val.length && typeof val[0] === "object") return val as unknown[];
  }
  if (["subject", "job_title", "email_index"].some((k) => k in obj)) return [obj];
  return [];
}

function normalizeResult(item: unknown): ResultItem {
  if (!item || typeof item !== "object") return coerceResult({});
  const it = item as Record<string, unknown>;
  const pick = (alts: string[]): unknown => {
    for (const a of alts) if (it[a] !== undefined && it[a] !== null) return it[a];
    return undefined;
  };
  const asStr = (v: unknown): string | undefined =>
    v === undefined || v === null ? undefined : typeof v === "string" ? v : String(v);

  const ei = pick(["email_index", "index", "id", "number"]);
  const ai = pick(["action_items", "actions", "next_steps", "todos"]);
  const company = pick(["company", "employer", "organization"]);

  const out: ResultItem = {
    subject: asStr(pick(["subject", "job_title", "title", "headline"])),
    from: asStr(pick(["from", "sender", "source"])),
    company: company === undefined || company === null ? null : asStr(company) ?? null,
    category: asStr(pick(["category", "type", "kind"])),
    summary: asStr(pick(["summary", "description", "details"])),
    action_items: Array.isArray(ai) ? ai.map((x) => String(x)) : undefined,
    priority: asStr(pick(["priority", "importance"])),
    email_index: typeof ei === "number" ? ei : undefined,
    date: asStr(pick(["date", "received"])),
  };
  return coerceResult(out);
}

// ── Ollama backend ───────────────────────────────────────────────────────────
function estimateCtx(prompt: string, numPredict: number): number {
  const needed = Math.floor(prompt.length / 3) + numPredict + 256;
  let bucket = 2048;
  while (bucket < needed) bucket *= 2;
  return bucket;
}

async function checkOllama(url: string, model: string): Promise<void> {
  let data: { models?: Array<{ name: string }> };
  try {
    const res = await fetch(`${url}/api/tags`);
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch {
    throw new Error(
      `Cannot reach Ollama at ${url}. Start it with: ollama serve. ` +
        `For a deployed page, allow this origin: OLLAMA_ORIGINS="${location.origin}" ollama serve`,
    );
  }
  const models = (data.models ?? []).map((m) => m.name);
  const base = model.split(":")[0];
  if (!models.some((m) => m.includes(base))) {
    throw new Error(
      `Model '${model}' not found in Ollama. Pull it: ollama pull ${model}. ` +
        `Available: ${models.join(", ") || "(none)"}`,
    );
  }
}

async function ollamaGenerate(
  url: string,
  model: string,
  prompt: string,
  temperature: number,
  numPredict: number,
): Promise<string> {
  const res = await fetch(`${url}/api/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model,
      prompt,
      stream: false,
      format: "json",
      think: false, // Qwen3 family: route content out of the <think> block
      options: { temperature, num_predict: numPredict, num_ctx: estimateCtx(prompt, numPredict) },
    }),
  });
  if (!res.ok) throw new Error(`Ollama request failed (${res.status})`);
  const data = (await res.json()) as { response?: string };
  return (data.response ?? "").trim();
}

// ── WebLLM backend ───────────────────────────────────────────────────────────
type WebllmEngine = {
  chat: {
    completions: {
      create: (req: unknown) => Promise<{ choices: Array<{ message?: { content?: string } }> }>;
    };
  };
  unload?: () => Promise<void>;
};

let engine: WebllmEngine | null = null;
let engineKey = "";

export type ModelProgress = { text: string; progress: number };

async function getWebllmEngine(
  modelId: string,
  onProgress?: (p: ModelProgress) => void,
  ctxSize?: number,
): Promise<WebllmEngine> {
  // Context size is baked in at load time, so it belongs in the cache key —
  // reusing a 4096-ctx engine for the low-memory path would defeat the point.
  const key = `${modelId}|${ctxSize ?? "default"}`;
  if (engine && engineKey === key) return engine;
  const webllm = await import("@mlc-ai/web-llm");
  if (engine?.unload) await engine.unload();
  engine = (await webllm.CreateMLCEngine(
    modelId,
    {
      initProgressCallback: (p: { text: string; progress: number }) =>
        onProgress?.({ text: p.text, progress: p.progress }),
    },
    ctxSize ? { context_window_size: ctxSize } : undefined,
  )) as unknown as WebllmEngine;
  engineKey = key;
  return engine;
}

async function webllmGenerate(
  eng: WebllmEngine,
  prompt: string,
  temperature: number,
  maxTokens: number,
): Promise<string> {
  const resp = await eng.chat.completions.create({
    messages: [{ role: "user", content: prompt }],
    temperature,
    max_tokens: maxTokens,
    response_format: { type: "json_object" },
  });
  return (resp.choices[0]?.message?.content ?? "").trim();
}

// ── Capability detection (answers "what can this PC run?") ───────────────────
export type Capability = {
  webgpu: boolean;
  mobile: boolean;
  deviceMemoryGB?: number;
  cores?: number;
  gpuVendor?: string;
  maxBufferMB?: number;
};

/**
 * iOS/Android browsers hard-kill a tab that exceeds a per-tab memory ceiling far
 * below desktop limits, regardless of what the GPU adapter advertises — so this
 * has to gate model choice independently of maxBufferSize.
 */
function detectMobile(): boolean {
  if (typeof navigator === "undefined") return false;
  // iPadOS 13+ reports platform "MacIntel"; touch points disambiguate it from a Mac.
  const iPadOS = navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1;
  return iPadOS || /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);
}

export async function detectCapability(): Promise<Capability> {
  const nav = navigator as Navigator & { deviceMemory?: number; gpu?: unknown };
  const cap: Capability = {
    webgpu: typeof nav.gpu !== "undefined",
    mobile: detectMobile(),
    deviceMemoryGB: nav.deviceMemory,
    cores: navigator.hardwareConcurrency,
  };
  if (cap.webgpu) {
    try {
      const gpu = nav.gpu as { requestAdapter: () => Promise<unknown> };
      const adapter = (await gpu.requestAdapter()) as {
        limits?: { maxBufferSize?: number };
        info?: { vendor?: string; architecture?: string };
        requestAdapterInfo?: () => Promise<{ vendor?: string; architecture?: string }>;
      } | null;
      if (adapter) {
        cap.maxBufferMB = Math.round((adapter.limits?.maxBufferSize ?? 0) / 1e6);
        const ai = adapter.info ?? (await adapter.requestAdapterInfo?.());
        cap.gpuVendor = ai?.vendor || ai?.architecture || undefined;
      }
    } catch {
      // adapter probe failed — leave GPU fields undefined
    }
  }
  return cap;
}

export type ModelOption = { id: string; label: string; vramMB: number };

// Curated subset of @mlc-ai/web-llm prebuilt models, ascending by cost. `vramMB`
// is the model's declared `vram_required_MB` from prebuiltAppConfig — not an
// estimate — so the labels and the mobile budget below track the real numbers.
export const MODEL_OPTIONS: ModelOption[] = [
  { id: "Llama-3.2-1B-Instruct-q4f16_1-MLC", label: "Llama 3.2 1B — mobile / low memory (0.9 GB)", vramMB: 879 },
  { id: "Qwen2.5-1.5B-Instruct-q4f16_1-MLC", label: "Qwen2.5 1.5B — fastest, lightest (1.6 GB)", vramMB: 1630 },
  { id: "Llama-3.2-3B-Instruct-q4f16_1-MLC", label: "Llama 3.2 3B — balanced (2.3 GB)", vramMB: 2264 },
  { id: "Qwen2.5-3B-Instruct-q4f16_1-MLC", label: "Qwen2.5 3B — balanced (2.5 GB)", vramMB: 2505 },
  { id: "Llama-3.1-8B-Instruct-q4f16_1-MLC", label: "Llama 3.1 8B — best quality (5.0 GB)", vramMB: 5001 },
  { id: "Qwen2.5-7B-Instruct-q4f16_1-MLC", label: "Qwen2.5 7B — best quality (5.1 GB)", vramMB: 5107 },
];

// Largest model that has a realistic chance of surviving a mobile tab.
export const MOBILE_MODEL_ID = "Llama-3.2-1B-Instruct-q4f16_1-MLC";

/** Pick a sensible default model id given detected capability. */
export function recommendModel(cap: Capability): string {
  // Must come first: `deviceMemory` is Chromium-only (undefined in all Safari),
  // and a phone's advertised maxBufferSize can clear 4 GB — so the desktop
  // tiering below would otherwise hand an iPhone the 5.1 GB model.
  if (cap.mobile) return MOBILE_MODEL_ID;

  const bufOk7b = (cap.maxBufferMB ?? 0) >= 4000;
  const memOk7b = (cap.deviceMemoryGB ?? 0) >= 8;
  if (bufOk7b || memOk7b) return "Qwen2.5-7B-Instruct-q4f16_1-MLC";
  if ((cap.deviceMemoryGB ?? 0) >= 4 || (cap.maxBufferMB ?? 0) >= 1500) {
    return DEFAULT_WEBLLM_MODEL;
  }
  return "Qwen2.5-1.5B-Instruct-q4f16_1-MLC";
}

// ── Main analyze (mirror analyze_with_ollama: pre-filter, two-pass union, post) ─
export type AnalyzeOptions = {
  backend: LlmBackend;
  webllmModel?: string;
  ollamaUrl?: string;
  ollamaModel?: string;
  /** Phone/tablet profile: rules classify, the model only extracts fields. */
  lowMemory?: boolean;
  onLog?: (line: string) => void;
  onProgress?: (fraction: number) => void;
  onModelProgress?: (p: ModelProgress) => void;
  signal?: AbortSignal;
};

type GenerateFn = (prompt: string, temperature: number, maxTokens: number) => Promise<string>;

async function makeGenerate(opts: AnalyzeOptions): Promise<GenerateFn> {
  if (opts.backend === "ollama") {
    const url = opts.ollamaUrl || DEFAULT_OLLAMA_URL;
    const model = opts.ollamaModel || DEFAULT_OLLAMA_MODEL;
    opts.onLog?.(`Checking Ollama at ${url} (model ${model})…`);
    await checkOllama(url, model);
    return (prompt, temp, maxTokens) => ollamaGenerate(url, model, prompt, temp, maxTokens);
  }
  const model = opts.webllmModel || DEFAULT_WEBLLM_MODEL;
  opts.onLog?.(`Loading WebLLM model ${model} (first run downloads it)…`);
  const eng = await getWebllmEngine(
    model,
    opts.onModelProgress,
    opts.lowMemory ? LOW_MEM_CTX : undefined,
  );
  return (prompt, temp, maxTokens) => webllmGenerate(eng, prompt, temp, maxTokens);
}

/**
 * Low-memory pipeline: the rule filter owns relevance (same gate the full path
 * uses as its pre-filter), and the model runs one short extraction per kept
 * email to fill in company + summary. Extraction failures degrade to the plain
 * rule-based result rather than dropping the email.
 */
async function analyzeLowMemory(
  emails: GmailMessage[],
  generate: GenerateFn,
  opts: AnalyzeOptions,
): Promise<ResultItem[]> {
  const { onLog, onProgress, signal } = opts;
  const base = ruleBasedAnalyze(emails);
  const byId = new Map(emails.map((e) => [e.id ?? "", e]));
  const indexById = new Map(emails.map((e, i) => [e.id ?? "", i + 1]));
  // Pin each result to its source index. Without this postFilterResults falls
  // back to subject matching, and recurring digests that share a subject would
  // collide and get dropped as duplicates.
  for (const r of base) r.email_index = indexById.get(r.id ?? "");
  onLog?.(`Low-memory mode: ${base.length} rule-based match(es), extracting fields…`);

  for (let i = 0; i < base.length; i++) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const src = byId.get(base[i].id ?? "");
    if (!src) continue;
    onLog?.(`Email ${i + 1}/${base.length} · extracting…`);
    try {
      const raw = await generate(buildExtractPrompt(src), 0.0, LOW_MEM_MAX_TOKENS);
      const parsed = JSON.parse(raw) as { company?: unknown; summary?: unknown };
      const company = typeof parsed.company === "string" ? parsed.company.trim() : "";
      const summary = typeof parsed.summary === "string" ? parsed.summary.trim() : "";
      if (company) base[i].company = company;
      if (summary) base[i].summary = summary;
    } catch (e) {
      onLog?.(`  [!] extraction failed, keeping rule-based fields: ${(e as Error).message}`);
    }
    onProgress?.((i + 1) / base.length);
  }

  // Still validate: postFilterResults nulls any company not present in the
  // source text, which is the main hallucination risk with a 1B model.
  return postFilterResults(base, emails);
}

export async function analyze(emails: GmailMessage[], opts: AnalyzeOptions): Promise<ResultItem[]> {
  const { onLog, onProgress, signal } = opts;

  // Pre-filter: only analyze emails with an internship signal.
  const filteredIn = emails.filter((e) =>
    hasInternshipSignal(e.subject ?? "", e.body ?? "", e.from ?? ""),
  );
  const droppedPre = emails.length - filteredIn.length;
  if (droppedPre) onLog?.(`Pre-filtered ${droppedPre} email(s) with no internship signal`);

  // Stable ordering by Gmail id so batch composition doesn't shift run-to-run.
  const sorted = [...filteredIn].sort((a, b) => (a.id ?? "").localeCompare(b.id ?? ""));
  if (sorted.length === 0) {
    onLog?.("No emails to analyze.");
    return [];
  }

  const generate = await makeGenerate(opts);

  if (opts.lowMemory) return analyzeLowMemory(sorted, generate, opts);

  const results: ResultItem[] = [];
  const total = sorted.length; // BATCH_SIZE = 1
  for (let b = 0; b < total; b++) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const offset = b + 1;
    const emailText = buildEmailText([sorted[b]], offset);
    const prompt = buildPrompt(emailText);

    const batchCombined: ResultItem[] = [];
    const seenInBatch = new Set<number>();
    for (let p = 0; p < LLM_PASS_TEMPERATURES.length; p++) {
      const temp = LLM_PASS_TEMPERATURES[p];
      onLog?.(`Email ${b + 1}/${total} · pass ${p + 1}/${LLM_PASS_TEMPERATURES.length} (t=${temp})…`);
      let passResults: ResultItem[];
      try {
        const raw = await generate(prompt, temp, MAX_TOKENS);
        passResults = extractResultsArray(JSON.parse(raw)).map(normalizeResult);
      } catch (e) {
        onLog?.(`  [!] pass failed, skipping: ${(e as Error).message}`);
        continue;
      }
      for (const r of passResults) {
        const idx = r.email_index;
        if (typeof idx === "number") {
          if (seenInBatch.has(idx)) continue;
          seenInBatch.add(idx);
        }
        batchCombined.push(r);
      }
    }
    results.push(...batchCombined);
    onProgress?.((b + 1) / total);
  }

  // Validate against the real emails (mirror the safety-filter tail).
  const final = postFilterResults(results, sorted);
  onLog?.(`${final.length} candidate(s) kept.`);
  return final;
}
