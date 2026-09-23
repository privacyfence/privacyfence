/**
 * The local file bridge (ADR 0007, ``docs/adr/0007-local-file-bridge.md``):
 * the shim's one deliberate, bounded exception to proxy.ts's "no protocol
 * knowledge" rule -- see that module's own docstring for where the
 * exception is wired in.
 *
 * Privilege separation (ADR 0003) moved the daemon onto its own OS account,
 * so it can no longer ``open()`` a path in the real user's home directory --
 * a Drive upload's ``local_path``, a download's ``destination_dir``, a
 * Gmail attachment path. This process runs as the user and already sits on
 * every tool call's request path, so the daemon asks it to do those
 * reads/writes instead, over the same already-authenticated HTTP connection
 * it holds open to ``/mcp``. The python-side half of this is
 * ``src/privacyfence/local_files.py`` (decides when the bridge is needed
 * and builds the wire payloads below) and
 * ``src/privacyfence/web/routes_file_bridge.py`` (the ``/mcp-files/uploads``
 * and ``/mcp-files/downloads`` endpoints this module's PUT/GET calls hit).
 *
 * All of it rides on one vendor key under MCP's own ``_meta`` namespace,
 * ``"privacyfence.eu/file-bridge"`` (``META_KEY`` below), attached to a
 * ``CallToolResult`` the same way the daemon attaches it
 * (``routes_mcp.py``'s ``handle_call_tool``). Two flows:
 *
 * **Upload** (the daemon needs a local file the agent named as input):
 * the daemon answers a ``tools/call`` with a successful (non-error) result
 * whose ``_meta`` names ``op: "need_uploads"`` and a list of paths/slots.
 * This module intercepts that response (it never reaches Claude Desktop),
 * PUTs each file's bytes to the daemon's upload endpoint, then resends the
 * *original* request with an ``uploads`` map merged into its own ``_meta``
 * so the daemon can find the bytes it asked for. Only the response to that
 * resend is ever forwarded to the desktop.
 *
 * **Download** (the daemon produced a file the agent asked to have written
 * to disk): the daemon's successful result instead carries ``op:
 * "deliver"``, naming a download endpoint, the declared destination and a
 * few integrity fields. This module GETs the bytes, verifies size and
 * SHA-256, writes them to disk without ever overwriting an existing file,
 * then rewrites the result (the real path, ``delivery: "local_disk"``, a
 * regenerated text block) before it is forwarded.
 *
 * Every path this module ever touches must be one the agent itself named in
 * the exact request that triggered the handshake -- ``pathIsDeclaredInArguments``
 * (guard G1) is what enforces that, on both flows. This module resolves
 * ``~`` and writes to / reads from the real filesystem; the daemon never
 * sees an expanded path or a real destination until this module reports one
 * back.
 */
import { randomBytes, createHash, type Hash } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";
import type { ProxyInterceptor } from "./proxy.js";

/** The one vendor key every file-bridge payload lives under, in MCP's own
 * ``_meta`` namespace -- matches ``local_files.py``'s ``_META_KEY`` and
 * ``routes_mcp.py``'s ``_FILE_BRIDGE_META_KEY`` exactly; a typo here is a
 * silent no-op on this side (the daemon's meta simply goes unrecognized and
 * every message passes through unchanged), not a startup-time error. */
const META_KEY = "privacyfence.eu/file-bridge";

/** The header the shim advertises so the daemon knows a bridge-capable
 * client is on the other end -- matches ``routes_mcp.py``'s
 * ``_FILE_BRIDGE_HEADER`` (compared case-insensitively there, as HTTP
 * headers always are). Exported so index.ts can attach it without
 * duplicating the literal. */
export const FILE_BRIDGE_HEADER = "X-PrivacyFence-File-Bridge";

/** A remembered request is forgotten after this long even if no response
 * ever named its id -- the shim is long-running (one process per Claude
 * Desktop session) and a client that never resends after a crash or a
 * dropped connection must not leak memory for the rest of that session. Ten
 * minutes is generous for a round trip that is normally sub-second, even
 * accounting for a large upload. */
const PENDING_TTL_MS = 10 * 60 * 1000;

// ---------------------------------------------------------------------------
// Wire-format shapes. Deliberately not validated with a schema library (this
// package has none) -- each field is read defensively, one `typeof`/`in`
// check at a time, exactly like proxy.ts's own `pendingRequestId`. A daemon
// sending something this module doesn't recognize (an `op` from a future
// version, a shape that doesn't parse) is treated as "pass through
// unchanged", never as an error -- see `bridgeMetaOf`.
// ---------------------------------------------------------------------------

interface UploadFileSpec {
  path: string;
  slot: string;
  upload_path: string;
  max_bytes: number;
}

interface NeedUploadsMeta {
  v: 1;
  op: "need_uploads";
  files: UploadFileSpec[];
}

interface DeliverFileSpec {
  dest_dir: string;
  name: string;
  download_path: string;
  size_bytes: number;
  sha256: string;
  result_path_pointer: string;
}

interface DeliverMeta {
  v: 1;
  op: "deliver";
  files: DeliverFileSpec[];
}

// ---------------------------------------------------------------------------
// Pure guards and small helpers -- kept as standalone exported functions,
// not buried in the hooks below, so they're unit-testable without any
// network or filesystem double.
// ---------------------------------------------------------------------------

/** Expands a single leading ``~`` the way a shell would for a bare home
 * reference (``~`` or ``~/...``) -- Node's own ``fs`` calls never do this,
 * unlike Python's ``os.path.expanduser`` that ``local_files.py`` calls on
 * the daemon's own (non-bridge) direct-access path. Anything else --
 * ``~otheruser/...``, no leading ``~`` at all -- is returned unchanged;
 * ``~otheruser`` in particular is deliberately not resolved (Node has no
 * portable way to look up another account's home directory, and the wire
 * protocol never expects one: every path here is the current user's own). */
export function expandHome(inputPath: string, homedir: string = os.homedir()): string {
  if (inputPath === "~") {
    return homedir;
  }
  if (inputPath.startsWith("~/") || (process.platform === "win32" && inputPath.startsWith("~\\"))) {
    return path.join(homedir, inputPath.slice(2));
  }
  return inputPath;
}

/** Parses ``value`` as JSON only when the result is an array of strings --
 * the shape a Gmail-style ``attachments`` argument takes (a JSON-encoded
 * array of paths, passed as one string argument). Returns ``null`` for
 * anything else, including a parse failure; never throws. */
function parseStringArray(value: string): string[] | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed) || !parsed.every((item): item is string => typeof item === "string")) {
    return null;
  }
  return parsed;
}

/**
 * Guard G1 (ADR 0007 SS1.1/SS1.2): true only when ``declaredPath`` (trimmed)
 * is a string value the agent itself put somewhere in ``args`` -- a deep
 * walk of every nested object/array, plus, for any string argument that
 * itself parses as a JSON array of strings, a membership check against that
 * array's elements too (the Gmail ``attachments`` case). This is the one
 * thing standing between "the daemon named a path" and "the shim touches
 * that path" -- a path is never read, written, or even ``fs.stat``'d unless
 * it was found here, so a compromised or buggy daemon cannot walk the
 * user's filesystem by naming paths the agent never asked for. */
export function pathIsDeclaredInArguments(declaredPath: string, args: unknown): boolean {
  const target = declaredPath.trim();
  let found = false;
  const visit = (value: unknown): void => {
    if (found) {
      return;
    }
    if (typeof value === "string") {
      if (value === target) {
        found = true;
        return;
      }
      const asArray = parseStringArray(value);
      if (asArray !== null && asArray.includes(target)) {
        found = true;
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        visit(item);
        if (found) return;
      }
      return;
    }
    if (value !== null && typeof value === "object") {
      for (const item of Object.values(value)) {
        visit(item);
        if (found) return;
      }
    }
  };
  visit(args);
  return found;
}

/** A basename safe to join onto a directory the shim already resolved and
 * created: no path separator of either flavor (a client on Windows could
 * otherwise smuggle a traversal via ``\``, which ``path.basename`` alone
 * would not catch on POSIX), no ``.``/``..``, no embedded NUL. ``name``
 * always comes from the daemon's ``deliver`` payload, never from the agent
 * directly, but it is still untrusted input that ends up in a filesystem
 * call. */
export function isSafeBasename(name: string): boolean {
  if (name.length === 0 || name === "." || name === "..") {
    return false;
  }
  if (name.includes("/") || name.includes("\\") || name.includes("\0")) {
    return false;
  }
  return true;
}

/** Returns whether ``candidatePath`` already exists, without throwing --
 * the default ``exists`` callback for ``pickNonCollidingName``. Kept
 * separate so tests can inject a fake without touching real disk. */
function defaultExists(candidatePath: string): boolean {
  return fs.existsSync(candidatePath);
}

/** The first of ``name``, ``name (1)<ext>``, ``name (2)<ext>``, ... (split
 * on the extension, so ``report.pdf`` collides into ``report (1).pdf``, not
 * ``report.pdf (1)``) that ``exists`` reports as free in ``dir``. Pure and
 * injectable: ``exists`` defaults to a real ``fs.existsSync`` check but
 * takes any ``(candidatePath: string) => boolean`` for testing without disk
 * I/O. Never overwrites -- the caller is expected to use the returned name
 * for an exclusive-ish rename (this module renames into place, which is not
 * atomic against a concurrent writer of the exact same name, but there is
 * no multi-writer scenario for one user's own downloads directory). */
export function pickNonCollidingName(
  dir: string,
  name: string,
  exists: (candidatePath: string) => boolean = defaultExists,
): string {
  if (!exists(path.join(dir, name))) {
    return name;
  }
  const ext = path.extname(name);
  const base = name.slice(0, name.length - ext.length);
  for (let n = 1; ; n++) {
    const candidate = `${base} (${n})${ext}`;
    if (!exists(path.join(dir, candidate))) {
      return candidate;
    }
  }
}

/** Sets ``value`` at an RFC 6901 JSON Pointer inside ``root``, creating
 * intermediate objects as needed. Only the small subset this module needs:
 * object-key segments (every pointer the daemon sends is a plain top-level
 * key like ``"/path"``; array-index segments are not special-cased, since
 * nothing here ever produces one). ``pointer`` must start with ``/`` or be
 * empty (the empty case is a no-op: replacing the whole document isn't a
 * shape this protocol uses). */
export function setAtJsonPointer(root: Record<string, unknown>, pointer: string, value: unknown): void {
  if (pointer === "" || pointer === "/") {
    return;
  }
  if (!pointer.startsWith("/")) {
    return;
  }
  const segments = pointer
    .slice(1)
    .split("/")
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
  let target: Record<string, unknown> = root;
  for (let i = 0; i < segments.length - 1; i++) {
    const key = segments[i] as string;
    const next = target[key];
    if (typeof next !== "object" || next === null) {
      target[key] = {};
    }
    target = target[key] as Record<string, unknown>;
  }
  target[segments[segments.length - 1] as string] = value;
}

/** Turns a thrown value (almost always a Node ``ErrnoException`` from an
 * ``fs`` or ``fetch`` call) into the short, actionable fragment every
 * failure message below is built from -- ``"ENOENT (no such file)"`` reads
 * far better to whoever is staring at Claude's chat transcript than a raw
 * stack trace, while still naming the real errno code for anyone who wants
 * to search for it. */
function describeError(exc: unknown): string {
  if (exc && typeof exc === "object" && "code" in exc && typeof (exc as { code?: unknown }).code === "string") {
    const code = (exc as NodeJS.ErrnoException).code as string;
    const known: Record<string, string> = {
      ENOENT: "no such file",
      EACCES: "permission denied",
      EPERM: "operation not permitted",
      EISDIR: "is a directory",
      ENOTDIR: "not a directory",
      ENOSPC: "no space left on device",
      EROFS: "read-only file system",
    };
    return known[code] ? `${code} (${known[code]})` : code;
  }
  return exc instanceof Error ? exc.message : String(exc);
}

// ---------------------------------------------------------------------------
// CallToolResult shape helpers. Kept to exactly the fields this module
// reads or writes -- ``content``/``structuredContent``/``isError``/``_meta``
// -- rather than importing the SDK's full zod-inferred CallToolResult type,
// which describes every method's result shape at once and is awkward to
// build object literals against directly.
// ---------------------------------------------------------------------------

type JsonRecord = Record<string, unknown>;

function isPlainObject(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** ``{content: [{type: "text", text: message}], isError: true}`` -- the
 * shape every failure path in this module answers Claude Desktop with
 * directly (ADR 0007 SS1.1/SS1.2's "upload failure"/"write failure" cases).
 * A tool-level error, not a JSON-RPC protocol error: the call reached the
 * daemon and produced *a* result, just not the one the agent asked for. */
function errorCallToolResult(message: string): JsonRecord {
  return { content: [{ type: "text", text: message }], isError: true };
}

/** The file-bridge ``_meta`` payload on a successful ``CallToolResult``, or
 * ``undefined`` for anything that isn't one -- an error result, a result
 * with no ``_meta``, a ``_meta`` with no file-bridge key, or a file-bridge
 * payload this module doesn't recognize (wrong ``v``, an ``op`` from a
 * future version). Every one of those is "pass through unchanged", not an
 * error -- see the module docstring. */
function bridgeMetaOf(result: unknown): NeedUploadsMeta | DeliverMeta | undefined {
  if (!isPlainObject(result) || result.isError === true) {
    return undefined;
  }
  const meta = result._meta;
  if (!isPlainObject(meta)) {
    return undefined;
  }
  const bridge = meta[META_KEY];
  if (!isPlainObject(bridge) || bridge.v !== 1) {
    return undefined;
  }
  if (bridge.op === "need_uploads" && Array.isArray(bridge.files)) {
    return bridge as unknown as NeedUploadsMeta;
  }
  if (bridge.op === "deliver" && Array.isArray(bridge.files)) {
    return bridge as unknown as DeliverMeta;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// JSON-RPC envelope reading. Mirrors proxy.ts's own `pendingRequestId` in
// spirit (read only the JSON-RPC framing fields, defensively) but this
// module additionally needs to tell a `tools/call` request apart from any
// other request, and a response apart from a request/notification -- both
// still envelope-level facts, not tool-schema knowledge.
// ---------------------------------------------------------------------------

interface RpcEnvelope {
  jsonrpc?: unknown;
  id?: unknown;
  method?: unknown;
  params?: JsonRecord;
  result?: unknown;
  error?: unknown;
}

function asEnvelope(message: JSONRPCMessage): RpcEnvelope {
  return message as unknown as RpcEnvelope;
}

function requestIdOf(env: RpcEnvelope): string | number | undefined {
  return typeof env.id === "string" || typeof env.id === "number" ? env.id : undefined;
}

/** The id of a ``tools/call`` request, or ``undefined`` for anything else
 * (a different method, a notification, a response). */
function toolCallRequestId(env: RpcEnvelope): string | number | undefined {
  if (env.method !== "tools/call") {
    return undefined;
  }
  return requestIdOf(env);
}

/** The id of a response (has an id, has no ``method`` -- a request or
 * notification always has one, a response never does -- and carries either
 * ``result`` or ``error``), or ``undefined`` for anything else. */
function responseId(env: RpcEnvelope): string | number | undefined {
  if (typeof env.method === "string") {
    return undefined;
  }
  if (!("result" in env) && !("error" in env)) {
    return undefined;
  }
  return requestIdOf(env);
}

function buildResponse(id: string | number, result: JsonRecord): JSONRPCMessage {
  return { jsonrpc: "2.0", id, result } as unknown as JSONRPCMessage;
}

// ---------------------------------------------------------------------------
// Upload flow.
// ---------------------------------------------------------------------------

type Outcome<T> = { ok: true; value: T } | { ok: false; message: string };

async function uploadOne(
  file: UploadFileSpec,
  args: JsonRecord,
  origin: string,
  authHeader: string,
  doFetch: typeof fetch,
): Promise<Outcome<void>> {
  const label = JSON.stringify(file.path);
  if (!pathIsDeclaredInArguments(file.path, args)) {
    return { ok: false, message: `Could not read ${label} for upload: this path was not part of the original tool call` };
  }
  const expanded = expandHome(file.path.trim());
  if (!path.isAbsolute(expanded)) {
    return { ok: false, message: `Could not read ${label} for upload: not an absolute path` };
  }
  let stat: fs.Stats;
  try {
    stat = await fsp.stat(expanded);
  } catch (exc) {
    return { ok: false, message: `Could not read ${label} for upload: ${describeError(exc)}` };
  }
  if (!stat.isFile()) {
    return { ok: false, message: `Could not read ${label} for upload: not a regular file` };
  }
  if (stat.size > file.max_bytes) {
    return {
      ok: false,
      message: `Could not read ${label} for upload: ${stat.size} bytes exceeds the ${file.max_bytes}-byte limit`,
    };
  }
  try {
    const response = await doFetch(new URL(file.upload_path, origin), {
      method: "PUT",
      headers: { Authorization: authHeader, "Content-Type": "application/octet-stream" },
      // A Node `fs.ReadStream` is an `AsyncIterable<Buffer>`, one of
      // undici's accepted `BodyInit` shapes -- streamed rather than read
      // into memory first, since nothing here bounds how large a declared
      // upload might be up front (the daemon's own `max_bytes` is checked
      // above, but that is a ceiling, not a reason to buffer the whole
      // file). `duplex: "half"` is what undici's fetch requires for any
      // streamed request body.
      body: fs.createReadStream(expanded) as unknown as RequestInit["body"],
      duplex: "half",
    } as RequestInit);
    if (!response.ok) {
      await response.body?.cancel().catch(() => undefined);
      return { ok: false, message: `Could not read ${label} for upload: upload failed with HTTP ${response.status}` };
    }
  } catch (exc) {
    return { ok: false, message: `Could not read ${label} for upload: ${describeError(exc)}` };
  }
  return { ok: true, value: undefined };
}

async function performUploads(
  files: UploadFileSpec[],
  args: JsonRecord,
  origin: string,
  authHeader: string,
  doFetch: typeof fetch,
): Promise<Outcome<Record<string, string>>> {
  const uploads: Record<string, string> = {};
  for (const file of files) {
    const outcome = await uploadOne(file, args, origin, authHeader, doFetch);
    if (!outcome.ok) {
      return outcome;
    }
    uploads[file.path] = file.slot;
  }
  return { ok: true, value: uploads };
}

/** The original request, resent with an ``uploads`` map merged into
 * whatever ``_meta`` object it already carried (ADR 0007 SS1.1 step 4) --
 * merged, not replaced, since a future MCP feature (progress tokens,
 * related-task ids) could already occupy other ``_meta`` keys on the same
 * request and this must not clobber them. */
function buildResendRequest(original: JSONRPCMessage, uploads: Record<string, string>): JSONRPCMessage {
  const env = asEnvelope(original);
  const params: JsonRecord = { ...(env.params ?? {}) };
  const existingMeta = isPlainObject(params._meta) ? params._meta : {};
  params._meta = { ...existingMeta, [META_KEY]: { v: 1, uploads } };
  return { ...(original as unknown as JsonRecord), params } as unknown as JSONRPCMessage;
}

// ---------------------------------------------------------------------------
// Download flow.
// ---------------------------------------------------------------------------

function hashingCounter(hash: Hash, counter: { bytes: number }): Transform {
  return new Transform({
    transform(chunk: Buffer, _enc, callback) {
      hash.update(chunk);
      counter.bytes += chunk.length;
      callback(null, chunk);
    },
  });
}

async function downloadOne(
  file: DeliverFileSpec,
  args: JsonRecord,
  origin: string,
  authHeader: string,
  doFetch: typeof fetch,
): Promise<Outcome<string>> {
  const label = JSON.stringify(file.dest_dir);
  if (!pathIsDeclaredInArguments(file.dest_dir, args)) {
    return { ok: false, message: `Could not save to ${label}: this destination was not part of the original tool call` };
  }
  if (!isSafeBasename(file.name)) {
    return { ok: false, message: `Could not save to ${label}: ${JSON.stringify(file.name)} is not a safe file name` };
  }
  const destDir = expandHome(file.dest_dir.trim());
  if (!path.isAbsolute(destDir)) {
    return { ok: false, message: `Could not save to ${label}: not an absolute path` };
  }

  let tempPath: string | undefined;
  try {
    await fsp.mkdir(destDir, { recursive: true });
    tempPath = path.join(destDir, `.${file.name}.pf-partial-${randomBytes(8).toString("hex")}`);

    const response = await doFetch(new URL(file.download_path, origin), {
      headers: { Authorization: authHeader },
    });
    if (!response.ok || response.body === null) {
      await response.body?.cancel().catch(() => undefined);
      throw new Error(`download failed with HTTP ${response.status}`);
    }

    const hash = createHash("sha256");
    const counter = { bytes: 0 };
    await pipeline(
      // `response.body` is a web `ReadableStream<Uint8Array>` (undici); the
      // rest of this pipeline is Node streams, hence the conversion.
      Readable.fromWeb(response.body as import("node:stream/web").ReadableStream<Uint8Array>),
      hashingCounter(hash, counter),
      fs.createWriteStream(tempPath),
    );

    if (counter.bytes !== file.size_bytes) {
      throw new Error(`downloaded ${counter.bytes} bytes, expected ${file.size_bytes}`);
    }
    const digest = hash.digest("hex");
    if (digest !== file.sha256) {
      throw new Error("downloaded content's SHA-256 did not match");
    }

    const finalName = pickNonCollidingName(destDir, file.name);
    const finalPath = path.join(destDir, finalName);
    await fsp.rename(tempPath, finalPath);
    tempPath = undefined;
    return { ok: true, value: finalPath };
  } catch (exc) {
    return {
      ok: false,
      message:
        `Could not save to ${label}: ${describeError(exc)}. The file was fetched but not saved; ` +
        "call the tool again with a different destination_dir.",
    };
  } finally {
    if (tempPath !== undefined) {
      await fsp.unlink(tempPath).catch(() => undefined);
    }
  }
}

/** Rewrites ``result`` in place, once every file in ``meta.files`` has been
 * written: each file's ``result_path_pointer`` gets the real on-disk path,
 * ``structuredContent.delivery`` becomes ``"local_disk"``, and the first
 * (or only) text content block is regenerated from the updated
 * ``structuredContent`` -- matching the effect of the daemon's own
 * ``mcp_tools.to_call_tool_result`` without importing that logic, per ADR
 * 0007 SS1.1. The wire-protocol ``_meta`` key is deleted so nothing about
 * the handshake leaks to Claude Desktop. */
function applyDeliveries(result: JsonRecord, files: DeliverFileSpec[], realPaths: string[]): JsonRecord {
  const rewritten: JsonRecord = { ...result };
  const structuredContent: JsonRecord = isPlainObject(rewritten.structuredContent)
    ? { ...rewritten.structuredContent }
    : {};
  files.forEach((file, index) => {
    setAtJsonPointer(structuredContent, file.result_path_pointer, realPaths[index]);
  });
  structuredContent.delivery = "local_disk";
  rewritten.structuredContent = structuredContent;
  rewritten.content = [{ type: "text", text: JSON.stringify(structuredContent) }];
  if (isPlainObject(rewritten._meta)) {
    const { [META_KEY]: _dropped, ...restMeta } = rewritten._meta;
    if (Object.keys(restMeta).length > 0) {
      rewritten._meta = restMeta;
    } else {
      delete rewritten._meta;
    }
  }
  return rewritten;
}

async function performDeliveries(
  meta: DeliverMeta,
  args: JsonRecord,
  origin: string,
  authHeader: string,
  doFetch: typeof fetch,
): Promise<Outcome<string[]>> {
  const realPaths: string[] = [];
  for (const file of meta.files) {
    const outcome = await downloadOne(file, args, origin, authHeader, doFetch);
    if (!outcome.ok) {
      return outcome;
    }
    realPaths.push(outcome.value);
  }
  return { ok: true, value: realPaths };
}

// ---------------------------------------------------------------------------
// Public entry point.
// ---------------------------------------------------------------------------

export interface FileBridgeOptions {
  /** The daemon's ``/mcp`` origin (scheme + host + port) -- upload/download
   * paths from the wire payload are resolved against this, exactly as
   * index.ts already derives it from the discovered ``mcp_url``. */
  origin: string;
  /** The same ``Authorization: Bearer <token>`` header value the MCP
   * transport itself sends -- Phase 1 reuses that token verbatim rather
   * than reading a second credential; see the module docstring. */
  authHeader: string;
  /** Overridable for tests; defaults to the global ``fetch``. */
  fetch?: typeof fetch;
}

interface PendingCall {
  message: JSONRPCMessage;
  args: JsonRecord;
  timestamp: number;
  resent: boolean;
}

export type FileBridge = ProxyInterceptor;

/** Builds the file bridge's hook into ``proxyTransports`` -- see
 * ``ProxyInterceptor``'s own doc comment in proxy.ts, and this module's
 * docstring for the two flows it implements. One instance is created per
 * shim process (index.ts does this once, alongside the two transports) and
 * tracks in-flight ``tools/call`` requests by JSON-RPC id for the lifetime
 * of the process. */
export function createFileBridge(opts: FileBridgeOptions): FileBridge {
  const doFetch = opts.fetch ?? fetch;
  const pending = new Map<string | number, PendingCall>();

  function sweep(): void {
    const cutoff = Date.now() - PENDING_TTL_MS;
    for (const [id, entry] of pending) {
      if (entry.timestamp < cutoff) {
        pending.delete(id);
      }
    }
  }

  function onDesktopRequest(message: JSONRPCMessage): void {
    sweep();
    const env = asEnvelope(message);
    const id = toolCallRequestId(env);
    if (id === undefined) {
      return;
    }
    const args = isPlainObject(env.params?.arguments) ? (env.params.arguments as JsonRecord) : {};
    pending.set(id, { message, args, timestamp: Date.now(), resent: false });
  }

  async function onDaemonMessage(
    message: JSONRPCMessage,
  ): Promise<{ forwardToDesktop?: JSONRPCMessage; resendToDaemon?: JSONRPCMessage }> {
    sweep();
    const env = asEnvelope(message);
    const id = responseId(env);
    if (id === undefined) {
      return { forwardToDesktop: message };
    }
    const entry = pending.get(id);
    if (entry === undefined || "error" in env) {
      // Not a call this module is tracking, or the daemon answered with a
      // JSON-RPC-level error rather than a CallToolResult -- either way,
      // nothing for the bridge to do.
      pending.delete(id);
      return { forwardToDesktop: message };
    }

    const meta = bridgeMetaOf(env.result);
    if (meta === undefined) {
      pending.delete(id);
      return { forwardToDesktop: message };
    }

    if (meta.op === "need_uploads") {
      if (entry.resent) {
        // ADR 0007 SS1.1's "at most one handshake round" rule: a second
        // need_uploads for the same id is the daemon violating its own
        // protocol, not something to retry into forever.
        pending.delete(id);
        return {
          forwardToDesktop: buildResponse(
            id,
            errorCallToolResult(
              "PrivacyFence requested a second upload round for the same call -- refusing to loop.",
            ),
          ),
        };
      }
      const outcome = await performUploads(meta.files, entry.args, opts.origin, opts.authHeader, doFetch);
      if (!outcome.ok) {
        pending.delete(id);
        return { forwardToDesktop: buildResponse(id, errorCallToolResult(outcome.message)) };
      }
      entry.resent = true;
      entry.timestamp = Date.now();
      return { resendToDaemon: buildResendRequest(entry.message, outcome.value) };
    }

    // meta.op === "deliver"
    pending.delete(id);
    const outcome = await performDeliveries(meta, entry.args, opts.origin, opts.authHeader, doFetch);
    if (!outcome.ok) {
      return { forwardToDesktop: buildResponse(id, errorCallToolResult(outcome.message)) };
    }
    const result = isPlainObject(env.result) ? env.result : {};
    return { forwardToDesktop: buildResponse(id, applyDeliveries(result, meta.files, outcome.value)) };
  }

  return { onDesktopRequest, onDaemonMessage };
}
