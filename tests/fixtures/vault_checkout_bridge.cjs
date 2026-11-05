// Opt-in native-browser fixture. No user profiles, remote sites, real cards,
// screenshots, tracing, or raw evaluation errors. stdin/stdout is private IPC.
const readline = require('node:readline');
const http = require('node:http');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const {firefox} = require(path.join(process.env.VAULT_BROWSER_FIXTURE_MODULES, 'playwright-core'));
const {launchOptions} = require(path.join(process.env.VAULT_BROWSER_FIXTURE_MODULES, 'camoufox-js'));
const CARD = {number: '4111111111111111', cvc: '9876'};
const native = `<select aria-label="Expiry month"><option>Month</option><option value="6">July</option></select>
<select aria-label="Expiry year"><option>Year</option><option value="29">2029</option></select>`;
const aria = `<div><span id="month-name">Expiry month</span>
<button type="button" role="combobox" aria-labelledby="month-name" aria-controls="months" onclick="document.getElementById('months').hidden=false">Month</button>
<div role="listbox" id="months" hidden><div role="option" onclick="this.setAttribute('aria-selected','true');document.querySelector('[aria-controls=months]').textContent='July';this.parentElement.hidden=true;document.querySelector('[aria-controls=months]').dispatchEvent(new Event('change',{bubbles:true}))">July</div></div></div>
<div><button type="button" role="combobox" aria-label="Expiry year" aria-controls="years" onclick="document.getElementById('years').hidden=false">Year</button>
<div role="listbox" id="years" hidden><div role="option" onclick="this.setAttribute('aria-selected','true');document.querySelector('[aria-controls=years]').textContent='2029';this.parentElement.hidden=true;document.querySelector('[aria-controls=years]').dispatchEvent(new Event('change',{bubbles:true}))">2029</div></div></div>`;
const number = '<input autocomplete="cc-number">';
const cvc = '<div><span>CVC</span><input type="password" placeholder="###"></div>';
function html(kind) {
  const fields = number + (kind === 'aria' ? aria : native) + cvc;
  // This fixture-only innerHTML contains static synthetic markup, never external/user input.
  if (kind === 'shadow') return `<div id="host"></div><script>document.getElementById('host').attachShadow({mode:'open'}).innerHTML=${JSON.stringify(fields)}</script>`;
  if (kind === 'duplicate') return number + native + '<div><span>CVC</span><input placeholder="###"><input placeholder="###"></div>';
  if (kind === 'partial') return number + '<input autocomplete="postal-code">';
  return fields + '<section><span>Promo code</span><input id="promo" placeholder="###"></section>';
}
(async () => {
  const processor = http.createServer((req,res) => {res.setHeader('content-type','text/html');res.end(html('native'));});
  await new Promise(resolve => processor.listen(0,'127.0.0.1', resolve));
  const processorOrigin = `http://127.0.0.1:${processor.address().port}`;
  let kind = 'native';
  const merchant = http.createServer((req,res) => {res.setHeader('content-type','text/html');res.end(kind === 'frame' ? `<iframe src="${processorOrigin}"></iframe>` : html(kind));});
  await new Promise(resolve => merchant.listen(0,'127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${merchant.address().port}`;
  const browser = await firefox.launch(await launchOptions({headless:true, geoip:false, exclude_addons:['UBO']}));
  const context = await browser.newContext();
  const page = await context.newPage();
  const {trackContextTracing} = await import(pathToFileURL(path.join(process.env.VAULT_BROWSER_FIXTURE_SOURCE,'lib/context-tracing.js')));
  trackContextTracing(context);
  const {createPaymentFrames} = await import(pathToFileURL(path.join(process.env.VAULT_BROWSER_FIXTURE_SOURCE,'lib/payment-frames.js')));
  const frames = createPaymentFrames({allowedOrigins:[processorOrigin]});
  process.stdout.write(JSON.stringify({ready:true,origin,processorOrigin})+'\n');
  const lines = readline.createInterface({input:process.stdin});
  try {
    for await (const line of lines) {
      try {
        const msg = JSON.parse(line); let result;
        if (msg.op === 'load') { kind=msg.kind; await page.goto(origin); result=true; }
        if (msg.op === 'eval') result=await page.evaluate(msg.expression);
        if (msg.op === 'discover') result=await frames.discover(page,origin);
        if (msg.op === 'frame') result=await frames.evaluate(page,{frameId:msg.frameId,expression:msg.expression,merchantOrigin:origin,frameOrigin:processorOrigin});
        if (msg.op === 'navigate-frame') {await page.frames()[1].goto(processorOrigin+'/changed');result=true;}
        if (msg.op === 'check') {
          const target=kind==='frame'?page.frames()[1]:page;
          result=await target.evaluate(card=>{
            const root=document.getElementById('host')?.shadowRoot||document;
            const inputs=Array.from(root.querySelectorAll('input'));
            const selects=Array.from(root.querySelectorAll('select'));
            const combos=Array.from(root.querySelectorAll('[role=combobox]'));
            return {pan:inputs.some(x=>x.value===card.number),cvc:inputs.some(x=>x.value===card.cvc),
              expiry: selects.length ? selects[0].value==='6'&&selects[1].value==='29' : combos[0]?.textContent==='July'&&combos[1]?.textContent==='2029',
              promoEmpty:!root.getElementById('promo')?.value,
              events:Number(document.documentElement.dataset.events||0)};
          },CARD);
        }
        if(msg.op === 'events') {await page.evaluate(()=>document.addEventListener('change',()=>document.documentElement.dataset.events=String(Number(document.documentElement.dataset.events||0)+1)));result=true;}
        process.stdout.write(JSON.stringify({ok:true,result})+'\n');
      } catch {process.stdout.write(JSON.stringify({ok:false,error:'fixture operation refused'})+'\n');}
    }
  } finally {await browser.close();merchant.closeAllConnections();processor.closeAllConnections();merchant.close();processor.close();}
})().catch(()=>{process.stderr.write('fixture initialization failed\n');process.exitCode=1;});
