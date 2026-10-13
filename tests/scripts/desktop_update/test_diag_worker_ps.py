"""Scratch-only: measure PowerShell primitives inside a real pytest worker (diag branch only)."""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]
PS = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
OUT = Path(r'D:\a\_temp\diag-worker.jsonl')
RUNNER_ENV = Path(r'D:\a\_temp\runner-env.json')


def emit(**row):
    line = json.dumps(row)
    print(line, flush=True)
    with OUT.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def timed(label, argv, env, cwd=None, n=2):
    secs = []
    for i in range(n):
        start = time.monotonic()
        p = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL)
        s = round(time.monotonic() - start, 3)
        secs.append(s)
        emit(label=label, i=i, seconds=s, code=p.returncode, out=p.stdout[-600:], err=p.stderr[-400:])
    return min(secs)


PRIMS = {
    'exit0': 'exit 0',
    'getdate': 'Get-Date | Out-Null',
    'cim': "Get-CimInstance Win32_Process -Filter ('ProcessId='+$PID) | Out-Null",
    'getprocess': 'Get-Process -Id $PID | Out-Null',
    'addcontent': "Add-Content -LiteralPath (Join-Path $env:TEMP 'diag-ac.txt') -Value x -Encoding UTF8",
    'newitem': "New-Item -ItemType Directory -Path (Join-Path $env:TEMP 'diag-dir') -Force | Out-Null",
    'addtype': "Add-Type -TypeDefinition ('public class P' + [guid]::NewGuid().ToString('N') + ' { public static int V = 1; }')",
    'startproc': "$p = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList '-NoProfile -Command exit 0' -WindowStyle Hidden -PassThru; $p.WaitForExit()",
    'runspace': '$r = [powershell]::Create(); [void]$r.AddScript("Get-Date").Invoke(); $r.Dispose()',
}


def prim(name, env):
    cmd = "$w=[Diagnostics.Stopwatch]::StartNew(); " + PRIMS[name] + "; [Console]::WriteLine('inner=' + $w.Elapsed.TotalSeconds)"
    return timed('prim:' + name, [PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', cmd], env)


def marker_op(env, home):
    (home / 'hermes-agent').mkdir(parents=True, exist_ok=True)
    env = {**env, 'HERMES_HOME': str(home), 'HERMES_RUNTIME_DIR': str(home / 'empty-store')}
    return timed('markerop', [PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                              str(ROOT / 'scripts/desktop-update/windows.ps1'), '-InstallRoot',
                              str(home / 'hermes-agent'), '-MarkerOp', 'reclaim'], env, cwd=home)


@pytest.mark.platforms('windows')
def test_worker_powershell_primitives(tmp_path):
    worker = dict(os.environ)
    emit(label='worker-env', keys=sorted(worker), PSModulePath=repr(worker.get('PSModulePath', '<ABSENT>')),
         psmp_any=[k for k in worker if k.upper() == 'PSMODULEPATH'])
    runner = json.loads(RUNNER_ENV.read_text(encoding='utf-8')) if RUNNER_ENV.exists() else None
    emit(label='runner-env', keys=sorted(runner or {}),
         PSModulePath=repr((runner or {}).get('PSModulePath', '<ABSENT>')))
    envs = {'worker': worker}
    if runner:
        envs['runner'] = runner
    results = {}
    for ename, env in envs.items():
        emit(label='phase', env=ename)
        for name in PRIMS:
            results[(ename, name)] = prim(name, env)
        results[(ename, 'markerop')] = marker_op(env, tmp_path / ('h-' + ename))
    emit(label='summary', table={f'{e}:{n}': v for (e, n), v in results.items()})
    if not runner:
        return
    # Bisect over runner-only keys for every primitive that is >=3 s slower in the worker.
    upper = {k.upper() for k in worker}
    missing = sorted(k for k in runner if k.upper() not in upper)
    emit(label='missing', keys=missing)
    for name in list(PRIMS) + ['markerop']:
        slow, fast = results[('worker', name)], results[('runner', name)]
        if slow - fast < 3:
            continue

        def run(keys, name=name):
            env = {**worker, **{k: runner[k] for k in keys}}
            if name == 'markerop':
                return marker_op(env, tmp_path / ('b-' + str(time.monotonic_ns())))
            return prim(name, env)

        cand = list(missing)
        if run(cand) - fast >= 3:
            emit(label='bisect', prim=name, verdict='not explained by missing keys (value differences?)')
            # value differences: swap worker values for runner values key by key group
            diff = sorted(k for k in worker if k in runner and worker[k] != runner[k])
            emit(label='value-diff', keys=diff)
            continue
        while len(cand) > 1:
            half = cand[:len(cand) // 2]
            cand = half if run(half) - fast < 3 else cand[len(cand) // 2:]
        emit(label='bisect', prim=name, culprit=cand, value=runner[cand[0]][:400])
