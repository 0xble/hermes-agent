import { readFileSync } from 'node:fs'
import { load } from 'js-yaml'
import { expect, it } from 'vitest'

const action = path => load(readFileSync(new URL(path, import.meta.url), 'utf8'))
const setup = action('../.github/actions/setup-pm/action.yml')
const save = action('../.github/actions/save-pm-cache/action.yml')

// Evaluates the Actions expression subset these gates use, so the tests pin
// how a gate behaves rather than how it is spelled. Status functions default
// to a healthy, uncancelled run; `status` overrides them.
const evaluate = (expression, { status = {}, ...context } = {}) => {
  const lookup = path => path.split('.').reduce(
    (value, key) => (value !== null && typeof value === 'object' ? value[key] : undefined), context) ?? ''
  const statuses = { always: true, success: true, failure: false, cancelled: false, ...status }
  const body = String(expression).trim().replace(/^\$\{\{/, '').replace(/\}\}$/, '')
    .replace(/\b(?:inputs|steps|needs|github)(?:\.[\w-]+)+/g, path => JSON.stringify(lookup(path)))
    .replace(/\b(always|success|failure|cancelled)\(\)/g, (_, name) => String(statuses[name]))
  return Boolean(Function(`"use strict"; return (${body})`)())
}

it('restores compatible wheels without freezing a partial build under its dependency key', () => {
  const cached = setup.runs.steps.find(step => step.id === 'python-cache')
  const restored = setup.runs.steps.find(step => step.id === 'python-cache-restore')
  const prefixes = restored.with['restore-keys'].trim().split('\n')
  // Prefer this dependency set before falling back across dependency changes.
  const rollingPrefix = prefixes[0]
  expect(restored.with.key).toBe(`${rollingPrefix}\${{ github.run_id }}-\${{ github.run_attempt }}-\${{ github.job }}`)
  expect(rollingPrefix).toBe(`${cached.with.key}-`)
  expect(prefixes[1].trim()).toBe(cached.with['restore-keys'])
  for (const boundary of ['target', 'os-version', 'python-version']) {
    expect(prefixes[1]).toContain(`steps.prepare.outputs.${boundary}`)
  }
  expect(prefixes[1]).toContain("inputs.cache-suffix == ''")
  expect(prefixes[1]).not.toContain('hashFiles')
  // Only dependency-carrying callers save; tool-only jobs must not freeze an
  // empty cache under the production key (a stub exact-hit blocks real saves).
  const saving = { toolchain: 'all', 'cache-python': 'true', 'save-python-cache': 'true', 'test-environment': 'false' }
  expect(evaluate(cached.if, { inputs: { ...saving, extras: '' } })).toBe(false)
  expect(evaluate(cached.if, { inputs: { ...saving, extras: 'voice' } })).toBe(true)
  expect(evaluate(cached.if, { inputs: { ...saving, extras: '', 'test-environment': 'true' } })).toBe(true)
  // A caller either saves the exact key or restores the rolling one, never both.
  for (const saveCache of ['true', 'false']) {
    const inputs = { ...saving, extras: 'voice', 'save-python-cache': saveCache }
    expect(evaluate(restored.if, { inputs })).toBe(!evaluate(cached.if, { inputs }))
  }
  expect(setup.outputs['python-cache-key'].value).toContain('steps.python-cache-restore.outputs.cache-primary-key')

  // A suffix-only namespace isolates smoke reads but still lets production
  // restore smoke writes through its broad dependency fallback.
  const namespace = "${{ inputs.cache-suffix || 'production' }}"
  for (const template of [cached.with.key, restored.with.key, rollingPrefix]) {
    const production = template.replace(namespace, 'production')
    const smoke = template.replace(namespace, 'smoke-42-1')
    expect(production.startsWith('setup-pm-uv-v3-production-')).toBe(true)
    expect(smoke.startsWith('setup-pm-uv-v3-smoke-42-1-')).toBe(true)
    expect(smoke.startsWith('setup-pm-uv-v3-production-')).toBe(false)
    expect(production.startsWith('setup-pm-uv-v3-smoke-42-1-')).toBe(false)
  }
})

it('explicit PM saves prune to the lock and do not prune during cancellation', () => {
  const [prune, upload] = save.runs.steps
  expect(evaluate(prune.if)).toBe(true)
  expect(evaluate(prune.if, { status: { failure: true, success: false } })).toBe(true)
  expect(evaluate(prune.if, { status: { cancelled: true, success: false } })).toBe(false)
  expect(prune.run.split(/\s+/)).toEqual(expect.arrayContaining(['pm.build_env', '--exact-lock', '--cache', '--lock-source']))
  expect(prune.env.PM_PYTHON).toBe('${{ inputs.python }}')
  expect(prune.env.PM_CACHE).toBe('${{ inputs.path }}')
  expect(prune.env.PM_LOCK_SOURCE).toBe('${{ github.workspace }}')
  const pruned = outcome => ({ steps: { [prune.id]: { outcome } } })
  expect(evaluate(upload.if, pruned('success'))).toBe(true)
  expect(evaluate(upload.if, pruned('failure'))).toBe(false)
  expect(evaluate(upload.if, { ...pruned('success'), status: { cancelled: true, success: false } })).toBe(false)
  expect(upload.uses.split('@')[0]).toBe('actions/cache/save')
  expect(upload.with).toEqual({ path: '${{ inputs.path }}', key: '${{ inputs.key }}' })
})

it('explicit npm snapshots remain replaceable and isolated from toolchain-only producers', () => {
  const restored = setup.runs.steps.find(step => step.id === 'node-cache-restore')
  expect(restored).toBeDefined()
  const prefix = restored.with['restore-keys'].trim()
  expect(restored.with.key).toBe(`${prefix}\${{ github.run_id }}-\${{ github.run_attempt }}`)
  // A toolchain-only job must not shadow the desktop consumer's warm snapshot.
  for (const boundary of ['github.job', 'node-cache-dependency-path', 'cache-suffix', 'target', 'npm-version']) {
    expect(prefix).toContain(boundary)
  }
  expect(setup.outputs['node-cache-key'].value).toContain('steps.node-cache-restore.outputs.cache-primary-key')
})
