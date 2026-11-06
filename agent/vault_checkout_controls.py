"""Payment control semantics shared by native and ARIA checkout widgets."""
from __future__ import annotations

# Bounded local context only; a page-wide CVC label is never evidence for an
# anonymous input elsewhere in the checkout.
INSPECT_HELPERS = r"""
  const localPaymentContext = (element) => {
    let parent = element.parentElement;
    for (let depth = 0; parent && depth < 3 && !['BODY','HTML','FORM'].includes(parent.tagName); depth++, parent = parent.parentElement) {
      const controls = Array.from(parent.querySelectorAll('input, select, [role=combobox], [role=listbox]'));
      const text = (parent.innerText || '').replace(/\s+/g, ' ').trim();
      if (text.length > 180 || controls.length > 4) break;
      if (/\b(cvc|cvv|csc|card\s*code|card\s*security\s*code|exp(?:iry|iration)?)\b/i.test(text)) {
        const anonymous = controls.filter(n => /^#{3,4}$/.test(n.getAttribute('placeholder') || ''));
        return {label: text.match(/\b(cvc|cvv|csc|card\s*code|card\s*security\s*code|exp(?:iry|iration)?)\b/i)[0], candidates: anonymous.length};
      }
    }
    return {label: '', candidates: 0};
  };
"""

# This executes in the same evaluation as the native writes. The control stamp
# binds the trigger, aria-controls/owns binds its popup, and readback is required
# after events; neither a click nor an attempted assignment counts as success.
FILL_HELPERS = r"""
  const live = (el) => window.location.origin === expectedOrigin && el.isConnected &&
    !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true' && !el.readOnly &&
    getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden' &&
    el.getClientRects().length > 0;
  const usable = (el, f) => !!el && live(el) &&
    el.getAttribute('data-hermes-vault-slot') === nonce + ':' + f.index;
  const safeClick = el => {
    const button = el.closest('button, input');
    return live(el) && (!button ||
      (button.tagName === 'BUTTON' ? button.type === 'button' : !['submit', 'reset', 'image'].includes(button.type))) &&
      !el.closest('a[href]');
  };
  const monthNames = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
  const optionMatch = (text, value, token) => {
    const t = norm(text), v = norm(value);
    if (token === 'cc-exp-month') {
      const named = monthNames.findIndex(m => new RegExp('(^|[^a-z])' + m).test(t));
      return named >= 0 ? named + 1 === Number(v) : /^\d{1,2}$/.test(t) && Number(t) === Number(v);
    }
    if (token === 'cc-type') {
      const brand = s => ({visa:'visa', mastercard:'mastercard', amex:'amex', americanexpress:'amex', discover:'discover'})[norm(s).replace(/[^a-z]/g, '')];
      return brand(t) === v;
    }
    if (token === 'cc-exp-year') return /^\d{2}(\d{2})?$/.test(t) && (t === v || t === v.slice(-2));
    return t === v;
  };
  const chooseOption = (options, f, aria) => {
    const textOf = o => f.token === 'cc-type' ? o.textContent : (o.textContent || o.getAttribute('aria-label') || '');
    const byText = options.filter(o => optionMatch(textOf(o), f.value, f.token));
    // Brands are human-facing semantics, never opaque provider IDs. This also
    // governs native readback: a value of visa cannot certify a Mastercard label.
    if (byText.length || f.token === 'cc-type') return byText.length === 1 ? byText[0] : null;
    // Numeric month values can be zero-based. Visible month text wins; never
    // guess from a numeric opaque value when the labels carry month semantics.
    if (f.token === 'cc-exp-month' && options.some(o => monthNames.some(m => norm(textOf(o)).includes(m)))) return null;
    const hits = options.filter(o => optionMatch(aria ? o.getAttribute('data-value') : o.value, f.value, f.token));
    return hits.length === 1 ? hits[0] : null;
  };
  const fillAria = async (el, f) => {
    if ((!f.token.startsWith('cc-exp') && f.token !== 'cc-type') || !usable(el, f) || !safeClick(el)) return false;
    const root = el.getRootNode(), role = el.getAttribute('role');
    const association = () => el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '';
    const ids = association().split(/\s+/).filter(Boolean);
    if (role !== 'listbox' && ids.length !== 1) return false;
    const binding = association();
    const bound = () => usable(el, f) && el.getRootNode() === root &&
      el.getAttribute('role') === role && association() === binding;
    if (role === 'combobox') el.click();
    let popup, options;
    // Frameworks may mount or reveal the associated popup asynchronously. Keep
    // the old bounded readiness window, revalidating authority after EVERY yield.
    for (let attempt = 0; attempt < 10; attempt++) {
      if (!bound()) return false;
      await new Promise(resolve => setTimeout(resolve, 25));
      if (!bound()) return false;
      popup = role === 'listbox' ? el : root.getElementById(ids[0]);
      if (!popup || popup.getAttribute('role') !== 'listbox' || !live(popup)) continue;
      options = Array.from(popup.querySelectorAll('[role=option]')).filter(safeClick);
      if (options.length) break;
    }
    const option = chooseOption(options || [], f, true);
    if (!option || !bound() || !safeClick(el) || !safeClick(option) ||
        !popup.contains(option) || (role !== 'listbox' && root.getElementById(ids[0]) !== popup)) return false;
    option.click();
    if (!bound()) return false;
    await new Promise(resolve => setTimeout(resolve, 0));
    if (!bound()) return false;
    if (f.token === 'cc-type' && !optionMatch(option.textContent, f.value, f.token)) return false;
    return option.getAttribute('aria-selected') === 'true' ||
      [el.value, el.getAttribute('aria-valuetext'), el.textContent].some(t => optionMatch(t, f.value, f.token));
  };
"""

