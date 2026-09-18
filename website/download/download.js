/**
 * Builds the download page from the release manifests the Cloudflare Worker serves.
 *
 * Nothing about a release is hardcoded here -- filenames, sizes, checksums and which platforms
 * exist all come from `GET /api/releases/<channel>`, so shipping a new build (or adding a
 * platform) needs no website change. See docs/downloads-and-release-kpi.md.
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

  // Every pre-release channel the Worker knows about. Which one actually gets offered is decided
  // by version, not by a fixed channel-name priority -- see pickPreRelease below for why "rc beats
  // beta beats alpha regardless of version" broke as soon as more than one release cycle existed:
  // an already-shipped cycle's rc/beta manifests don't get cleared out when a new cycle starts, so
  // a fixed priority kept surfacing a stale, already-superseded rc over a genuinely newer alpha.
  const PRERELEASE_CHANNELS = ['rc', 'beta', 'alpha'];

  // Mirrors cloudflare/downloads/src/channel.ts's VERSION_RE/STAGE_TO_CHANNEL -- kept in sync by
  // hand for the same reason that module gives for duplicating scripts/r2_release.py's own copy:
  // this is a separate deployable (a static site, not the Worker bundle) with nothing to import
  // this from.
  const VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?(?:\+.*)?$/;
  const STAGE_RANK = { a: 0, b: 1, rc: 2 };

  /** Parses a manifest version into comparable parts, or null if it doesn't look like one --
   * a manifest this page fetched should always parse, but a null here just drops that channel
   * from consideration rather than throwing and blanking the whole pre-release section. */
  function parseVersion(version) {
    const match = VERSION_RE.exec(String(version || '').trim());
    if (!match || match[6] !== undefined) return null; // no match, or a between-tags dev build
    const [, major, minor, patch, stage, stageNum] = match;
    return {
      major: Number(major), minor: Number(minor), patch: Number(patch),
      // No stage (a stable version) outranks every pre-release of the same major.minor.patch --
      // never actually reachable through PRERELEASE_CHANNELS, but keeps this comparator correct
      // on its own terms rather than relying on the caller to never pass it a stable version.
      stageRank: stage ? STAGE_RANK[stage] : 3,
      stageNum: stage ? Number(stageNum) : 0,
    };
  }

  /** True if version `a` is newer than version `b`: major.minor.patch first, then stage maturity
   * (rc > beta > alpha), then stage number -- so a 4.1.0 alpha correctly outranks a 4.0.0 rc left
   * over from an already-shipped cycle, and within one cycle an rc still outranks an earlier beta. */
  function isNewerVersion(a, b) {
    if (a.major !== b.major) return a.major > b.major;
    if (a.minor !== b.minor) return a.minor > b.minor;
    if (a.patch !== b.patch) return a.patch > b.patch;
    if (a.stageRank !== b.stageRank) return a.stageRank > b.stageRank;
    return a.stageNum > b.stageNum;
  }

  /** The published pre-release manifest with the highest actual version, or null if `manifests`
   * (one settled fetch result per PRERELEASE_CHANNELS entry, same order, null for an unpublished
   * channel) has nothing published. */
  function pickPreRelease(manifests) {
    let best = null;
    let bestVersion = null;
    for (const manifest of manifests) {
      if (!manifest) continue;
      const version = parseVersion(manifest.version);
      if (!version) continue;
      if (!best || isNewerVersion(version, bestVersion)) {
        best = manifest;
        bestVersion = version;
      }
    }
    return best;
  }

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

  /** The newest published pre-release across every channel, or null if none has anything.
   * Fetches every channel (a 404 just means "unpublished", not an error -- see pickPreRelease)
   * rather than stopping at the first one that answers, so a channel further down
   * PRERELEASE_CHANNELS can still win on version. */
  async function newestPublishedPreRelease() {
    const manifests = await Promise.all(
      PRERELEASE_CHANNELS.map((channel) => fetchJson(`/api/releases/${channel}`).catch(() => null)),
    );
    return pickPreRelease(manifests);
  }

  newestPublishedPreRelease()
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
