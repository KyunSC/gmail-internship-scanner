import { spawn } from "node:child_process";
import path from "node:path";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const PROJECT_ROOT = path.resolve(process.cwd(), "..");
const PYTHON = path.join(PROJECT_ROOT, "venv", "bin", "python");
const SCANNER = path.join(PROJECT_ROOT, "scanner.py");
const RESULTS_OUT = path.join(PROJECT_ROOT, ".last_results.json");

type ScanOptions = {
  keyword?: string;
  days?: number;
  maxEmails?: number;
  all?: boolean;
  fast?: boolean;
};

function buildArgs(opts: ScanOptions): string[] {
  const args = ["-u", SCANNER];
  if (opts.keyword) args.push("-k", opts.keyword);
  if (typeof opts.days === "number") args.push("-d", String(opts.days));
  if (typeof opts.maxEmails === "number") args.push("-n", String(opts.maxEmails));
  if (opts.all) args.push("--all");
  if (opts.fast) args.push("--fast");
  args.push("-o", RESULTS_OUT);
  return args;
}

export async function POST(req: Request) {
  let opts: ScanOptions = {};
  try {
    opts = (await req.json()) as ScanOptions;
  } catch {
    opts = {};
  }

  const args = buildArgs(opts);
  const child = spawn(PYTHON, args, { cwd: PROJECT_ROOT });

  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const send = (chunk: Buffer) => {
        try {
          controller.enqueue(encoder.encode(chunk.toString("utf8")));
        } catch {
          // controller closed
        }
      };
      child.stdout.on("data", send);
      child.stderr.on("data", send);
      child.on("close", (code) => {
        try {
          controller.enqueue(encoder.encode(`\n[exit ${code ?? 0}]\n`));
          controller.close();
        } catch {
          // already closed
        }
      });
      child.on("error", (err) => {
        try {
          controller.enqueue(encoder.encode(`\n[error] ${err.message}\n`));
          controller.close();
        } catch {
          // already closed
        }
      });
    },
    cancel() {
      child.kill("SIGTERM");
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
    },
  });
}
