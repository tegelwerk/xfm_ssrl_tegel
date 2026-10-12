FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_HTTP_TIMEOUT=300
RUN pip install --no-cache-dir uv hatchling

WORKDIR /src
COPY tegel/tegel-core /src/tegel-core
COPY tegel/tegel-base/packages/dataserver /src/tegel-base/packages/dataserver
COPY tegel/tegel-base/packages/actions /src/tegel-base/packages/actions
COPY xfm_ssrl_tegel/dataserver /src/xfm/dataserver

WORKDIR /src/xfm/dataserver
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-build-isolation --no-sources \
    /src/tegel-core \
    /src/tegel-base/packages/dataserver \
    /src/tegel-base/packages/actions \
    /src/xfm/dataserver

CMD ["python", "-m", "tegel.dataserver"]
