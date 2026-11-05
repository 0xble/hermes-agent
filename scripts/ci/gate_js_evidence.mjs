// Frozen Vitest observer and npm proxy; candidate source/configuration stays untouched.
import { spawnSync } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export default class GateReporter {
  onInit(vitest) {
    this.root = process.env.FROZEN_JS_ROOT
    this.infra = []
    this.planned = []
    for (const project of vitest.projects) {
      if (project.config.retry || project.config.repeats) this.infra.push('Retries/repeats configured')
    }
  }

  onTestRunStart(specifications) {
    this.planned = specifications.map(s => JSON.stringify([s.project?.name || '', path.relative(this.root, s.moduleId)])).sort()
  }

  onTestRunEnd(modules, errors, reason) {
    const nodes = []
    const visit = (task, file, names = [], skipped, project = '') => {
      const result = task.result || (['skip', 'todo'].includes(task.mode) ? { state: task.mode } :
        skipped ? { state: skipped } : undefined)
      if (!result || !['pass', 'fail', 'skip', 'todo'].includes(result.state)) {
        this.infra.push(`Incomplete task: ${file}: ${task.name}`)
        return
      }
      if (Object.values(result.hooks || {}).some(state => state !== 'pass')) {
        this.infra.push(`Setup/teardown failure: ${file}: ${task.name}`)
      }
      if (result.retryCount || result.repeatCount || task.repeats || (typeof task.retry === 'object' ? task.retry?.count : task.retry)) {
        this.infra.push('Retried/repeated task')
      }
      if (task.type === 'test' && result.state === 'fail' && (result.frozenNonCallError || !result.frozenCallError)) {
        this.infra.push(`Failure outside test callback: ${file}: ${task.name}`)
      }
      if (task.type !== 'test') {
        if (result.errors?.length) this.infra.push(`Collection/suite error: ${file}: ${task.name}`)
        for (const child of task.tasks || []) {
          visit(child, file, task.filepath ? [] : [...names, task.name], ['skip', 'todo'].includes(result.state) ? result.state : undefined, project)
        }
        return
      }
      const categories = [...new Set((result.errors || []).map(error => error.name || error.nameStr || ''))].sort()
      if (result.state === 'fail' && (!categories.length || categories.includes(''))) this.infra.push('Missing error category')
      nodes.push({ node: JSON.stringify([project, file, ...names, task.name]), state: result.state, category: categories.join(',') })
    }
    const files = modules.map(module => JSON.stringify([module.project?.name || '', path.relative(this.root, module.moduleId)])).sort()
    for (const module of modules) visit(module.task, path.relative(this.root, module.moduleId), [], undefined, module.project?.name || '')
    if (!files.length || JSON.stringify(files) !== JSON.stringify(this.planned)) this.infra.push('Missing selected module')
    if (errors.length || !['passed', 'failed'].includes(reason)) this.infra.push('Unhandled error/interrupted run')
    if ((reason === 'failed') !== nodes.some(node => node.state === 'fail')) this.infra.push('Unexplained runner result')
    const nodeIds = nodes.map(node => node.node)
    if (new Set(nodeIds).size !== nodeIds.length) this.infra.push('Duplicate test node')
    writeFileSync(process.env.FROZEN_JS_REPORT, JSON.stringify({ schema: 1, files, nodes, infra: this.infra }))
  }
}

// Only a pure Vitest npm script (optionally its existing --project) is observable.
// All other commands retain their original exit code. Returning zero here lets the *original* && chain finish
// lint/build; collect-js restores the test-failure exit after all checks finish.
function npmProxy(args) {
  let cwd = process.cwd()
  const words = []
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--prefix') cwd = path.resolve(cwd, args[++i])
    else words.push(args[i])
  }
  let script
  if (['run', 'run-script'].includes(words[0]) && words.length === 2) {
    const pkg = JSON.parse(readFileSync(path.join(cwd, 'package.json'), 'utf8'))
    script = !pkg.scripts?.[`pre${words[1]}`] && !pkg.scripts?.[`post${words[1]}`] ? pkg.scripts?.[words[1]] : undefined
  }
  const observed = /^vitest run(?: --project [\w-]+)?$/.test(script || '')
  const report = path.join(process.env.FROZEN_JS_OUTPUT, `vitest-${randomUUID()}.json`)
  const command = observed ? [...args, '--', '--reporter=default', `--reporter=${fileURLToPath(import.meta.url)}`] : args
  const result = spawnSync(process.env.FROZEN_REAL_NPM, command, {
    stdio: 'inherit', env: { ...process.env, FROZEN_JS_REPORT: report },
  })
  let code = result.status ?? 1
  if (observed) {
    try {
      const evidence = JSON.parse(readFileSync(report, 'utf8'))
      if (code === 1 && evidence.schema === 1 && !evidence.infra.length && evidence.nodes.some(n => n.state === 'fail')) code = 0
    } catch {
      code = code || 1
    }
  }
  return code
}

// Vitest's public result API folds fixture and callback errors together. Add
// observation only to the exact locked runner bytes; unknown versions fail shut.
// Installed dependencies are disposable; candidate source is never modified.
export function instrumentRunner(source) {
  const hash = createHash('sha256').update(source).digest('hex')
  if (hash !== 'bf6af6d2f4469911879a67e83b8f9e7b20332cf42795032c53457b10ccc1c617') {
    // Leave unknown runners untouched: a full-green run can still qualify,
    // but without callback-phase markers no failure receives node relief.
    return source
  }
  const helpers = `
const frozenCallErrors = new WeakMap();
function frozenRecordCallError(context, error) {
  const result = context.task.result;
  if (!frozenCallErrors.has(result)) frozenCallErrors.set(result, new Set());
  frozenCallErrors.get(result).add(error);
}
async function frozenTestCall(fn, context) {
  context.task.meta.frozenCallActive = true;
  try { return await fn(context); }
  catch (error) { frozenRecordCallError(context, error); throw error; }
  finally { context.task.meta.frozenCallActive = false; }
}
`
  return helpers + source
    // Suite hooks also supply options.context, but their file/worker context has no test task.
    .replaceAll('return fn(context);', 'return options?.context && !options.suiteHook ? frozenTestCall(fn, context) : fn(context);')
    .replace('(_, error) => abortIfTimeout([context], error)',
      '(_, error) => { if (context.task.meta.frozenCallActive) frozenRecordCallError(context, error); abortIfTimeout([context], error); }')
    .replace('failTask(test.result, e, runner.config.diffOptions);\n\t\t\t\t}\n\t\t\t\ttry {\n\t\t\t\t\tawait runner.onTaskFinished',
      'failTask(test.result, e, runner.config.diffOptions, true);\n\t\t\t\t}\n\t\t\t\ttry {\n\t\t\t\t\tawait runner.onTaskFinished')
    .replace('function failTask(result, err, diffOptions) {', `function failTask(result, err, diffOptions, frozenCallPhase = false) {
      if (frozenCallPhase && frozenCallErrors.get(result)?.has(err)) result.frozenCallError = true;
      else result.frozenNonCallError = true;`)
}

// A synchronous Node loader observes worker-local callback phases in memory.
// No candidate source, installed dependency, config, filter or deadline is edited.
if (process.env.FROZEN_JS_OBSERVE === '1' && !globalThis[Symbol.for('frozen.js.observer')]) {
  globalThis[Symbol.for('frozen.js.observer')] = true
  registerHooks({
    load(url, context, nextLoad) {
      const loaded = nextLoad(url, context)
      if (/\/@vitest\/runner\/dist\/chunk-[^/]+\.js$/.test(url)) {
        const source = Buffer.isBuffer(loaded.source) || loaded.source instanceof Uint8Array ?
          Buffer.from(loaded.source).toString('utf8') : loaded.source
        if (source?.includes('function withFixtures(fn, options) {')) {
          return { ...loaded, source: instrumentRunner(source) }
        }
      }
      return loaded
    },
  })
}

if (process.argv[2] === 'npm-proxy') process.exitCode = npmProxy(process.argv.slice(3))
