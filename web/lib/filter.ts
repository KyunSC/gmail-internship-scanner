// Rule-based pre/post filter, ported faithfully from cli/scanner.py.
// Pure functions, no I/O — used to (a) pre-filter emails before the LLM,
// (b) post-validate LLM output, and (c) power the no-LLM "fast" path.

import type { GmailMessage } from "@/lib/gmail";

export type ResultItem = {
  id?: string;
  email_index?: number;
  subject?: string;
  from?: string;
  date?: string;
  company?: string | null;
  category?: string;
  summary?: string;
  action_items?: string[];
  priority?: string;
};

// ── Sender sets ──────────────────────────────────────────────────────────────
const AGGREGATOR_SENDERS = ["linkedin.com", "glassdoor.com", "jobright.ai", "match.indeed.com"];

const RECRUITER_SENDER_HINTS = [
  "talent@", "careers@", "career@", "hr@", "recruiter@", "recruiting@",
  "hiring@", "jobs@", "noreply@", "no-reply@",
  "lever.co", "greenhouse.io", "workday.com", "myworkday.com",
  "icims.com", "smartrecruiters", "ashbyhq", "bamboohr",
  "successfactors", "taleo.net",
];

// Indeed engagement nudges from no-reply@indeed.com are never job postings.
const INDEED_NOISE_SENDERS = ["no-reply@indeed.com", "noreply@indeed.com"];

// ── Keyword sets ─────────────────────────────────────────────────────────────
const SOFTWARE_KEYWORDS = [
  "software", "developer", "développeur", "developpeur",
  "programmer", "programming", "coding",
  "computer science", "computer engineering", "computer engineer",
  "informatique", "génie logiciel", "genie logiciel",
  "frontend", "front-end",
  "backend", "back-end",
  "full-stack", "fullstack", "full stack",
  "devops", "site reliability",
  "data science", "data scientist", "data analyst", "data engineer",
  "machine learning", "deep learning",
  "cybersecurity", "cyber security",
  "cloud engineer", "cloud developer",
  "embedded systems", "embedded software", "embedded engineer", "firmware",
  "robotics",
  "ios developer", "ios engineer", "android developer", "android engineer",
  "mobile developer", "mobile engineer",
  "python", "javascript", "typescript",
  "automation tester", "automation engineer", "qa engineer", "qa analyst",
  "qa automation", "test engineer", "sdet",
  "tech intern", "technology intern", "it intern",
  "software engineer", "software engineering",
  "systems engineer", "systems engineering",
  "hardware engineer", "fpga",
  "sde intern", "swe intern",
  "web developer", "web engineer",
  "engineering intern", "engineering internship",
  "engineering co-op", "engineering coop", "engineering student",
  "stage en génie", "stage en informatique", "stagiaire en informatique",
];

// ── Regexes (mirror scanner.py; no `g` flag so .test() stays stateless) ──────
const LOCATION_REGEXES: RegExp[] = [
  /\bmontr[eé]al\b/i,
  /\bmtl\b/i,
  /\bgreater montreal\b/i,
  /\bgrand montr[eé]al\b/i,
  /\b(?:laval|longueuil|brossard)\b/i,
  /\bqu[eé]bec\b/i,
  /\bqc\b/i,
  /\bremote\b/i,
  /\bt[eé]l[eé][\s-]?travail\b/i,
  /\bwork[\s-]?from[\s-]?home\b/i,
  /\bwfh\b/i,
  /\bhybride?\b/i,
];

const STAGE_FRENCH_RE =
  /(?:(?:un|de|du|en|le|la|au|bénévole|offre|recherche|cherche|propose)\s+stage\b|\bstage\s+(?:en|de|du|chez|pour|rémunéré|développeur|developer|informatique|logiciel|étudiant|pratique))/i;

const EXCLUDE_TERM_REGEX =
  /\b(fall|automne)\b.{0,20}\b2026\b|\b2026\b.{0,20}\b(fall|automne)\b|\bsept(\.|ember|embre)?\s*\.?\s*2026\b|\b[fa][-\s]?2026\b/i;

const GLASSDOOR_HEAD_RE = /Your job listings for\s+\w+\s+\d+,\s+\d{4}/i;
const GLASSDOOR_FOOT_RE = /See more jobs|Want more listings/;

const INTERNSHIP_WORD_RE = /\b(?:interns?|internships?|stagiaires?|students?|co-?ops?)\b/i;
const STAGE_WORD_RE = /\bstages?\b/i;

// ── Digest splitting ─────────────────────────────────────────────────────────
function cleanGlassdoorBody(body: string): string {
  const head = GLASSDOOR_HEAD_RE.exec(body);
  if (head) {
    body = body.slice(head.index + head[0].length);
    const star = body.indexOf("★");
    if (star >= 0) body = body.slice(star + 1);
  }
  const foot = GLASSDOOR_FOOT_RE.exec(body);
  if (foot) body = body.slice(0, foot.index);
  return body;
}

function splitAggregatorListings(sender: string, body: string): string[] {
  const s = (sender || "").toLowerCase();
  if (s.includes("glassdoor.com") && body.includes("★")) {
    return cleanGlassdoorBody(body)
      .split("★")
      .map((c) => c.trim())
      .filter(Boolean);
  }
  if (s.includes("jobright.ai") && body.includes("APPLY NOW")) {
    return body.split("APPLY NOW").map((c) => c.trim()).filter(Boolean);
  }
  if (s.includes("match.indeed.com") && body.includes("Easily apply")) {
    return body.split("Easily apply").map((c) => c.trim()).filter(Boolean);
  }
  return [body];
}

// ── Predicates ───────────────────────────────────────────────────────────────
export function isAggregator(sender: string): boolean {
  const s = (sender || "").toLowerCase();
  return AGGREGATOR_SENDERS.some((d) => s.includes(d));
}

function isRecruiterSender(sender: string): boolean {
  const s = (sender || "").toLowerCase();
  return RECRUITER_SENDER_HINTS.some((h) => s.includes(h));
}

function isIndeedNoiseSender(sender: string): boolean {
  const s = (sender || "").toLowerCase();
  return INDEED_NOISE_SENDERS.some((a) => s.includes(a));
}

function subjectMentionsInternship(subject: string): boolean {
  const s = subject || "";
  return INTERNSHIP_WORD_RE.test(s) || STAGE_WORD_RE.test(s);
}

function bodyMentionsInternship(body: string): boolean {
  const b = body || "";
  return INTERNSHIP_WORD_RE.test(b) || STAGE_FRENCH_RE.test(b);
}

function bodyMentionsSoftware(body: string): boolean {
  const b = (body || "").toLowerCase();
  return SOFTWARE_KEYWORDS.some((kw) => b.includes(kw));
}

function mentionsLocation(text: string): boolean {
  if (!text) return false;
  return LOCATION_REGEXES.some((rx) => rx.test(text));
}

function hasSoftwareInternshipListing(sender: string, body: string): boolean {
  for (const c of splitAggregatorListings(sender, body)) {
    if (bodyMentionsInternship(c) && bodyMentionsSoftware(c) && mentionsLocation(c)) return true;
  }
  return false;
}

/**
 * Combined signal: an email must carry BOTH an internship signal AND a
 * software/tech signal. Aggregator digests use the stricter per-listing form.
 */
export function hasInternshipSignal(subject: string, body: string, sender: string): boolean {
  if (isIndeedNoiseSender(sender)) return false;
  if (isAggregator(sender)) {
    if (subjectMentionsInternship(subject) && bodyMentionsSoftware(subject) && mentionsLocation(subject)) {
      return true;
    }
    return hasSoftwareInternshipListing(sender, body);
  }
  const hasSoftware = bodyMentionsSoftware(subject) || bodyMentionsSoftware(body);
  if (subjectMentionsInternship(subject) || bodyMentionsInternship(body)) return hasSoftware;
  return isRecruiterSender(sender) && hasSoftware;
}

function allInternListingsExcluded(sender: string, body: string): boolean {
  const internChunks = splitAggregatorListings(sender, body).filter(
    (c) => bodyMentionsInternship(c) || subjectMentionsInternship(c),
  );
  if (internChunks.length === 0) return false;
  return internChunks.every((c) => EXCLUDE_TERM_REGEX.test(c));
}

// ── Result coercion + post-filter (mirror _validate_result + analyze tail) ───
const VALID_CATEGORIES = new Set(["internship", "recruiter", "confirmation", "reply", "status"]);
const VALID_PRIORITIES = new Set(["high", "medium", "low"]);

export function coerceResult(out: ResultItem): ResultItem {
  const cat = String(out.category ?? "").toLowerCase();
  out.category = VALID_CATEGORIES.has(cat) ? cat : "internship";
  const pri = String(out.priority ?? "").toLowerCase();
  out.priority = VALID_PRIORITIES.has(pri) ? pri : "medium";
  out.action_items = out.action_items ?? [];
  out.summary = out.summary ?? "";
  if (out.company === undefined) out.company = null;
  return out;
}

/**
 * Validate LLM results against the real emails: map fields back to the source
 * email, null hallucinated companies, drop aggregator-no-signal / Fall-2026 /
 * duplicate / unmatched results. Mirrors the safety-filter block in
 * analyze_with_ollama.
 */
export function postFilterResults(results: ResultItem[], emails: GmailMessage[]): ResultItem[] {
  const bySubject = new Map<string, GmailMessage>();
  for (const e of emails) {
    const key = (e.subject || "").trim().toLowerCase();
    if (key) bySubject.set(key, e);
  }
  const indexById = new Map<string, number>();
  emails.forEach((e, i) => {
    if (e.id) indexById.set(e.id, i + 1);
  });

  const filtered: ResultItem[] = [];
  const seenIndices = new Set<number>();

  for (const r of results) {
    const idx = r.email_index;
    let original: GmailMessage | undefined;
    let matchIdx: number | undefined;
    if (typeof idx === "number" && idx >= 1 && idx <= emails.length) {
      original = emails[idx - 1];
      matchIdx = idx;
    } else {
      const key = (r.subject || "").trim().toLowerCase();
      original = bySubject.get(key);
      if (original) matchIdx = indexById.get(original.id ?? "");
    }
    if (!original) continue; // hallucinated email — drop

    const out: ResultItem = {
      ...r,
      from: original.from ?? "",
      subject: original.subject ?? "",
      date: original.date ?? "",
      id: original.id ?? "",
    };

    const company = out.company;
    if (typeof company === "string" && company.trim()) {
      const haystack = `${original.body ?? ""} ${original.subject ?? ""} ${original.from ?? ""}`.toLowerCase();
      if (!haystack.includes(company.trim().toLowerCase())) out.company = null;
    }

    const sender = out.from ?? "";
    const subject = out.subject ?? "";
    const body = original.body ?? "";

    if (isAggregator(sender) && !hasInternshipSignal(subject, body, sender)) continue;
    if (allInternListingsExcluded(sender, body)) continue;

    if (matchIdx !== undefined) {
      if (seenIndices.has(matchIdx)) continue;
      seenIndices.add(matchIdx);
    }
    filtered.push(out);
  }

  return filtered;
}

/** No-LLM fast path (mirror rule_based_analyze). */
export function ruleBasedAnalyze(emails: GmailMessage[]): ResultItem[] {
  const out: ResultItem[] = [];
  for (const e of emails) {
    const subject = e.subject ?? "";
    const body = e.body ?? "";
    const sender = e.from ?? "";
    if (!hasInternshipSignal(subject, body, sender)) continue;
    if (allInternListingsExcluded(sender, body)) continue;
    out.push({
      id: e.id ?? "",
      subject,
      from: sender,
      date: e.date ?? "",
      company: null,
      category: "internship",
      summary: "(rule-based match — full-body keyword filter)",
      action_items: [],
      priority: "medium",
    });
  }
  return out;
}
