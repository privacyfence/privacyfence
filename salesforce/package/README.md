# PrivacyFence Salesforce package

The source of the managed package `PrivacyFence` (namespace `privacyfence`), which carries
PrivacyFence's built-in Salesforce app: a **Packaged** External Client App. Each customer org
installs the package once; its users then connect Salesforce from PrivacyFence's `/connect` page.

| Path | What it is |
|---|---|
| `sfdx-project.json` | The Salesforce DX project: namespace, package id and version aliases |
| `force-app/main/default/externalClientApps/PrivacyFence.eca-meta.xml` | The app: label, description, contact, info URL, logo, icon, distribution state Packaged |
| `force-app/main/default/extlClntAppOauthSettings/PrivacyFence_oauth.ecaOauth-meta.xml` | Its OAuth settings: the `Api` and `RefreshToken` scopes, and the link to the global OAuth settings |
| `unpackaged/extlClntAppOauthPolicies/PrivacyFence_oauthPlcy.ecaOauthPlcy-meta.xml` | The Dev Hub's own OAuth policies, kept for reference only. Salesforce does not package policies: each installing org sets its own |
| `config/project-scratch-def.json`, `.forceignore` | Salesforce DX template files |

## What is not here

The app's **global OAuth settings** (its consumer key and secret, and the callback
`https://oauth.privacyfence.eu/callback/salesforce`) live only in the Dev Hub org
(`apps@privacyfence.eu.devhub`). They are never retrieved or committed. The consumer key and secret
reach the build only as the repository secrets `PF_APP_SALESFORCE_CLIENT_ID` and
`PF_APP_SALESFORCE_CLIENT_SECRET`.

## Building a new version

With the Salesforce CLI, signed in to the Dev Hub as `pf-devhub`:

```bash
cd salesforce/package
# change the app in the Dev Hub's Setup first, then bring the change here:
sf project retrieve start --target-org pf-devhub --metadata \
  ExternalClientApplication:PrivacyFence \
  ExtlClntAppOauthSettings:PrivacyFence_oauth
sf package version create --package PrivacyFence --installation-key-bypass --code-coverage \
  --wait 30 --target-dev-hub pf-devhub
sf package version promote --package 04t... --target-dev-hub pf-devhub
```

A promoted version's install URL is
`https://login.salesforce.com/packaging/installPackage.apexp?p0=<04t version id>`. Keep it in the
repository variable `PF_APP_SALESFORCE_INSTALL_URL`, and commit the changed files and
`sfdx-project.json`'s new alias.
