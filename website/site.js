/**
 * Consent banner, Google Analytics 4 behind it, and the header menu's small conveniences.
 *
 * Nothing reaches Google before the visitor clicks "Accept" (Consent Mode's *basic*
 * implementation -- the *advanced* mode's cookieless pre-consent pings are rejected, see
 * docs/adr/0050-website-analytics-is-ga4-behind-consent.md). With no stored choice, or after
 * "Decline", this file makes no network request at all. tests/integration/test_website_consent.py
 * holds it to that on every page.
 *
 * The choice lives in localStorage under CONSENT_KEY, never in a cookie, and is reopened from the
 * footer's "Cookie settings" button. /privacy/ describes what GA collects once accepted.
 *
 * Also exposes `window.pfAnalytics.event(name, params)` for page scripts (download.js sends
 * `download_click`). It is a no-op until the visitor has accepted.
 */
(() => {
  // Not a secret: a GA4 measurement ID is public in every page that uses it.
  const MEASUREMENT_ID = 'G-7Z3PFP4XPT';
  const CONSENT_KEY = 'pf-analytics-consent';
  const GRANTED = 'granted';
  const DENIED = 'denied';

  function readChoice() {
    try {
      const value = window.localStorage.getItem(CONSENT_KEY);
      return value === GRANTED || value === DENIED ? value : null;
    } catch {
      return null; // storage blocked: behave as "no choice yet", which loads nothing
    }
  }

  function storeChoice(value) {
    try {
      window.localStorage.setItem(CONSENT_KEY, value);
    } catch {
      // Storage blocked: the choice holds for this page view only. Declining still loads nothing.
    }
  }

  /** The page's GA4 content group, from <meta name="pf-content-group">. */
  function contentGroup() {
    const meta = document.querySelector('meta[name="pf-content-group"]');
    return meta ? meta.content : undefined;
  }

  let gaLoaded = false;

  function loadAnalytics() {
    if (gaLoaded) return;
    gaLoaded = true;
    window[`ga-disable-${MEASUREMENT_ID}`] = false;
    window.dataLayer = window.dataLayer || [];
    // gtag.js's standard bootstrap, injected here rather than pasted into the page so that it
    // runs only after consent.
    window.gtag = function gtag() {
      window.dataLayer.push(arguments); // eslint-disable-line prefer-rest-params
    };
    window.gtag('js', new Date());
    window.gtag('config', MEASUREMENT_ID, {
      content_group: contentGroup(),
      allow_google_signals: false,
      allow_ad_personalization_signals: false,
    });
    const script = document.createElement('script');
    script.async = true;
    script.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(MEASUREMENT_ID)}`;
    document.head.append(script);
  }

  /** Withdrawing consent: stop gtag from sending and delete the cookies it set. */
  function unloadAnalytics() {
    window[`ga-disable-${MEASUREMENT_ID}`] = true;
    const host = window.location.hostname;
    const domains = ['', host, `.${host}`, `.${host.split('.').slice(-2).join('.')}`];
    for (const cookie of document.cookie.split(';')) {
      const name = cookie.split('=')[0].trim();
      if (name !== '_ga' && !name.startsWith('_ga_')) continue;
      for (const domain of domains) {
        document.cookie = `${name}=; Max-Age=0; path=/${domain ? `; domain=${domain}` : ''}`;
      }
    }
  }

  window.pfAnalytics = {
    event(name, params) {
      if (readChoice() !== GRANTED || typeof window.gtag !== 'function') return;
      window.gtag('event', name, params || {});
    },
  };

  let banner = null;

  function privacyHref() {
    const link = document.querySelector('a[data-privacy-link]');
    return link ? link.getAttribute('href') : '/privacy/';
  }

  function closeBanner() {
    if (banner) {
      banner.remove();
      banner = null;
    }
  }

  function choose(value) {
    storeChoice(value);
    closeBanner();
    if (value === GRANTED) loadAnalytics();
    else unloadAnalytics();
  }

  function showBanner() {
    if (banner) return;
    banner = document.createElement('section');
    banner.className = 'consent-banner';
    banner.setAttribute('role', 'region');
    banner.setAttribute('aria-labelledby', 'consent-heading');

    const heading = document.createElement('h2');
    heading.id = 'consent-heading';
    heading.textContent = 'Analytics cookies';

    const text = document.createElement('p');
    text.append(
      'May we use Google Analytics to count visits and see which pages help? It sets cookies and ' +
        'runs only if you accept. Downloads are counted either way, without cookies. ',
    );
    const more = document.createElement('a');
    more.href = privacyHref();
    more.textContent = 'Privacy policy';
    text.append(more);

    // Equal weight on purpose: same element, same class, same size; neither is pre-selected.
    const actions = document.createElement('div');
    actions.className = 'actions cluster';
    for (const [label, value] of [['Accept', GRANTED], ['Decline', DENIED]]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button';
      button.dataset.consent = value;
      button.textContent = label;
      button.addEventListener('click', () => choose(value));
      actions.append(button);
    }

    banner.append(heading, text, actions);
    document.body.append(banner);
  }

  function init() {
    const choice = readChoice();
    if (choice === GRANTED) loadAnalytics();
    else if (choice === null) showBanner();

    for (const button of document.querySelectorAll('[data-cookie-settings]')) {
      button.hidden = false;
      button.addEventListener('click', showBanner);
    }

    // A same-page link in the open mobile menu scrolls the page but would leave the menu open
    // over it; close it on click. Without JavaScript the menu still works, it just stays open.
    for (const menu of document.querySelectorAll('.nav-menu')) {
      menu.addEventListener('click', (event) => {
        if (event.target.closest('a')) menu.open = false;
      });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
