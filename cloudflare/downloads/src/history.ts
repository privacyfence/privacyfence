/**
 * Every published version on every channel, for `GET /api/releases/history` (the website's
 * /releases/ page). Metadata only: it reads manifests, never an artifact, and never touches the
 * D1 counters.
 *
 * A version is listed when its `releases/<channel>/<version>/manifest.json` exists and it is not
 * newer than the version the channel's `latest.json` points at. `r2_release.py finalize` writes
 * the manifest before it verifies and promotes, so a manifest newer than `latest.json` is a
 * release that never finished publishing (or one rolled back by re-promoting an older version) --
 * the same rule that keeps it off `/download/<channel>/...` keeps it off the list. A channel with
 * no `latest.json` lists nothing.
 *
 * The response carries no R2 key or URL: each artifact is reduced to what the page shows, and
 * downloads stay on `/download/version/<version>/<artifact-id>`.
 */
import { CHANNELS, channelForVersion, compareVersions, type Channel } from "./channel.js";
import {
  getJson,
  latestPointerKey,
  manifestKey,
  type LatestPointer,
  type Manifest,
  type ManifestArtifact,
} from "./manifest.js";

export type PublicArtifact = Omit<ManifestArtifact, "key">;

export interface PublicRelease {
  schema: number;
  version: string;
  channel: Channel;
  published_at: string;
  artifacts: PublicArtifact[];
}

/** The `<version>` directory names under `releases/<channel>/`, across every list page. */
async function listVersionDirectories(bucket: R2Bucket, channel: Channel): Promise<string[]> {
  const prefix = `releases/${channel}/`;
  const versions: string[] = [];
  let cursor: string | undefined;
  do {
    const page = await bucket.list(cursor ? { prefix, delimiter: "/", cursor } : { prefix, delimiter: "/" });
    for (const directory of page.delimitedPrefixes) versions.push(directory.slice(prefix.length, -1));
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return versions;
}

function isListable(version: string, channel: Channel, latest: string): boolean {
  try {
    return channelForVersion(version) === channel && compareVersions(version, latest) <= 0;
  } catch {
    return false; // not a release version: nothing r2_release.py would have uploaded
  }
}

function toPublic(manifest: Manifest, channel: Channel): PublicRelease {
  return {
    schema: manifest.schema,
    version: manifest.version,
    channel, // the directory it was listed from, which is what its download route resolves
    published_at: manifest.published_at,
    artifacts: (manifest.artifacts ?? [])
      .filter((artifact) => artifact.kind === "installer")
      .map(({ id, kind, platform, architecture, filename, size, sha256 }) => ({
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

async function channelHistory(bucket: R2Bucket, channel: Channel): Promise<PublicRelease[]> {
  const pointer = await getJson<LatestPointer>(bucket, latestPointerKey(channel));
  if (!pointer || !isListable(pointer.version, channel, pointer.version)) return [];

  const versions = (await listVersionDirectories(bucket, channel)).filter((version) =>
    isListable(version, channel, pointer.version),
  );
  const manifests = await Promise.all(
    versions.map(async (version) => {
      try {
        return await getJson<Manifest>(bucket, manifestKey(channel, version));
      } catch (err) {
        // One unreadable manifest must not take the whole list down; that version is left out.
        console.error(`privacyfence-downloads: unreadable manifest for ${version}`, err);
        return null;
      }
    }),
  );
  return manifests
    .filter((manifest, i): manifest is Manifest => manifest !== null && manifest.version === versions[i])
    .map((manifest) => toPublic(manifest, channel))
    .filter((release) => release.artifacts.length > 0);
}

/** Every listed release on every channel, newest version first. */
export async function listReleaseHistory(bucket: R2Bucket): Promise<PublicRelease[]> {
  const perChannel = await Promise.all(CHANNELS.map((channel) => channelHistory(bucket, channel)));
  return perChannel.flat().sort((a, b) => compareVersions(b.version, a.version));
}
