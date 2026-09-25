/**
 * listReleaseHistory against a stand-in bucket, for what the Miniflare fixtures cannot reach
 * cheaply: an R2 listing that spans more than one page.
 */
import { describe, expect, it } from "vitest";
import { listReleaseHistory } from "../src/history";

function manifest(version: string, channel: string): object {
  return {
    schema: 1,
    version,
    channel,
    published_at: "2026-09-01T12:00:00Z",
    artifacts: [
      {
        id: "macos-arm64",
        kind: "installer",
        platform: "macos",
        architecture: "arm64",
        filename: `PrivacyFence-${version}.dmg`,
        key: `releases/${channel}/${version}/PrivacyFence-${version}.dmg`,
        size: 1,
        sha256: "0".repeat(64),
      },
    ],
  };
}

/** A bucket holding stable 4.0.0-4.2.0 that lists one version directory per page. */
function pagedBucket(): { bucket: R2Bucket; listCalls: (string | undefined)[] } {
  const objects: Record<string, object> = {
    "releases/stable/latest.json": { version: "4.2.0", manifest: "releases/stable/4.2.0/manifest.json" },
  };
  const versions = ["4.0.0", "4.1.0", "4.2.0"];
  for (const version of versions) objects[`releases/stable/${version}/manifest.json`] = manifest(version, "stable");
  const listCalls: (string | undefined)[] = [];
  const bucket = {
    async get(key: string) {
      const body = objects[key];
      return body ? { json: async () => body } : null;
    },
    async list(options: R2ListOptions) {
      if (options.prefix !== "releases/stable/") return { objects: [], delimitedPrefixes: [], truncated: false };
      listCalls.push(options.cursor);
      const index = options.cursor ? Number(options.cursor) : 0;
      const last = index === versions.length - 1;
      return {
        objects: [],
        delimitedPrefixes: [`releases/stable/${versions[index]}/`],
        ...(last ? { truncated: false } : { truncated: true, cursor: String(index + 1) }),
      };
    },
  } as unknown as R2Bucket;
  return { bucket, listCalls };
}

describe("listReleaseHistory", () => {
  it("follows the listing's cursor across every page", async () => {
    const { bucket, listCalls } = pagedBucket();
    const releases = await listReleaseHistory(bucket);
    expect(releases.map((release) => release.version)).toEqual(["4.2.0", "4.1.0", "4.0.0"]);
    expect(listCalls).toEqual([undefined, "1", "2"]);
  });
});
