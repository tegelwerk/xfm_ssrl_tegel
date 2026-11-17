# Image for the SSRL XFM data server and the Tegel pipeline workers (staging, and the
# OME-Zarr ETL via xfm-etl).
# Build context: the directory that contains both checkouts, tegel/ and xfm_ssrl_tegel/
#   docker build -f xfm_ssrl_tegel/Containerfile -t tegel-dataserver:local .
# Only package metadata and src/ are copied, so no ignore file is needed (the context
# directory belongs to neither repository). Third-party versions are pinned by
# deploy/constraints.txt, exported from uv.lock.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_HTTP_TIMEOUT=300
RUN pip install --no-cache-dir uv hatchling

WORKDIR /src
COPY tegel/tegel-core/pyproject.toml tegel/tegel-core/README.md tegel/tegel-core/LICENSE /src/tegel-core/
COPY tegel/tegel-core/src /src/tegel-core/src
COPY tegel/tegel-register/pyproject.toml tegel/tegel-register/README.md tegel/tegel-register/LICENSE /src/tegel-register/
COPY tegel/tegel-register/src /src/tegel-register/src
COPY tegel/tegel-base/packages/dataserver/pyproject.toml tegel/tegel-base/packages/dataserver/README.md tegel/tegel-base/packages/dataserver/LICENSE /src/dataserver/
COPY tegel/tegel-base/packages/dataserver/src /src/dataserver/src
COPY tegel/tegel-base/packages/actions/pyproject.toml tegel/tegel-base/packages/actions/README.md tegel/tegel-base/packages/actions/LICENSE /src/actions/
COPY tegel/tegel-base/packages/actions/src /src/actions/src
COPY xfm_ssrl_tegel/etl/pyproject.toml xfm_ssrl_tegel/etl/README.md xfm_ssrl_tegel/etl/LICENSE /src/xfm/etl/
COPY xfm_ssrl_tegel/etl/src /src/xfm/etl/src
COPY xfm_ssrl_tegel/dataserver/pyproject.toml xfm_ssrl_tegel/dataserver/README.md xfm_ssrl_tegel/dataserver/LICENSE /src/xfm/dataserver/
COPY xfm_ssrl_tegel/dataserver/src /src/xfm/dataserver/src
COPY xfm_ssrl_tegel/dataserver/schema /src/xfm/dataserver/schema
COPY xfm_ssrl_tegel/schemas /schemas
COPY xfm_ssrl_tegel/deploy/constraints.txt /src/constraints.txt

RUN --mount=type=cache,target=/root/.cache/uv \
    find /src -name __pycache__ -prune -exec rm -rf {} + \
    && uv pip install --system --no-build-isolation --no-sources \
    --constraint /src/constraints.txt \
    /src/tegel-core \
    "/src/tegel-register[tiled]" \
    /src/dataserver \
    /src/actions \
    /src/xfm/etl \
    /src/xfm/dataserver

ENV TEGEL_SCHEMA_ROOT=/schemas \
    TEGEL_PROFILE=/src/xfm/dataserver/schema/profile.yaml

CMD ["python", "-m", "tegel.dataserver"]
