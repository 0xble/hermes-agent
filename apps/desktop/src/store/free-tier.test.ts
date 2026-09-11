import { afterEach, describe, expect, it } from 'vitest'

import type { FreeTierStatus } from '@/types/hermes'

import {
  $freeTierRoute,
  $freeTierStatus,
  refreshFreeTierStatus,
  resetFreeTierStatus,
  setFreeTierRoute
} from './free-tier'
import type { FreeTierRequester } from './free-tier'

const A = { has_guest: true, notice_pending: true } as FreeTierStatus
const B = { has_guest: false, notice_pending: false } as FreeTierStatus
const requester = (value: FreeTierStatus) => (async () => value) as FreeTierRequester

afterEach(() => resetFreeTierStatus())

describe('free-tier cache authority', () => {
  it('retains transient failures only within one activation, clearing route and status on a switch', async () => {
    await refreshFreeTierStatus(requester(A))
    setFreeTierRoute(true)

    const failing = async () => {
      throw new Error('Method not found')
    }

    expect(await refreshFreeTierStatus(failing)).toBe(A)
    resetFreeTierStatus()
    expect($freeTierRoute.get()).toBeNull()
    expect(await refreshFreeTierStatus(failing)).toBeNull()
    expect($freeTierStatus.get()).toBeNull()
  })

  it('drops old-source responses and out-of-order responses in the current source', async () => {
    let resolveOld!: (value: FreeTierStatus) => void

    const pending = () =>
      new Promise<FreeTierStatus>(resolve => {
        resolveOld = resolve
      })

    const old = refreshFreeTierStatus(pending as FreeTierRequester)
    resetFreeTierStatus()
    await refreshFreeTierStatus(requester(B))
    resolveOld(A)
    expect(await old).toBeNull()
    expect($freeTierStatus.get()).toBe(B)
    const earlier = refreshFreeTierStatus(pending as FreeTierRequester)
    await refreshFreeTierStatus(requester(B))
    resolveOld(A)
    await earlier
    expect($freeTierStatus.get()).toBe(B)
  })
})
