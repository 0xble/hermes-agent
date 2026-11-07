import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConfirmDialog } from './confirm-dialog'

afterEach(async () => {
  cleanup()

  try {
    if (vi.isFakeTimers()) {
      await act(async () => {
        await vi.runOnlyPendingTimersAsync()
      })
    }
  } finally {
    // Restore spies before timers so setTimeout cannot revert to a fake clock.
    vi.restoreAllMocks()
    vi.useRealTimers()
  }
})

describe('ConfirmDialog lifetime', () => {
  it('keeps the done beat for 600ms before closing a mounted dialog', async () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    render(<ConfirmDialog onClose={onClose} onConfirm={() => {}} open title="Confirm action" />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(599)
    })
    expect(onClose).not.toHaveBeenCalled()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it.each([
    { dismissOnConfirm: false, unmountDuringConfirm: false },
    { dismissOnConfirm: false, unmountDuringConfirm: true },
    { dismissOnConfirm: true, unmountDuringConfirm: false },
    { dismissOnConfirm: true, unmountDuringConfirm: true }
  ])(
    'does not schedule or close after unmount ($dismissOnConfirm, $unmountDuringConfirm)',
    async ({ dismissOnConfirm, unmountDuringConfirm }) => {
      vi.useFakeTimers()
      let resolve!: () => void
      const pending = new Promise<void>(done => {
        resolve = done
      })
      const onClose = vi.fn()

      const onConfirm = () => {
        if (unmountDuringConfirm) {
          view.unmount()
        }

        return pending
      }

      const view = render(
        <ConfirmDialog
          dismissOnConfirm={dismissOnConfirm}
          onClose={onClose}
          onConfirm={onConfirm}
          open
          title="Confirm action"
        />
      )
      const schedule = vi.spyOn(window, 'setTimeout')
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

      if (!unmountDuringConfirm) {
        view.unmount()
      }

      await act(async () => {
        resolve()
        await pending
      })
      expect(schedule.mock.calls.filter(([, delay]) => delay === 600)).toHaveLength(0)
      await act(async () => {
        await vi.runOnlyPendingTimersAsync()
      })
      expect(onClose).not.toHaveBeenCalled()
    }
  )

  it('cancels an already armed close timer when unmounted', async () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    const view = render(<ConfirmDialog onClose={onClose} onConfirm={() => {}} open title="Confirm action" />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    })
    view.unmount()
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('ConfirmDialog secondary action', () => {
  function renderWithSecondary() {
    const onConfirm = vi.fn()
    const onClose = vi.fn()
    const onSecondary = vi.fn()

    render(
      <ConfirmDialog
        onClose={onClose}
        onConfirm={onConfirm}
        open
        secondaryAction={{ label: 'Remove from sidebar', onClick: onSecondary }}
        title="Remove worktree?"
      />
    )

    return { onClose, onConfirm, onSecondary }
  }

  it('runs the secondary action and closes without confirming', async () => {
    const { onClose, onConfirm, onSecondary } = renderWithSecondary()

    fireEvent.click(await screen.findByRole('button', { name: 'Remove from sidebar' }))

    expect(onSecondary).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('still opens focused on Confirm, so Enter confirms rather than picking the secondary', async () => {
    const { onConfirm, onSecondary } = renderWithSecondary()

    const dialog = await screen.findByRole('dialog')

    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
    vi.useFakeTimers()
    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    await act(async () => {
      fireEvent.keyDown(document.activeElement!, { key: 'Enter' })
    })

    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(onSecondary).not.toHaveBeenCalled()
  })
})
