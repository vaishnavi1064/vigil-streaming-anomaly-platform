# The vigil application image: the API, the detector, the reconciler and both storage sinks.
#
# One image for all of them rather than five, because they are five entry points into one
# package that shares a wire format, a settings loader and a ledger. Five images would mean
# five chances for those to drift, and the thing this platform claims is that they cannot.
# Which process runs is decided by the container's command, which is what the Kubernetes
# manifests in k8s/ set.
#
# What is deliberately NOT here: the `foundation` extra (torch plus chronos, several GB) and
# the `train` extra. The foundation-model detector runs on the reference laptop and on the
# GPU host, not in this image, and baking multi-gigabyte model dependencies into the image
# that serves the dashboard would make every rollout pay for them. A detector deployment that
# wants Chronos needs a derived image; `k8s/` runs the z-score baseline, which is what the
# hot path uses anyway (ADR-017).

FROM python:3.12-slim AS base

# librdkafka is a C library confluent-kafka links against. The wheel bundles it on most
# platforms, but the build tools are needed for anything that falls back to a source build,
# and curl is here for the container's own health probe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies before source, so a code change does not invalidate the dependency layer.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[lake]"

# The entry points are scripts at the repo root rather than console_scripts, so they are
# copied explicitly. Listed one by one rather than `COPY . .` so that a stray artifacts/ or
# .venv/ on the build host cannot silently end up in the image.
COPY detector.py reconciler.py warehouse.py lake.py loadgen.py mqtt_bridge.py ./
COPY agent_quality.py demo.py ./
COPY runbooks/ ./runbooks/

# Non-root. The API binds 8000 and nothing here needs a privileged port.
RUN useradd --create-home --uid 10001 vigil \
 && chown -R vigil:vigil /app
USER 10001

EXPOSE 8000

# Overridden by every Kubernetes workload; this default makes `docker run` do the useful
# thing rather than nothing.
CMD ["python", "-m", "uvicorn", "vigil.api:app", "--host", "0.0.0.0", "--port", "8000"]
