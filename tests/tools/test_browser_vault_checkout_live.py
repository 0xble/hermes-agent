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
