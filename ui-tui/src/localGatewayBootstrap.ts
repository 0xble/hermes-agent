/** Local gateway bootstrap for the Ink TUI: run ui-tui's `gateway_bootstrap.py` on the install's
interpreter and parse its grant (or its named terminal failure). Kept out of the transport class;
`GatewayClient` takes it as its injected `bootstrap`. */
import { execFile, spawn } from 'node:child_process'
import { resolve } from 'node:path'
import { promisify } from 'node:util'

import { pythonHelperPath } from './lib/pythonHelpers.js'

const resolvePython = () => {
  // Trust HERMES_PYTHON only. The launcher guarantees it: hermes_cli/main.py
  // validates it and falls back to its own sys.executable, and the Nix
  // wrapper sets it too. So a TUI started the normal way already knows its
  // interpreter, and scanning VIRTUAL_ENV / .venv here can only find a
  // DIFFERENT python than the parent process runs on — with the pm store,
  // a stale venv path is actively dangerous (the interpreter a gateway
  // child gets must match the one that spawned it).
  const configured = process.env.HERMES_PYTHON?.trim()

  if (configured) {
    return configured
  }

  // The one case with no launcher above it: `npm run dev` / `npm start`
  // straight out of ui-tui/. A developer doing that runs inside their own
  // activated environment, so PATH is the right question there.
  return process.platform === 'win32' ? 'python' : 'python3'
}

export interface LocalGatewayGrant { url: string; protocols: string[]; profile_id: string; instance_id: string }

export interface TypedInput { home: string; session: string; request: string; text: string }

export async function withNativeTypedInput<T>(input: TypedInput, send: () => Promise<T>): Promise<T> {
  const root = process.env.HERMES_PYTHON_SRC_ROOT ?? resolve(import.meta.dirname, '../../')

  const child = spawn(resolvePython(), [pythonHelperPath('gateway_bootstrap.py', root), '--typed-input'], {
    cwd: root,
    env: { ...process.env, HERMES_HOME: input.home, PYTHONPATH: root, HERMES_PYTHON_SRC_ROOT: root },
    stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true
  })

  const closed = new Promise<void>(done => child.once('close', () => done()))

  try {
    try {
      await new Promise<void>((ready, reject) => {
        let output = ''
        const timer = setTimeout(() => reject(new Error('Native input receipt timed out')), 2000)
        child.once('error', error => { clearTimeout(timer); reject(error) })
        child.stdin.once('error', error => { clearTimeout(timer); reject(error) })
        child.once('exit', () => { clearTimeout(timer); reject(new Error('Native input receipt unavailable')) })
        child.stdout.on('data', (chunk: Buffer) => {
          output += chunk.toString()

          if (/(?:^|\n)ready\r?\n/.test(output)) { clearTimeout(timer); ready() }
        })
        child.stdin.write(JSON.stringify({ session: input.session, request: input.request, text: input.text }) + '\n')
      })
    } catch (error) {
      child.stdin.end()
      console.error('Native input origin unavailable:', error instanceof Error ? error.message : String(error))
    }

    return await send()
  } finally {
    child.stdin.end()
    const timer = setTimeout(() => child.kill(), 5000)
    await closed.finally(() => clearTimeout(timer))
  }
}

/** gateway_bootstrap.py's verdict for an interpreter that cannot load Hermes or its dependencies. */
// execFile's error text is `Command failed: …` followed by the child's stderr, hence the line match.
export const isTerminalBootstrapFailure = (message: string) => /^gateway unavailable: /m.test(message)

export const bootstrapLocalGateway = async (start: boolean, recover = false): Promise<LocalGatewayGrant> => {
  const root = process.env.HERMES_PYTHON_SRC_ROOT ?? resolve(import.meta.dirname, '../../')

  const { stdout } = await promisify(execFile)(resolvePython(),
    [pythonHelperPath('gateway_bootstrap.py', root), ...(start ? ['--start'] : recover ? ['--recover'] : [])],
    { cwd: root, env: { ...process.env, PYTHONPATH: root, HERMES_PYTHON_SRC_ROOT: root }, timeout: 40_000, maxBuffer: 1024 * 1024 })

  return JSON.parse(stdout) as LocalGatewayGrant
}
