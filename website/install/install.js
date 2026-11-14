/**
 * Drives the picker on /install/: shows the one result for the chosen setup and the notes that
 * apply to it. Without JavaScript the form stays hidden and every result and note is visible,
 * so the page reads as a list of all the paths. No network, no dependencies.
 */
(() => {
  'use strict';

  function init() {
    const form = document.getElementById('install-picker');
    if (!form) return;
    form.hidden = false;

    const state = () => {
      const s = {};
      for (const name of ['who', 'bundle', 'signin', 'apps', 'proxy']) {
        const checked = form.querySelector(`input[name="${name}"]:checked`);
        s[name] = checked ? checked.value : '';
      }
      return s;
    };

    const resultKey = (s) => {
      if (s.who === 'single') return `${s.bundle}-single`;
      if (s.bundle === 'standalone') return s.proxy === 'caddy' ? 'standalone-caddy' : 'standalone-proxy';
      if (s.signin === 'own' && s.proxy === 'caddy') return 'suite-caddy-idp';
      if (s.signin === 'builtin' && s.proxy === 'caddy') return 'suite-caddy-google';
      if (s.signin === 'builtin' && s.proxy === 'nginx') return 'suite-nginx-google';
      return 'suite-no-config';
    };

    const tokens = (s) => {
      const set = new Set([s.who]);
      if (s.who === 'org') {
        set.add(`signin-${s.signin}`);
        set.add(`apps-${s.apps}`);
      }
      return set;
    };

    const apply = () => {
      const s = state();
      const key = resultKey(s);
      const have = tokens(s);
      for (const el of document.querySelectorAll('[data-result]')) {
        el.hidden = el.dataset.result !== key;
      }
      for (const el of document.querySelectorAll('[data-when]')) {
        el.hidden = !el.dataset.when.split(' ').every((tok) => have.has(tok));
      }
    };

    form.addEventListener('change', apply);
    apply();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
