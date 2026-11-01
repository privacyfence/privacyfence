# Website for PrivacyFence 7.x, and an Install menu

## Goal

PrivacyFence 7.0.0 is released (`CHANGELOG.md`, `## [7.0.0] — 2026-11-01`), but the hand-written
pages of privacyfence.eu still describe 6.x. They say an administrator registers an app with every
provider and ships a config bundle. They present the desktop installers as the way in, and they say
nothing about single-user Docker, the Admin page or the environment-only organization server.
This plan brings every hand-written page under `website/` (not `website/_docs/`) in line with 7.x.
It also replaces the header's highlighted **Download** item with **Install**, which leads to a new
`/install/` page. That page has an interactive picker that walks through the install modes:

- the PrivacyFence suite (with a built-in presentation tool, Peek, and a built-in data lake, Grist)
  or PrivacyFence alone;
- an organization server, or single-user Docker on this computer;
- the built-in Google sign-in, or your own identity provider;
- PrivacyFence's built-in apps, or your organization's own;
- the included Caddy, nginx, or another proxy.

The commands come from `privacyfence/privacyfence-suite` (its README and `docs/configurations.md`,
release 0.1.0) and from `docs/org-mode-docker.md`. The deprecated desktop mode is not mentioned on
`/install/`.

`/download/` stays live for existing desktop users. It now recommends single-user Docker for one
person, keeps the desktop installers with the deprecation notice, and leaves the header.
`docs/oauth-relay.md` is published on `/docs/`, and relay-terms and the privacy statement link to it.

The user docs the website now points at are brought along in the same PR (D8):
- the organization guide sends a single person to single-user Docker instead of the desktop install;
- How it works describes single-user mode and `PRIVACYFENCE_MODE`;
- the Claude Code and Claude Desktop guides get a "Single-user Docker" section.

Because `/docs/` is rendered from the newest stable tag, these pages go live with the next release.

Everything lands as one PR from `chore/website-7x` to `main`.

Decisions taken with the user while planning:
- `/download/` stays, but is no longer in the header.
- `/install/` uses an interactive picker, not a static table.
- `oauth-relay.md` is published on `/docs/`.
- Until the next stable tag carries that page, the build points links to it at GitHub by itself.

## Current state

- **Header.** `website/_partials/header.html:35` is `<a class="nav-cta" href="{{cta_href}}">{{cta_label}}</a>`.
  - The default is `scripts/build_site.py:315`:
    `"header": {"skip_target": "#top", "cta_href": "/download/", "cta_label": "Download"}`.
  - `website/download/index.html:60` overrides it with `cta_href="/releases/" cta_label="All releases"`.
  - The 7 nav links (`header.html:14-34`) do not change.
  - `chrome.css:30` styles `.nav-cta`.
- **Page registry.** `build_site.PAGES` is at `scripts/build_site.py:97-127` and `STATIC` at `:137-162`.
  - `download/download.js` is the pattern for a page script (`:146`).
  - The llms.txt "## Website" list is hand-written at `:1131-1158`.
  - Guardrail 7 (`tests/unit/test_website_download_cta.py:63-87`) fails on any file under `website/` that is
    in no manifest.
  - Every page in `PAGES` is checked by `tests/unit/test_website_pages.py`: canonical, `pf-content-group` in
    `{marketing, download, connector, platform, ai-agent, docs}`, title of at least 10 characters,
    description of 50-250 characters, footer, `/site.js`. It is also loaded by the browser tests
    (`tests/website_site.py:69-75`).
  - CSP: a same-origin script file is allowed by `script-src 'self'`
    (`scripts/site_security_headers.py:28`); hand-written pages may have no inline script or style
    (`tests/unit/test_website_security_headers.py:137-147`).
- **Tests that pin the CTA.**
  - `tests/unit/test_build_site.py:121-127` pins `'<a class="nav-cta" href="/download/">Download</a>'`.
  - `:179-194` normalizes download's CTA with `replace('href="/releases/">All releases', 'href="/download/">Download')`.
  - `tests/unit/test_website_download_cta.py:25,38-50` needs exactly 3 `Download` CTAs on the homepage, all
    `/download/`: the header, `website/index.html:82` and `:209`.
- **Docs links.** `/docs/` is rendered from the newest stable tag (ADR 0052; that tag is `v7.0.0` today).
  - `build_site.CONTRIBUTOR_DOCS` (`scripts/build_site.py:183-197`) lists `oauth-relay.md`, so
    `/docs/oauth-relay/` does not exist.
  - `point_docs_links_at_github` (`:1277-1293`) rewrites hand-written `/docs/...` links to GitHub only when
    `/docs/` is not built at all (`:1407-1408`).
  - The link check (`:1415-1418`) fails the build on any other dead link.
  - ADR 0099 is the rule for agent pages: wait for the release.
  - `tests/unit/test_website_docs_allowlist.py:49-54` requires `docs/README.md`'s contributor half to equal
    `CONTRIBUTOR_DOCS - {"README.md"}`.
- **Pages still on 6.x** (line numbers on `main` at `1ebc614`):
  - `website/enterprise/index.html`:
    - the table at `:42-53` (Install `pip install`; step-up "in the organization config bundle");
    - the prerequisite cards at `:64-92` (one OAuth app per service; "The bundle builder");
    - the button `:134` "Download for one computer".
  - `website/connectors/index.html:203`; `google-workspace:155-156`; `jira-confluence:117-118`;
    `salesforce:92-93`; `miro:61`; `grist:62`. Each says an administrator registers an app and packages a
    bundle.
  - `website/connectors/slack/index.html:96` ends "That app is never publicly distributed."
  - `website/connectors/{grist,miro,peek}/index.html:66` link the guide on GitHub instead of `/docs/`.
  - `website/how-it-works/index.html:49,143,146`; `faq/index.html:58,86,93-96,107`; `security/index.html:41,56`.
  - `index.html:82,87,177,209`; `ai-agents/**` "download page" links.
  - `download/index.html:63-71,104`.
  - "Admin page" appears only at `connectors/slack:63` and `relay-terms:69`. Docker, single-user and the
    Admin page are otherwise absent.
- **Already updated for 7.x:**
  - `website/relay-terms/index.html` (its one docs link is `:71`, `<a href="/docs/">documentation</a>`);
  - `website/privacy/index.html` (`#relay` at `:122-146`, no `/docs/` link).
- **Tests that must keep passing unchanged:**
  - `tests/unit/test_website_pages.py:300-302`: "PrivacyFence 8 removes it" on `/download/` and `/faq/`.
  - `:293-297`: no page contains "there are none" (lower case) or "no PrivacyFence-operated server is
    involved".
  - `:202-203`: exactly 15 second-level pages.
  - `tests/unit/test_website_claims.py`: forbidden `certified|compliant with|SLAs?|guarantee[sd]?`. It pins
    no sentence, so it does not change.
  - `tests/unit/test_website_canonical_description.py` keeps `website/index.html:114-116` verbatim;
    those lines do not change.

## Design

### D1. Facts, as sentences to reuse

Every claim about 7.x on any page uses one of these. Adapt the grammar to the sentence it sits in if
you must, but add no fact that is not here. Each fact's source is in brackets.

- **F1, the data path** (already on several pages; keep it where it is): "No data passes through a
  PrivacyFence-operated server. When an organization server uses PrivacyFence's built-in sign-in apps,
  only the sign-in redirect passes through the PrivacyFence relay, and an organization can use its own
  apps instead."
- **F2, built-in apps** [CHANGELOG.md:55, ADR 0255]: "PrivacyFence ships its own apps for Google, Slack,
  Salesforce, Atlassian and Miro, so an organization server, or the Docker image on your own computer,
  connects them with nothing to register. An organization may register its own app with any of them
  instead and upload it on the Admin page; its own apps never touch the relay."
- **F3, what the relay sees** [org-mode-setup-guide.md:431-435]: "The relay at oauth.privacyfence.eu
  forwards the browser's sign-in redirect (a one-time authorization code and the state) to your server.
  It never sees a token, keeps no logs and stores no code: your server exchanges the code with the
  provider itself."
- **F4, Google's limits** [CHANGELOG.md:55]: "Until Google verifies PrivacyFence's Google app, people see
  Google's unverified-app screen, and the app is limited to 100 users across all installs; an
  organization that needs neither registers its own Google app."
- **F5, PKCE** [miro-setup.md:7-8, org-mode-setup-guide.md:433-435]: "Slack and Miro send no PKCE, so with
  the built-in app their one-time sign-in code passes through the relay without it; an organization's
  own app avoids this."
- **F6, the Admin page** [org-mode-setup-guide.md:1006-1023]: "Administrators run the server from the Admin
  page: which services are offered and which app each uses, who may sign in, step-up, scoped tokens,
  audit forwarding and the install-wide privacy filter. Every change that alters what the server does
  asks for a passkey and is recorded in the audit log. The server address, the identity provider and the admins are set in the
  environment, not on the page."
- **F7, the Docker organization server** [org-mode-docker.md:21-35,112-234]: "An organization server starts
  from environment variables: the `ghcr.io/privacyfence/privacyfence-org` image, a `privacyfence.env`
  file naming the identity provider and the admins, and Caddy in front for HTTPS. No config bundle is
  needed."
- **F8, how people sign in** [org-mode-setup-guide.md:136-162,278-335]: "People sign in through your
  organization's OpenID Connect identity provider, or, for a small Google Workspace team, through
  PrivacyFence's built-in Google sign-in, which needs nothing registered at Google and admits only
  verified Workspace accounts."
- **F9, single-user** [org-mode-docker.md:37-101]: "For one person, the Docker image runs on your own
  computer in single-user mode: published only on localhost, and set up with a one-time code from the
  container's log, then a passkey."
- **F10, the Docker warning** [security-and-compliance.md:95-100, ADR 0260]: "Single-user mode is no
  protection against an AI agent that can run `docker` on that computer: whoever can run `docker` can
  read the setup code from the log or open the data volume. Run it under a separate account with
  rootless Docker or in a virtual machine, or use an organization server."
- **F11, desktop deprecation** [ADR 0261, org_mode.py:67-72]: "PrivacyFence 7 deprecates desktop mode, and
  PrivacyFence 8 removes it. Desktop installs keep working through 7.x, without the built-in apps. Move
  to the Docker image on your computer or to an organization server." Always link
  `/docs/moving-to-organization-mode/` from it. Keep the words "PrivacyFence 8 removes it" exactly.
- **F12, the suite** [privacyfence-suite README]: "The PrivacyFence suite starts PrivacyFence together with
  a built-in presentation tool (Peek, for sharing HTML pages and collecting comments) and a built-in
  data lake (Grist, for spreadsheet-style databases) with one `docker compose up -d`. Everyone signs in
  to all three through PrivacyFence, and their Peek and Grist accounts are connected automatically
  (the Peek and Grist admin addresses excepted)."

Rules for all copy:
- No word from `test_website_claims.FORBIDDEN`.
- No "there are none".
- No client named that `website/_data/clients.json` does not list.
- No third-party logo (ADR 0101).
- Where the old text says "organization config bundle" for an organization server, the new text says
  the server uses PrivacyFence's built-in app or the organization's own app uploaded on the Admin page.
- Where it says "PrivacyFence's Settings page" as the place a person connects a service, the new text
  says "PrivacyFence's Connect page" (CHANGELOG.md:98 uses that name). D6 gives every such sentence in
  full.
- The desktop mode is called "desktop mode" (the term in F11). The old "local mode" may remain only
  where the page compares the two modes, marked deprecated.

### D2. Docs URLs to link

- Provider guides:
  - `/docs/google-cloud-setup/`, `/docs/slack-setup/`, `/docs/salesforce-setup/`,
    `/docs/atlassian-setup/`, `/docs/miro-setup/`, `/docs/telegram-setup/`, `/docs/grist-setup/`,
    `/docs/peek-setup/`.
- Organization guide anchors:
  - `/docs/org-mode-setup-guide/#built-in-google-sign-in`
  - `#built-in-apps-through-the-relay`
  - `#signed-connector-configs`
  - `#the-admin-page`
  - `#7-reverse-proxy-and-tls`
- Docker guide anchors:
  - `/docs/org-mode-docker/#run-it-on-this-computer`
  - `#quick-start`
  - `#environment-variables`
- Other docs:
  - `/docs/moving-to-organization-mode/`
  - `/docs/security-and-compliance/#single-user-mode`
  - `/docs/oauth-relay/`, once D4 is in.
- Suite links (external, not link-checked):
  - `https://github.com/privacyfence/privacyfence-suite#readme`
  - `https://github.com/privacyfence/privacyfence-suite/blob/main/docs/configurations.md`
  - `https://github.com/privacyfence/privacyfence-suite/blob/main/configs/nginx-google/nginx/README.md`

The build's link check verifies every internal fragment. If one of these anchors does not resolve,
that is a stop condition: report it, do not invent another anchor.

### D3. The Install CTA

- `scripts/build_site.py:315` becomes `"header": {"skip_target": "#top", "cta_href": "/install/", "cta_label": "Install"}`.
- `website/_partials/header.html:3`'s note reads "(default /install/ and Install)".
- `website/download/index.html:60` keeps its `All releases` override.
- In `test_build_site.py`, the pinned string becomes `'<a class="nav-cta" href="/install/">Install</a>'`,
  and the normalization becomes `replace('href="/releases/">All releases', 'href="/install/">Install')`.
- `test_website_download_cta.py`'s homepage CTA test becomes an Install test:
  - `CTA_PATTERN = re.compile(r'<a class="(?:nav-cta|button primary)" href="([^"]+)">([^<]*Install[^<]*)</a>')`;
  - exactly 3 matches on the built homepage, all `href == "/install/"`;
  - none on GitHub;
  - rename the functions `test_homepage_install_ctas_point_at_the_install_page` and
    `test_homepage_install_ctas_do_not_point_at_github`;
  - update the module docstring's sentence about the CTAs to match.
- `website/index.html:82` becomes `<a class="button primary" href="/install/">Install PrivacyFence</a>`.
- `website/index.html:209` becomes `<a class="button primary" href="/install/">Install PrivacyFence</a>`.
- Every other hand-written link to the `/download/` page becomes `/install/`, with link text that says
  install (for example "Install PrivacyFence"). That covers enterprise, ai-agents/**, connectors/**,
  how-it-works and faq. The exceptions stay as they are:
  - `website/download/index.html` itself;
  - `website/releases/**`;
  - the JSON-LD `downloadUrl` on `/` and `/download/` (pinned by `test_website_pages.py:127-141`);
  - `website/_docs/**` (out of scope);
  - `faq/index.html:107`, which is about verifying a desktop download and keeps `/download/`;
  - the desktop-mode "Set it up" steps on `website/ai-agents/*/index.html` ("Install PrivacyFence from
    the download page"), which keep `/download/` because they set up the deprecated installers.

### D4. Publishing `oauth-relay.md`, and the docs-link fallback

- **Publishing.** Remove `"oauth-relay.md"` from `build_site.CONTRIBUTOR_DOCS`. In `docs/README.md`, move
  its bullet from "Contributor docs" to the end of "### Organization deployment and security", worded:
  "- [`oauth-relay.md`](oauth-relay.md) — the OAuth redirect relay at `oauth.privacyfence.eu`: what it
  forwards and stores, and how the maintainers run, check and revoke it."
- **The fallback.** Add a constant below `CONTRIBUTOR_DOCS`:
  ```python
  # Published docs that the newest stable tag may not carry yet. A hand-written page's link to one of
  # them points at GitHub `main` until a release renders it on /docs/ (ADR 0276); any other missing
  # docs page still fails the link check, as ADR 0099 requires.
  DOCS_PENDING_RELEASE: frozenset[str] = frozenset({"oauth-relay"})
  ```
  Change the signature to
  `def point_docs_links_at_github(site: Path, ref: str, only: frozenset[str] | None = None) -> None:`.
  With `only=None`, behaviour is unchanged. With `only` set, it rewrites a link only when its `stem`
  is in `only` and `site / "docs" / stem / "index.html"` does not exist. `/docs/` itself is never
  rewritten in that mode. Extend the docstring by one sentence saying so. At `:1407-1408`:
  ```python
  if not docs_pages:
      point_docs_links_at_github(out, "main")
  else:
      point_docs_links_at_github(out, "main", only=DOCS_PENDING_RELEASE)
  ```
  The fallback is deliberately narrow: an ai-agents page linking an unreleased connect doc still fails
  the link check (ADR 0099 stands).
  - Rejected: a fallback for every missing stem, which would silently undo ADR 0099's guard.
  - Rejected: waiting for 7.0.1 before linking, because the user wants one PR now.
  - Rejected: a hand-edited GitHub link, which ADR 0099 already rejected as a remembered second edit.
  - ADR 0099's other objection, that GitHub's `main` documents the wrong version, does not apply here:
    `oauth-relay.md` describes a service deployed from `main` (docs/oauth-relay.md:14-20), not a
    released version.
- **Links.**
  - `website/relay-terms/index.html:71`: that sentence is about an organization registering its own
    apps, so its link becomes `<a href="/docs/org-mode-setup-guide/#signed-connector-configs">documentation</a>`.
    Directly after it, inside the same paragraph, add the sentence "How the relay works and how the
    maintainers run it is in the <a href="/docs/oauth-relay/">relay documentation</a>."
  - `website/privacy/index.html` `#relay`: after the sentence ending "and desktop (local) installs do not
    use it.", add "How the maintainers run the relay is described in the
    <a href="/docs/oauth-relay/">relay documentation</a>." Change nothing else on either page.

### D5. The `/install/` page

Files:
- `website/install/index.html` (registered in `PAGES` as `"/install/": "install/index.html"`, placed after
  `"/faq/"`);
- `website/install/install.js` (in `STATIC` as `"install/install.js": "website/install/install.js"`, next to
  `download/download.js`);
- picker styles appended to `website/styles.css` under a `/* Install page */` comment, using only existing
  custom properties and the existing `.card` and `.grid-auto` patterns;
- in llms.txt's website list (`build_site.py:1131-1158`), add before the Download line:
  `f"- [Install]({SITE_URL}/install/): choose single-user Docker or an organization server, with or without the built-in presentation tool and data lake, and get the commands",`.

Head:
- `<title>Install PrivacyFence with Docker — PrivacyFence</title>`;
- meta description "Choose how to install PrivacyFence: for one person on this computer or as an
  organization server, alone or with a built-in presentation tool and data lake, with your own sign-in,
  apps and proxy.";
- `pf-content-group` `download`;
- canonical `https://privacyfence.eu/install/`;
- the OG tags in the form of `website/enterprise/index.html:16-21` (no Twitter tags): `og:url`
  `https://privacyfence.eu/install/`, `og:title` "Install PrivacyFence", `og:description` "For one person
  on this computer or as an organization server, with or without a built-in presentation tool and data
  lake: pick your setup and get the commands.", `og:image` as on enterprise;
- `<!-- include: header -->` and `<!-- include: footer -->`;
- `<script src="/site.js" defer></script>` and `<script src="/install/install.js" defer></script>`;
- no inline script or style, and no JSON-LD.

Body, in order:
1. **Intro.** `section.page-intro.shell`: kicker `Install`, `<h1>Install PrivacyFence.</h1>`, and the lede
   "PrivacyFence runs as Docker containers. Answer a few questions to get the commands for your setup:
   who uses it, whether it comes with a presentation tool and a data lake, how people sign in, which
   apps connect the services, and what sits in front of it."
2. **Picker.** `<form id="install-picker" class="install-picker" hidden>` (the script removes `hidden`). It
   has five `<fieldset>`s, each with a `<legend>` and radio inputs. The labels and descriptions are
   below, defaults first. Fieldsets 3 to 5 carry `data-when="org"`.
   1. `name="who"`, legend "Who will use it?":
      - `single` (checked): "Just me, on this computer", with "Single-user mode: published only on
        localhost, protected by your passkey."
      - `org`: "My organization, on a server", with "Everyone signs in; each person's connections,
        approvals and audit log are kept apart."
   2. `name="bundle"`, legend "What should it include?":
      - `suite` (checked): "PrivacyFence with a presentation tool and a data lake", with "The PrivacyFence
        suite: Peek for sharing HTML pages, Grist for spreadsheet-style databases, already connected."
      - `standalone`: "PrivacyFence only", with "Connect your own Peek or Grist later, or none."
   3. `name="signin"`, legend "How do people sign in?":
      - `own` (checked): "Your own identity provider", with "Any OpenID Connect provider, such as Microsoft
        Entra ID, Okta, Keycloak or Google Cloud. You register one client."
      - `builtin`: "Built-in Google sign-in", with "For a small Google Workspace team. Nothing to register
        at Google; only verified Workspace accounts sign in."
   4. `name="apps"`, legend "Which apps connect the services?":
      - `builtin` (checked): "PrivacyFence's built-in apps", with "Google, Slack, Salesforce, Atlassian and
        Miro connect with nothing to register."
      - `own`: "Your organization's own apps", with "Register an app with each provider and upload it on
        the Admin page. The relay is not used for them."
   5. `name="proxy"`, legend "What sits in front of it?":
      - `caddy` (checked): "Caddy, included", with "A Caddy container serves HTTPS and gets the certificate
        itself."
      - `nginx`: "nginx already on the server".
      - `other`: "Another reverse proxy".
3. **Results.** `<div id="install-results" aria-live="polite">`. It holds one
   `<section class="install-result card stack" data-result="<key>">` per key, each starting with an
   `<h2>` that names the combination, so that without JavaScript they read as a list of all paths.
   Every command or file block on the page is `<pre class="verify-commands"><code>…</code></pre>`
   (that class gives `overflow-x: auto`, website/styles.css:234), copied exactly from the sources named.
   - `suite-single`, h2 "The suite, for one person on this computer" [suite README "### localhost"]:
     ```
     curl -fsSL https://github.com/privacyfence/privacyfence-suite/releases/latest/download/install.sh | sh -s -- localhost --start
     cd privacyfence-localhost
     docker compose logs privacyfence | grep "Setup code"
     ```
     Then open <http://localhost:8765>, enter the code and set up your passkey. Peek is at
     <http://localhost:7700> and Grist at <http://localhost:8484>. Needs Docker Engine 24 or later with
     Compose 2.24 or later.
   - `standalone-single`, h2 "PrivacyFence alone, for one person on this computer" [org-mode-docker.md:50-78]:
     ```
     docker run --rm -p 127.0.0.1:8765:8765 \
       -v pf-data:/var/lib/privacyfence-org/.privacyfence \
       ghcr.io/privacyfence/privacyfence-org:latest
     ```
     The log prints `Setup code: XXXX-XXXX. Open http://localhost:8765/login to set up your passkey.`
     Open `http://localhost:8765`, not `http://127.0.0.1:8765`: the passkey belongs to `localhost`.
     Link `/docs/org-mode-docker/#run-it-on-this-computer`.
   - `suite-caddy-idp`, h2 "The suite with Caddy and your identity provider" [suite README "### caddy-idp"]:
     - the install line `curl -fsSL https://github.com/privacyfence/privacyfence-suite/releases/latest/download/install.sh | sh -s -- caddy-idp`;
     - then its 5 steps, shortened to one sentence each: the three redirect URIs, the `.env` names, the
       secret file, `docker compose up -d`, and sign in.
     - Needs three DNS names pointing at the server (PrivacyFence, Peek and Grist), ports 80 and 443
       reachable from the internet, and Docker Engine 24 or later with Compose 2.24 or later.
   - `suite-caddy-google`, h2 "The suite with Caddy and the built-in Google sign-in" [suite README "### caddy-google"]:
     - the install line with `caddy-google`;
     - edit `.env` (the three host names, `ADMIN_EMAILS`, `ALLOWED_DOMAIN`, `GRIST_ADMIN_EMAIL`);
     - `docker compose up -d`;
     - publish the `_privacyfence-relay` DNS TXT records printed by
       `docker compose exec privacyfence privacyfence-app --relay-dns-records`;
     - sign in with a Google Workspace account.
     - Needs the same three DNS names, ports and Docker versions as `suite-caddy-idp`.
   - `suite-nginx-google`, h2 "The suite behind your nginx, with the built-in Google sign-in" [suite README "### nginx-google"]:
     - the install line with `nginx-google`;
     - `.env` as for caddy-google;
     - set up the host's nginx from the suite's `nginx/README.md` (link);
     - `docker compose up -d`;
     - the DNS records.
     - Needs Linux with rootful Docker, nginx 1.25.1 or later, and a certificate for the three names.
       Docker Desktop and rootless Docker do not work; use Caddy there.
   - `suite-no-config`, h2 "No ready-made suite configuration for this combination": "The suite ships
     Caddy with your identity provider, and Caddy or nginx with the built-in Google sign-in. With your
     identity provider behind nginx or another proxy, either use the Caddy configuration, or install
     PrivacyFence alone behind your proxy and connect Peek and Grist to it." It links the suite README,
     `/docs/org-mode-setup-guide/#7-reverse-proxy-and-tls` and `/docs/oidc-provider/`.
   - `standalone-caddy`, h2 "PrivacyFence alone, with Caddy" [org-mode-docker.md "## Quick start"]:
     - first: "Point a public host name at the Docker host and open TCP 443 (and 80 if you can). With
       the built-in Google sign-in or built-in apps, TCP 443 must be reachable from the whole internet.
       If the DNS is on Cloudflare, set the record to DNS only.";
     - inside `<div data-when="signin-own">`: "Register one OIDC client with your identity provider, with
       the redirect URIs `https://<host>/oauth/idp/callback`, `/oauth/idp/login-callback` and
       `/oauth/stepup/callback`.";
     - make the folder (`sudo install -d -o root -g root -m 0750 /opt/privacyfence-org` and
       `cd /opt/privacyfence-org`);
     - the four `curl` lines of step 4, verbatim with `vX.Y.Z`, plus the sentence "Replace `vX.Y.Z` with
       the release you run.";
     - fill in `privacyfence.env`, showing the two snippets below;
     - set `PF_HOSTNAME` and `PRIVACYFENCE_IMAGE_TAG` in `.env`;
     - `sudo docker compose up -d`;
     - sign in as an admin and add a passkey.
     - Link `/docs/org-mode-docker/#quick-start`.
     - Snippet wrapped in `<div data-when="signin-own">`, followed (same div) by the secret file block
       from org-mode-docker.md step 5 and the sentence "Then uncomment the `secrets:` lines in
       `compose.yaml`, under the service and at the bottom.":
       ```
       PRIVACYFENCE_IDP_ISSUER=https://login.example.com
       PRIVACYFENCE_IDP_CLIENT_ID=
       PRIVACYFENCE_IDP_CLIENT_SECRET_FILE=/run/secrets/idp_client_secret
       PRIVACYFENCE_ADMIN_EMAILS=alice@example.com,bob@example.com
       ```
       ```
       sudo install -m 0400 -o 10001 -g 10001 /dev/null idp_client_secret
       sudo sh -c 'cat > idp_client_secret'   # paste the secret, then press Ctrl-D
       ```
     - Snippet wrapped in `<div data-when="signin-builtin">`, preceded (same div) by "Comment out the
       `PRIVACYFENCE_IDP_ISSUER` and `PRIVACYFENCE_IDP_CLIENT_ID` lines at the top of
       `privacyfence.env`: the built-in Google sign-in refuses to start next to any
       `PRIVACYFENCE_IDP_*` setting. Then set:":
       ```
       PRIVACYFENCE_IDP=builtin-google
       PRIVACYFENCE_ADMIN_EMAILS=alice@example.com,bob@example.com
       PRIVACYFENCE_ALLOWED_DOMAINS=example.com
       ```
   - `standalone-proxy`, h2 "PrivacyFence alone, behind nginx or another proxy": "PrivacyFence's Docker
     files bring Caddy. Behind nginx or another reverse proxy, install PrivacyFence from PyPI and run it
     with systemd as the organization deployment guide describes; its reverse proxy section has a Caddy
     and an nginx example." It links `/docs/org-mode-setup-guide/` and
     `/docs/org-mode-setup-guide/#7-reverse-proxy-and-tls`. The `signin-own` and `signin-builtin`
     snippets are not repeated here.
4. **Notes.** `<section class="page-section shell">`, each note a `<div class="install-note card stack" data-when="...">`:
   - `data-when="single"`: F10, linking `/docs/security-and-compliance/#single-user-mode`.
   - `data-when="org apps-builtin"`:
     - F2's first sentence, then F3, F4 and F5;
     - "The server needs a public `https://` address reachable on TCP 443 from the whole internet, and
       outbound HTTPS to `oauth.privacyfence.eu`.";
     - links `/docs/org-mode-setup-guide/#built-in-apps-through-the-relay` and `/relay-terms/`.
   - `data-when="org apps-own"`:
     - "Register an app with each provider, sign its client ID and secret as a connector config, and upload
       it on Admin → Services. Your own apps use your server's own redirect addresses and never touch the
       relay.";
     - links `/docs/org-mode-setup-guide/#signed-connector-configs` only. Do not link
       `/tools/connector-config/`: it is an unlisted page, and `test_website_pages.py:262-263` forbids
       links to it from listed pages.
   - `data-when="org signin-builtin"`: "The built-in Google sign-in admits only verified Google Workspace
     accounts in your allowed domains. Nobody from a domain signs in until you publish the DNS TXT record
     `_privacyfence-relay.<domain>` that the server prints. Every sign-in goes through
     `oauth.privacyfence.eu`." It links `/docs/org-mode-setup-guide/#built-in-google-sign-in`.
   - `data-when="org"`: F6, linking `/docs/org-mode-setup-guide/#the-admin-page`.
5. **What gets installed.** A short list of the images:
   - `ghcr.io/privacyfence/privacyfence-org`;
   - with the suite, `ghcr.io/privacyfence/peek`, `ghcr.io/privacyfence/grist-core` (PrivacyFence's
     grist-core fork) and `ghcr.io/privacyfence/grist-provisioner`;
   - in the Caddy configurations (suite and standalone), `caddy`;
   - with the suite, `alpine`, for the one-shot setup container that generates the secrets.
   Add "The suite pins every image except PrivacyFence, which follows `latest`; its `.env` holds the
   tags." No version numbers on the page.
6. **Verify.** The suite's verified path [suite README "The verified path"], verbatim:
   ```
   base=https://github.com/privacyfence/privacyfence-suite/releases/latest/download
   curl -fsSLO $base/install.sh && curl -fsSLO $base/SHA256SUMS
   sha256sum -c --ignore-missing SHA256SUMS      # prints "install.sh: OK"
   gh attestation verify install.sh --repo privacyfence/privacyfence-suite
   sh install.sh caddy-google
   ```
   Then the image check `gh attestation verify oci://ghcr.io/privacyfence/privacyfence-org:X.Y.Z --owner privacyfence`.

Inside `<main>`, the page never mentions desktop mode, the installers or `/download/`. The header,
which the build adds, is not part of that rule.

`install.js`:
- an IIFE in the style of `website/download/download.js`;
- `'use strict'`, no dependencies, no network, and no `innerHTML` with non-constant strings.
- Behaviour:
  1. On load, find `#install-picker`. If it is missing, return. Otherwise remove its `hidden`
     attribute.
  2. `state()` reads the checked value of each radio group.
  3. `resultKey(s)`:
     - who `single`: `${bundle}-single`;
     - bundle `standalone`: proxy `caddy` gives `standalone-caddy`, otherwise `standalone-proxy`;
     - suite, own identity provider, caddy: `suite-caddy-idp`;
     - suite, built-in sign-in, caddy: `suite-caddy-google`;
     - suite, built-in sign-in, nginx: `suite-nginx-google`;
     - anything else: `suite-no-config`.
  4. `tokens(s)` is the set
     `{s.who, 'signin-' + s.signin, 'apps-' + s.apps}`. The `signin-*` and `apps-*` tokens are added
     only when who is `org`.
  5. `apply()` sets `el.hidden = el.dataset.result !== key` on every `[data-result]`, and
     `el.hidden = !el.dataset.when.split(' ').every(tok => tokens.has(tok))` on every `[data-when]`
     (fieldsets included), so it both hides and un-hides.
  6. `apply()` runs once at start and on the form's `change` event.

### D6. Page-by-page changes

Leave out of each page anything not listed here.

- **`website/index.html`** (besides D3's two buttons):
  - `:87` becomes `<span>Apache 2.0</span><span>Docker · Linux · macOS · Windows</span><span>No account required</span>`.
  - `:177`: the three sentences before the links ("PrivacyFence runs on macOS…" up to "…built-in apps.") become:
    "PrivacyFence runs as Docker containers: on your own computer for one person, or as one
    organization server, with each person signing in through your identity provider or the built-in
    Google sign-in. Neither runs on PrivacyFence-operated infrastructure; the only PrivacyFence service
    in the path is the sign-in relay, which carries just the sign-in redirect for PrivacyFence's built-in
    apps."
    The links after them stay.
  - `:114-116` (the canonical description) does not change.
- **`website/enterprise/index.html`:**
  - Head `:6`: `<title>Organization server or single-user Docker — PrivacyFence</title>`. `:7` meta description:
    "Run PrivacyFence as one organization server with identity-provider or built-in Google sign-in, or
    for one person in Docker: the deployment modes side by side, prerequisites, limits." `:20`
    og:description: "One organization server with sign-in through your identity provider or the built-in
    Google sign-in, or Docker for one person. Modes side by side, with prerequisites."
  - `:28` h1: "One gateway for the whole organization, or one for yourself."
  - `:30` lede: replace "on an employee's own computer, or once on a Linux server" with "for one person
    on their own computer, or once on a Linux server". Keep the rest, including F1.
  - `:37` h2 becomes "Desktop mode and organization mode." After the table, add a paragraph with F11.
  - Table header `Local mode` becomes `Desktop mode (deprecated)`.
  - Rows:
    - Sign-in, org cell: "Your OpenID Connect identity provider, or the built-in Google sign-in".
    - Install, org cell: "The <code>privacyfence-org</code> Docker image with Caddy, configured from
      environment variables; or <code>pip install privacyfence</code> under systemd".
    - Passkey step-up, org cell: "On the Admin page; there it can only be made stricter".
    - Add a row "Connector apps": desktop cell "The organization's own apps, in a config bundle"; org
      cell "PrivacyFence's built-in apps, or the organization's own, uploaded on the Admin page".
  - Prerequisite cards:
    - "A Linux server" text: "Docker Engine 24 or newer with the Compose plugin 2.24 or newer. Without
      Docker: Python 3.11 or newer and systemd, as in the deployment guide. Do not install the desktop
      <code>.deb</code> on this host."
    - "A reverse proxy on the same host" becomes "HTTPS in front". Text: "The Docker files bring Caddy,
      which gets the certificate itself. With nginx or another proxy, the deployment guide has the
      settings; PrivacyFence trusts forwarded headers only from the proxy addresses you list."
    - "Your identity provider" text: F8, plus "You name the admins by verified email or by a group
      claim."
    - "Connector apps" text: F2, then "Each person then connects their own account. The server needs
      outbound HTTPS to the services' APIs and, for the built-in apps, to
      <code>oauth.privacyfence.eu</code>."
    - Replace the "The bundle builder" card with "The Admin page" and F6.
  - `:130`: replace "with Caddy or nginx, a hardened systemd unit and a validation checklist" with "with
    Docker and Caddy or with systemd behind your own proxy, and a validation checklist".
  - `:133-134` buttons: primary `/docs/org-mode-docker/` "Docker deployment guide"; secondary
    `/install/` "All install options".
- **`website/how-it-works/index.html`:**
  - `:43` figcaption: unchanged.
  - `:49`: replace its first sentence ("Claude Desktop connects through the PrivacyFence extension …
    which ships with the macOS and Windows installers.") with "In the deprecated desktop mode, Claude
    Desktop connects through the PrivacyFence extension (<code>PrivacyFence.mcpb</code>) that ships with
    the macOS and Windows installers." The second sentence (Claude Code and the token) gets the prefix
    "There, " in front of "Claude Code". After "…with OAuth through your identity provider." add "On the Docker image on your own
    computer, Claude Desktop and Claude Code use <code>http://localhost:8765/mcp</code> and sign in with
    your passkey." [org-mode-docker.md:86-90].
  - `:142-143`: h2 "Try it on your own computer." stays. The text becomes "Run PrivacyFence's Docker
    image on your computer, connect Claude Desktop or Claude Code, and approve your first request."
  - `:146` becomes `<a class="button primary" href="/install/">Install PrivacyFence</a>`.
- **`website/faq/index.html`:**
  - `:58`: replace "<strong>No. There are none.</strong>" with "<strong>No.</strong>". After "…No hosted
    service sits in the data path," the sentence continues as today. Then add F2's last clause as its own
    sentence: "With PrivacyFence's built-in apps, only the sign-in redirect passes through the
    PrivacyFence relay; an organization's own apps never touch it."
  - `:93` (`#modes`):
    - keep the bold sentence, but make it F11's first sentence: "PrivacyFence 7 deprecates desktop mode,
      and PrivacyFence 8 removes it.";
    - then F11's remaining two sentences, linking `/docs/moving-to-organization-mode/`.
  - `:94`: rewrite as "<strong>Single-user mode</strong> runs PrivacyFence's Docker image on one person's
    computer." followed by the rest of F9, then F10. Link `/install/`.
  - `:95`: "Organization mode runs one PrivacyFence on a Linux server your organization controls, from
    environment variables in Docker or under systemd, behind HTTPS." followed by F8, then "administrators
    set the privacy filter and PII check for everyone on the Admin page, and web clients can connect
    too."
  - Add a new FAQ item after `#modes`, `<article class="faq-item page-copy stack" id="built-in-apps">`,
    with h2 "Do I have to register an app with Google, Slack and the others?". Its text is F2, F3 and
    F4, linking `/docs/org-mode-setup-guide/#built-in-apps-through-the-relay`. In the question list at
    `:36-44`, add `<li><a href="#built-in-apps">Do I have to register an app with Google, Slack and the others?</a></li>`
    after the `#modes` line (`:42`).
  - The `#modes` heading "Local or organization mode?" (`:42` in the list and `:92`) becomes
    "Single-user, organization or desktop mode?" in both places. `:97`'s link text stays.
  - `:107` keeps `/download/`. It is about the desktop installers.
- **`website/security/index.html`:**
  - `:41`: replace "On one person's own computer (local mode)" with "On one person's own computer
    (single-user Docker, or the deprecated desktop mode)". Replace the last sentence ("Slack’s built-in
    app sends no PKCE…") with F5.
  - `:56`: replace the last sentence with "On an organization server, administrators turn step-up on
    and make it stricter on the Admin page; in single-user mode it is on from the start."
    [org-mode-docker.md:77-78].
  - `:116`: "compare local mode with a central deployment" becomes "compare single-user Docker with an
    organization server".
  - Add one list item to the limitations list (the section `#what-privacyfence-does-not-claim`, after
    the `:100` item): "<strong>Single-user mode does not protect against a local agent.</strong>" followed
    by F10. It must contain neither "SLA" nor "certif". The section must keep the words the claims test
    needs: "certification", "SLA", "root", "compliant" and "local mode". Those are in the existing items;
    do not remove them.
- **`website/gdpr/index.html`:**
  - `:70` stays; it is already correct.
  - `:68` (Art. 32), PrivacyFence cell: replace "on packaged installs they sit under a separate service
    account where it can't read them" with "in desktop mode they sit under a separate service account
    where it can't read them, and on an organization server the AI client reaches PrivacyFence only
    through its <code>/mcp</code> endpoint. Single-user Docker does not protect them from an AI agent
    that can run <code>docker</code> on the same computer".
  - `:108` (Who can approve?), first sentence becomes "Only a person: in a session signed in through your
    identity provider or the built-in Google sign-in, with the passkey of a single-user install, or
    opened through the companion app in desktop mode." The second sentence stays.
  - Nothing else on the page changes.
- **`website/compare/claude-connectors/index.html`:**
  - `:70` and **`compare/mcp-gateways/index.html:68`**: "An installer on each computer, or one Linux
    server" becomes "Docker on your own computer, or one organization server, from environment
    variables".
  - `compare/claude-connectors:72` (F1) stays.
  - In `mcp-gateways:64`, the PrivacyFence cell becomes "your OpenID Connect identity provider or the
    built-in Google sign-in, and OAuth 2.1, on an organization server".
  - `compare/index.html` changes nothing.
- **`website/ai-agents/index.html`:**
  - `:94` → D3.
  - `:42` (Claude Desktop): replace "<strong>Local mode and organization mode.</strong> On your own
    computer, through PrivacyFence's extension, <code>PrivacyFence.mcpb</code>: no token to copy." with
    "<strong>Your own computer and organization mode.</strong> On your own computer, with the Docker image
    and your passkey, or in the deprecated desktop mode through PrivacyFence's extension,
    <code>PrivacyFence.mcpb</code>." The sentence after it stays.
  - `:47` (Claude Code): replace "<strong>Local mode and organization mode.</strong> On your own
    computer, straight to <code>/mcp</code> over HTTP with your own token." with "<strong>Your own
    computer and organization mode.</strong> On your own computer, straight to <code>/mcp</code>: with
    your passkey on the Docker image, or with your own token in the deprecated desktop mode."
  - `:57` (ChatGPT): "<strong>Local mode and organization mode.</strong>" becomes "<strong>Desktop mode
    (deprecated) and organization mode.</strong>"; the rest stays.
- **`website/ai-agents/claude-code/index.html` and `claude-desktop/index.html`:**
  - Lede: "PrivacyFence on your own computer (<strong>local mode</strong>)" becomes "PrivacyFence on your
    own computer (the Docker image in <strong>single-user mode</strong>, or the deprecated desktop mode)".
    claude-code drops "on macOS, Windows and Linux" from that sentence.
  - In the "How it connects" cards, add a card "Single-user Docker: passkey sign-in": "Use
    <code>http://localhost:8765/mcp</code> as the connector address. PrivacyFence sends you to its
    sign-in page for your passkey, then to a consent page; there is no token to copy."
    [org-mode-docker.md:86-90].
  - The local-mode card's heading becomes "Desktop mode (deprecated): …" (keep the rest of the heading).
  - In "Set it up", add a first card "Single-user Docker" with steps:
    1. "Start PrivacyFence's Docker image as on the <a href="/install/">Install page</a> and set up your
       passkey."
    2. "Add <code>http://localhost:8765/mcp</code> as a connector." For claude-code:
       "<code>claude mcp add --transport http --scope user privacyfence http://localhost:8765/mcp</code>".
    3. "Sign in with your passkey when the client opens the browser."
  - The existing local-mode card's heading becomes "Desktop mode (deprecated)", and its "download page"
    link stays pointing to `/download/` (deprecated installers).
  - The remaining "local mode" paragraphs become "desktop mode": claude-code `:89` "What we observed in
    local mode:" → "What we observed in desktop mode:"; claude-code `:109`, claude-desktop `:102` and
    `:113` "<strong>Local mode.</strong>" → "<strong>Desktop mode.</strong>". Their text stays.
- **`website/ai-agents/chatgpt/index.html`:** `:30` "(<strong>local mode</strong>)" → "(<strong>desktop
  mode</strong>, deprecated)"; `:40` h3 "Local mode: ChatGPT desktop" → "Desktop mode (deprecated):
  ChatGPT desktop"; `:57` h3 "Local mode" → "Desktop mode (deprecated)"; `:103` and `:115`
  "<strong>Local mode.</strong>" → "<strong>Desktop mode.</strong>". The "download page" link stays
  `/download/`. Add nothing about single-user: no doc says ChatGPT works with it.
- **`website/ai-agents/claude-ai/index.html:57` and `gemini-enterprise/index.html:73`:** h3 "Local mode"
  → "On your own computer" (the card's "Not available" text is true of single-user Docker too:
  org-mode-docker.md:87). Nothing else changes.
- **`website/connectors/index.html`:**
  - `:202`'s first sentence becomes "Each person connects their own account on PrivacyFence's Connect
    page, in the browser." The rest of the paragraph stays.
  - `:203` becomes "PrivacyFence ships its own apps for Google, Slack, Salesforce, Atlassian and Miro, so
    an organization server or the Docker image connects them with nothing to register; Telegram needs no
    registration either. An organization may register its own app instead and upload it on the Admin
    page. Each connector's page links its provider's setup guide, and
    <a href="/docs/connecting-a-service/">Connecting a service</a> the steps each person takes."
  - `:216` → D3.
- **Connector pages (`website/connectors/<name>/index.html`).** Each entry below says, for the page's
  "Set it up" `<ol>`, which `<li>` is replaced, which is inserted and which stays, with the final text.
  An `<li>` not named stays as it is.
  - google-workspace:
    - h2 "PrivacyFence's Google app, or your own, then each person signs in."
    - Replace `<li>` 1 ("An administrator creates an OAuth client…") with "On an organization server or
      the Docker image, Google works at once with PrivacyFence's built-in Google app; the sign-in
      redirect goes through the PrivacyFence relay." followed by F4.
    - Insert a new `<li>` 2: "An organization may register its own OAuth client in a Google Cloud
      project instead and upload it on the Admin page. With a Workspace account, an
      <strong>Internal</strong> app needs no Google verification and has no user limit."
    - The old `<li>` 2 ("Each person installs the bundle, then connects…") becomes "Each person connects
      each Google connector on PrivacyFence's Connect page and signs in to Google in the browser."
    - The old `<li>` 3 stays.
  - slack:
    - h2 "PrivacyFence's Slack app, or your own, then each person approves it."
    - `<li>` 1 (`:95`) stays, with "Slack sends no PKCE, so with the built-in app its one-time sign-in
      code passes through the relay without it." appended.
    - `<li>` 2 (`:96`) becomes "For full speed, an organization creates its workspace's own Slack app,
      with user token scopes only, and uploads its client ID and secret on the Admin page as a signed
      connector config; its sign-in does not touch the relay. A desktop install, which has no built-in
      apps, takes that app in its organization config bundle instead." The sentence "That app is never
      publicly distributed." is gone.
    - `<li>` 3 and 4 stay.
    - `:63` stays.
  - jira-confluence:
    - h2 "PrivacyFence's Atlassian app, or your own, then each person approves it."
    - Replace `<li>` 1 with "On an organization server or the Docker image, Jira and Confluence work at
      once with PrivacyFence's built-in Atlassian app; the sign-in redirect goes through the PrivacyFence
      relay."
    - Insert a new `<li>` 2: "An organization may create its own Atlassian OAuth 2.0 (3LO) app instead
      and upload it on the Admin page. A desktop install has no built-in apps and takes its own app in an
      organization config bundle; it cannot share the server's app, because an Atlassian app has a
      single callback URL."
    - The old `<li>` 2 becomes "Each person connects on PrivacyFence's Connect page and approves the app
      in the browser. One sign-in covers both Jira and Confluence."
    - The old `<li>` 3 stays.
  - salesforce:
    - h2 "PrivacyFence's Salesforce app, or your own, then each person signs in."
    - Replace `<li>` 1 with "On an organization server or the Docker image, PrivacyFence's built-in
      Salesforce app needs nothing registered; each Salesforce org's administrator installs the
      PrivacyFence package once, from the link on the Admin page." [CHANGELOG.md:55, salesforce-setup.md:9-26]
    - Insert a new `<li>` 2: "An organization may register its own External Client App or Connected App
      instead and upload it on the Admin page."
    - The old `<li>` 2 becomes "Each person connects Salesforce on PrivacyFence's Connect page and signs
      in on Salesforce's own page. Nobody types a Salesforce password, security token or API key into
      PrivacyFence."
    - The old `<li>` 3 stays.
  - miro:
    - h2 "PrivacyFence's Miro app, or your organization's own."
    - Replace `<li>` 1 with "On an organization server or the Docker image, Miro works at once with
      PrivacyFence's built-in Miro app; the sign-in redirect goes through the PrivacyFence relay. Miro
      sends no PKCE, so that one-time code passes through the relay without it."
    - Insert a new `<li>` 2: "An organization may register its own app in Miro instead and upload it on
      the Admin page; it does not touch the relay."
    - The old `<li>` 2 and 3 stay.
    - The "Set it up" button becomes `/docs/miro-setup/`.
  - grist: there is no built-in app (org-mode-setup-guide.md:350-352).
    - `<li>` 1 stays.
    - `<li>` 2 becomes "If your organization registered its own PrivacyFence app in Grist and uploaded
      it on the Admin page, you sign in on Grist's own page instead. PrivacyFence ships no Grist app."
    - `<li>` 3: "In PrivacyFence's Settings page, enter" becomes "On PrivacyFence's Connect page, enter";
      the rest stays.
    - Add after the list: "With the <a href="/install/">PrivacyFence suite</a>, Grist comes built in as
      the data lake, and each person's Grist key is connected automatically when they sign in (the
      Grist admin address excepted)."
    - The button becomes `/docs/grist-setup/`.
  - peek: no app needed.
    - Add after the list: "With the <a href="/install/">PrivacyFence suite</a>, Peek comes built in as
      the presentation tool, and each person's Peek account is connected automatically when they sign
      in (Peek admins excepted)."
    - `<li>` 2: "In PrivacyFence's Settings page, choose Peek" becomes "On PrivacyFence's Connect page,
      choose Peek"; the rest stays.
    - The button becomes `/docs/peek-setup/`.
  - telegram:
    - `<li>` 1 (`:89`) becomes "Nothing to register: every PrivacyFence installer, the Docker image and
      the PyPI package carry PrivacyFence's Telegram app credentials, which identify the application,
      not you."
    - `<li>` 2 (`:90`): "In PrivacyFence's Settings page, enter" becomes "On PrivacyFence's Connect page,
      enter"; the rest stays.
- **`website/download/index.html`:**
  - Lede `:67-68`: keep, and append "For one person, the recommended way is now
    <a href="#docker-heading">PrivacyFence's Docker image</a>; the installers below set up the deprecated
    desktop mode."
  - Add a new section before the stable section: `<section class="page-section" aria-labelledby="docker-heading">`
    with `<h2 id="docker-heading">Recommended: the Docker image on your computer</h2>`, F9, the
    `standalone-single` command block from D5 as `<pre class="verify-commands"><code>`, F10, and a link "Other ways to install, including the
    suite with Peek and Grist" to `/install/`.
  - `#stable-heading` (`:78`): text becomes "Desktop installers (deprecated)", keep the id, and remove
    its `visually-hidden` class so the heading shows (this is the one class change allowed).
  - `:104`: replace the paragraph with F11, linking `/docs/moving-to-organization-mode/` and `/install/`.
    It must keep "PrivacyFence 8 removes it".
  - Touch no other element id or class, and not the `#release-meta` or `#download-loading` strings, the script tags or
    the JSON-LD.

### D7. llms.txt descriptions

In `llms_txt()`'s "## Website" list (`scripts/build_site.py:1131-1158`), p2 replaces these four
descriptions (the text after the link and colon); every other line stays:

- Enterprise: "an organization server or single-user Docker, side by side with the deprecated desktop
  mode, with prerequisites"
- Claude Desktop: "Claude Desktop with the Docker image and your passkey on your own computer, or as a
  custom connector to an organization deployment"
- Claude Code: "Claude Code with the Docker image and your passkey on your own computer, or with OAuth
  sign-in to an organization deployment"
- Download: "the Docker image recommended for one person, and the deprecated desktop installers for
  macOS, Windows and Linux, each with its SHA-256 checksum"

### D8. User docs

Edit only what is listed; keep every existing heading (its anchor is linked).

- **`docs/org-mode-setup-guide.md:8-9`**: replace the paragraph with "For a single person on their own
  computer, run the Docker image in single-user mode instead
  ([Organization deployment with Docker](org-mode-docker.md#run-it-on-this-computer)). The desktop
  install is deprecated in PrivacyFence 7 and removed in 8
  ([Moving to organization mode](moving-to-organization-mode.md))."
- **`docs/how-it-works.md`, "## Local mode and organization mode" (`:52-63`)**:
  - The **Local mode** bullet starts "**Local mode** (desktop mode) is what the macOS, Windows and Linux
    installers set up; it is deprecated in PrivacyFence 7 and removed in 8
    ([Moving to organization mode](moving-to-organization-mode.md))." The rest of the bullet ("One daemon
    on your own computer…") stays; drop "is the default".
  - Append to the **Organization mode** bullet: "It runs from the `ghcr.io/privacyfence/privacyfence-org`
    Docker image configured by environment variables
    ([Organization deployment with Docker](org-mode-docker.md)), or from PyPI under systemd."
  - Add a third bullet: "- **Single-user mode** is organization mode with no identity provider: one
    administrator, served only on `localhost`, who signs in with a passkey after a one-time setup code.
    It is how the Docker image starts on your own computer
    ([Run it on this computer](org-mode-docker.md#run-it-on-this-computer)). It is no protection against
    an agent that can run `docker` on that computer
    ([Single-user mode](security-and-compliance.md#single-user-mode))."
  - `:62-63` becomes "Which mode a daemon runs in is set by `PRIVACYFENCE_MODE` (`local` by default,
    `org` in the Docker image), or by a legacy organization config bundle's `mode` key. See
    [Configuration reference](configuration-reference.md#environment-variables)."
    [configuration-reference.md:361]
- **`docs/connect-claude-code.md`**:
  - In the opening list, the **Local mode** bullet's label becomes "**Local mode** (desktop mode,
    deprecated)". Add a bullet between the two: "- **Single-user Docker**: PrivacyFence's Docker image on
    the same computer. Claude Code connects to `http://localhost:8765/mcp` and you sign in with your
    passkey."
  - Add a section `## Single-user Docker` directly before `## Organization mode` (`:96`):
    > Start the Docker image on your computer and set up your passkey
    > ([Run it on this computer](org-mode-docker.md#run-it-on-this-computer)). Then:
    >
    > ```bash
    > claude mcp add --transport http --scope user privacyfence http://localhost:8765/mcp
    > ```
    >
    > Run `/mcp` inside Claude Code and choose **privacyfence** to sign in. PrivacyFence opens its
    > sign-in page for your passkey, then a consent page; after that Claude Code holds its own tokens, as
    > with an organization server. Use `localhost`, not `127.0.0.1`: the passkey belongs to
    > `localhost`.

    (Written as plain Markdown, not as a quote.)
- **`docs/connect-claude-desktop.md`**:
  - The same change to the opening list's **Local mode** bullet. The added bullet reads
    "- **Single-user Docker**: PrivacyFence's Docker image on the same computer. Claude Desktop connects
    to `http://localhost:8765/mcp` as a custom connector and you sign in with your passkey."
  - Add `## Single-user Docker` directly before `## Organization mode` (`:39`): "Start the Docker image
    on your computer and set up your passkey
    ([Run it on this computer](org-mode-docker.md#run-it-on-this-computer)). In Claude Desktop's
    connector settings, add a custom connector with the URL `http://localhost:8765/mcp`, click
    **Connect** and sign in with your passkey. Leave the optional OAuth client ID and secret empty:
    Claude Desktop registers itself. Do not install the extension (`PrivacyFence.mcpb`) as well: it is
    for desktop mode only."
- **`docs/org-mode-docker.md:196`**: "such as `7.4`" becomes "such as `7.0`" (7.4 is not a release).

## ADRs

- **0276** — "Links to a docs page the next release publishes fall back to GitHub until it does".
  `build_site.DOCS_PENDING_RELEASE` names published docs that the newest stable tag may not carry; a
  hand-written page's link to one points at GitHub `main` until `/docs/` renders it. Any other missing
  docs page still fails the link check. `oauth-relay.md` is published on `/docs/`. Amends 0099.
- **0277** — "The website's primary call to action is Install". The header's highlighted item is
  **Install** (`/install/`), an interactive picker over the Docker install modes that does not mention
  desktop mode. `/download/` stays for the deprecated desktop installers, off the header.
  - Rejected: a static table, because the user chose the picker.
  - Rejected: redirecting `/download/`, because of release links, the Worker, `pyproject.toml` and the
    `.mcpb` text.
  - Rejected: an Install link next to Download, because one highlighted item is the design.

## Manual steps

There are no `manual_before` steps. `manual_after` has two, both after the last phase merges and
before the PR merges. The step-by-step page is the artifact linked from the manifest
(`manual_steps_artifact`).

- **ma1:** proofread the built pages, the `/install/` picker above all, from the PR's `website-build`
  artifact at phone and desktop widths. The browser tests check structure, not wording.
- **ma2:** on a computer with Docker, start single-user Docker, connect Claude Code and Claude Desktop
  to `http://localhost:8765/mcp`, and approve one request. The website and the D8 docs now promise
  this; no automated test can.

## Risks and open questions

- **An anchor in D2 does not resolve.** The link check prints it. Stop with `status=blocked` and name
  it; do not pick another anchor.
- **Zensical strict mode refuses `docs/oauth-relay.md`.** For example, a link out of the published set
  that the export does not rewrite. `python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/site`
  shows it. Fix only what the error names, in `docs/oauth-relay.md`. If the fix needs more than link
  rewrites, stop with `status=blocked`.
- **The docs extra is missing in the container.** `/docs/` is then not built, the tests skip it, and
  D4's `only=` path is exercised only by its unit test. That is acceptable. Do not report a skipped
  browser test as passed. Read the session-start hook's Chromium `==>` line (steward skill).
- **The homepage CTA count.** If the built homepage has an Install CTA other than the three in D3
  (header, `:82`, `:209`), the CTA test fails. Do not add a fourth.
- **A page section that D6 names is not where the line numbers say** (the lines are from `1ebc614`).
  Find it by its quoted text. If the quoted text is gone, stop with `status=blocked`.
- **The canonical description** (`website/canonical-description.md`, README and `index.html:114-116`)
  still names "macOS, Windows, or Linux". It stays unchanged in this plan; changing it needs README in
  the same PR (`test_website_canonical_description.py`). This is a follow-up for the maintainer, not a
  phase.
- **Single-user Docker with Claude Desktop.** That Claude Desktop reaches `http://localhost:8765/mcp` as a
  custom connector rests on `docs/org-mode-docker.md:86-90`; no automated test covers it.
  `manual_after` ma2 checks it with the real clients before the PR merges. If it fails, the D8 Claude
  Desktop section and the matching website copy (p5) are corrected in the same PR.
- **D8 must not rename a docs heading.** The website links its anchors (D2) and the connect docs link
  `how-it-works.md#local-mode-and-organization-mode`. A renamed heading fails the link check.

## Implementation manifest

```yaml
plan_slug: website-7x
feature_branch: chore/website-7x
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/WTyXo7ZGTwa4GNoNStSQSZ
manual_steps_source: docs/website-7x-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-proofread-built-site
    title: Proofread /install/ (all picker combinations), /download/, /enterprise/, /faq/ and two connector pages in the PR's website-build artifact
    why: The browser tests check structure and layout, not whether the copy reads right and claims only what 7.0.0 does
  - id: ma2-single-user-clients
    title: Connect Claude Code and Claude Desktop to single-user Docker at http://localhost:8765/mcp and approve one request
    why: The website (p5) and the connect docs (p7) now say both clients work with single-user Docker; only a real computer with Docker and the real clients can show it
verify_after_merge:
  - 'python3 -m pytest tests/unit -q -k "website or build_site"'
  - 'python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site-check'
  - 'python3 scripts/build_site.py --offline --out /tmp/pf-site-tag-check'
final_checks:
  - 'docs/website-7x-plan.md and docs/website-7x-plan-manual-steps.html are deleted and nothing links to them (grep -rn "website-7x-plan" . finds nothing outside .git)'
  - 'docs/adr/0276-*.md and docs/adr/0277-*.md exist, are Accepted, and are rows in docs/adr/README.md'
  - 'CHANGELOG.md has the new entries under "## [Unreleased]" and no new version heading'
  - 'grep -rn "never publicly distributed" website/ finds nothing'
  - 'grep -n ''cta_href": "/install/"'' scripts/build_site.py matches'
  - 'grep -c "use the desktop install instead" docs/org-mode-setup-guide.md prints 0'
  - 'python3 -m pytest tests/unit -q -k website passes and ruff check . passes'
phases:
  - id: p1-install-page
    title: The /install/ page with its interactive picker
    depends_on: []
    complexity: M
    touches:
      - 'website/install/index.html'
      - 'website/install/install.js'
      - 'website/styles.css'
      - 'scripts/build_site.py'
      - 'tests/unit/test_website_install_page.py'
      - 'tests/integration/test_install_page.py'
      - '.github/workflows/website-build.yml'
    brief: |
      Read docs/website-7x-plan.md sections D1, D2 and D5 first; every string and command below comes from there.
      1. Create website/install/index.html exactly as D5 "Head" and "Body" describe, copying the structure (header/footer includes, section classes, OG tags) from website/enterprise/index.html. Every command block is copied verbatim from D5.
      2. Create website/install/install.js as D5 "install.js" describes, in the style of website/download/download.js (IIFE, 'use strict', DOMContentLoaded guard as site.js:173 does).
      3. Append the picker styles to website/styles.css under a "/* Install page */" comment, using only existing custom properties; fieldsets as a .grid-auto of cards, radios with their labels and descriptions stacked. No horizontal scroll at 360 px.
      4. In scripts/build_site.py: add "/install/": "install/index.html" to PAGES after "/faq/"; add "install/install.js": "website/install/install.js" to STATIC next to download/download.js; add D5's llms.txt line before the Download line in llms_txt(). Do NOT change PARTIAL_DEFAULTS in this phase (p2 does).
      5. Add tests/unit/test_website_install_page.py (use tests.website_site.built_site like tests/unit/test_website_releases_page.py): the page is in PAGES and the sitemap; llms.txt has "[Install](https://privacyfence.eu/install/)"; the built page has the five radio groups with the names and values of D5; it has one [data-result] per key in D5 (8 keys); the text of its <main> element (only <main>: the header still carries the Download CTA until p2) contains none of "desktop mode", "/download/", ".dmg", ".deb", ".exe", case-insensitive; install.js is deployed.
      6. Add tests/integration/test_install_page.py in the pattern of tests/integration/test_download_page.py (same importorskip, pytestmark, build_site/serve/chromium_launch_kwargs helpers): (a) with JS, #install-picker is visible and exactly one [data-result] is visible, "suite-single" by default; (b) choosing who=org hides the single note and shows the three org fieldsets; (c) a table-driven test over all 8 keys sets the radios and checks the visible result (own+nginx and builtin+other under suite give suite-no-config; standalone+nginx gives standalone-proxy); (d) data-when="signin-builtin" snippet is visible only for org+builtin; (e) with JavaScript disabled (browser.new_context(java_script_enabled=False)) every [data-result] is visible and the form is hidden.
      6b. Add tests/integration/test_install_page.py to the pytest list of the "Website guardrails on the built site" step in .github/workflows/website-build.yml, after tests/integration/test_download_page.py.
      7. Run: python3 -m pytest tests/unit -q -k "website or build_site", python3 -m pytest tests/integration/test_install_page.py tests/integration/test_website_layout.py tests/integration/test_website_csp.py -q, python3 scripts/build_site.py --offline --no-docs --out /tmp/pf-site, ruff check .
      Stop condition: if test_website_layout.py fails on /install/ for horizontal overflow and the fix would need changes outside the "/* Install page */" block, stop with status=blocked.
    acceptance:
      - 'python3 -m pytest tests/unit/test_website_install_page.py -q passes'
      - 'python3 -m pytest tests/integration/test_install_page.py -q passes (not skipped; PRIVACYFENCE_TEST_CHROMIUM set per the session hook)'
      - 'python3 -m pytest tests/unit -q -k "website or build_site" passes'
      - 'python3 -m pytest tests/integration/test_website_layout.py tests/integration/test_website_csp.py -q passes'
      - 'grep -c ''data-result='' website/install/index.html prints 8'
      - 'grep -ci ''desktop mode'' website/install/index.html prints 0'
      - 'grep -c ''tests/integration/test_install_page.py'' .github/workflows/website-build.yml prints 1'
      - 'grep -c ''/tools/connector-config/'' website/install/index.html prints 0'
      - 'ruff check . passes'

  - id: p2-nav-and-relay-docs
    title: Install replaces Download in the header; oauth-relay published with the docs-link fallback
    depends_on: [p1-install-page]
    complexity: M
    touches:
      - 'scripts/build_site.py'
      - 'website/_partials/header.html'
      - 'website/index.html'
      - 'website/relay-terms/index.html'
      - 'website/privacy/index.html'
      - 'docs/README.md'
      - 'tests/unit/test_build_site.py'
      - 'tests/unit/test_website_download_cta.py'
      - 'tests/unit/test_website_docs_allowlist.py'
    brief: |
      Read docs/website-7x-plan.md sections D3 and D4 first.
      1. D3: change PARTIAL_DEFAULTS["header"] in scripts/build_site.py to /install/ and Install; update the note on website/_partials/header.html:3; change website/index.html:82 and :209 to the two Install buttons of D3. Touch no other line of website/index.html (p3 owns the rest).
      1b. In llms_txt() (scripts/build_site.py, the "## Website" list), replace the four descriptions given in the plan's D7, exactly.
      2. Update tests/unit/test_build_site.py: the pinned nav-cta string (:121-127) and the normalization (:179-194) per D3. Update tests/unit/test_website_download_cta.py's CTA pattern, the two homepage CTA tests (renamed per D3) and its docstring sentence; rename test_the_download_page_the_ctas_point_at_exists to test_the_install_page_the_ctas_point_at_exists and make it check website/install/index.html and website/install/install.js; guardrail 7 (manifest completeness) stays as it is.
      3. D4 publishing: remove "oauth-relay.md" from CONTRIBUTOR_DOCS; move its bullet in docs/README.md with D4's wording. tests/unit/test_website_docs_allowlist.py should then pass unchanged; edit it only if a test names oauth-relay explicitly.
      4. D4 fallback: add DOCS_PENDING_RELEASE with its comment, the `only` parameter of point_docs_links_at_github, and the else branch at the call site, exactly as D4 shows.
      5. Add to tests/unit/test_build_site.py, next to test_docs_links_fall_back_to_github_when_docs_are_not_built: test_pending_docs_links_fall_back_when_the_tag_lacks_them (site with docs/index.html and docs/getting-started/index.html but no docs/oauth-relay/; a page linking /docs/, /docs/getting-started/ and /docs/oauth-relay/#x; after point_docs_links_at_github(site, "main", only=frozenset({"oauth-relay"})) only the oauth-relay link is rewritten to .../blob/main/docs/oauth-relay.md#x) and test_pending_docs_links_stay_when_the_tag_has_them (docs/oauth-relay/index.html exists: nothing rewritten); and test_oauth_relay_is_pending_and_published (assert "oauth-relay" in DOCS_PENDING_RELEASE and "oauth-relay.md" not in CONTRIBUTOR_DOCS).
      6. D4 links: website/relay-terms/index.html:71 and the one added sentence in website/privacy/index.html #relay, exactly as D4 words them. Change nothing else on those two pages.
      7. Install the docs generator as the website-build workflow does: python3 -m pip install -q --require-hashes -r requirements/docs.lock.txt. If that fails, stop with status=blocked (every later phase relies on it to check /docs/ anchors). Check the docs render: python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site. Then python3 scripts/build_site.py --offline --out /tmp/pf-site-tag (docs from v7.0.0): it must succeed, and /tmp/pf-site-tag/relay-terms/index.html must link https://github.com/privacyfence/privacyfence/blob/main/docs/oauth-relay.md.
      8. Run python3 -m pytest tests/unit -q -k "website or build_site", the browser tests tests/integration/test_website_layout.py and tests/integration/test_website_consent.py, and ruff check .
      Stop condition: if Zensical strict mode rejects docs/oauth-relay.md for anything other than a link the export should rewrite, stop with status=blocked (plan "Risks").
    acceptance:
      - 'python3 -m pytest tests/unit/test_build_site.py tests/unit/test_website_download_cta.py tests/unit/test_website_docs_allowlist.py -q passes'
      - 'python3 -m pytest tests/unit -q -k "website or build_site" passes'
      - 'grep -n ''cta_href": "/install/", "cta_label": "Install"'' scripts/build_site.py matches'
      - 'grep -n ''"oauth-relay.md"'' scripts/build_site.py finds nothing'
      - 'grep -n ''href="/docs/oauth-relay/"'' website/relay-terms/index.html website/privacy/index.html matches once in each'
      - 'python3 scripts/build_site.py --offline --out /tmp/pf-site-tag succeeds and grep -c ''blob/main/docs/oauth-relay.md'' /tmp/pf-site-tag/relay-terms/index.html prints 1'
      - 'ruff check . passes'

  - id: p3-core-pages
    title: Homepage, Enterprise, How it works, FAQ, Security and GDPR for 7.x
    depends_on: [p2-nav-and-relay-docs]
    complexity: M
    touches:
      - 'website/index.html'
      - 'website/enterprise/index.html'
      - 'website/how-it-works/index.html'
      - 'website/faq/index.html'
      - 'website/security/index.html'
      - 'website/gdpr/index.html'
    brief: |
      Read docs/website-7x-plan.md D1 (facts and copy rules), D2 (docs URLs), D3 (the /download/ → /install/ rule) and D6's entries for these six pages. Make exactly the D6 changes; use the F-sentences verbatim; leave everything D6 does not name.
      1. website/index.html: D6 (:87 and :177 only). Do not touch :82, :209 (done in p2) or the canonical description at :114-116.
      2. website/enterprise/index.html: D6 in full, including the new table row, the replaced bundle card and the two buttons.
      3. website/how-it-works/index.html: D6.
      4. website/faq/index.html: D6, including the new #built-in-apps item. Keep "PrivacyFence 8 removes it" and the /download/ link at :107.
      5. website/security/index.html: D6, including the new limitations item; keep the words the claims test needs in #what-privacyfence-does-not-claim.
      6. website/gdpr/index.html: D6.
      7. Run python3 -m pytest tests/unit -q -k "website or build_site", python3 -m pip install -q --require-hashes -r requirements/docs.lock.txt (if not yet installed), python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site and python3 scripts/build_site.py --offline --out /tmp/pf-site-tag (the link check covers every /docs/ anchor in both) (link check), the browser layout test, and ruff check .
      Stop condition: a quoted D6 target text is not on the page, or a test that pins a sentence on these pages fails for a change D6 asks for — stop with status=blocked and name the test.
    acceptance:
      - 'python3 -m pytest tests/unit -q -k "website or build_site" passes'
      - 'python3 -m pytest tests/integration/test_website_layout.py -q passes'
      - 'grep -c ''id="built-in-apps"'' website/faq/index.html prints 1'
      - 'grep -c "PrivacyFence 8 removes it" website/faq/index.html prints at least 1'
      - 'grep -c "build_org_bundle" website/enterprise/index.html prints 0'
      - 'grep -c ''href="/install/"'' website/enterprise/index.html prints at least 1'
      - 'grep -c "no protection against an AI agent that can run" website/security/index.html website/faq/index.html prints at least 1 for each file (keep that phrase on one line)'
      - 'python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site succeeds'
      - 'python3 scripts/build_site.py --offline --out /tmp/pf-site-tag succeeds'

  - id: p4-connector-pages
    title: Connector pages say PrivacyFence ships its own apps and link the /docs/ guides
    depends_on: [p2-nav-and-relay-docs]
    complexity: M
    touches:
      - 'website/connectors/index.html'
      - 'website/connectors/google-workspace/index.html'
      - 'website/connectors/slack/index.html'
      - 'website/connectors/jira-confluence/index.html'
      - 'website/connectors/salesforce/index.html'
      - 'website/connectors/miro/index.html'
      - 'website/connectors/grist/index.html'
      - 'website/connectors/peek/index.html'
      - 'website/connectors/telegram/index.html'
    brief: |
      Read docs/website-7x-plan.md D1, D2, D3 and D6's "connectors/index.html" and "Connector pages" entries. Make exactly those changes.
      1. website/connectors/index.html: :202, :203 and the /download/ link (D3).
      2. Each connector page: the "Set it up" h2 and steps per D6; the /download/ links per D3; the "Set it up" buttons of miro, grist and peek become /docs/miro-setup/, /docs/grist-setup/ and /docs/peek-setup/.
      3. Slack: remove "That app is never publicly distributed." exactly.
      4. Run python3 -m pytest tests/unit -q -k "website or build_site" (test_website_connector_pages.py accepts /docs/<guide>/ buttons) and python3 -m pip install -q --require-hashes -r requirements/docs.lock.txt (if not yet installed), python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site and python3 scripts/build_site.py --offline --out /tmp/pf-site-tag (the link check covers every /docs/ anchor in both), and ruff check .
      Stop condition: tests/unit/test_website_connectors_page.py or test_website_connector_pages.py fails on a count or a button — stop with status=blocked rather than changing the test.
    acceptance:
      - 'grep -rn "never publicly distributed" website/ finds nothing'
      - 'grep -rln "config bundle" website/connectors/ lists exactly website/connectors/slack/index.html and website/connectors/jira-confluence/index.html'
      - 'python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site succeeds'
      - 'python3 scripts/build_site.py --offline --out /tmp/pf-site-tag succeeds'
      - 'grep -c ''href="/docs/miro-setup/">Set it up'' website/connectors/miro/index.html prints 1, and likewise grist-setup and peek-setup on their pages'
      - 'grep -rln "Admin page" website/connectors/ lists at least google-workspace, slack, jira-confluence, salesforce, miro and index'
      - 'python3 -m pytest tests/unit -q -k "website or build_site" passes'

  - id: p5-agents-compare-download
    title: AI agents and comparison pages, and /download/ recommending single-user Docker
    depends_on: [p2-nav-and-relay-docs]
    complexity: M
    touches:
      - 'website/ai-agents/index.html'
      - 'website/ai-agents/claude-code/index.html'
      - 'website/ai-agents/claude-desktop/index.html'
      - 'website/ai-agents/chatgpt/index.html'
      - 'website/ai-agents/claude-ai/index.html'
      - 'website/ai-agents/gemini-enterprise/index.html'
      - 'website/compare/claude-connectors/index.html'
      - 'website/compare/mcp-gateways/index.html'
      - 'website/download/index.html'
    brief: |
      Read docs/website-7x-plan.md D1, D2, D3, D5 (the standalone-single command block) and D6's entries for ai-agents, compare and download. Make exactly those changes.
      1. ai-agents/index.html, claude-code, claude-desktop: D6 (single-user cards and steps, desktop mode marked deprecated, /download/ → /install/ only where D3 says; the desktop-mode steps keep /download/).
      2. chatgpt, claude-ai, gemini-enterprise: "Local mode" → "Desktop mode (deprecated)"; nothing about single-user.
      3. compare/claude-connectors and compare/mcp-gateways: D6.
      4. download/index.html: D6 — the new #docker-heading section, the lede sentence, the stable heading text and the :104 paragraph. Change no id, class, script tag, JSON-LD, #release-meta or #download-loading string.
      5. Run python3 -m pytest tests/unit -q -k "website or build_site", python3 -m pytest tests/integration/test_download_page.py tests/integration/test_website_layout.py -q, python3 -m pip install -q --require-hashes -r requirements/docs.lock.txt (if not yet installed), python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site and python3 scripts/build_site.py --offline --out /tmp/pf-site-tag (the link check covers every /docs/ anchor in both), and ruff check .
      Stop condition: tests/unit/test_website_agent_pages.py or tests/integration/test_download_page.py fails — stop with status=blocked and name the assertion; do not edit those tests.
    acceptance:
      - 'python3 -m pytest tests/unit -q -k "website or build_site" passes'
      - 'python3 -m pytest tests/integration/test_download_page.py -q passes'
      - 'grep -c ''id="docker-heading"'' website/download/index.html prints 1'
      - 'grep -c "PrivacyFence 8 removes it" website/download/index.html prints at least 1'
      - 'grep -c "localhost:8765/mcp" website/ai-agents/claude-code/index.html website/ai-agents/claude-desktop/index.html prints at least 1 for each file'
      - 'grep -rni "local mode" website/ai-agents/ finds nothing'
      - 'python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site succeeds'
      - 'python3 scripts/build_site.py --offline --out /tmp/pf-site-tag succeeds'

  - id: p6-retire
    title: ADRs 0276 and 0277, changelog, delete the plan
    depends_on: [p3-core-pages, p4-connector-pages, p5-agents-compare-download, p7-user-docs]
    complexity: S
    touches:
      - 'docs/adr/0276-links-to-a-docs-page-the-next-release-publishes-fall-back-to-github.md'
      - 'docs/adr/0277-the-websites-primary-call-to-action-is-install.md'
      - 'docs/adr/README.md'
      - 'docs/adr/0099-an-ai-agents-website-page-lands-after-the-release-that-carries-its-doc.md'
      - 'docs/testing-policy.md'
      - 'CHANGELOG.md'
      - 'docs/website-7x-plan.md'
      - 'docs/website-7x-plan-manual-steps.html'
    brief: |
      1. Write docs/adr/0276-links-to-a-docs-page-the-next-release-publishes-fall-back-to-github.md and docs/adr/0277-the-websites-primary-call-to-action-is-install.md from the plan's "ADRs" section and D3, D4, D5 (decision, the rejected alternatives listed there, consequences, verification naming the tests p1 and p2 added). Use the template in docs/adr/README.md; Status "Accepted — <today>"; 0276 says "Amends ADR 0099". Link source files and PR numbers, never the plan.
      2. Add both rows to the index in docs/adr/README.md after 0275, and append "; amended by 0276" to 0099's status cell. In docs/adr/0099-*.md, add one line to its Status section, "Amended by [ADR 0276](0276-links-to-a-docs-page-the-next-release-publishes-fall-back-to-github.md) for docs listed in `build_site.DOCS_PENDING_RELEASE`.", as docs/adr/README.md:41 describes; change nothing else in it.
      3. In docs/testing-policy.md, where website-build.yml's browser tests are listed (around line 173-174), add tests/integration/test_install_page.py next to test_download_page.py.
      4. Add under CHANGELOG.md's "## [Unreleased]" (create "### Changed" / "### Added" subheadings under it if absent; never a version heading):
         - Added: "**An Install page on privacyfence.eu.** [privacyfence.eu/install](https://privacyfence.eu/install/) asks who uses PrivacyFence, whether it comes with the built-in presentation tool (Peek) and data lake (Grist), how people sign in, which apps connect the services and what sits in front, and gives the commands for that setup. **Install** replaces Download in the site's header ([ADR 0277](docs/adr/0277-the-websites-primary-call-to-action-is-install.md))."
         - Changed: "**privacyfence.eu describes PrivacyFence 7.** The homepage, Enterprise, How it works, FAQ, Security, GDPR, comparison, AI agent and connector pages describe the built-in sign-in apps (and an organization's own), the Admin page, the organization server started from environment variables in Docker, single-user Docker and its limits, and desktop mode's deprecation. The [download page](https://privacyfence.eu/download/) recommends single-user Docker for one person and keeps the desktop installers as deprecated."
         - Changed: "**Docs for single-user Docker.** The organization guide sends a single person to the Docker image instead of the deprecated desktop install, How it works describes single-user mode and `PRIVACYFENCE_MODE`, and [Connect Claude Code](https://privacyfence.eu/docs/connect-claude-code/) and [Connect Claude Desktop](https://privacyfence.eu/docs/connect-claude-desktop/) have a Single-user Docker section."
         - Changed: "**The OAuth relay documentation is on privacyfence.eu.** `docs/oauth-relay.md` is published at [privacyfence.eu/docs/oauth-relay](https://privacyfence.eu/docs/oauth-relay/) from the next release, and the relay terms and privacy statement link it; until then the link opens the file on GitHub ([ADR 0276](docs/adr/0276-links-to-a-docs-page-the-next-release-publishes-fall-back-to-github.md))."
      5. git rm docs/website-7x-plan.md docs/website-7x-plan-manual-steps.html; grep -rn "website-7x-plan" . --exclude-dir=.git must find nothing.
      6. Run python3 -m pytest tests/unit -q and ruff check .
    acceptance:
      - 'test -f docs/adr/0276-links-to-a-docs-page-the-next-release-publishes-fall-back-to-github.md && test -f docs/adr/0277-the-websites-primary-call-to-action-is-install.md'
      - 'grep -c "0276\|0277" docs/adr/README.md prints at least 2'
      - 'test ! -e docs/website-7x-plan.md && test ! -e docs/website-7x-plan-manual-steps.html'
      - 'grep -rn "website-7x-plan" . --exclude-dir=.git finds nothing'
      - 'python3 -m pytest tests/unit -q passes'
      - 'ruff check . passes'

  - id: p7-user-docs
    title: User docs point a single person at single-user Docker
    depends_on: []
    complexity: S
    touches:
      - 'docs/org-mode-setup-guide.md'
      - 'docs/how-it-works.md'
      - 'docs/connect-claude-code.md'
      - 'docs/connect-claude-desktop.md'
      - 'docs/org-mode-docker.md'
    brief: |
      Read docs/website-7x-plan.md section D8 and make exactly those edits, with the wording given there. Rename no heading: the website and other docs link their anchors.
      1. docs/org-mode-setup-guide.md:8-9: the replacement paragraph.
      2. docs/how-it-works.md "## Local mode and organization mode": the three bullets and the closing paragraph.
      3. docs/connect-claude-code.md: the opening-list changes and the new "## Single-user Docker" section before "## Organization mode".
      4. docs/connect-claude-desktop.md: the same, with D8's Claude Desktop text.
      5. docs/org-mode-docker.md:196: 7.4 becomes 7.0.
      6. Install the docs generator (python3 -m pip install -q --require-hashes -r requirements/docs.lock.txt), then run python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site (Zensical strict mode plus the link check), python3 -m pytest tests/unit -q -k "website or docs", and ruff check .
      Stop condition: if the docs build reports a broken link or anchor caused by these edits that is not fixed by correcting a typo in them, stop with status=blocked.
    acceptance:
      - 'grep -c "use the desktop install instead" docs/org-mode-setup-guide.md prints 0'
      - 'grep -c "^## Single-user Docker" docs/connect-claude-code.md docs/connect-claude-desktop.md prints 1 for each file'
      - 'grep -c "^## Local mode and organization mode" docs/how-it-works.md prints 1'
      - 'grep -c "PRIVACYFENCE_MODE" docs/how-it-works.md prints at least 1'
      - 'grep -c "such as `7.4`" docs/org-mode-docker.md prints 0'
      - 'python3 scripts/build_site.py --docs-ref worktree --offline --out /tmp/pf-site succeeds'
      - 'python3 -m pytest tests/unit -q -k "website or docs" passes'
      - 'ruff check . passes'
```
