// Shared helpers for the devflow pack's scripts. Diagnostics go to stderr; stdout is the node's JSON.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

export interface Run {
  code: number;
  out: string;
  err: string;
}

export function run(cmd: string[], opts: { cwd?: string; env?: Record<string, string>; timeoutMs?: number } = {}): Run {
  const r = Bun.spawnSync(cmd, {
    cwd: opts.cwd,
    env: { ...process.env, ...(opts.env ?? {}) },
    stdout: "pipe",
    stderr: "pipe",
    timeout: opts.timeoutMs,
  });
  return { code: r.exitCode ?? 1, out: r.stdout.toString(), err: r.stderr.toString() };
}

// Runs a command that must succeed; on failure, fails the node with the command and its output.
export function must(cmd: string[], opts: Parameters<typeof run>[1] = {}): string {
  const r = run(cmd, opts);
  if (r.code !== 0) fail(`\`${cmd.join(" ")}\` exited ${r.code}\n${tail(r.out + r.err, 40)}`);
  return r.out.trim();
}

export function fail(msg: string): never {
  console.error(`devflow: ${msg}`);
  process.exit(1);
}

export function tail(text: string, lines: number): string {
  return text.trimEnd().split("\n").slice(-lines).join("\n");
}

export function artifactsDir(): string {
  const dir = process.env.ARTIFACTS_DIR;
  if (!dir) fail("ARTIFACTS_DIR is not set");
  return dir;
}

export function input(name: string, required = true): string {
  const v = process.env[`INPUTS_${name.toUpperCase()}`] ?? "";
  if (required && v === "") fail(`input '${name}' is empty`);
  return v;
}

export function onPath(exe: string): boolean {
  return run(["sh", "-c", `command -v "$0" >/dev/null`, exe]).code === 0;
}

// The project venv the setup node builds; prepended to PATH for every project command.
export function projectEnv(): Record<string, string> {
  const venvBin = join(process.cwd(), ".venv", "bin");
  return existsSync(venvBin) ? { PATH: `${venvBin}:${process.env.PATH ?? ""}`, VIRTUAL_ENV: join(process.cwd(), ".venv") } : {};
}

// The raw profile; preflight has already validated it with the toolkit's own validator.
export function profile(): any {
  const path = ".claude/toolkit.yaml";
  if (!existsSync(path)) fail(`${path} not found: this workflow runs only in a repo with a devflow profile`);
  return Bun.YAML.parse(readFileSync(path, "utf8"));
}
