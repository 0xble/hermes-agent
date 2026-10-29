import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { PanelRowMenu } from './panel'

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.setPointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

describe('PanelRowMenu', () => {
  afterEach(async () => {
    cleanup()
    // Radix restores focus on the next timer turn after unmount. Finish that
    // callback before Vitest tears down this file's jsdom event constructors.
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0))
    })
  })

  it('opens its actions menu from the kebab without a tooltip', async () => {
    const onSelect = vi.fn()

    render(<PanelRowMenu items={[{ label: 'Rename', onSelect }]} />)

    const trigger = screen.getByRole('button', { name: 'Actions' })

    expect(trigger.closest('[data-slot="tooltip-trigger"]')).toBeNull()

    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Rename' }))

    expect(onSelect).toHaveBeenCalledOnce()
    await waitFor(() => {
      expect(screen.queryByRole('menu')).toBeNull()
      expect(document.activeElement).toBe(trigger)
    })
  })
})
