"""Isolated real Chromium DOM fixture; no user profile or merchant traffic.

The optional companion-backed suite owns the Camofox document capability. This
fixture exercises actual cross-origin DOMs using a document-bound test adapter.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

CARD = {"number": "4111111111111111", "cvc": "9876"}
NATIVE = '<select aria-label="Expiry month"><option>Month</option><option value="6">July</option></select><select aria-label="Expiry year"><option>Year</option><option value="29">2029</option></select>'
ARIA = """<button type=button role=combobox aria-label="Expiry month" aria-controls=months>Month</button>
<div role=listbox id=months hidden><div role=option>July</div></div>
<button type=button role=combobox aria-label="Expiry year" aria-controls=years>Year</button>
<div role=listbox id=years hidden><div role=option>2029</div></div>
<script>for(const combo of document.querySelectorAll('[role=combobox]')) {
 const list=document.getElementById(combo.getAttribute('aria-controls'));
 combo.onclick=()=>list.hidden=false;
 for(const option of list.children) option.onclick=()=>{combo.textContent=option.textContent;list.hidden=true;combo.dispatchEvent(new Event('change',{bubbles:true}));};
}</script>"""
NUMBER = '<input autocomplete=cc-number>'
CVC = '<div><span>CVC</span><input type=password placeholder="###"></div>'


def html(kind):
    fields = NUMBER + NATIVE + CVC
    if kind == 'aria':
        fields = NUMBER + CVC + ARIA
    if kind == 'shadow':
        return '<div id=host></div><script>document.getElementById("host").attachShadow({mode:"open"}).innerHTML=' + json.dumps(fields) + '</script>'
    if kind == 'duplicate':
        return NUMBER + NATIVE + '<div><span>CVC</span><input placeholder="###"><input placeholder="###"></div>'
    if kind == 'partial':
        return NUMBER + '<input autocomplete=postal-code>'
    if kind == 'washington':
        return (Path(__file__).parents[1] / 'fixtures/browser_vault/washington_checkout.html').read_text(encoding='utf-8')
    return fields + '<section><span>Promo code</span><input id=promo placeholder="###"></section>'


@pytest.fixture(scope='module')
def bridge():
    state = {'kind': 'native'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            markup = ('<iframe src="' + state['processorOrigin'] + '"></iframe>'
                      if self.server is merchant and state['kind'] == 'frame' else html(state['kind']))
            self.wfile.write(markup.encode())

        def log_message(self, format, *args):
            pass

    processor = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    merchant = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    state['processorOrigin'] = f'http://127.0.0.1:{processor.server_port}'
    origin = f'http://127.0.0.1:{merchant.server_port}'
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (processor, merchant)]
    for thread in threads:
        thread.start()
    try:
        with sync_playwright() as pw, pw.chromium.launch(headless=True, args=['--use-mock-keychain']) as browser:
            page = browser.new_page()
            page.set_default_timeout(5000)
            binding = {}

            def call(op, **kwargs):
                try:
                    if op == 'load':
                        state['kind'] = kwargs['kind']
                        page.goto(origin)
                        return {'ok': True}
                    if op == 'eval':
                        return {'ok': True, 'result': page.evaluate(kwargs['expression'])}
                    if op == 'discover':
                        frame = page.frames[1]
                        binding['document'] = frame.evaluate_handle('document')
                        binding['frame'] = frame
                        return {'ok': True, 'result': [{'frame_id': 'fixture-document'}]}
                    if op == 'frame':
                        frame = binding['frame']
                        if kwargs['frameId'] != 'fixture-document' or not frame.evaluate('(doc) => doc === document', binding['document']):
                            return {'ok': False}
                        return {'ok': True, 'result': frame.evaluate(kwargs['expression'])}
                    if op == 'navigate-frame':
                        page.frames[1].goto(state['processorOrigin'] + '/changed')
                        return {'ok': True}
                    if op == 'events':
                        page.evaluate("document.addEventListener('change',()=>document.documentElement.dataset.events=String(Number(document.documentElement.dataset.events||0)+1))")
                        return {'ok': True}
                    if op == 'check':
                        target = page.frames[1] if state['kind'] == 'frame' else page
                        result = target.evaluate("""card=>{
                          const root=document.getElementById('host')?.shadowRoot||document;
                          const inputs=Array.from(root.querySelectorAll('input'));
                          const selects=Array.from(root.querySelectorAll('select'));
                          const combos=Array.from(root.querySelectorAll('[role=combobox]'));
                          return {pan:inputs.some(x=>x.value===card.number),cvc:inputs.some(x=>x.value===card.cvc),
                            expiry:selects.length?selects[0].value==='6'&&selects[1].value==='29':combos[0]?.textContent==='July'&&combos[1]?.textContent==='2029',
                            promoEmpty:!root.getElementById('promo')?.value,
                            events:Number(document.documentElement.dataset.events||0)};
                        }""", CARD)
                        return {'ok': True, 'result': result}
                    raise ValueError('Unknown fixture operation')
                except Exception:
                    return {'ok': False, 'error': 'fixture operation refused'}

            yield {'origin': origin, 'processorOrigin': state['processorOrigin']}, call
    finally:
        for server in (merchant, processor):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
