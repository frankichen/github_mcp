FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 tar zstd \
    && rm -rf /var/lib/apt/lists/*

COPY scripts /opt/private-ci/scripts
ENV PYTHONPATH=/opt/private-ci

ENTRYPOINT ["python3"]
