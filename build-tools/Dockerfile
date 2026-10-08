FROM moby/buildkit:v0.32.2 AS buildkit
FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home builder \
    && mkdir /workspace && chown 1000:1000 /workspace
COPY --from=buildkit /usr/bin/buildctl /usr/local/bin/buildctl
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 1000:1000
WORKDIR /workspace
# The versioned runner comes from the chart's immutable ConfigMap.
ENTRYPOINT ["python", "/runner/runner.py"]
