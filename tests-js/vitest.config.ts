import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'node',
    // Root tests exercise real Git and subprocess fixtures; allow load headroom while
    // retaining a finite per-test bound instead of relying on Vitest's 5s default.
    testTimeout: 30_000,
    include: ['**/*.test.ts', '**/*.test.mjs'],
  },
})
