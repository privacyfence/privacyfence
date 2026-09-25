/**
 * Runs before each test file and seeds R2/D1 with the hand-written fixtures under
 * test/fixtures/ -- deliberately hand-written fixtures rather than real release metadata, so the
 * suite never depends on production infrastructure (docs/downloads-and-release-kpi.md).
 *
 * Storage is isolated per test file, not per test. Each file starts from this seed with empty
 * R2, D1 and Cache API storage, but within a file every test sees what earlier tests wrote: D1
 * counter increments, R2 puts and deletes, and responses the Worker edge-cached. (This
 * @cloudflare/vitest-pool-workers version has no per-test `isolatedStorage`.) So a test must not
 * assume fixture state an earlier test in its file could have changed:
 *  - counter assertions compare a before/after delta, never an absolute total;
 *  - a test that changes R2 puts it back (download.test.ts's `withTemporaryObject`, `withoutObject`);
 *  - download.test.ts clears the edge-cached /api/releases* responses before every test.
 */
import { applyD1Migrations, env } from "cloudflare:test";

import stableLatest from "./fixtures/stable/latest.json";
import stableManifest from "./fixtures/stable/manifest.json";
import stableOldManifest from "./fixtures/stable/old-manifest.json";
import stableUnpromotedManifest from "./fixtures/stable/unpromoted-manifest.json";
import betaLatest from "./fixtures/beta/latest.json";
import betaManifest from "./fixtures/beta/manifest.json";
import alphaLatest from "./fixtures/alpha/latest.json";
import alphaManifest from "./fixtures/alpha/manifest.json";
import rcLatest from "./fixtures/rc/latest.json";
import rcManifest from "./fixtures/rc/manifest.json";
import rcOldManifest from "./fixtures/rc/old-manifest.json";

await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);

// Fake installer bytes -- never real binaries, just distinct content whose length/sha256 match
// the "size"/"sha256" fields the fixture manifests above declare for the same artifact.
await Promise.all([
  env.RELEASES.put("releases/stable/latest.json", JSON.stringify(stableLatest)),
  env.RELEASES.put("releases/stable/4.3.0/manifest.json", JSON.stringify(stableManifest)),
  env.RELEASES.put("releases/stable/4.3.0/PrivacyFence-4.3.0.dmg", "FAKE-DMG-BYTES-stable-4.3.0-macos-arm64"),
  env.RELEASES.put(
    "releases/stable/4.3.0/PrivacyFence-4.3.0-setup.exe",
    "FAKE-EXE-BYTES-stable-4.3.0-windows-x64",
  ),
  env.RELEASES.put("releases/stable/4.3.0/privacyfence_4.3.0_amd64.deb", "FAKE-DEB-BYTES-stable-4.3.0-linux-x64"),

  // An older, no-longer-latest stable version -- still reachable via /download/version/....
  env.RELEASES.put("releases/stable/4.2.0/manifest.json", JSON.stringify(stableOldManifest)),
  env.RELEASES.put("releases/stable/4.2.0/PrivacyFence-4.2.0.dmg", "FAKE-DMG-BYTES-stable-4.2.0-macos-arm64"),

  // A manifest newer than what latest.json points at: finalize wrote it but never promoted it, so
  // the release history must leave it out. Next to it, a directory that is not a version at all.
  env.RELEASES.put("releases/stable/4.4.0/manifest.json", JSON.stringify(stableUnpromotedManifest)),
  env.RELEASES.put("releases/stable/not-a-version/manifest.json", JSON.stringify(stableOldManifest)),

  env.RELEASES.put("releases/beta/latest.json", JSON.stringify(betaLatest)),
  env.RELEASES.put("releases/beta/4.4.0b1/manifest.json", JSON.stringify(betaManifest)),
  env.RELEASES.put("releases/beta/4.4.0b1/PrivacyFence-4.4.0b1.dmg", "FAKE-DMG-BYTES-beta-4.4.0b1-macos-arm64"),

  env.RELEASES.put("releases/alpha/latest.json", JSON.stringify(alphaLatest)),
  env.RELEASES.put("releases/alpha/4.4.0a1/manifest.json", JSON.stringify(alphaManifest)),
  env.RELEASES.put("releases/alpha/4.4.0a1/PrivacyFence-4.4.0a1.dmg", "FAKE-DMG-BYTES-alpha-4.4.0a1-macos-arm64"),

  env.RELEASES.put("releases/rc/latest.json", JSON.stringify(rcLatest)),
  env.RELEASES.put("releases/rc/4.4.0rc1/manifest.json", JSON.stringify(rcManifest)),
  env.RELEASES.put("releases/rc/4.4.0rc1/PrivacyFence-4.4.0rc1.dmg", "FAKE-DMG-BYTES-rc-4.4.0rc1-macos-arm64"),
  // An rc from the previous cycle, older than the current stable; its manifest also lists a
  // non-installer, which the release history must not pass on.
  env.RELEASES.put("releases/rc/4.3.0rc1/manifest.json", JSON.stringify(rcOldManifest)),
  // channel "alpha" deliberately has no releases/alpha/9.9.9/... version-pinned fixture --
  // used by tests to prove a missing manifest is a safe 404, not a crash.
]);
