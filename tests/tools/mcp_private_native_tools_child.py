"""Disposable stdlib MCP tools child; effects contain counts only, never private arguments."""
import json
import os
import sys
from pathlib import Path

for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:continue
    method = message['method']
    if method == 'initialize':
        if '--startup-fail' in sys.argv:sys.exit(4)
        result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'owned-tools-child', 'version': '1'}}
    elif method == 'tools/list':result = {'tools': [{'name': 'write', 'inputSchema': {'type': 'object'}}]}
    elif method == 'tools/call':
        args = message['params']['arguments']
        effect = Path(sys.argv[1])
        with effect.open('a') as stream:stream.write(json.dumps({'pid': os.getpid(), 'effect': 1}) + '\n')
        mode = args.get('mode', 'success')
        if mode == 'exit':sys.exit(9)
        if mode == 'pipe-close':
            os.close(1)
            # Keep the child alive until teardown closes stdin: SDK EOF must win, not the PID watcher.
            for _ in sys.stdin:pass
            sys.exit(0)
        result = {'content': [{'type': 'text', 'text': args.get('marker', 'normal-control')}], 'isError': mode == 'error'}
    else:result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
