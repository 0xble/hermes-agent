import { atom } from 'nanostores'

export type BusyInputMode = 'interrupt' | 'queue' | 'steer'

export function normalizeBusyInputMode(value: unknown): BusyInputMode {
  const mode = typeof value === 'string' ? value.trim().toLowerCase() : ''

  // Backend `auto` absorbs or defers busy text. Desktop has no deferral path, so it keeps the
  // input lossless and never interrupts, matching the TUI's fallback for unknown modes.
  if (mode === 'auto') {
    return 'queue'
  }

  return mode === 'steer' || mode === 'queue' ? mode : 'interrupt'
}

export function busyInputOwnerKey(connectionId?: string | null, profile?: string | null): string {
  return JSON.stringify([connectionId ?? '', profile?.trim() || 'default'])
}

// A backend config snapshot, never a persisted Desktop-only preference.
export const $busyInputConfig = atom<{ owner: string; connection: unknown; mode: BusyInputMode } | null>(null)
