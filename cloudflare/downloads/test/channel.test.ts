import { describe, expect, it } from "vitest";
import { CHANNELS, channelForVersion, compareVersions, isChannel } from "../src/channel";

describe("channelForVersion", () => {
  it.each([
    ["4.3.0", "stable"],
    ["v4.3.0", "stable"],
    ["4.4.0a1", "alpha"],
    ["4.4.0b2", "beta"],
    ["4.4.0rc3", "rc"],
  ] as const)("resolves %s to %s", (version, expected) => {
    expect(channelForVersion(version)).toBe(expected);
  });

  it("rejects a between-tags dev build", () => {
    expect(() => channelForVersion("4.2.1.dev3+gabc1234")).toThrow(/dev build/);
  });

  it("rejects a string that isn't a version at all", () => {
    expect(() => channelForVersion("not-a-version")).toThrow(/doesn't look like a release version/);
  });
});

describe("isChannel", () => {
  it("accepts every known channel", () => {
    for (const channel of CHANNELS) expect(isChannel(channel)).toBe(true);
  });

  it("rejects an unknown channel name", () => {
    expect(isChannel("nightly")).toBe(false);
  });
});

describe("compareVersions", () => {
  it("orders major.minor.patch, then stable above rc above beta above alpha, then stage number", () => {
    const newestFirst = ["4.10.0", "4.4.0", "4.4.0rc2", "4.4.0rc1", "4.4.0b1", "4.4.0a2", "4.4.0a1", "4.3.1", "4.3.0"];
    const shuffled = [...newestFirst].reverse();
    expect(shuffled.sort((a, b) => compareVersions(b, a))).toEqual(newestFirst);
  });

  it("treats a v prefix as the same version", () => {
    expect(compareVersions("v4.3.0", "4.3.0")).toBe(0);
  });

  it("rejects a version channelForVersion rejects", () => {
    expect(() => compareVersions("4.2.1.dev3+gabc1234", "4.2.0")).toThrow(/dev build/);
  });
});
