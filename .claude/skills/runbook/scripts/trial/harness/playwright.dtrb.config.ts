import { defineConfig } from "@playwright/test";

// Runbook trial holder (staged by dtrb.sh). One worker, no retries: a trial
// needs the first failure, not a masked flake.
export default defineConfig({
  testDir: "./e2e/dtrb",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 12 * 3600_000,
  reporter: [["list"]],
  outputDir: (process.env.DTRB_RUN_DIR ?? "./tmp") + "/pw-results",
  use: { trace: "off" },
});
