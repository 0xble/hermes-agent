import { readFileSync } from 'node:fs'
import { load } from 'js-yaml'
import { expect, it } from 'vitest'

const action = path => load(readFileSync(new URL(path, import.meta.url), 'utf8'))
const setup = action('../.github/actions/setup-pm/action.yml')

// Evaluates the Actions expression subset these gates use, so the tests pin
// how a gate behaves rather than how it is spelled. Status functions default
// to a healthy, uncancelled run; `status` overrides them.
const value = (expression, { status = {}, ...context } = {}) => {
  const lookup = path => path.split('.').reduce(
    (value, key) => (value !== null && typeof value === 'object' ? value[key] : undefined), context) ?? ''
  const statuses = { always: true, success: true, failure: false, cancelled: false, ...status }
  const body = String(expression).trim().replace(/^\$\{\{/, '').replace(/\}\}$/, '')
    .replace(/\b(?:inputs|steps|needs|github)(?:\.[\w-]+)+/g, path => JSON.stringify(lookup(path)))
    .replace(/\b(always|success|failure|cancelled)\(\)/g, (_, name) => String(statuses[name]))
  return Function(`"use strict"; return (${body})`)()
}
const evaluate = (expression, context) => Boolean(value(expression, context))

it('pull requests restore the caches main saves and never write their own', () => {
  const step = id => setup.runs.steps.find(candidate => candidate.id === id)
  const base = { toolchain: 'all', cache: 'true', 'cache-python': 'true', 'cache-node': 'true', 'test-environment': 'false', extras: 'voice' }
  for (const [kind, saving, restoring] of [
    ['tools', 'tools-cache', 'tools-cache-restore'],
    ['python', 'python-cache', 'python-cache-restore'],
    ['node', 'node-cache', 'node-cache-restore'],
  ]) {
    const input = `save-${kind}-cache`
    const save = step(saving)
    const restore = step(restoring)
    // A PR-scoped entry is restorable only by that PR, so PRs never save.
    for (const [event_name, saves] of [['pull_request', false], ['push', true], ['workflow_dispatch', true]]) {
      const inputs = { ...base, [input]: value(setup.inputs[input].default, { github: { event_name } }) }
      expect(evaluate(save.if, { inputs }), `${kind} on ${event_name}`).toBe(saves)
      expect(evaluate(restore.if, { inputs }), `${kind} on ${event_name}`).toBe(!saves)
    }
    // Restore-only reads exactly what the saving step writes on main.
    expect(restore.uses.split('@')[0]).toBe('actions/cache/restore')
    expect(restore.with.key).toBe(save.with.key)
    expect(restore.with['restore-keys']).toBe(save.with['restore-keys'])
    expect(restore.with.path).toBe(save.with.path)
  }
})

it('only dependency-carrying callers touch the uv cache', () => {
  const cached = setup.runs.steps.find(step => step.id === 'python-cache')
  // Tool-only jobs must not freeze an empty cache under the production key
  // (a stub exact-hit blocks real saves).
  const saving = { toolchain: 'all', 'cache-python': 'true', 'save-python-cache': 'true', 'test-environment': 'false' }
  expect(evaluate(cached.if, { inputs: { ...saving, extras: '' } })).toBe(false)
  expect(evaluate(cached.if, { inputs: { ...saving, extras: 'voice' } })).toBe(true)
  expect(evaluate(cached.if, { inputs: { ...saving, extras: '', 'test-environment': 'true' } })).toBe(true)
  // A suffix-only namespace isolates smoke runs from production's prefix.
  const namespace = "${{ inputs.cache-suffix || 'production' }}"
  expect(cached.with.key.replace(namespace, 'smoke-42-1').startsWith('setup-pm-uv-v3-production-')).toBe(false)
  expect(cached.with['restore-keys']).toContain("inputs.cache-suffix == ''")
})
