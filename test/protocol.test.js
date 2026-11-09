import test from 'node:test'
import assert from 'node:assert/strict'

import {
  extractHumanMessages,
  contentText,
  normalizeSessionSummary,
  isOrdinarySession,
  basenameOf,
} from '../lib/protocol.js'

test('contentText keeps only text blocks', () => {
  assert.equal(contentText([
    { type: 'text', text: 'hello' },
    { type: 'image', source: 'ignored' },
    { type: 'text', text: ' world ' },
  ]), 'hello\n\nworld')
  assert.equal(contentText('plain'), 'plain')
  assert.equal(contentText(undefined), '')
})

test('extractHumanMessages ignores injected user/message context', () => {
  const events = [
    { type: 'user/message', seq: 1, data: { source: { kind: 'agent-instructions' }, content: [{ type: 'text', text: 'system' }] } },
    { type: 'user/message', seq: 2, data: { source: { kind: 'user' }, content: [{ type: 'text', text: 'first question' }] } },
    { type: 'assistant/message', seq: 3, data: {} },
    { type: 'user/message', seq: 4, data: { source: { kind: 'time-context' }, content: [{ type: 'text', text: 'clock' }] } },
    { type: 'user/message', seq: 5, data: { source: { kind: 'user' }, content: [{ type: 'text', text: 'follow up' }] } },
  ]
  const result = extractHumanMessages(events)
  assert.equal(result.count, 2)
  assert.deepEqual(result.messages.map((message) => message.text), ['first question', 'follow up'])
})

test('extractHumanMessages keeps the head and the tail', () => {
  const events = Array.from({ length: 20 }, (_, index) => ({
    type: 'user/message',
    seq: index + 1,
    data: { source: { kind: 'user' }, content: [{ type: 'text', text: `message ${index + 1}` }] },
  }))
  const result = extractHumanMessages(events, { first: 2, last: 3 })
  assert.equal(result.count, 20)
  assert.deepEqual(result.messages.map((message) => message.text), ['message 1', 'message 2', 'message 18', 'message 19', 'message 20'])
})

test('normalizeSessionSummary uses the log title, then the cwd basename', () => {
  const record = { header: { id: 's1', cwd: 'D:\\app\\workspace\\DS-RSI', createdAt: 1794207137483 } }
  const titled = normalizeSessionSummary(record, { title: { title: '1109｜功能｜标题整理', source: { kind: 'user' }, eventSeq: 7 } })
  assert.equal(titled.title, '1109｜功能｜标题整理')
  assert.equal(titled.titleSource, 'user')
  assert.equal(titled.createdAt, 1794207137483)
  assert.equal(isOrdinarySession(titled), true)

  const untitled = normalizeSessionSummary(record, {})
  assert.equal(untitled.title, null)
  assert.equal(untitled.displayTitle, 'DS-RSI')
  assert.equal(basenameOf('D:\\app\\workspace\\DS-RSI'), 'DS-RSI')
})

test('isOrdinarySession excludes subagent and archived rows', () => {
  assert.equal(isOrdinarySession({ id: 'a', origin: 'subagent', archived: false }), false)
  assert.equal(isOrdinarySession({ id: 'b', origin: 'ordinary', archived: true }), false)
  assert.equal(isOrdinarySession({ id: 'c', origin: 'ordinary', archived: false }), true)
})
