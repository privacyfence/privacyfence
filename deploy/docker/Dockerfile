# syntax=docker/dockerfile:1
# The organization-mode image (docs/org-mode-docker.md). Built only through scripts/build_org_image.sh,
# which stages this directory's files, requirements/runtime.lock.txt and exactly one privacyfence
# wheel into the build context.
FROM python:3.12-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

ARG PRIVACYFENCE_VERSION
LABEL org.opencontainers.image.title="PrivacyFence (organization mode)" \
      org.opencontainers.image.description="PrivacyFence's central organization-mode daemon" \
      org.opencontainers.image.source="https://github.com/privacyfence/privacyfence" \
      org.opencontainers.image.url="https://privacyfence.eu" \
      org.opencontainers.image.documentation="https://github.com/privacyfence/privacyfence/blob/main/docs/org-mode-docker.md" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${PRIVACYFENCE_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PRIVACYFENCE_MODE=org \
    PRIVACYFENCE_BIND_HOST=0.0.0.0 \
    PATH=/opt/privacyfence/venv/bin:$PATH

COPY runtime.lock.txt /tmp/build/runtime.lock.txt
COPY dist/ /tmp/build/dist/
# pip_cert is optional: only a build behind a TLS-intercepting proxy passes one.
RUN --mount=type=secret,id=pip_cert,required=false \
    if [ -f /run/secrets/pip_cert ]; then export PIP_CERT=/run/secrets/pip_cert; fi \
 && python -m venv /opt/privacyfence/venv \
 && pip install --require-hashes -r /tmp/build/runtime.lock.txt \
 && pip install --no-deps /tmp/build/dist/privacyfence-*.whl \
 && rm -rf /tmp/build

COPY --chmod=0755 docker-entrypoint.sh /opt/privacyfence/docker-entrypoint.sh
COPY --chmod=0644 healthcheck.py /opt/privacyfence/healthcheck.py

RUN groupadd --system --gid 10001 privacyfence-org \
 && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/privacyfence-org --create-home \
      --shell /usr/sbin/nologin privacyfence-org \
 && install -d -m 700 -o 10001 -g 10001 /var/lib/privacyfence-org/.privacyfence \
      /var/lib/privacyfence-org/.privacyfence/org

USER 10001:10001
WORKDIR /var/lib/privacyfence-org
VOLUME ["/var/lib/privacyfence-org/.privacyfence"]
EXPOSE 8765
# The daemon shuts down cleanly on SIGINT (KeyboardInterrupt); as PID 1 it would ignore SIGTERM.
STOPSIGNAL SIGINT
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "/opt/privacyfence/healthcheck.py"]
ENTRYPOINT ["/opt/privacyfence/docker-entrypoint.sh"]
