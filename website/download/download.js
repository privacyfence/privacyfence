/**
 * Builds the download page from the release manifests the Cloudflare Worker serves.
 *
 * Nothing about a release is hardcoded here -- filenames, sizes, checksums and which platforms
 * exist all come from `GET /api/releases/<channel>`, so shipping a new build (or adding a
 * platform) needs no website change. See docs/release-publishing-kpi-plan.md Phase 5.
 *
 * Every download button points at downloads.privacyfence.eu, never at R2 or GitHub: the Worker is
 * the only public path to the artifacts, and it is what counts real installer downloads.
 */
(() => {
  const API = 'https://downloads.privacyfence.eu';

  // Display names for the artifact ids the manifest uses. An id missing from this map still
  // renders -- falling back to the id itself -- because a manifest that gains a platform should
  // show it rather than silently drop it while waiting for a website deploy.
  const PLATFORMS = {
    'macos-arm64': { name: 'macOS', detail: 'Apple silicon', match: /mac/i },
    'windows-x64': { name: 'Windows', detail: '64-bit', match: /win/i },
    'linux-x64': { name: 'Linux', detail: 'Debian / Ubuntu, 64-bit', match: /linux/i },
  };

  // Which pre-release channel to offer testers, most production-ready first. A release candidate
  // is a safer thing to hand someone than an alpha, so the page offers whichever mature channel
  // actually has a release rather than assuming "pre-release" means "beta" -- this project has
  // shipped only alphas so far, and hardcoding beta left the section permanently hidden with a
  // perfectly good build published one channel over.
  const PRERELEASE_CHANNELS = ['rc', 'beta', 'alpha'];

  const numberFormat = new Intl.NumberFormat('en');

  function formatSize(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '';
    const mb = bytes / (1024 * 1024);
    return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
  }

  /**
   * Which platform this visitor is probably on. Used only to *highlight* one card -- never to
   * hide the others, since guessing wrong must not hide the download someone actually needs
   * (a Mac user downloading the Windows installer for a colleague is an ordinary thing to do).
   */
  function detectPlatformId() {
    const hint = navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || '';
    for (const [id, spec] of Object.entries(PLATFORMS)) {
      if (spec.match.test(hint)) return id;
    }
    return null;
  }

  function releaseNotesUrl(version) {
    return `https://github.com/privacyfence/privacyfence/releases/tag/v${version}`;
  }

  function buildCard(artifact, channel, recommendedId) {
    const spec = PLATFORMS[artifact.id] || { name: artifact.id, detail: '' };
    const card = document.createElement('article');
    card.className = 'download-card';
    card.dataset.artifactId = artifact.id;

    if (artifact.id === recommendedId) {
      card.classList.add('recommended');
      const badge = document.createElement('span');
      badge.className = 'download-badge';
      badge.textContent = 'Looks like your system';
      card.append(badge);
    }

    const heading = document.createElement('h3');
    heading.textContent = spec.name;
    card.append(heading);

    if (spec.detail) {
      const detail = document.createElement('p');
      detail.className = 'download-detail';
      detail.textContent = spec.detail;
      card.append(detail);
    }

    const link = document.createElement('a');
    link.className = 'button primary download-button';
    link.href = `${API}/download/${channel}/${artifact.id}`;
    link.textContent = 'Download';
    // The filename is the manifest's, not a guess -- and naming it in the accessible label means
    // a screen-reader user knows what they are about to get, not just "Download".
    link.setAttribute('aria-label', `Download ${spec.name}: ${artifact.filename}`);
    card.append(link);

    const meta = document.createElement('p');
    meta.className = 'download-meta';
    const size = formatSize(artifact.size);
    meta.textContent = size ? `${artifact.filename} · ${size}` : artifact.filename;
    card.append(meta);

    if (artifact.sha256) {
      const checksum = document.createElement('details');
      checksum.className = 'download-checksum';
      const summary = document.createElement('summary');
      summary.textContent = 'SHA-256';
      const value = document.createElement('code');
      value.textContent = artifact.sha256;
      checksum.append(summary, value);
      card.append(checksum);
    }

    return card;
  }

  function renderStable(manifest) {
    const grid = document.getElementById('download-grid');
    const loading = document.getElementById('download-loading');
    if (loading) loading.remove();
    if (!grid) return;

    const recommendedId = detectPlatformId();
    for (const artifact of manifest.artifacts || []) {
      grid.append(buildCard(artifact, manifest.channel, recommendedId));
    }

    const meta = document.getElementById('release-meta');
    if (meta && manifest.version) {
      const version = document.createElement('span');
      version.textContent = `Version ${manifest.version}`;
      meta.append(version);

      const notes = document.createElement('span');
      const link = document.createElement('a');
      link.className = 'text-link';
      link.href = releaseNotesUrl(manifest.version);
      link.textContent = 'Release notes';
      notes.append(link);
      meta.append(notes);
    }
  }

  function renderPreRelease(manifest) {
    const block = document.getElementById('prerelease-block');
    const grid = document.getElementById('prerelease-grid');
    if (!block || !grid || !manifest || !(manifest.artifacts || []).length) return;

    const summary = document.getElementById('prerelease-summary');
    if (summary) {
      // Names the channel from the manifest rather than the page, so an alpha is called an alpha.
      summary.textContent = `${manifest.version} is available on the ${manifest.channel} channel.`;
    }
    for (const artifact of manifest.artifacts) {
      grid.append(buildCard(artifact, manifest.channel, null));
    }
    block.hidden = false;
  }

  function showFallback() {
    const loading = document.getElementById('download-loading');
    if (loading) loading.remove();
    const fallback = document.getElementById('download-fallback');
    if (fallback) fallback.hidden = false;
  }

  async function fetchJson(path) {
    const response = await fetch(`${API}${path}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${path} responded ${response.status}`);
    return response.json();
  }

  // Stable is the only fetch whose failure the visitor needs to hear about; it is what the page
  // exists to deliver.
  fetchJson('/api/releases/stable').then(renderStable).catch(showFallback);

  /** The first pre-release channel with something published, or null if none has anything. */
  async function firstPublishedPreRelease() {
    for (const channel of PRERELEASE_CHANNELS) {
      try {
        return await fetchJson(`/api/releases/${channel}`);
      } catch {
        // 404 means nothing is published on that channel, which is a normal state rather than an
        // error -- try the next one down.
      }
    }
    return null;
  }

  firstPublishedPreRelease()
    .then(renderPreRelease)
    .catch(() => {});

  // Download counts are enhancement-only: a stats outage must never affect the downloads
  // themselves, so this failing leaves the line hidden and nothing else changes.
  fetchJson('/api/stats/downloads')
    .then((stats) => {
      const line = document.getElementById('download-stats');
      if (!line || !stats || !stats.total) return;
      // Not "through this page": the counter records every installer download the Worker serves,
      // whatever sent the visitor there, so claiming the page's own credit would overstate it.
      line.textContent = `${numberFormat.format(stats.total)} installers downloaded so far.`;
      line.hidden = false;
    })
    .catch(() => {});
})();
