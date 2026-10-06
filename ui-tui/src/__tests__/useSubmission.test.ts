import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ComposerActions, ComposerRefs, ComposerState, ComposerToken } from '../app/interfaces.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { prepareSubmission, shouldInterpolateSubmission, useSubmission } from '../app/useSubmission.js'
import type { GatewayClient } from '../gatewayClient.js'
import { queueItem, type QueueItem } from '../hooks/useQueue.js'

describe('prepareSubmission', () => {
  it('keeps the collapsed paste for display and expands the model payload', () => {
    const label = '[[ first.. [3 lines] .. last ]]'
    const tokens: ComposerToken[] = [{ kind: 'paste', label, text: 'first\nmiddle\nlast' }]

    expect(prepareSubmission(`review this: ${label}`, tokens)).toEqual({
      display: `review this: ${label}`,
      text: 'review this: first\nmiddle\nlast'
    })
  })

  it('does not execute interpolation syntax hidden inside pasted content', () => {
    const label = '[[ copied log [1 lines] ]]'
    const tokens: ComposerToken[] = [{ kind: 'paste', label, text: 'untrusted {!touch /tmp/pwned}' }]
    const submission = prepareSubmission(label, tokens)

    expect(shouldInterpolateSubmission(submission.display)).toBe(false)
    expect(submission.text).toContain('{!touch /tmp/pwned}')
  })
})

describe('visible interpolation combined with a collapsed paste', () => {
  it('routes to interpolation when {!...} is visible in the composer alongside a paste token', () => {
    const label = '[[ log [2 lines] ]]'

    expect(shouldInterpolateSubmission(`show {!date} for ${label}`)).toBe(true)
  })

  // The interpolation branch of dispatchSubmission submits
  //   send(prepareSubmission(text, tokens).text, true, text, identity)
  // where `text` is interpolate()'s output: the visible {!...} already resolved,
  // with the collapsed paste label still intact. This asserts both halves of
  // that composition so the transcript shows resolved interpolation + the
  // compact paste, while the model receives resolved interpolation + the full
  // expanded paste.
  it('display keeps resolved interpolation and the compact paste; payload expands the paste', () => {
    const label = '[[ log [2 lines] ]]'
    const tokens: ComposerToken[] = [{ kind: 'paste', label, text: 'line one\nline two' }]

    // interpolate() has resolved the visible {!date} -> "Tue" and left the paste label alone.
    const interpolated = `Tue for ${label}`
    const submission = prepareSubmission(interpolated, tokens)

    expect(submission.display).toBe(`Tue for ${label}`)
    expect(submission.display).not.toContain('{!')
    expect(submission.text).toBe('Tue for line one\nline two')
  })
})

type SubmissionHarness = ReturnType<typeof useSubmission>

const createSubmissionHarness = (takeQueue: () => QueueItem | undefined = () => undefined) => {
  let result!: SubmissionHarness

  const request = vi.fn((method: string) => {
    if (method === 'input.detect_drop') {
      return Promise.resolve({ matched: false })
    }

    return Promise.resolve({})
  })

  const actions = {
    attachClipboardImage: vi.fn(),
    attachImagePath: vi.fn(),
    clearIn: vi.fn(),
    dequeue: vi.fn(),
    enqueue: vi.fn(),
    handleTextPaste: vi.fn(),
    openEditor: vi.fn(),
    prependQueue: vi.fn(),
    pushHistory: vi.fn(),
    removeQueue: vi.fn(),
    setCompIdx: vi.fn(),
    setComposerTokens: vi.fn(),
    setHistoryIdx: vi.fn(),
    setInput: vi.fn(),
    setInputBuf: vi.fn(),
    setQueueEdit: vi.fn(),
    takeQueue: vi.fn(takeQueue),
    syncTokens: vi.fn()
  } as unknown as ComposerActions

  const refs = {
    historyDraftRef: { current: '' },
    historyRef: { current: [] as string[] },
    queueEditRef: { current: null as null | number },
    queueRef: { current: [] as QueueItem[] },
    submitRef: { current: vi.fn() },
    tokensRef: { current: [] as ComposerToken[] }
  } as unknown as ComposerRefs

  const state = {
    compIdx: 0,
    compReplace: 0,
    completions: [],
    historyIdx: null,
    input: '',
    inputBuf: [],
    queueEditIdx: null,
    queuedDisplay: [],
    tokens: []
  } as ComposerState

  const gw = { request } as unknown as GatewayClient
  const appendMessage = vi.fn()
  const setLastUserMsg = vi.fn()
  const sys = vi.fn()
  const slashRef = { current: vi.fn(() => false) }
  const submitRef = { current: vi.fn() }

  function Harness() {
    result = useSubmission({
      appendMessage,
      composerActions: actions,
      composerRefs: refs,
      composerState: state,
      gw,
      setLastUserMsg,
      slashRef,
      submitRef,
      sys
    })

    return null
  }

  const stdin = new PassThrough()
  const stdout = new PassThrough()
  const stderr = new PassThrough()
  Object.assign(stdout, { columns: 80, isTTY: false, rows: 20 })

  const instance = renderSync(React.createElement(Harness), {
    patchConsole: false,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream
  })

  return { actions, close: () => instance.unmount(), gw: request, refs, result }
}

describe('deferred MoA submissions while busy', () => {
  afterEach(resetUiState)

  it('keeps a token-bearing submission queued in steer mode', () => {
    patchUiState({ busy: true, busyInputMode: 'steer', sid: 'sid-steer' })
    const harness = createSubmissionHarness()

    harness.result.dispatchSubmission('!inspect', 'token-steer')

    expect(harness.actions.enqueue).toHaveBeenCalledWith('!inspect', '!inspect', 'token-steer')
    expect(harness.gw).not.toHaveBeenCalledWith('session.steer', expect.anything())
    harness.close()
  })

  it('keeps a token-bearing submission queued in interrupt mode', () => {
    patchUiState({ busy: true, busyInputMode: 'interrupt', sid: 'sid-interrupt' })
    const harness = createSubmissionHarness()

    harness.result.dispatchSubmission('!inspect', 'token-interrupt')

    expect(harness.actions.enqueue).toHaveBeenCalledWith('!inspect', '!inspect', 'token-interrupt')
    expect(harness.gw).not.toHaveBeenCalledWith('prompt.submit', expect.anything())
    harness.close()
  })

  it('preserves the token when the busy re-queue branch receives a deferred item', () => {
    patchUiState({ busy: true, busyInputMode: 'queue', sid: 'sid-queue' })
    const harness = createSubmissionHarness()

    harness.result.dispatchSubmission('!inspect', 'token-queue')

    expect(harness.actions.enqueue).toHaveBeenCalledWith('!inspect', '!inspect', 'token-queue')
    harness.close()
  })

  it('keeps a token-bearing queue edit at the front while busy', () => {
    patchUiState({ busy: true, busyInputMode: 'interrupt', sid: 'sid-edit' })
    const item = queueItem('original payload', '/moa original payload', 'token-edit')
    const harness = createSubmissionHarness(() => item)
    harness.refs.queueEditRef.current = 0

    harness.result.dispatchSubmission('edited payload')

    expect(harness.actions.prependQueue).toHaveBeenCalledWith(item)
    expect(harness.gw).not.toHaveBeenCalledWith('prompt.submit', expect.anything())
    harness.close()
  })

  it('does not route a token-bearing ! payload through the shell shortcut', async () => {
    patchUiState({ sid: 'sid-shell' })
    const harness = createSubmissionHarness()

    harness.result.sendQueued(queueItem('!inspect', '/moa !inspect', 'token-shell'))

    await vi.waitFor(() =>
      expect(harness.gw).toHaveBeenCalledWith('prompt.submit', {
        moa_token: 'token-shell',
        session_id: 'sid-shell',
        text: '!inspect'
      })
    )
    expect(harness.gw).not.toHaveBeenCalledWith('shell.exec', expect.anything())
    harness.close()
  })
})
