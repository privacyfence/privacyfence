(() => {
  const format = new Intl.NumberFormat('en');

  // Installer downloads now come from two places and the headline number is their sum:
  // GitHub Releases (counted by scripts/release_stats.py into release-stats.json) and
  // downloads.privacyfence.eu (counted in D1 by the Worker). See
  // docs/release-publishing-kpi-plan.md Phase 5.
  //
  // Only the GitHub half is required. The Cloudflare half resolves to 0 if its API is
  // unreachable, so a Worker outage understates the total for a while rather than hiding the
  // social proof entirely or blocking the page -- the same enhancement-only posture the whole
  // file already takes.
  const cloudflareDownloads = fetch('https://downloads.privacyfence.eu/api/stats/downloads', { cache: 'no-store' })
    .then((response) => (response.ok ? response.json() : null))
    .then((stats) => (stats && Number.isFinite(stats.total) ? stats.total : 0))
    .catch(() => 0);

  Promise.all([
    fetch('./release-stats.json', { cache: 'no-store' }).then((response) => {
      if (!response.ok) throw new Error('stats unavailable');
      return response.json();
    }),
    cloudflareDownloads,
  ])
    .then(([data, cloudflareInstallerDownloads]) => {
      const releaseMeta = document.getElementById('release-meta');
      if (releaseMeta && data.latest_release) {
        const version = document.createElement('span');
        version.textContent = `Latest ${data.latest_release}`;
        releaseMeta.prepend(version);
      }

      // Social proof should help, not advertise an empty launch.
      // Show the counters only once they are meaningful.
      const githubInstallerDownloads = data.github_installer_downloads || 0;
      const totalDownloads = githubInstallerDownloads + cloudflareInstallerDownloads;
      if (totalDownloads >= 50 || (data.stars || 0) >= 10) {
        document.getElementById('download-count').textContent = format.format(totalDownloads);
        document.getElementById('star-count').textContent = format.format(data.stars || 0);
        document.getElementById('stats').hidden = false;
      }
    })
    .catch(() => {
      // Statistics are enhancement-only. The site remains fully functional
      // if metadata generation or loading fails.
    });
})();
