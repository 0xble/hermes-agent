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
  const monthNames = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
  const optionMatch = (text, value, token) => {
    const t = norm(text), v = norm(value);
    if (token === 'cc-exp-month') {
      const named = monthNames.findIndex(m => new RegExp('(^|[^a-z])' + m).test(t));
      return named >= 0 ? named + 1 === Number(v) : /^\d{1,2}$/.test(t) && Number(t) === Number(v);
    }
    if (token === 'cc-exp-year') return /^\d{2}(\d{2})?$/.test(t) && (t === v || t === v.slice(-2));
    return t === v;
  };
  const chooseOption = (options, f, aria) => {
    const textOf = o => o.textContent || o.getAttribute('aria-label') || '';
    const byText = options.filter(o => optionMatch(textOf(o), f.value, f.token));
    if (byText.length) return byText.length === 1 ? byText[0] : null;
    // Numeric month values can be zero-based. Visible month text wins; never
    // guess from a numeric opaque value when the labels carry month semantics.
    if (f.token === 'cc-exp-month' && options.some(o => monthNames.some(m => norm(textOf(o)).includes(m)))) return null;
    const hits = options.filter(o => optionMatch(aria ? o.getAttribute('data-value') : o.value, f.value, f.token));
    return hits.length === 1 ? hits[0] : null;
  };
  const fillAria = async (el, f) => {
    if (!f.token.startsWith('cc-exp') || !live(el)) return false;
    const root = el.getRootNode();
    if (el.getAttribute('role') === 'combobox') el.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    if (!live(el)) return false;
    const ids = (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean);
    const popups = el.getAttribute('role') === 'listbox' ? [el] : ids.map(id => root.getElementById(id)).filter(Boolean);
    if (popups.length !== 1 || popups[0].getAttribute('role') !== 'listbox') return false;
    const options = Array.from(popups[0].querySelectorAll('[role=option]')).filter(o =>
      o.getAttribute('aria-disabled') !== 'true' && o.getClientRects().length && getComputedStyle(o).display !== 'none');
    const option = chooseOption(options, f, true);
    if (!option || !live(el)) return false;
    option.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    if (!live(el)) return false;
    return option.getAttribute('aria-selected') === 'true' ||
      [el.value, el.getAttribute('aria-valuetext'), el.textContent].some(t => optionMatch(t, f.value, f.token));
  };
"""

