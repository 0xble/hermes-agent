import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { ConfirmDialog } from '@/components/ui/confirm-dialog'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel', confirm: 'Confirm', delete: 'Delete', done: 'Done', loading: 'Working' },
      errors: { genericFailure: 'Something failed' }
    }
  })
}))

// ConfirmDialog schedules window.setTimeout(onClose, 600) after a successful
// confirm. The timer had no cleanup, so an unmount inside that window left it
// pending. In CI it came due after the environment was gone. The setState
// path of React then touched `window`:
//
//   ReferenceError: window is not defined
//    at resolveUpdatePriority (react-dom-client.development.js:1308)
//    at dispatchSetState
//    at Timeout.t4 [as _onTimeout] session-actions-menu.tsx:574
//
// The frame at session-actions-menu.tsx:574 is the `onClose` prop of
// DeleteSessionDialog. The owner of the timer is this component.
//
// This test confirms, unmounts inside the 600ms window, and then lets the
// timer come due on the dead tree.
test('the close timer does not fire after unmount', async () => {
  vi.useFakeTimers()
  const onClose = vi.fn()
  const onConfirm = vi.fn()

  render(<ConfirmDialog confirmLabel="Delete" onClose={onClose} onConfirm={onConfirm} open title="Delete session" />)

  fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

  // Not waitFor: it polls on real timers, and the fake timers of this test
  // never let it advance. onConfirm runs synchronously inside the click, and
  // one microtask turn is enough for the await in run() to settle and reach
  // the setTimeout.
  await Promise.resolve()
  await Promise.resolve()
  expect(onConfirm).toHaveBeenCalled()

  // Unmount while the close timer is still pending.
  cleanup()

  // Let the timer come due on the unmounted tree.
  vi.advanceTimersByTime(1000)

  expect(onClose).not.toHaveBeenCalled()
  vi.useRealTimers()
})

test.each([false, true])('a confirmation resolving after unmount cannot close its former owner (dismiss=%s)', async dismissOnConfirm => {
  vi.useFakeTimers()
  let finish!: () => void
  const onClose = vi.fn()
  const onConfirm = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
  const view = render(
    <ConfirmDialog dismissOnConfirm={dismissOnConfirm} onClose={onClose} onConfirm={onConfirm} open title="Confirm action" />
  )

  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
  expect(onConfirm).toHaveBeenCalledOnce()
  view.unmount()
  await act(async () => finish())
  await act(async () => vi.advanceTimersByTime(600))

  expect(onClose).not.toHaveBeenCalled()
})
