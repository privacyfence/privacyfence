/**
 * Builds the release history table (/releases/) from the download Worker's `GET /api/releases`,
 * which returns the newest manifest on every channel. Nothing about a release is hardcoded here:
 * versions, dates, filenames, sizes, checksums and which platforms exist all come from the
 * manifests, so a new release needs no website change. See docs/downloads-and-release-kpi.md.
 *
 * scripts/build_site.py renders the same rows at deploy time (render_release_rows), so the page
 * works without JavaScript; this replaces them with the live list. Only installers are presented
 * as downloads, and every download link points at downloads.privacyfence.eu, pinned to its row's
 * exact version -- never at R2 or GitHub: the Worker is the only public path to the artifacts,
 * and it is what counts installer downloads.
 */
(() => {
  const API = 'https://downloads.privacyfence.eu';
  const REPO = 'https://github.com/privacyfence/privacyfence';

  // Display names per artifact id: the same map as website/download/download.js and
  // scripts/build_site.py's PLATFORMS (tests/unit/test_build_site.py keeps the three in step).
  // An id missing here still renders, under the id itself.
  const PLATFORMS = {
    'macos-arm64': { name: 'macOS', detail: 'Apple silicon · installer + Claude extension' },
    'windows-x64': { name: 'Windows', detail: '64-bit' },
    'linux-x64': { name: 'Linux', detail: 'Debian / Ubuntu, 64-bit' },
  };

  // The channels /api/releases reports, with their display names. Mirrors build_site.py's
  // CHANNEL_NAMES.
  const CHANNEL_NAMES = { stable: 'Stable', rc: 'Release candidate', beta: 'Beta', alpha: 'Alpha' };

  // The version order download.js uses (major.minor.patch, then stable > rc > beta > alpha, then
  // stage number), for a list rather than a single pick. A version that does not parse -- a
  // between-tags dev build, say -- drops that channel instead of breaking the table.
  const VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$/;
  const STAGE_RANK = { a: 0, b: 1, rc: 2 };

  function versionKey(version) {
    const match = VERSION_RE.exec(String(version || '').trim());
    if (!match) return null;
    const [, major, minor, patch, stage, stageNum] = match;
    return [Number(major), Number(minor), Number(patch), stage ? STAGE_RANK[stage] : 3, stage ? Number(stageNum) : 0];
  }

  function compareKeys(a, b) {
    for (let i = 0; i < a.length; i += 1) {
      if (a[i] !== b[i]) return a[i] - b[i];
    }
    return 0;
  }

  /** The releases to list, newest first: one per channel with a published manifest that has at
   * least one installer. */
  function publishedReleases(data) {
    const channels = (data && data.channels) || {};
    const releases = [];
    for (const channel of Object.keys(CHANNEL_NAMES)) {
      const manifest = channels[channel];
      if (!manifest || !versionKey(manifest.version)) continue;
      const installers = (manifest.artifacts || []).filter((artifact) => artifact && artifact.kind === 'installer');
      if (installers.length) releases.push({ ...manifest, channel: manifest.channel || channel, artifacts: installers });
    }
    return releases.sort((a, b) => compareKeys(versionKey(b.version), versionKey(a.version)));
  }

  function formatSize(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return '';
    const mb = bytes / (1024 * 1024);
    return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
  }

  function element(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    Object.assign(node, props);
    node.append(...children);
    return node;
  }

  function installerItem(artifact, release) {
    const name = (PLATFORMS[artifact.id] || { name: artifact.id }).name;
    const link = element('a', {
      className: 'release-download',
      href: `${API}/download/version/${encodeURIComponent(release.version)}/${encodeURIComponent(artifact.id)}`,
      textContent: name,
    });
    link.setAttribute('aria-label', `Download ${name} ${release.version}: ${artifact.filename}`);
    // Which platform and channel people pick, for GA4 (site.js). A no-op unless the visitor
    // accepted analytics; the Worker's cookieless counter stays the full count either way.
    link.addEventListener('click', () => {
      window.pfAnalytics?.event('download_click', {
        platform: artifact.platform || artifact.id,
        architecture: artifact.architecture || '',
        channel: release.channel,
      });
    });
    const item = element('li', {}, link);
    const size = formatSize(artifact.size);
    if (size) item.append(' ', element('span', { className: 'release-size', textContent: size }));
    if (artifact.sha256) {
      item.append(
        element(
          'details',
          { className: 'download-checksum' },
          element('summary', { textContent: 'SHA-256' }),
          element('code', { textContent: artifact.sha256 }),
        ),
      );
    }
    return item;
  }

  function releaseRow(release, stable) {
    const version = element('th', { scope: 'row', textContent: release.version });
    if (release === stable) version.append(' ', element('span', { className: 'download-badge', textContent: 'Current' }));

    const channel = element('td', { textContent: CHANNEL_NAMES[release.channel] || release.channel });
    if (stable && release.channel !== 'stable' && compareKeys(versionKey(release.version), versionKey(stable.version)) < 0) {
      channel.append(element('span', { className: 'release-superseded', textContent: `Superseded by ${stable.version}` }));
    }

    const published = String(release.published_at || '').slice(0, 10);
    const date = element('td');
    if (published) date.append(element('time', { dateTime: published, textContent: published }));

    const installers = element('ul', { className: 'release-installers' });
    installers.append(...release.artifacts.map((artifact) => installerItem(artifact, release)));

    const notes = element('td', {}, element('a', { href: `${REPO}/releases/tag/v${release.version}`, textContent: 'Release notes' }));

    const row = element('tr', {}, version, channel, date, element('td', {}, installers), notes);
    row.dataset.channel = release.channel;
    return row;
  }

  function render(data) {
    const releases = publishedReleases(data);
    // A list with nothing published is treated like an outage: rows the build rendered stay.
    if (!releases.length) throw new Error('no published release');
    const stable = releases.find((release) => release.channel === 'stable') || null;
    document.getElementById('releases-body')?.replaceChildren(...releases.map((release) => releaseRow(release, stable)));
  }

  function showFallback() {
    const loading = document.getElementById('releases-loading');
    // Rows the build pre-rendered still work: their links go to the Worker, not to this fetch.
    // Only an empty table needs the way out to GitHub Releases.
    if (!loading) return;
    document.getElementById('releases-table')?.setAttribute('hidden', '');
    const fallback = document.getElementById('releases-fallback');
    if (fallback) fallback.hidden = false;
  }

  fetch(`${API}/api/releases`, { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error(`/api/releases responded ${response.status}`);
      return response.json();
    })
    .then(render)
    .catch(showFallback);
})();
