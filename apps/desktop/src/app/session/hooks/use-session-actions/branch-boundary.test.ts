import { expect, test } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages/hydration'
import type { SessionMessage } from '@/types/hermes'

import { branchThroughRowId } from './branch-boundary'
import { selectBranchMessages } from './utils'

test('branching from a folded assistant bubble keeps through its last durable row, not its first', () => {
  const messages = toChatMessages([
    { row_id: 10, role: 'user', content: 'first question', timestamp: 1 },
    {
      row_id: 11,
      role: 'assistant',
      content: '',
      tool_calls: [{ id: 'c1', type: 'function', function: { name: 'terminal', arguments: '{}' } }],
      timestamp: 2
    },
    { row_id: 12, role: 'tool', content: 'ran', tool_call_id: 'c1', timestamp: 3 },
    { row_id: 13, role: 'assistant', content: 'first answer', timestamp: 4 },
    { row_id: 14, role: 'user', content: 'later question the branch must leave out', timestamp: 5 },
    { row_id: 15, role: 'assistant', content: 'later answer', timestamp: 6 }
  ] as never)

  const answer = messages.find(
    message => message.role === 'assistant' && JSON.stringify(message.parts).includes('first answer')
  )!

  const branch = selectBranchMessages(messages, messages, answer.id)

  expect(branch.map(message => message.content)).toEqual(['first question', 'first answer'])
  expect(branchThroughRowId(branch)).toBe(13)
  // An optimistic bubble with no durable row has no boundary (the adapter refuses it).
  expect(
    branchThroughRowId([{ content: 'x', role: 'user', source: { id: 'local', role: 'user', parts: [] } }])
  ).toBeUndefined()
})

test.each([
  { kind: 'tool-call', rowId: 13 },
  { kind: 'reasoning', rowId: 15 }
])('branching retains the last represented $kind row after assistant text', ({ kind, rowId }) => {
  const rows: SessionMessage[] = [
    { row_id: 11, role: 'user', content: 'question', timestamp: 1 },
    { row_id: 12, role: 'assistant', content: 'visible answer', timestamp: 2 },
    {
      row_id: 13,
      role: 'assistant',
      content: '',
      tool_calls: [{ id: 'c1', type: 'function', function: { name: 'terminal', arguments: '{}' } }],
      timestamp: 3
    },
    { row_id: 14, role: 'tool', content: 'ran', tool_call_id: 'c1', timestamp: 4 }
  ]

  if (kind === 'reasoning') {
    rows.push({ row_id: 15, role: 'assistant', content: '', reasoning: 'last represented reasoning', timestamp: 5 })
  }

  rows.push(
    { row_id: 20, role: 'user', content: 'later question', timestamp: 6 },
    { row_id: 21, role: 'assistant', content: 'later answer', timestamp: 7 }
  )
  const messages = toChatMessages(rows)

  const answer = messages.find(
    message => message.role === 'assistant' && message.parts.some(part => part.type === 'tool-call')
  )!

  const branch = selectBranchMessages(messages, messages, answer.id)

  expect(branch.map(message => message.content)).toEqual(['question', 'visible answer'])
  expect(branch.at(-1)?.source.rowId).toBe(12)
  expect(branchThroughRowId(branch)).toBe(rowId)
  expect(answer.parts.find(part => part.type === kind)?.sourceRowId).toBe(rowId)
})
