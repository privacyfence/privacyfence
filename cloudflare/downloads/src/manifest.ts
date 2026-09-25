/**
 * Release manifest types and R2 lookups. Schema matches exactly what
 * `scripts/r2_release.py finalize` writes (schema `1`) -- see docs/downloads-and-release-kpi.md
 * "Publication is transactional". This Worker only ever reads manifests; the test suite reads
 * hand-written fixtures of the same shape (see test/fixtures.ts).
 */
import { channelForVersion, type Channel } from "./channel.js";

export interface ManifestArtifact {
  id: string;
  kind: string;
  platform: string;
  architecture: string;
  filename: string;
  key: string;
  size: number;
  sha256: string;
}

export interface Manifest {
  schema: number;
  version: string;
  channel: Channel;
  published_at: string;
  artifacts: ManifestArtifact[];
}

export interface LatestPointer {
  version: string;
  manifest: string;
}

export function latestPointerKey(channel: Channel): string {
  return `releases/${channel}/latest.json`;
}

export function manifestKey(channel: Channel, version: string): string {
  return `releases/${channel}/${version}/manifest.json`;
}

export async function getJson<T>(bucket: R2Bucket, key: string): Promise<T | null> {
  const object = await bucket.get(key);
  if (!object) return null;
  return object.json<T>();
}

/** Resolves a channel's `latest.json` pointer to its manifest. Null if nothing is published yet. */
export async function resolveLatestManifest(bucket: R2Bucket, channel: Channel): Promise<Manifest | null> {
  const pointer = await getJson<LatestPointer>(bucket, latestPointerKey(channel));
  if (!pointer) return null;
  return getJson<Manifest>(bucket, pointer.manifest);
}

/**
 * Resolves a specific version's manifest directly (no `latest.json` involved -- an older
 * version stays downloadable by exact version even after a newer one becomes `latest`).
 * Throws the same way `channelForVersion` does for a version string that was never tagged.
 */
export async function resolveVersionManifest(
  bucket: R2Bucket,
  version: string,
): Promise<{ channel: Channel; manifest: Manifest } | null> {
  const channel = channelForVersion(version);
  const manifest = await getJson<Manifest>(bucket, manifestKey(channel, version));
  if (!manifest) return null;
  return { channel, manifest };
}

/** A manifest artifact as the `/api/*` routes publish it: everything but its R2 `key`. */
export type PublicArtifact = Omit<ManifestArtifact, "key">;

export interface PublicManifest extends Omit<Manifest, "artifacts"> {
  artifacts: PublicArtifact[];
}

/**
 * The manifest as the `/api/*` routes return it. No R2 key or URL ever leaves the Worker (the
 * browser has no use for one: downloads go through `/download/...`, which resolves the key
 * itself). Fields are copied one by one rather than by dropping `key`, so a field added to the
 * manifest later is not published until it is listed here.
 */
export function publicManifest(manifest: Manifest): PublicManifest {
  return {
    schema: manifest.schema,
    version: manifest.version,
    channel: manifest.channel,
    published_at: manifest.published_at,
    artifacts: (manifest.artifacts ?? []).map(({ id, kind, platform, architecture, filename, size, sha256 }) => ({
      id,
      kind,
      platform,
      architecture,
      filename,
      size,
      sha256,
    })),
  };
}

export function findArtifact(manifest: Manifest, artifactId: string): ManifestArtifact | undefined {
  return manifest.artifacts.find((artifact) => artifact.id === artifactId);
}
