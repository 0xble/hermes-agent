import assert from 'node:assert/strict'
import { execFile, execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'

import { afterEach, test, vi } from 'vitest'

import { resolveDesktopHermesHome } from './data-paths'
import { readUiPreferences, uiPreferencesPath, uiPreferencesRoot, writeUiPreferences } from './ui-preferences'

// Native Desktop (this module) and the Webapp's Python server must land on ONE
// file and merge into it without losing each other's keys (#145213).
const repo = path.resolve(import.meta.dirname, '../../..')
const python = process.env.HERMES_PYTHON || 'python3'
const homes: string[] = []

afterEach(() => {
  vi.useRealTimers()

  for (const home of homes.splice(0)) {
    fs.rmSync(home, { force: true, recursive: true })
  }
})

function scratchHome(): string {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-ui-prefs-'))
  homes.push(home)

  return home
}

function pythonEnv(home: string, hermesHome: string): NodeJS.ProcessEnv {
  return { HERMES_HOME: hermesHome, HOME: home, PATH: process.env.PATH, PYTHONPATH: repo }
}

const PY_PATH = 'from hermes_cli.ui_preferences import store_path; print(store_path())'

/** The file main's IPC registers for this HERMES_HOME (registerUiPreferencesIpc). */
const electronStorePath = (home: string, hermesHome: string) =>
  uiPreferencesPath(uiPreferencesRoot(resolveDesktopHermesHome({ env: { HERMES_HOME: hermesHome }, home })))

test('native Desktop and the Webapp server resolve the same store from a profile home', () => {
  const home = scratchHome()
  const root = path.join(home, 'custom-root')
  const profileHome = path.join(root, 'profiles', 'coder')
  fs.mkdirSync(profileHome, { recursive: true })

  const electronFile = electronStorePath(home, profileHome)

  const pythonFile = execFileSync(python, ['-c', PY_PATH], {
    cwd: repo,
    encoding: 'utf8',
    env: pythonEnv(home, profileHome)
  }).trim()

  assert.equal(electronFile, path.join(fs.realpathSync(root), 'ui-preferences.json'))
  assert.equal(pythonFile, electronFile)
})

test.skipIf(process.platform === 'win32')(
  'a symlinked profile directory still resolves its root, the same store on both sides',
  () => {
    const home = scratchHome()
    const root = path.join(home, '.hermes')
    const elsewhere = path.join(home, 'elsewhere', 'coder')
    fs.mkdirSync(path.join(root, 'profiles'), { recursive: true })
    fs.mkdirSync(elsewhere, { recursive: true })
    fs.symlinkSync(elsewhere, path.join(root, 'profiles', 'coder'))
    const profileHome = path.join(root, 'profiles', 'coder')

    const pythonFile = execFileSync(python, ['-c', PY_PATH], {
      cwd: repo,
      encoding: 'utf8',
      env: pythonEnv(home, profileHome)
    }).trim()

    assert.equal(electronStorePath(home, profileHome), path.join(fs.realpathSync(root), 'ui-preferences.json'))
    assert.equal(pythonFile, electronStorePath(home, profileHome))
  }
)

test('a custom home inside the default home resolves the same store on both sides', () => {
  const home = scratchHome()
  const sandbox = path.join(home, '.hermes', 'sandbox')
  fs.mkdirSync(sandbox, { recursive: true })

  const electronFile = electronStorePath(home, sandbox)

  const pythonFile = execFileSync(python, ['-c', PY_PATH], {
    cwd: repo,
    encoding: 'utf8',
    env: pythonEnv(home, sandbox)
  }).trim()

  assert.equal(electronFile, path.join(fs.realpathSync(sandbox), 'ui-preferences.json'))
  assert.equal(fs.realpathSync(path.dirname(pythonFile)), fs.realpathSync(path.dirname(electronFile)))
})

test('a live-owner lock stays pending and unchanged until the owner releases it', async () => {
  const root = scratchHome()
  const file = uiPreferencesPath(root)
  const lockDir = `${file}.lock`
  const owner = path.join(lockDir, 'owner')
  fs.mkdirSync(lockDir)
  fs.writeFileSync(owner, String(process.pid))

  let settled = false

  const pending = writeUiPreferences(file, { values: { 'hermes.desktop.waiter': 'released' } }).finally(() => {
    settled = true
  })

  await new Promise(resolve => setTimeout(resolve, 100))
  assert.equal(settled, false)
  assert.equal(fs.readFileSync(owner, 'utf8'), String(process.pid))
  assert.equal(fs.existsSync(lockDir), true)

  fs.rmSync(lockDir, { force: true, recursive: true })
  await pending
  assert.equal(readUiPreferences(file).values['hermes.desktop.waiter'], 'released')
})

test('a dead-pid lock is reclaimed', async () => {
  const root = scratchHome()
  const file = uiPreferencesPath(root)
  const lockDir = `${file}.lock`
  fs.mkdirSync(lockDir)
  fs.writeFileSync(path.join(lockDir, 'owner'), '2147483647-dead')

  const written = await writeUiPreferences(file, { values: { 'hermes.desktop.reclaimed': 'yes' } })

  assert.equal(written.values['hermes.desktop.reclaimed'], 'yes')
  assert.equal(fs.existsSync(lockDir), false)
})

test('an ownerless lock dir is reclaimed once it is stale', async () => {
  const root = scratchHome()
  const file = uiPreferencesPath(root)
  const lockDir = `${file}.lock`
  fs.mkdirSync(lockDir)
  const old = new Date(Date.now() - 11_000)
  fs.utimesSync(lockDir, old, old)

  const written = await writeUiPreferences(file, { values: { 'hermes.desktop.reclaimed': 'ownerless' } })

  assert.equal(written.values['hermes.desktop.reclaimed'], 'ownerless')
  assert.equal(fs.existsSync(lockDir), false)
})

test('a waiter retries when an owner appears during stale recovery', async () => {
  const root = scratchHome()
  const file = uiPreferencesPath(root)
  const lockDir = `${file}.lock`
  const owner = path.join(lockDir, 'owner')
  const newOwner = `${process.pid}-holder`
  fs.mkdirSync(lockDir)
  const old = new Date(Date.now() - 11_000)
  fs.utimesSync(lockDir, old, old)

  let restoredOwner: string | undefined
  let wroteBeforeRelease = false
  const rename = fs.promises.rename

  const spy = vi.spyOn(fs.promises, 'rename').mockImplementation(async (source, target) => {
    if (source === lockDir) {
      // Publish the holder's token after the waiter judged the directory ownerless.
      fs.writeFileSync(owner, newOwner)
    }

    await rename(source, target)

    if (target === lockDir) {
      restoredOwner = fs.readFileSync(owner, 'utf8')
      wroteBeforeRelease = fs.existsSync(file)
      fs.rmSync(lockDir, { recursive: true })
    }
  })

  const values = { 'hermes.desktop.waiter': 'released' }

  try {
    await writeUiPreferences(file, { values })
  } finally {
    spy.mockRestore()
  }

  assert.equal(restoredOwner, newOwner)
  assert.equal(wroteBeforeRelease, false)
  assert.deepEqual(readUiPreferences(file).values, values)
  assert.deepEqual(fs.readdirSync(root), [path.basename(file)])
})

test('stale recovery leaves an observed successor at its lock path', async () => {
  const root = scratchHome()
  const file = uiPreferencesPath(root)
  const lockDir = `${file}.lock`
  const owner = path.join(lockDir, 'owner')
  const successor = `${process.pid}-successor`
  fs.mkdirSync(lockDir)
  fs.writeFileSync(owner, '2147483647-dead')
  const old = new Date(Date.now() - 11 * 60_000)
  fs.utimesSync(owner, old, old)

  let successorReady = false
  const stat = fs.promises.stat
  const mkdir = fs.promises.mkdir
  const statSpy = vi.spyOn(fs.promises, 'stat').mockImplementation(async (...args) => {
    const observed = await stat(...args)

    if (args[0] === owner && !successorReady) {
      fs.writeFileSync(owner, successor)
      successorReady = true
    }

    return observed
  })
  const mkdirSpy = vi.spyOn(fs.promises, 'mkdir').mockImplementation((...args) => {
    if (args[0] === lockDir && successorReady) {
      assert.equal(fs.readFileSync(owner, 'utf8'), successor)
      assert.equal(fs.existsSync(file), false)
      fs.rmSync(lockDir, { recursive: true })
    }

    return mkdir(...args)
  })
  const renameSpy = vi.spyOn(fs.promises, 'rename')

  try {
    const values = { 'hermes.desktop.waiter': 'released' }
    assert.deepEqual((await writeUiPreferences(file, { values })).values, values)
    assert.equal(renameSpy.mock.calls.some(([source]) => source === lockDir), false)
  } finally {
    renameSpy.mockRestore()
    mkdirSpy.mockRestore()
    statSpy.mockRestore()
  }
})

test('a live owner hits the bounded backstop and does not wedge later writes', async () => {
  // Fake only the wall clock. The lock loop awaits real fs I/O between polls, so faking
  // setTimeout would strand a poll scheduled after the jump; real 20 ms polls keep running.
  vi.useFakeTimers({ toFake: ['Date'] })
  const root = scratchHome()
  const file = uiPreferencesPath(root)
  const lockDir = `${file}.lock`
  fs.mkdirSync(lockDir)
  fs.writeFileSync(path.join(lockDir, 'owner'), String(process.pid))

  try {
    let outcome: unknown = 'pending'

    const pending = writeUiPreferences(file, { values: { 'hermes.desktop.stuck': 'yes' } }).then(
      () => {
        outcome = 'resolved'
      },
      (error: unknown) => {
        outcome = error
      }
    )

    for (let step = 0; step < 200 && outcome === 'pending'; step += 1) {
      vi.setSystemTime(Date.now() + 1_000)
      await new Promise(resolve => setTimeout(resolve, 25))
    }

    await pending
    assert.match(String(outcome), /preferences store is busy/)
  } finally {
    vi.useRealTimers()
    fs.rmSync(lockDir, { force: true, recursive: true })
  }

  const written = await writeUiPreferences(file, { values: { 'hermes.desktop.afterBusy': 'yes' } })
  assert.equal(written.values['hermes.desktop.afterBusy'], 'yes')
})

test('concurrent native and Webapp writers keep every key the other wrote', async () => {
  const home = scratchHome()
  const root = path.join(home, 'root')
  fs.mkdirSync(root)
  const file = uiPreferencesPath(root)

  const pyWrites = `
from hermes_cli.ui_preferences import write_preferences
for n in range(25):
    write_preferences({f"hermes.desktop.py{n}": "web"})
`

  const pythonRun = promisify(execFile)(python, ['-c', pyWrites], { cwd: repo, env: pythonEnv(home, root) })

  for (let n = 0; n < 25; n += 1) {
    await writeUiPreferences(file, { values: { [`hermes.desktop.native${n}`]: 'native' } })
  }

  await pythonRun

  const { revision, values } = readUiPreferences(file)

  assert.equal(Object.keys(values).length, 50)
  assert.equal(revision, 50)

  for (let n = 0; n < 25; n += 1) {
    assert.equal(values[`hermes.desktop.native${n}`], 'native')
    assert.equal(values[`hermes.desktop.py${n}`], 'web')
  }

  assert.equal(fs.existsSync(`${file}.lock`), false)
})

test('a client’s localStorage import only adds the keys a store lacks', async () => {
  const root = scratchHome()
  const file = uiPreferencesPath(root)

  await writeUiPreferences(file, { values: { 'hermes-desktop-mode-v1': 'dark' } })

  const seeded = await writeUiPreferences(file, {
    seed: true,
    values: { 'hermes-desktop-mode-v1': 'light', 'hermes-desktop-theme-v2': 'nous' }
  })

  assert.deepEqual(seeded.values, { 'hermes-desktop-mode-v1': 'dark', 'hermes-desktop-theme-v2': 'nous' })
  assert.deepEqual(readUiPreferences(file).values, seeded.values)
})

// Only a missing file is "no store": merging over a store that exists but
// cannot be read (EACCES, a Windows AV lock, EMFILE) dropped every other key.
test.skipIf(process.platform === 'win32' || process.getuid?.() === 0)(
  'neither writer merges over a store it cannot read',
  async () => {
    const home = scratchHome()
    const file = uiPreferencesPath(home)
    await writeUiPreferences(file, { values: { 'hermes-desktop-theme-v2': 'nous', 'hermes.desktop.keybinds': '{}' } })
    const before = fs.readFileSync(file, 'utf8')
    fs.chmodSync(file, 0)

    try {
      await assert.rejects(writeUiPreferences(file, { values: { 'hermes-desktop-mode-v1': 'dark' } }), /unreadable/)

      const pythonWrite = promisify(execFile)(
        python,
        ['-c', 'from hermes_cli.ui_preferences import write_preferences as w; w({"hermes-desktop-mode-v1": "dark"})'],
        { cwd: repo, env: pythonEnv(home, home) }
      )

      await assert.rejects(pythonWrite, /PreferencesBusy: preferences store is unreadable/)
    } finally {
      fs.chmodSync(file, 0o600)
    }

    assert.equal(fs.readFileSync(file, 'utf8'), before)
  }
)
