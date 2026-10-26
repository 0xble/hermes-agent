/**
 * Prepare the lock-pinned Electron runtime once, before any electron-project worker starts.
 * Electron's package entry point downloads and unzips its binary lazily on first `require`.
 * With parallel workers, several test files did that into the same `dist` at once, and a
 * live-launch test could start a half-extracted framework. The repository's explicit
 * preparation route (scripts/build/electron-runtime.mjs) is a no-op when the runtime is
 * already prepared.
 */
import path from 'node:path'

// @ts-expect-error -- plain ESM build script without type declarations
import { prepareElectronRuntime } from '../../scripts/build/electron-runtime.mjs'

const repo = path.resolve(import.meta.dirname, '../..')

export default function setup(): void {
  try {
    prepareElectronRuntime({ source: repo })
  } catch (error: unknown) {
    // Contracts that never launch Electron still run; a launch test reports its own failure.
    console.warn(`vitest: could not prepare the Electron runtime: ${String(error)}`)
  }
}
