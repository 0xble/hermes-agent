import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const fixtures = vi.hoisted(() => ({
  invalidate: vi.fn(),
  select: vi.fn(),
  globalModels: vi.fn(async () => undefined),
  onboarding: vi.fn(async () => undefined),
  request: vi.fn(async () => ({ has_guest: false }))
}))

vi.mock('@tanstack/react-query', () => ({ useQueryClient: () => ({ invalidateQueries: fixtures.invalidate }) }))
vi.mock('@/hermes', () => ({
  getGlobalModelOptions: fixtures.globalModels,
  cancelOAuthSession: vi.fn(async () => undefined)
}))
vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ requestGateway: fixtures.request })
}))
vi.mock('@/components/onboarding/flow', () => ({ DeviceCode: () => null }))
vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick }: { children: ReactNode; onClick: () => void }) => (
    <button onClick={onClick}>{children}</button>
  )
}))
vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, onOpenChange }: { children: ReactNode; onOpenChange: (open: boolean) => void }) => (
    <div>
      <button onClick={() => onOpenChange(false)}>Dismiss</button>
      {children}
    </div>
  ),
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  preventCloseButtonAutoFocus: vi.fn()
}))
vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: { common: { cancel: 'Cancel' }, freeTier: { done: 'Done', change: 'Change', signedIn: 'Signed in' } }
  })
}))
vi.mock('@/store/onboarding', () => ({ refreshOnboardingProviders: fixtures.onboarding }))
vi.mock('@/store/session', async () => {
  const { atom } = await import('nanostores')

  return { $currentModel: atom('nous/welcome'), setModelPickerOpen: vi.fn() }
})

import { $freeTierSignIn, closeFreeTierSignIn } from '@/store/free-tier-sign-in'
import { $currentModel } from '@/store/session'

import { FreeTierSignInDialog } from './sign-in-dialog'

afterEach(() => {
  cleanup()
  closeFreeTierSignIn()
  vi.clearAllMocks()
})

describe('completed sign-in settlement', () => {
  it.each(['Dismiss', 'Done', 'Change'])('settles exactly once through %s', async door => {
    $currentModel.set('nous/welcome')
    $freeTierSignIn.set({ status: 'completed', model: 'account/model', email: null })
    render(<FreeTierSignInDialog onSelectModel={fixtures.select} />)
    const button = screen.getByText(door)
    await act(async () => {
      fireEvent.click(button)
      fireEvent.click(button)
    })
    expect($freeTierSignIn.get().status).toBe('closed')
    expect(fixtures.select).toHaveBeenCalledExactlyOnceWith({ model: 'account/model', provider: 'nous' })
    expect(fixtures.invalidate).toHaveBeenCalledTimes(2)
    expect(fixtures.globalModels).toHaveBeenCalledTimes(1)
    expect(fixtures.onboarding).toHaveBeenCalledTimes(1)
  })

  it('does not settle a failed flow or overwrite a user-selected model', async () => {
    $freeTierSignIn.set({ status: 'failed', kind: 'error', message: 'fixture' })
    const view = render(<FreeTierSignInDialog onSelectModel={fixtures.select} />)
    fireEvent.click(screen.getByText('Dismiss'))
    expect(fixtures.invalidate).not.toHaveBeenCalled()
    $currentModel.set('user/pinned')
    act(() => {
      $freeTierSignIn.set({ status: 'completed', model: 'account/model', email: null })
    })
    view.rerender(<FreeTierSignInDialog onSelectModel={fixtures.select} />)
    await act(async () => {
      fireEvent.click(screen.getByText('Dismiss'))
    })
    expect(fixtures.select).not.toHaveBeenCalled()
    expect(fixtures.invalidate).toHaveBeenCalledTimes(2)
  })
})
