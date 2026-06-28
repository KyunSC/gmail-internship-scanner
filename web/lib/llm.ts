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
  type ResultItem,
} from "@/lib/filter";

export type LlmBackend = "webllm" | "ollama";

const LLM_PASS_TEMPERATURES = [0.0, 0.5]; // deterministic baseline + diversity pass
const DEFAULT_OLLAMA_URL = "http://localhost:11434";
const DEFAULT_OLLAMA_MODEL = "qwen3.5:9b";
const DEFAULT_WEBLLM_MODEL = "Qwen2.5-3B-Instruct-q4f16_1-MLC";

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
      options: { temperature, num_predict: 4096, num_ctx: estimateCtx(prompt, 4096) },
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
let engineModel = "";

export type ModelProgress = { text: string; progress: number };

async function getWebllmEngine(
  modelId: string,
  onProgress?: (p: ModelProgress) => void,
): Promise<WebllmEngine> {
  if (engine && engineModel === modelId) return engine;
  const webllm = await import("@mlc-ai/web-llm");
  if (engine?.unload) await engine.unload();
  engine = (await webllm.CreateMLCEngine(modelId, {
    initProgressCallback: (p: { text: string; progress: number }) =>
      onProgress?.({ text: p.text, progress: p.progress }),
  })) as unknown as WebllmEngine;
  engineModel = modelId;
  return engine;
}

async function webllmGenerate(
  eng: WebllmEngine,
  prompt: string,
  temperature: number,
): Promise<string> {
  const resp = await eng.chat.completions.create({
    messages: [{ role: "user", content: prompt }],
    temperature,
    max_tokens: 4096,
    response_format: { type: "json_object" },
  });
  return (resp.choices[0]?.message?.content ?? "").trim();
}

// ── Capability detection (answers "what can this PC run?") ───────────────────
export type Capability = {
  webgpu: boolean;
  deviceMemoryGB?: number;
  cores?: number;
  gpuVendor?: string;
  maxBufferMB?: number;
};

export async function detectCapability(): Promise<Capability> {
  const nav = navigator as Navigator & { deviceMemory?: number; gpu?: unknown };
  const cap: Capability = {
    webgpu: typeof nav.gpu !== "undefined",
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

export type ModelOption = { id: string; label: string };

// Curated subset of @mlc-ai/web-llm prebuilt models (ids verified present).
export const MODEL_OPTIONS: ModelOption[] = [
  { id: "Qwen2.5-1.5B-Instruct-q4f16_1-MLC", label: "Qwen2.5 1.5B — fastest, lightest (~1.2 GB)" },
  { id: "Qwen2.5-3B-Instruct-q4f16_1-MLC", label: "Qwen2.5 3B — balanced (~2.2 GB)" },
  { id: "Llama-3.2-3B-Instruct-q4f16_1-MLC", label: "Llama 3.2 3B — balanced (~2.3 GB)" },
  { id: "Qwen2.5-7B-Instruct-q4f16_1-MLC", label: "Qwen2.5 7B — best quality (~4.5 GB)" },
  { id: "Llama-3.1-8B-Instruct-q4f16_1-MLC", label: "Llama 3.1 8B — best quality (~4.6 GB)" },
];

/** Pick a sensible default model id given detected capability. */
export function recommendModel(cap: Capability): string {
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
  onLog?: (line: string) => void;
  onProgress?: (fraction: number) => void;
  onModelProgress?: (p: ModelProgress) => void;
  signal?: AbortSignal;
};

async function makeGenerate(
  opts: AnalyzeOptions,
): Promise<(prompt: string, temperature: number) => Promise<string>> {
  if (opts.backend === "ollama") {
    const url = opts.ollamaUrl || DEFAULT_OLLAMA_URL;
    const model = opts.ollamaModel || DEFAULT_OLLAMA_MODEL;
    opts.onLog?.(`Checking Ollama at ${url} (model ${model})…`);
    await checkOllama(url, model);
    return (prompt, temp) => ollamaGenerate(url, model, prompt, temp);
  }
  const model = opts.webllmModel || DEFAULT_WEBLLM_MODEL;
  opts.onLog?.(`Loading WebLLM model ${model} (first run downloads it)…`);
  const eng = await getWebllmEngine(model, opts.onModelProgress);
  return (prompt, temp) => webllmGenerate(eng, prompt, temp);
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
        const raw = await generate(prompt, temp);
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
