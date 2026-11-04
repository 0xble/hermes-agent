import { mkdtempSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { captureDestination, type SubmissionDestination } from '../app/submissionDestination.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { useQueue } from '../hooks/useQueue.js'

const info = (profile_name: string) => ({ model: 'test', profile_name, skills: {}, tools: {} })
const submissionId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const claimed = (text: string, display: string, destination: SubmissionDestination) => ({
  createdAt: expect.any(Number),
  destination,
  display,
  failed: false,
  inFlight: true,
  settle: expect.any(Function),
  submissionId: expect.stringMatching(submissionId),
  text
})

afterEach(resetUiState)

describe('pending input destination', () => {
  it.each([
    { profile: 'alpha', sid: 'other-session' },
    { profile: 'beta', sid: 'same-session' }
  ])('does not drain or edit another destination: $profile/$sid', async destination => {
    const home = mkdtempSync(join(tmpdir(), 'ink-queue-scope-'))
    vi.stubEnv('HERMES_HOME', home)
    resetUiState()
    patchUiState({ info: null, sid: null })
    let queue!: ReturnType<typeof useQueue>

    function Harness() {
      queue = useQueue()

      return <Text>{queue.queuedDisplay.join('|') || 'empty queue'}</Text>
    }

    const stdout = new PassThrough()
    Object.assign(stdout, { columns: 80, isTTY: false, rows: 20 })
    let output = ''
    stdout.on('data', chunk => {
      output += String(chunk)
    })

    const instance = renderSync(<Harness />, {
      patchConsole: false,
      stdin: new PassThrough() as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream
    })

    try {
      queue.enqueue('startup payload')
      patchUiState({ info: info('alpha'), sid: 'same-session' })
      const alpha = captureDestination()
      const startup = queue.dequeue()
      expect(startup).toEqual(claimed('startup payload', 'startup payload', alpha))
      expect(alpha).toEqual({
        ownerHome: realpathSync(home),
        profile: 'alpha',
        profileHome: realpathSync(home),
        sid: 'same-session',
        storedSid: null
      })
      // A claim stays at the head until the submit settles. The private row is
      // index 0 only after that acknowledgement.
      startup!.settle!(true)
      expect(queue.queueRef.current).toEqual([])
      queue.enqueue('private payload', 'private preview')
      queue.setQueueEdit(0)
      patchUiState({ info: info(destination.profile), sid: destination.sid })
      const other = captureDestination()
      expect(other).toEqual({
        ownerHome: realpathSync(home),
        profile: destination.profile,
        profileHome: realpathSync(home),
        sid: destination.sid,
        storedSid: null
      })
      expect(other).not.toEqual(alpha)
      // Input handlers can run before React commits the navigation render.
      expect(queue.dequeue()).toBeUndefined()
      expect(queue.queueRef.current).toEqual([])
      expect(queue.queueEditRef.current).toBeNull()
      queue.enqueue('destination payload')
      await expect.poll(() => queue.queuedDisplay).toEqual(['destination payload'])
      expect(output).not.toContain('private preview|destination payload')
      patchUiState({ info: info('alpha'), sid: 'same-session' })
      expect(captureDestination()).toEqual(alpha)
      expect(queue.queueEditRef.current).toBe(0)
      expect(queue.takeQ(0)).toEqual(claimed('private payload', 'private preview', alpha))
      patchUiState({ info: info(destination.profile), sid: destination.sid })
      expect(queue.dequeue()).toEqual(claimed('destination payload', 'destination payload', other))
      expect(queue.dequeue()).toBeUndefined()
    } finally {
      instance.unmount()
      resetUiState()
      vi.unstubAllEnvs()
      rmSync(home, { recursive: true, force: true })
    }
  })
})
