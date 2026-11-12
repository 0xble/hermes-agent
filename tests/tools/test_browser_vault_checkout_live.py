"""Required synthetic Chromium checkout matrix, including real cross-origin DOMs."""
import json
from unittest.mock import patch

import pytest

from agent.vault_login_classifier import LoginControl, build_inspection_js, classify_checkout_control
from tools import browser_vault_payment as payment
from tests.tools import vault_checkout_browser_fixture

bridge = vault_checkout_browser_fixture.bridge

CARD = {"card_number": "4111111111111111", "exp_month": "07", "exp_year": "2029", "cvc": "9876"}


def inspected(call, origin, frame_id=None):
    response = call("frame" if frame_id else "eval", expression=build_inspection_js("synthetic"), **({"frameId": frame_id} if frame_id else {}))
    assert response["ok"]
    raw = json.loads(response["result"])
    # Expiry options legitimately expose public month/year choices. Only the
    # sensitive PAN and security code must be absent from inspection metadata.
    assert all(CARD[key] not in json.dumps(raw) for key in ('card_number', 'cvc'))
    controls = [LoginControl.from_dict(c) for c in raw]
    return {"origin": origin, "nonce": "synthetic", "controls": controls,
            "classified": [c for control in controls if (c := classify_checkout_control(control))],
            **({"frame_id": frame_id} if frame_id else {})}


@pytest.mark.parametrize("kind", ["native", "aria", "shadow", "partial", "duplicate", "frame"])
def test_isolated_checkout_matrix(bridge, kind):
    ready, call = bridge
    assert call("load", kind=kind)["ok"]
    call("events")
    if kind == "frame":
        discovered = call("discover")["result"]
        assert len(discovered) == 1
        contexts = [inspected(call, ready["processorOrigin"], discovered[0]["frame_id"])]
    else:
        contexts = [inspected(call, ready["origin"])]
    selected, ambiguous = payment.plan(contexts, CARD)
    def evaluate(task, expression):
        response = call("eval", expression=expression)
        return {"success": response["ok"], "result": response.get("result")}
    def frame_evaluate(task, frame_id, expression, *args, **kwargs):
        response = call("frame", frameId=frame_id, expression=expression)
        return {"success": response["ok"], "result": response.get("result")}
    with patch.object(payment, "evaluate_payment_frame", side_effect=frame_evaluate):
        result = payment.fill("fixture", ready["origin"], selected, ambiguous, evaluate)
    assert all(value not in json.dumps(result) for value in CARD.values())
    check = call("check")["result"]
    if kind in {"partial", "duplicate"}:
        assert not result["success"] and result["partial"]
        assert check["cvc"] is False
        if kind == "duplicate":
            assert check["pan"] is False
            assert result["field_results"]["cc-csc"] == "ambiguous"
    else:
        assert result["success"] and result["partial"] is False, result
        assert check["pan"] and check["cvc"] and check["expiry"] and check["promoEmpty"]
        if kind in {"native", "aria"}:
            assert check["events"] >= 2


def test_real_frame_navigation_revokes_document(bridge):
    ready, call = bridge
    call("load", kind="frame")
    frame_id = call("discover")["result"][0]["frame_id"]
    context = inspected(call, ready["processorOrigin"], frame_id)
    call("navigate-frame")
    selected, ambiguous = payment.plan([context], CARD)
    def evaluate_frame(task, frame_id, expression, *a, **kw):
        response = call("frame", frameId=frame_id, expression=expression)
        return {"success": response["ok"], "result": response.get("result")}
    with patch.object(payment, "evaluate_payment_frame", side_effect=evaluate_frame):
        result = payment.fill("fixture", ready["origin"], selected, ambiguous, lambda *a: pytest.fail("page fallback"))
    assert not result["success"] and result["partial"] and result["filled_fields"] == 0
    check = call("check")["result"]
    assert not check["pan"] and not check["cvc"]


def fill_html(call, origin, html, card=None):
    assert call("eval", expression="document.body.innerHTML=" + json.dumps(html))["ok"]
    ctx = inspected(call, origin)
    selected, ambiguous = payment.plan([ctx], card or CARD)
    def evaluate(task, expression):
        response = call("eval", expression=expression)
        return {"success": response["ok"], "result": response.get("result")}
    return payment.fill("fixture", origin, selected, ambiguous, evaluate)


@pytest.mark.parametrize("target", ["trigger", "option"])
@pytest.mark.parametrize("element", ['button', 'button type=submit', 'button type=reset',
                                    'input type=submit', 'input type=reset', 'input type=image'])
def test_expiry_never_clicks_submit_reset_or_default_buttons(bridge, target, element):
    ready, call = bridge
    call("load", kind="native")
    def tag(attributes, text):
        return '<' + element + ' ' + attributes + (' value=' + text if element.startswith('input') else '') + '>' + ('' if element.startswith('input') else text + '</button>')
    trigger = tag('role=combobox aria-label="Expiry month" aria-controls=months', 'Month') if target == 'trigger' else '<button type=button role=combobox aria-label="Expiry month" aria-controls=months>Month</button>'
    option = tag('role=option', 'July') if target == 'option' else '<div role=option>July</div>'
    # Attach the counter to the unsafe target, never the safe trigger that
    # legitimately opens an unsafe option's listbox.
    trigger_tag = '<' + element + ' '
    if target == 'trigger':
        trigger = trigger.replace(trigger_tag, trigger_tag + 'onclick="window.unsafeClicks++" ', 1)
    else:
        option = option.replace(trigger_tag, trigger_tag + 'onclick="window.unsafeClicks++" ', 1)
    html = '<form onsubmit="event.preventDefault();window.submissions++" onreset="window.resets++"><input autocomplete=cc-number>' + trigger + '<div id=months role=listbox>' + option + '</div></form>'
    assert call("eval", expression="window.submissions=0;window.resets=0;window.unsafeClicks=0")["ok"]
    result = fill_html(call, ready['origin'], html)
    assert result['partial'] and 'cc-exp-month' in result['missing_fields']
    state = call('eval', expression='[window.submissions,window.resets,window.unsafeClicks]')['result']
    assert state == [0, 0, 0]


@pytest.mark.parametrize("reset_kind", ["sync", "timer"])
def test_later_cvc_event_clears_pan_and_final_receipt_is_partial(bridge, reset_kind):
    ready, call = bridge
    call("load", kind="native")
    reset = "document.querySelector('[autocomplete=cc-number]').value=''"
    if reset_kind == 'timer':
        reset = 'setTimeout(()=>{' + reset + '},10)'
    html = '<input autocomplete=cc-number><input autocomplete=cc-exp><input aria-label=CVC onchange="' + reset + '">'
    result = fill_html(call, ready['origin'], html)
    assert not result['success'] and result['partial']
    assert result['field_results']['cc-number'] == 'missing'
    assert result['field_results']['cc-csc'] == 'filled'
    assert all(value not in json.dumps(result) for value in CARD.values())


def test_later_frame_event_clears_merchant_pan_after_its_write(bridge):
    ready, call = bridge
    call("load", kind="frame")
    frame_id = call("discover")["result"][0]["frame_id"]
    call('eval', expression="document.body.insertAdjacentHTML('afterbegin','<input autocomplete=cc-number>')")
    call('frame', frameId=frame_id, expression="document.body.innerHTML='<input autocomplete=cc-exp><input aria-label=CVC>';document.querySelector('[aria-label=CVC]').onchange=()=>parent.postMessage('clear-card','*')")
    call('eval', expression="window.addEventListener('message',e=>{if(e.origin===" + json.dumps(ready['processorOrigin']) + ")document.querySelector('[autocomplete=cc-number]').value=''})")
    contexts = [inspected(call, ready['origin']), inspected(call, ready['processorOrigin'], frame_id)]
    selected, ambiguous = payment.plan(contexts, CARD)
    def evaluate(task, expression):
        response=call('eval', expression=expression)
        return {'success': response['ok'], 'result': response.get('result')}
    def frame_evaluate(task, frame_id, expression, *a, **kw):
        response=call('frame', frameId=frame_id, expression=expression)
        return {'success': response['ok'], 'result': response.get('result')}
    with patch.object(payment, 'evaluate_payment_frame', side_effect=frame_evaluate):
        result=payment.fill('fixture', ready['origin'], selected, ambiguous, evaluate)
    assert not result['success'] and result['field_results']['cc-number'] == 'missing'
    assert result['field_results']['cc-csc'] == 'filled'


@pytest.mark.parametrize('fixture', ['northwest', 'washington'])
def test_merchant_shaped_brand_reset_and_address_fixture(bridge, fixture):
    from pathlib import Path
    ready, call=bridge
    call('load', kind='native')
    html=(Path(__file__).parents[1] / 'fixtures/browser_vault' / f'{fixture}_checkout.html').read_text(encoding='utf-8')
    # innerHTML does not execute scripts, so install fixture script explicitly.
    markup, script = html.split('<script>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(markup))
    call('eval', expression=script.split('</script>')[0])
    ctx=inspected(call, ready['origin'])
    card={**CARD, 'card_number':'5555555555554444', 'billing_postal_code':'98000'}
    selected, ambiguous=payment.plan([ctx],card)
    assert {'cc-exp-month', 'cc-exp-year'} <= {c.token for c in ctx['classified']}
    if fixture == 'northwest':
        assert [c.control.label.strip() for c in ctx['classified']
                if c.token in {'cc-exp-month', 'cc-exp-year'}] == ['Expiration month', 'Expiration year']
    def evaluate(task, expression):
        response=call('eval',expression=expression)
        return {'success':response['ok'],'result':response.get('result')}
    result=payment.fill('fixture',ready['origin'],selected,ambiguous,evaluate)
    assert result['success'], result
    checks=call('eval',expression="""(() => {
      const get=n=>document.querySelector('[data-test='+n+']');
      return {streetEmpty:get('street').value==='',postal:get('postal').value==='98000',
        pan:get('number').value==='5555555555554444', cvc:get('cvc').value==='9876',
        expiry: get('month').tagName==='SELECT'?get('month').value==='6'&&get('year').value==='29':get('month').textContent==='07'&&get('year').textContent==='2029',
        brand:!get('brand')||get('brand').value==='mc',stamps:document.querySelectorAll('[data-hermes-vault-slot]').length};
    })()""")["result"]
    assert checks=={'streetEmpty':True,'postal':True,'pan':True,'cvc':True,'expiry':True,'brand':True,'stamps':0}


@pytest.mark.parametrize('metadata', ['name="CVVNumber"','id="cardCVC"','aria-label="CSC"','placeholder="CVV"'])
def test_text_expiry_and_authentication_exclusions(bridge, metadata):
    ready,call=bridge
    call('load',kind='native')
    html=f'<input autocomplete=cc-number><input {metadata}><input name=ExpirationDate placeholder="MM / YYYY" maxlength=9><input autocomplete=one-time-code aria-label="Security code"><input type=password autocomplete=current-password aria-label=CVC><input name=issueDate placeholder="Date">'
    result=fill_html(call,ready['origin'],html)
    assert result['success'], result
    checks=call('eval',expression="[document.querySelector('[name=ExpirationDate]').value==='07 / 2029',document.querySelector('[autocomplete=one-time-code]').value==='',document.querySelector('[autocomplete=current-password]').value==='',document.querySelector('[name=issueDate]').value==='']")['result']
    assert checks==[True]*4



@pytest.mark.parametrize('button_type',['','type=submit','type=reset'])
def test_option_nested_inside_unsafe_native_button_is_not_clicked(bridge, button_type):
    ready, call = bridge
    call('load',kind='native')
    call('eval',expression='window.unsafeClicks=0;window.submissions=0;window.resets=0')
    html='<form onsubmit="event.preventDefault();window.submissions++" onreset="window.resets++"><input autocomplete=cc-number><button type=button role=combobox aria-label="Expiry month" aria-controls=months>Month</button><div id=months role=listbox><button '+button_type+' onclick="window.unsafeClicks++"><span role=option>July</span></button></div></form>'
    result=fill_html(call,ready['origin'],html)
    assert result['partial'] and result['field_results']['cc-exp-month']=='missing'
    assert call('eval',expression='[window.unsafeClicks,window.submissions,window.resets]')['result']==[0,0,0]


def test_async_secret_rejection_contained_before_transport_logging(bridge, monkeypatch):
    from tools import browser_camofox as camofox
    from tools import browser_vault_tool as vault
    _ready,call=bridge
    call('load',kind='native')
    monkeypatch.setattr(camofox,'is_camofox_mode',lambda:True)
    monkeypatch.setattr(camofox,'get_camofox_url',lambda:'http://127.0.0.1:9377')
    monkeypatch.setattr(camofox,'_get_session',lambda task:{'tab_id':'fixture','user_id':'fixture'})
    def post(path,body,**kw):
        response=call('eval',expression=body['expression'])
        assert response['ok'], 'Async exception escaped in-page containment'
        return {'result':response.get('result')}
    monkeypatch.setattr(camofox,'_post',post)
    result=vault._eval_js_secret('fixture','Promise.reject(new Error("synthetic-secret"))')
    assert not result['success'] and 'synthetic-secret' not in json.dumps(result)


@pytest.mark.parametrize('mutation', ['none', 'restamp', 'text-replacement'])
def test_login_fill_binds_inspection_and_removes_markers(bridge, mutation):
    from agent.vault_login_classifier import build_fill_js

    ready, call = bridge
    call('load', kind='native')
    call('eval', expression="document.body.innerHTML='<input id=target type=password autocomplete=current-password>'")
    context = inspected(call, ready['origin'])
    index = context['controls'][0].index
    if mutation == 'restamp':
        call('eval', expression=build_inspection_js('new-inspection'))
    elif mutation == 'text-replacement':
        call('eval', expression="document.getElementById('target').type='text'")
    response = call('eval', expression=build_fill_js(
        [{'index': index, 'token': 'current-password', 'value': 'synthetic-login'}],
        expected_origin=ready['origin'], nonce='synthetic',
    ))
    assert response['ok']
    receipt = json.loads(response['result'])
    assert receipt['filled'] == (1 if mutation == 'none' else 0)
    assert 'synthetic-login' not in json.dumps(receipt)
    state = call('eval', expression="[document.getElementById('target').value==='synthetic-login',document.querySelectorAll('[data-hermes-vault-slot],[data-vault-secret],[data-vault-secret-filled]').length]")['result']
    assert state == [mutation == 'none', 0]


def test_native_expiry_controls_use_local_group_labels(bridge):
    ready, call = bridge
    call('load',kind='native')
    html='<input autocomplete=cc-number><fieldset><legend>Expiration</legend><select name=Month><option>Month</option><option value=07>July</option></select><select name=Year><option>Year</option><option value=2029>2029</option></select></fieldset><input aria-label=CVC>'
    result=fill_html(call,ready['origin'],html)
    assert result['success'], result


def direct_fill(call, origin, token, value, **kwargs):
    """Drive the actual shared fill template, keeping receipts value-blind."""
    from agent.vault_login_classifier import build_fill_js

    response = call('eval', expression=build_inspection_js('synthetic'))
    assert response['ok']
    controls = json.loads(response['result'])
    target = next(c for c in controls if c['name'].split()[-1] == 'target')
    response = call('eval', expression=build_fill_js(
        [{'index': target['index'], 'token': token, 'value': value}],
        origin, 'synthetic', **kwargs))
    assert response['ok']
    receipt = json.loads(response['result'])
    assert receipt.get('selectedOptions', []) == []
    return receipt


@pytest.mark.parametrize('mutation', ['none', 'click-restamp', 'timer-restamp', 'option-restamp', 'option-timer-restamp',
                                      'index-drift', 'detach', 'disable', 'association-drift'])
def test_aria_nonce_is_rechecked_at_each_event_and_yield(bridge, mutation):
    ready, call = bridge
    call('load', kind='native')
    restamp = "target.setAttribute('data-hermes-vault-slot','replacement:0');"
    actions = {
        'none': '', 'click-restamp': restamp,
        'timer-restamp': "setTimeout(()=>{" + restamp + "},0);",
        'index-drift': "target.setAttribute('data-hermes-vault-slot','synthetic:999');",
        'detach': 'target.remove();', 'disable': "target.setAttribute('aria-disabled','true');",
        'association-drift': "target.setAttribute('aria-controls','elsewhere');",
        'option-restamp': '', 'option-timer-restamp': '',
    }
    html = ('<button id=target type=button role=combobox aria-label="Expiry month" '
            'aria-controls=months>Month</button><div id=months role=listbox>'
            '<div role=option>July</div></div>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    call('eval', expression="""window.optionClicks=0;window.triggerClicks=0;
        window.target=document.getElementById('target');
        target.onclick=()=>{window.triggerClicks++;ACTION};
        document.querySelector('[role=option]').onclick=function(){
          window.optionClicks++;target.textContent='July';this.setAttribute('aria-selected','true');OPTION
        };void 0;""".replace('ACTION', actions[mutation]).replace('OPTION', restamp if mutation == 'option-restamp' else
          'setTimeout(()=>{' + restamp + '},0);' if mutation == 'option-timer-restamp' else ''))
    receipt = direct_fill(call, ready['origin'], 'cc-exp-month', '07')
    state = call('eval', expression="[window.optionClicks,target.textContent,document.querySelectorAll('[data-hermes-vault-slot]').length]")['result']
    assert state == [1 if mutation in {'none', 'option-restamp', 'option-timer-restamp'} else 0,
                     'July' if mutation in {'none', 'option-restamp', 'option-timer-restamp'} else 'Month', 0]
    assert receipt['filled'] == (1 if mutation == 'none' else 0)
    assert receipt['fieldResults'] == [{'token': 'cc-exp-month', 'status': 'filled' if mutation == 'none' else 'missing'}]


@pytest.mark.parametrize('mode', ['associated', 'restamp', 'wrong-association', 'unassociated', 'ambiguous', 'no-popup'])
def test_delayed_popup_requires_live_nonce_and_unique_association(bridge, mode):
    ready, call = bridge
    call('load', kind='native')
    association = {'unassociated': '', 'ambiguous': 'aria-controls="months other"'}.get(mode, 'aria-controls=months')
    html = '<button id=target type=button role=combobox aria-label="Expiry month" ' + association + '>Month</button>'
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    popup_id = 'other' if mode == 'wrong-association' else 'months'
    call('eval', expression="""window.optionClicks=0;window.popupReady=false;
        const target=document.getElementById('target');target.onclick=()=>{
          RESTAMP
          setTimeout(()=>{
            if(NOPOPUP) return;
            const list=document.createElement('div');list.id=POPUP;list.setAttribute('role','listbox');
            list.innerHTML='<div role=option>July</div>';
            list.firstChild.onclick=()=>{window.optionClicks++;target.textContent='July';};
            document.body.append(list);window.popupReady=true;
          },60);
        };void 0;""".replace('RESTAMP', "setTimeout(()=>target.setAttribute('data-hermes-vault-slot','replacement:0'),40);" if mode == 'restamp' else '')
            .replace('NOPOPUP', 'true' if mode == 'no-popup' else 'false').replace('POPUP', json.dumps(popup_id)))
    receipt = direct_fill(call, ready['origin'], 'cc-exp-month', '07')
    assert receipt['filled'] == (1 if mode == 'associated' else 0)
    state = call('eval', expression="[window.optionClicks,document.getElementById('target').textContent]")['result']
    assert state == ([1, 'July'] if mode == 'associated' else [0, 'Month'])


@pytest.mark.parametrize('kind', ['native', 'aria'])
@pytest.mark.parametrize('labels', [[], [''], ['Diners Club'], ['Mastercard'], ['Visa', 'Visa']])
def test_brand_never_falls_back_to_opaque_provider_values(bridge, kind, labels):
    ready, call = bridge
    call('load', kind='native')
    options = ''.join(f'<option aria-label=Visa value=visa>{label}</option>' if kind == 'native' else
                      f'<div aria-label=Visa role=option data-value=visa>{label}</div>' for label in labels)
    html = ('<select id=target aria-label="Card brand"><option value="">Choose</option>' + options + '</select>'
            if kind == 'native' else
            '<button id=target type=button role=combobox aria-label="Card brand" aria-controls=brands>Choose</button>'
            '<div id=brands role=listbox>' + options + '</div>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    call('eval', expression="""window.optionClicks=0;window.changes=0;
        const target=document.getElementById('target');target.onchange=()=>window.changes++;
        for(const option of document.querySelectorAll('[role=option]')) option.onclick=()=>{
          window.optionClicks++;target.textContent=option.textContent;option.setAttribute('aria-selected','true');
        };void 0;""")
    receipt = direct_fill(call, ready['origin'], 'cc-type', 'visa')
    assert receipt['filled'] == 0 and receipt['fieldResults'] == [{'token': 'cc-type', 'status': 'missing'}]
    state = call('eval', expression="[window.optionClicks,window.changes,document.getElementById('target').selectedIndex??-1,document.getElementById('target').textContent]")['result']
    assert state[:3] == [0, 0, 0 if kind == 'native' else -1]
    if kind == 'aria':
        assert state[3] == 'Choose'


@pytest.mark.parametrize('kind', ['native', 'aria'])
@pytest.mark.parametrize('label,brand', [('Visa', 'visa'), ('Mastercard', 'mastercard'),
                                        ('American Express', 'amex'), ('Discover', 'discover')])
def test_supported_visible_brand_beats_opaque_provider_value(bridge, kind, label, brand):
    ready, call = bridge
    call('load', kind='native')
    html = ('<select id=target aria-label="Card brand"><option value="">Choose</option><option value=opaque>' + label + '</option></select>'
            if kind == 'native' else
            '<button id=target type=button role=combobox aria-label="Card brand" aria-controls=brands>Choose</button>'
            '<div id=brands role=listbox><div role=option data-value=opaque>' + label + '</div></div>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    call('eval', expression="""const option=document.querySelector('[role=option]');if(option) option.onclick=()=>{
      document.getElementById('target').textContent=option.textContent;option.setAttribute('aria-selected','true');};void 0;""")
    receipt = direct_fill(call, ready['origin'], 'cc-type', brand)
    assert receipt['filled'] == 1 and receipt['fieldResults'] == [{'token': 'cc-type', 'status': 'filled'}]
    assert call('eval', expression="document.getElementById('target').selectedOptions?.[0]?.textContent||document.getElementById('target').textContent")['result'] == label


@pytest.mark.parametrize('kind', ['native', 'aria'])
def test_brand_final_readback_rejects_changed_visible_label(bridge, kind):
    ready, call = bridge
    call('load', kind='native')
    html = ('<select id=target aria-label="Card brand"><option value="">Choose</option><option value=visa>Visa</option></select>'
            if kind == 'native' else
            '<button id=target type=button role=combobox aria-label="Card brand" aria-controls=brands>Choose</button>'
            '<div id=brands role=listbox><div role=option data-value=visa>Visa</div></div>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    call('eval', expression="""const target=document.getElementById('target');
      if(target.tagName==='SELECT') target.onchange=()=>target.selectedOptions[0].textContent='Mastercard';
      else document.querySelector('[role=option]').onclick=function(){
        this.textContent='Mastercard';this.setAttribute('aria-selected','true');target.value='visa';target.textContent='Mastercard';
      };void 0;""")
    receipt = direct_fill(call, ready['origin'], 'cc-type', 'visa')
    assert receipt['filled'] == 0 and receipt['fieldResults'] == [{'token': 'cc-type', 'status': 'missing'}]



@pytest.mark.parametrize('association', ['', 'aria-controls="months other"', 'aria-controls=other', 'aria-owns=months'])
@pytest.mark.parametrize('duplicate', [False, True])
def test_expiry_options_are_never_guessed_page_wide_or_ambiguously(bridge, association, duplicate):
    ready, call = bridge
    call('load', kind='native')
    options = '<div role=option>July</div>' * (2 if duplicate else 1)
    html = ('<button id=target type=button role=combobox aria-label="Expiry month" ' + association + '>Month</button>'
            '<div id=months role=listbox>' + options + '</div><div id=other role=listbox><div role=option>June</div></div>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    call('eval', expression="""window.optionClicks=0;
      for(const option of document.querySelectorAll('[role=option]')) option.onclick=()=>{
        window.optionClicks++;document.getElementById('target').textContent=option.textContent;
      };void 0;""")
    receipt = direct_fill(call, ready['origin'], 'cc-exp-month', '07')
    valid = association == 'aria-owns=months' and not duplicate
    assert receipt['filled'] == int(valid)
    assert call('eval', expression="[window.optionClicks,document.getElementById('target').textContent]")['result'] == [int(valid), 'July' if valid else 'Month']


@pytest.mark.parametrize('kind', ['input', 'native', 'aria'])
@pytest.mark.parametrize('mutation', ['nonce', 'index', 'origin'])
def test_fill_refuses_invalid_binding_before_any_mutation(bridge, kind, mutation):
    from agent.vault_login_classifier import build_fill_js

    ready, call = bridge
    call('load', kind='native')
    html = {'input': '<input id=target autocomplete=cc-number>',
            'native': '<select id=target aria-label="Expiry month"><option>Month</option><option>July</option></select>',
            'aria': '<button id=target type=button role=combobox aria-label="Expiry month" aria-controls=months>Month</button><div id=months role=listbox><div role=option>July</div></div>'}[kind]
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    call('eval', expression="window.mutations=0;for(const event of ['focus','input','change','click']) document.addEventListener(event,()=>window.mutations++,true);void 0;")
    raw = json.loads(call('eval', expression=build_inspection_js('synthetic'))['result'])
    token, value = ('cc-number', CARD['card_number']) if kind == 'input' else ('cc-exp-month', '07')
    response = call('eval', expression=build_fill_js(
        [{'index': raw[0]['index'] + (999 if mutation == 'index' else 0), 'token': token, 'value': value}],
        ready['processorOrigin'] if mutation == 'origin' else ready['origin'],
        'replacement' if mutation == 'nonce' else 'synthetic'))
    assert response['ok']
    receipt = json.loads(response['result'])
    assert CARD['card_number'] not in json.dumps(receipt)
    assert receipt.get('filled', 0) == 0
    assert call('eval', expression='window.mutations')['result'] == 0


@pytest.mark.parametrize('kind,event', [('input', 'focus'), ('input', 'input'), ('native', 'input')])
def test_native_events_cannot_reuse_revoked_inspection(bridge, kind, event):
    ready, call = bridge
    call('load', kind='native')
    html = ('<input id=target autocomplete=cc-number>' if kind == 'input' else
            '<select id=target aria-label="Expiry month"><option>Month</option><option value=07>July</option></select>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    call('eval', expression="""window.changes=0;const target=document.getElementById('target');
      target.onchange=()=>window.changes++;
      target.addEventListener(EVENT,()=>target.setAttribute('data-hermes-vault-slot','replacement:0'));void 0;""".replace('EVENT', json.dumps(event)))
    token, value = ('cc-number', CARD['card_number']) if kind == 'input' else ('cc-exp-month', '07')
    receipt = direct_fill(call, ready['origin'], token, value)
    assert receipt['filled'] == 0 and value not in json.dumps(receipt)
    state = call('eval', expression="[window.changes,document.getElementById('target').value==='']")['result']
    assert state == [0, event == 'focus']


@pytest.mark.parametrize('kind', ['native', 'aria'])
def test_verify_only_preserves_stamps_but_rejects_conflicting_brand(bridge, kind):
    from agent.vault_login_classifier import build_fill_js

    ready, call = bridge
    call('load', kind='native')
    html = ('<select id=target aria-label="Card brand"><option value="">Choose</option><option value=visa>Visa</option></select>'
            if kind == 'native' else
            '<button id=target type=button role=combobox aria-label="Card brand" aria-controls=brands>Choose</button>'
            '<div id=brands role=listbox><div role=option>Visa</div></div>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    call('eval', expression="""const option=document.querySelector('[role=option]');if(option) option.onclick=()=>{
      document.getElementById('target').textContent='Visa';option.setAttribute('aria-selected','true');};void 0;""")
    receipt = direct_fill(call, ready['origin'], 'cc-type', 'visa', retain_stamps=True)
    assert receipt['filled'] == 1
    stamp = call('eval', expression="document.getElementById('target').getAttribute('data-hermes-vault-slot')")['result']
    assert stamp == 'synthetic:0'
    call('eval', expression="""const target=document.getElementById('target');
      if(target.tagName==='SELECT') target.selectedOptions[0].textContent='Mastercard';
      else {target.textContent='Mastercard';target.value='visa';target.setAttribute('aria-valuetext','Visa');}void 0;""")
    response = call('eval', expression=build_fill_js([{'index': 0, 'token': 'cc-type', 'value': 'visa'}],
                    ready['origin'], 'synthetic', verify_only=True))
    assert response['ok']
    receipt = json.loads(response['result'])
    assert receipt['filled'] == 0 and receipt['fieldResults'] == [{'token': 'cc-type', 'status': 'missing'}]
    assert call('eval', expression="document.querySelectorAll('[data-hermes-vault-slot]').length")['result'] == 0



@pytest.mark.parametrize('first_label', ['Mastercard', 'Visa'])
def test_native_brand_selects_visible_option_not_shared_provider_value(bridge, first_label):
    ready, call = bridge
    call('load', kind='native')
    second_label = 'Visa' if first_label == 'Mastercard' else 'Mastercard'
    html = ('<select id=target aria-label="Card brand"><option>Choose</option>'
            f'<option value=visa>{first_label}</option><option value=visa>{second_label}</option></select>')
    call('eval', expression='document.body.innerHTML=' + json.dumps(html))
    receipt = direct_fill(call, ready['origin'], 'cc-type', 'visa')
    assert receipt['filled'] == 1
    assert call('eval', expression="document.getElementById('target').selectedOptions[0].textContent")['result'] == 'Visa'


@pytest.mark.parametrize('kind', ['native', 'aria'])
def test_unsupported_brand_yields_partial_value_free_payment_receipt(bridge, kind):
    ready, call = bridge
    call('load', kind='native')
    brand = ('<select id=target aria-label="Card brand"><option>Choose</option><option value=visa>Mastercard</option></select>'
             if kind == 'native' else '<button id=target type=button role=combobox aria-label="Card brand" aria-controls=brands>Choose</button><div id=brands role=listbox><div role=option data-value=visa>Mastercard</div></div>')
    receipt = fill_html(call, ready['origin'], '<input autocomplete=cc-number><input autocomplete=cc-exp><input autocomplete=cc-csc>' + brand)
    assert not receipt['success'] and receipt['partial']
    assert receipt['field_results']['cc-type'] == 'missing'
    assert receipt['missing_fields'] == ['cc-type']
    assert receipt['field_results']['cc-number'] == 'filled'
    assert all(value not in json.dumps(receipt) for value in CARD.values())
    assert call('eval', expression="document.getElementById('target').selectedOptions?.[0]?.textContent||document.getElementById('target').textContent")['result'] == 'Choose'


@pytest.mark.parametrize('target', ['month', 'year'])
@pytest.mark.parametrize('stale', ['value', 'aria-valuetext'])
@pytest.mark.parametrize('reset', ['none', 'clear', 'wrong', 'blank'])
@pytest.mark.parametrize('phase', ['option', 'cvc'])
def test_required_expiry_combobox_visible_state_controls_payment_receipt(bridge, target, stale, reset, phase):
    ready, call = bridge
    assert call('load', kind='native')['ok']
    html = '''<form onsubmit="event.preventDefault();window.submissions++">
      <input autocomplete=cc-number>
      <button id=month type=button role=combobox aria-label="Expiry month" aria-controls=months>Month</button>
      <div id=months role=listbox><div role=option>July</div></div>
      <button id=year type=button role=combobox aria-label="Expiry year" aria-controls=years>Year</button>
      <div id=years role=listbox><div role=option>2029</div></div>
      <input id=cvc aria-label=CVC></form>'''
    assert call('eval', expression='document.body.innerHTML=' + json.dumps(html))['ok']
    setup = '''(() => {
      window.submissions=0;
      const reset=()=>{
        if(RESET==='none') return;
        document.getElementById(TARGET).textContent=RESET==='blank'?'':RESET==='clear'
          ? (TARGET==='month'?'Month':'Year') : (TARGET==='month'?'August':'2030');
      };
      for(const id of ['month','year']) {
        const trigger=document.getElementById(id);
        document.getElementById(trigger.getAttribute('aria-controls')).firstChild.onclick=function(){
          trigger.textContent=this.textContent;
          if(STALE==='value') trigger.value=id==='month'?'07':'2029';
          else trigger.setAttribute('aria-valuetext',id==='month'?'July':'2029');
          this.setAttribute('aria-selected','true');
          if(PHASE==='option' && id===TARGET) reset();
        };
      }
      document.getElementById('cvc').onchange=()=>{if(PHASE==='cvc') reset();};
    })()'''
    for key, value in {'RESET': reset, 'TARGET': target, 'STALE': stale, 'PHASE': phase}.items():
        setup = setup.replace(key, json.dumps(value))
    assert call('eval', expression=setup)['ok']
    context = inspected(call, ready['origin'])
    selected, ambiguous = payment.plan([context], CARD)
    def evaluate(task, expression):
        response = call('eval', expression=expression)
        return {'success': response['ok'], 'result': response.get('result')}
    receipt = payment.fill('fixture', ready['origin'], selected, ambiguous, evaluate)
    assert all(value not in json.dumps(receipt) for value in CARD.values())
    state = call('eval', expression='({month:document.getElementById("month").innerText,year:document.getElementById("year").innerText,submissions:window.submissions,stamps:document.querySelectorAll("[data-hermes-vault-slot],[data-hermes-vault-aria-only]").length})')['result']
    assert state['submissions'] == state['stamps'] == 0
    expected = 'July' if target == 'month' else '2029'
    if reset != 'none':
        expected = '' if reset == 'blank' else ('Month' if target == 'month' else 'Year') if reset == 'clear' else ('August' if target == 'month' else '2030')
    assert state[target] == expected
    assert receipt['success'] is (reset == 'none')
    assert receipt['partial'] is (reset != 'none')
    assert receipt['field_results']['cc-exp-' + target] == ('filled' if reset == 'none' else 'missing')
    assert receipt['field_results']['cc-number'] == receipt['field_results']['cc-csc'] == 'filled'


@pytest.mark.parametrize('kind', ['input', 'button', 'aria-only', 'aria-to-visible'])
@pytest.mark.parametrize('target', ['month', 'year'])
@pytest.mark.parametrize('reset', ['none', 'clear', 'wrong'])
@pytest.mark.parametrize('verify_only', [False, True])
def test_expiry_combobox_readback_uses_authoritative_representation(bridge, kind, target, reset, verify_only):
    from agent.vault_login_classifier import build_fill_js

    ready, call = bridge
    assert call('load', kind='native')['ok']
    label, value = ('July', '07') if target == 'month' else ('2029', '2029')
    placeholder, wrong = ('Month', 'August') if target == 'month' else ('Year', '2030')
    attributes = f'id=target role=combobox aria-label="Expiry {target}" aria-controls=choices'
    trigger = ('<input ' + attributes + '>' if kind == 'input' else
               '<button type=button ' + attributes + '><span hidden>' + label + '</span><span id=display>' + (placeholder if kind == 'button' else '') + '</span></button>')
    html = '<form onsubmit="event.preventDefault();window.submissions++">' + trigger + '<div id=choices role=listbox><div role=option>' + label + '</div></div></form>'
    assert call('eval', expression='document.body.innerHTML=' + json.dumps(html))['ok']
    setup = '''(() => {
      window.submissions=0;window.optionClicks=0;
      const trigger=document.getElementById('target');
      window.setDisplay=text=>{
        if(KIND==='input') trigger.value=text;
        else if(KIND==='button' || KIND==='aria-to-visible') document.getElementById('display').textContent=text;
        else trigger.setAttribute('aria-valuetext',text);
      };
      // A textless trigger that renders a value loses ARIA-only authority once
      // observed; verify-only must not restore it after a later blank reset.
      window.resetDisplay=()=>setDisplay(RESET==='clear'
        ? (KIND==='button' || (KIND==='aria-to-visible' && !VERIFY) ? PLACEHOLDER : '') : WRONG);
      document.querySelector('[role=option]').onclick=function(){
        window.optionClicks++;
        // Stale non-authoritative state must not overrule the displayed value.
        if(KIND==='input') {trigger.textContent=LABEL;trigger.setAttribute('aria-valuetext',LABEL);}
        else trigger.value=VALUE;
        if(KIND==='button' || KIND==='aria-to-visible') trigger.setAttribute('aria-valuetext',LABEL);
        setDisplay(LABEL);this.setAttribute('aria-selected','true');
        if(!VERIFY && RESET!=='none') resetDisplay();
      };
    })()'''
    for key, item in {'KIND': kind, 'RESET': reset, 'PLACEHOLDER': placeholder, 'WRONG': wrong,
                      'LABEL': label, 'VALUE': value, 'VERIFY': verify_only}.items():
        setup = setup.replace(key, json.dumps(item))
    assert call('eval', expression=setup)['ok']
    receipt = direct_fill(call, ready['origin'], 'cc-exp-' + target, value, retain_stamps=verify_only)
    if verify_only:
        assert receipt['filled'] == 1
        if reset != 'none':
            assert call('eval', expression='resetDisplay()')['ok']
        response = call('eval', expression=build_fill_js([{'index': 0, 'token': 'cc-exp-' + target, 'value': value}],
                        ready['origin'], 'synthetic', verify_only=True))
        assert response['ok']
        receipt = json.loads(response['result'])
    assert receipt['filled'] == int(reset == 'none')
    assert receipt['fieldResults'] == [{'token': 'cc-exp-' + target, 'status': 'filled' if reset == 'none' else 'missing'}]
    assert all(item not in json.dumps(receipt) for item in CARD.values())
    assert call('eval', expression='[window.submissions,window.optionClicks,document.querySelectorAll("[data-hermes-vault-slot],[data-hermes-vault-aria-only]").length]')['result'] == [0, 1, 0]
