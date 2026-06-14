import { NextResponse } from "next/server";
import { promises as fs } from "node:fs";
import path from "node:path";

export const dynamic = "force-dynamic";

const PROJECT_ROOT = path.resolve(process.cwd(), "..");
const SCAN_CACHE = path.join(PROJECT_ROOT, ".last_scan.json");
const RESULTS_CACHE = path.join(PROJECT_ROOT, ".last_results.json");

async function readJson<T>(file: string): Promise<T | null> {
  try {
    const text = await fs.readFile(file, "utf8");
    return JSON.parse(text) as T;
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  }
}

async function removeIfPresent(file: string): Promise<string | null> {
  try {
    await fs.unlink(file);
    return path.basename(file);
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw err;
  }
}

export async function DELETE() {
  const removed = (
    await Promise.all([removeIfPresent(SCAN_CACHE), removeIfPresent(RESULTS_CACHE)])
  ).filter((name): name is string => name !== null);

  return NextResponse.json({ removed });
}

export async function GET() {
  const [scan, results] = await Promise.all([
    readJson<{
      scan_time?: string;
      emails?: Array<{ id: string; subject: string; from: string; date: string }>;
      kept_ids?: string[];
    }>(SCAN_CACHE),
    readJson<
      Array<{
        id?: string;
        subject?: string;
        from?: string;
        date?: string;
        company?: string | null;
        category?: string;
        summary?: string;
        action_items?: string[];
        priority?: string;
      }>
    >(RESULTS_CACHE),
  ]);

  return NextResponse.json({ scan, results });
}
