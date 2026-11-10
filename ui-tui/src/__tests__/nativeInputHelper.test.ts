import { spawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { afterEach, expect, it, vi } from 'vitest'

import { withNativeTypedInput } from '../localGatewayBootstrap.js'

vi.mock('node:child_process', async original => ({ ...await original<Record<string, unknown>>(), spawn: vi.fn() }))

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

function helper(mode: 'ready' | 'error' | 'exit' | 'timeout') {
  const child = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(),
    kill: vi.fn(() => { queueMicrotask(() => child.emit('close'));

 return true })
  })

  child.stdin.once('finish', () => child.emit('close'))
  vi.mocked(spawn).mockImplementation(() => {
    queueMicrotask(() => {
      if (mode === 'ready') { child.stdout.write('ready\n') }

      if (mode === 'error') { child.emit('error', new Error('fictional spawn failure')); child.emit('close') }

      if (mode === 'exit') { child.emit('exit', 1); child.emit('close') }
    })

    return child as never
  })
  vi.spyOn(console, 'error').mockImplementation(() => {})

  return child
}

const input = { home: '/fictional', session: 'session', request: 'request', text: 'Keep notices off.' }

it.each(['error', 'exit'] as const)('submits exactly once without origin after helper %s', async mode => {
  helper(mode)
  const send = vi.fn(async () => 'submitted')
  await expect(withNativeTypedInput(input, send)).resolves.toBe('submitted')
  expect(send).toHaveBeenCalledTimes(1)
})

it('bounds a silent helper and submits without origin', async () => {
  vi.useFakeTimers()
  const child = helper('timeout')
  const send = vi.fn(async () => 'submitted')
  const result = withNativeTypedInput(input, send)
  await vi.advanceTimersByTimeAsync(2000)
  await expect(result).resolves.toBe('submitted')
  expect(send).toHaveBeenCalledTimes(1)
  expect(child.stdin.writableEnded).toBe(true)
})

it('never retries a submit failure after the witness becomes ready', async () => {
  helper('ready')
  const send = vi.fn(async () => { throw new Error('fictional delivery failure') })
  await expect(withNativeTypedInput(input, send)).rejects.toThrow('fictional delivery failure')
  expect(send).toHaveBeenCalledTimes(1)
})
