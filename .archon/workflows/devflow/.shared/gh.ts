// gh api helpers shared by the devflow scripts that read or change GitHub repository settings.
import { run } from "./util.ts";

export interface ApiResult {
  status: "ok" | "forbidden" | "not_found" | "error";
  code: number;
  data: unknown;
  message: string;
}

export function repoSlug(): string {
  const r = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]);
  if (r.code !== 0) throw new Error(`cannot resolve the repository (gh repo view): ${r.err.trim()}`);
  return r.out.trim();
}

// GET (or any method with an optional JSON body). Never throws: the caller decides what a 403 means.
export function api(method: string, endpoint: string, body?: string): ApiResult {
  const args = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json", endpoint];
  if (body) args.push("--input", "-");
  const proc = Bun.spawnSync(args, { stdin: body ? new TextEncoder().encode(body) : undefined, stdout: "pipe", stderr: "pipe", env: process.env });
  const out = proc.stdout.toString();
  const err = proc.stderr.toString();
  const http = Number((err.match(/HTTP (\d{3})/) ?? [])[1] ?? (proc.exitCode === 0 ? 200 : 0));
  let data: unknown = null;
  try {
    data = out.trim() ? JSON.parse(out) : null;
  } catch {
    data = out;
  }
  if (proc.exitCode === 0) return { status: "ok", code: http, data, message: "" };
  const message = (typeof data === "object" && data && "message" in data ? String((data as any).message) : err).trim().split("\n")[0];
  return { status: http === 403 || http === 401 ? "forbidden" : http === 404 ? "not_found" : "error", code: http, data, message };
}
