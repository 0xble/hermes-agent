import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { submitPrompt } from '../app/submissionCore.js'
import { captureDestination } from '../app/submissionDestination.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useSubmission } from '../app/useSubmission.js'
import type { GatewayClient } from '../gatewayClient.js'
import { queueItem, useQueue } from '../hooks/useQueue.js'
import { nativeTypedInput, verifiedTypedInput } from '../lib/nativeInputOrigin.js'

describe('native typed occurrence', () => {
  it('witnesses the exact live composer value, never an imported or copied receipt', () => {
    const text = 'Keep notices off.'
    const receipt = nativeTypedInput(text)
    expect(verifiedTypedInput(receipt, text)).toBe(true)
    expect(verifiedTypedInput(receipt, text + ' changed')).toBe(false)
    expect(verifiedTypedInput({}, text)).toBe(false)
    expect(verifiedTypedInput(JSON.parse(JSON.stringify(receipt)), text)).toBe(false)
    expect(verifiedTypedInput(structuredClone(receipt), text)).toBe(false)
    expect(verifiedTypedInput(undefined, text)).toBe(false)
  })

  it('hands only a witnessed composer occurrence to the private helper', async () => {
    const home = mkdtempSync(join(tmpdir(), 'ink-origin-'))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()
    patchUiState({ sid: 'owner', info: { stored_session_id: 'stored-owner', profile_name: 'default' } as never })
    const text = 'Keep notices off.'
    const request = vi.fn(async () => ({ status: 'queued' }))
    const withTypedInput = vi.fn(async (_input: unknown, send: () => Promise<unknown>) => send())

    const deps = { appendMessage: vi.fn(), enqueue: vi.fn(), expand: (value: string) => value,
      gw: { request, withTypedInput } as unknown as GatewayClient, setLastUserMsg: vi.fn(), sys: vi.fn() }

    const receipt = nativeTypedInput(text)

    try {
      for (const nativeOrigin of [receipt, JSON.parse(JSON.stringify(receipt)), structuredClone(receipt), {}]) {
        const item = { text, display: text, submissionId: 'fixture', destination: captureDestination(), nativeOrigin }
        await submitPrompt(text, deps, true, undefined, { queueItem: item, skipDetectDrop: true })
      }

      expect(withTypedInput).toHaveBeenCalledTimes(1)
      expect(request).toHaveBeenCalledTimes(4)

      for (const call of request.mock.calls) { expect(JSON.stringify(call)).not.toContain('nativeOrigin') }
    } finally {
      resetUiState()
      vi.unstubAllEnvs()
      rmSync(home, { recursive: true, force: true })
    }
  })
})

it.each([false, true])('witnesses the real composer dispatch, including paste=%s, but holds a journal replay', async pasted => {
  const home = mkdtempSync(join(tmpdir(), 'ink-composer-origin-'))
  vi.stubEnv('HERMES_HOME', home)
  resetUiState()
  patchUiState({ sid: 'owner', info: { stored_session_id: 'stored-owner', profile_name: 'default' } as never })
  const text = pasted ? 'Keep notices off.\nKeep summaries short.' : 'Keep notices off.'
  const label = '[[ pasted instruction ]]'

  const request = vi.fn(async (method: string, params: Record<string, unknown>) => method === 'input.detect_drop'
    ? { matched: false }
    : { status: 'queued', admission_id: params.submission_id, target_session_id: 'stored-owner', target_profile_home: home })

  const withTypedInput = vi.fn(async (_input: unknown, send: () => Promise<unknown>) => send())
  let submission!: ReturnType<typeof useSubmission>
  let queue!: ReturnType<typeof useQueue>

  function Harness() {
    queue = useQueue()
    submission = useSubmission({
      appendMessage: vi.fn(),
      composerActions: { ...queue, prependQueue: queue.prependQ, takeQueue: queue.takeQ, pushHistory: vi.fn(), clearIn: vi.fn() } as never,
      composerRefs: { queueRef: queue.queueRef, queueEditRef: queue.queueEditRef, tokensRef: { current: pasted ? [{ kind: 'paste', label, text }] : [] } } as never,
      composerState: { input: '', inputBuf: [], completions: [] } as never,
      gw: { request, withTypedInput, isCanonical: true } as unknown as GatewayClient,
      setLastUserMsg: vi.fn(), slashRef: { current: () => true }, submitRef: { current: () => {} }, sys: vi.fn()
    })

    return null
  }

  const stdout = Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false })

  const instance = renderSync(React.createElement(Harness), {
    stdin: new PassThrough() as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: new PassThrough() as unknown as NodeJS.WriteStream,
    patchConsole: false
  })

  try {
    submission.dispatchSubmission(pasted ? label : text)
    await expect.poll(() => withTypedInput.mock.calls.length, { timeout: 5000 }).toBe(1)
    expect(withTypedInput.mock.calls[0][0]).toMatchObject({ text, session: 'stored-owner' })
    patchUiState({ busy: false })
    const imported = JSON.parse(JSON.stringify({ ...queueItem(text, text), destination: captureDestination() }))
    submission.sendQueued(imported)
    await expect.poll(() => request.mock.calls.filter(call => call[0] === 'prompt.submit').length,
      { timeout: 5000 }).toBe(2)
    patchUiState({ busy: false })
    submission.submitLiteral(text)
    await expect.poll(() => request.mock.calls.filter(call => call[0] === 'prompt.submit').length,
      { timeout: 5000 }).toBe(3)
    expect(withTypedInput).toHaveBeenCalledTimes(1)
  } finally {
    instance.unmount()
    resetUiState()
    vi.unstubAllEnvs()
    rmSync(home, { recursive: true, force: true })
  }
})
