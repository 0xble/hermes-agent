import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { after, test } from 'node:test'

const runner = resolve(dirname(fileURLToPath(import.meta.url)), '../../run-workspace-checks.mjs')
const fixture = mkdtempSync(join(tmpdir(), 'workspace-checks-'))
after(() => rmSync(fixture, { recursive: true, force: true }))
writeFileSync(join(fixture, 'package.json'), JSON.stringify({ private: true, workspaces: ['packages/*'] }))
for (const [name, code] of [['pass', 0], ['fail', 7]]) {
  const dir = join(fixture, 'packages', name)
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'package.json'), JSON.stringify({
    name, version: '1.0.0', scripts: { check: 'node check.mjs' },
  }))
  writeFileSync(join(dir, 'check.mjs'), `console.log('executed-${name}'); process.exitCode = ${code}`)
}
const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm'
const install = spawnSync(npm, ['install', '--offline', '--ignore-scripts', '--package-lock=false', '--no-audit', '--no-fund'], {
  cwd: fixture, encoding: 'utf8', shell: process.platform === 'win32',
})
assert.equal(install.status, 0, install.stdout + install.stderr)
function run(...args) {
  return spawnSync(process.execPath, [runner, ...args], { cwd: fixture, encoding: 'utf8' })
}
for (const args of [
  ['--concurrency', 'banana'], ['--concurrency'], ['--concurrency', 'Infinity'],
  ['--concurrency', '0'], ['--concurrency', '-1'], ['--concurrency', '1.5'],
  ['--skip'], ['--unknown'], ['--concurrency', '2', '--concurrency', '3'],
]) {
  test(`rejects malformed arguments: ${args.join(' ')}`, () => {
    const result = run(...args)
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, /argument|concurrency|skip/i)
    assert.doesNotMatch(result.stdout, /all .* checks passed|executed-/)
  })
}
test('runs every selected unit and aggregates failure', () => {
  const result = run('--concurrency', '1')
  assert.equal(result.status, 1)
  assert.match(result.stdout, /executed-pass/)
  assert.match(result.stdout, /executed-fail/)
  assert.match(result.stderr, /1 of 2 checks failed/)
})
test('explicit nonrelease selection can pass', () => {
  const result = run('--skip', 'packages/fail', '--concurrency', '2')
  assert.equal(result.status, 0, result.stderr)
  assert.match(result.stdout, /all 1 checks passed/)
})
test('defaults to a bounded per-job budget and honors explicit concurrency', () => {
  const budgetFixture = mkdtempSync(join(tmpdir(), 'workspace-budget-'))
  try {
    writeFileSync(join(budgetFixture, 'package.json'), JSON.stringify({ private: true, workspaces: ['packages/*'] }))
    for (let index = 0; index < 6; index++) {
      const dir = join(budgetFixture, 'packages', `unit-${index}`)
      mkdirSync(dir, { recursive: true })
      writeFileSync(join(dir, 'package.json'), JSON.stringify({
        name: `unit-${index}`, version: '1.0.0', scripts: { check: `node -e "console.log('executed-${index}')"` },
      }))
    }
    const setup = spawnSync(npm, ['install', '--offline', '--ignore-scripts', '--package-lock=false', '--no-audit', '--no-fund'], {
      cwd: budgetFixture, encoding: 'utf8', shell: process.platform === 'win32',
    })
    assert.equal(setup.status, 0, setup.stdout + setup.stderr)
    for (const [workers, args, expected] of [
      [undefined, [], 4], ['2', [], 2], ['1', ['--concurrency', '3'], 3],
    ]) {
      const env = { ...process.env }
      delete env.HERMES_TEST_WORKERS
      if (workers !== undefined) env.HERMES_TEST_WORKERS = workers
      const result = spawnSync(process.execPath, [runner, ...args], { cwd: budgetFixture, env, encoding: 'utf8' })
      assert.equal(result.status, 0, result.stdout + result.stderr)
      assert.match(result.stdout, new RegExp(`up to ${expected} at a time`))
      assert.match(result.stdout, /all 6 checks passed/)
    }
    for (const budget of ['0', 'banana', '1.5', 'Infinity']) {
      const result = spawnSync(process.execPath, [runner], {
        cwd: budgetFixture, env: { ...process.env, HERMES_TEST_WORKERS: budget }, encoding: 'utf8',
      })
      assert.notEqual(result.status, 0)
      assert.match(result.stderr, /HERMES_TEST_WORKERS requires a positive integer/)
      assert.doesNotMatch(result.stdout, /executed-/)
    }
  } finally {
    rmSync(budgetFixture, { recursive: true, force: true })
  }
})

test('unmatched or all-unit skips cannot report success', () => {
  assert.equal(run('--skip', 'missing').status, 1)
  assert.equal(run('--skip', 'packages/').status, 1)
})
