/**
 * Version -> release-channel resolution, deliberately duplicated from
 * scripts/r2_release.py's `channel_for_version()` rather than shared across the Python/
 * TypeScript boundary -- see that function's own docstring for the reasoning (this repo's
 * CLAUDE.md "Releasing" section and src/privacyfence/update_checker.py's `_VERSION_RE`/
 * `_STAGE_RANK` are the same PEP 440 short-form scheme this mirrors: no suffix = stable,
 * `a`/`b`/`rc` = alpha/beta/rc).
 */

export type Channel = "stable" | "alpha" | "beta" | "rc";

/** Every channel this Worker knows about, in the order `/api/releases` reports them. */
export const CHANNELS: readonly Channel[] = ["stable", "alpha", "beta", "rc"];

export function isChannel(value: string): value is Channel {
  return (CHANNELS as readonly string[]).includes(value);
}

// Mirrors r2_release.py's _VERSION_RE. The trailing `.dev<n>` group is matched (not just left to
// fail the overall regex) so a between-tags dev version gets the clear "not a tagged release"
// error below instead of the generic "doesn't look like a version" one.
const VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?(?:\+.*)?$/;

const STAGE_TO_CHANNEL: Record<string, Channel> = { a: "alpha", b: "beta", rc: "rc" };

/**
 * Returns the channel a resolved version string like "4.1.0" or "4.2.0b1" was published under.
 * Throws for anything that isn't a real tagged release -- most importantly a between-tags dev
 * build ("4.2.1.dev3+gabc1234"), which was never `git tag`d and has nothing published for it.
 */
export function channelForVersion(version: string): Channel {
  const match = VERSION_RE.exec(version.trim());
  if (!match) {
    throw new Error(`"${version}" doesn't look like a release version (major.minor.patch[a|b|rc<n>])`);
  }
  if (match[6] !== undefined) {
    throw new Error(`"${version}" is a between-tags dev build, not a tagged release`);
  }
  const stage = match[4];
  return stage ? STAGE_TO_CHANNEL[stage]! : "stable";
}

const STAGE_RANK: Record<string, number> = { a: 0, b: 1, rc: 2 };

/**
 * Orders two release versions: negative if `a` is older, positive if newer, 0 if equal.
 * major.minor.patch first, then stage (a stable version above its own rc, beta and alpha), then
 * stage number -- the same order as website/releases/releases.js and download.js. Both must be
 * versions `channelForVersion` accepts; it throws the same way for anything else.
 */
export function compareVersions(a: string, b: string): number {
  const keyA = versionKey(a);
  const keyB = versionKey(b);
  for (let i = 0; i < keyA.length; i += 1) {
    if (keyA[i] !== keyB[i]) return keyA[i]! - keyB[i]!;
  }
  return 0;
}

function versionKey(version: string): number[] {
  channelForVersion(version); // throws for a non-release version, with the same messages
  const match = VERSION_RE.exec(version.trim())!;
  const stage = match[4];
  return [Number(match[1]), Number(match[2]), Number(match[3]), stage ? STAGE_RANK[stage]! : 3, Number(match[5] ?? 0)];
}
