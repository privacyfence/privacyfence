import { defineConfig } from "vitest/config";
import { cloudflareTest } from "@cloudflare/vitest-plugin";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.toml" },
      miniflare: {
        // Worker secrets are not in wrangler.toml; the suite supplies the maintainer token. The
        // Gmail secrets are left unset here and added per test (test/helpers.ts).
        bindings: { REVOKE_TOKEN: "test-revoke-token" },
      },
    }),
  ],
  test: {
    coverage: {
      provider: "istanbul",
      include: ["src/**"],
      thresholds: { lines: 94, branches: 85, functions: 98, statements: 93 },
    },
  },
});
