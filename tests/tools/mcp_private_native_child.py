"""Owned disposable MCP JSON-lines child: stdlib only, no user data or network."""
import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:continue
    method = message['method']
    if method == 'initialize':
        result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'resources': {}},
                  'serverInfo': {'name': 'owned-synthetic-child', 'version': '1'}}
    elif method == 'resources/read':
        uri = message['params']['uri']
        print(uri, file=sys.stderr, flush=True)
        if uri.endswith('/failure'):sys.exit(12)
        result = {'contents': [{'uri': uri, 'text': json.dumps({'pid': os.getpid(), 'uri': uri})}]}
    else:result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
