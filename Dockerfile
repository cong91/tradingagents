# syntax=docker/dockerfile:1.7

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS builder

WORKDIR /build
COPY . .

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install the project and web extra separately. Keeping the optional web
# packages in their own resolver pass avoids pip's deep backtracking across the
# full LLM/data dependency graph on a clean Docker build.
RUN pip install --no-cache-dir .
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27"

# Download bytes only. This stage never imports or executes the downloaded
# Kronos source; the source is consumed later by the isolated child runner.
FROM base AS kronos-download

ARG KRONOS_SOURCE_REVISION=67b630e67f6a18c9e9be918d9b4337c960db1e9a
ARG KRONOS_SMALL_MODEL_REVISION=901c26c1332695a2a8f243eb2f37243a37bea320
ARG KRONOS_SMALL_TOKENIZER_REVISION=0e0117387f39004a9016484a186a908917e22426
ARG KRONOS_MINI_MODEL_REVISION=f4e68697d9d5aed55cef5c96aabc3376bcad9f81
ARG KRONOS_MINI_TOKENIZER_REVISION=26966d0035065a0cae0ebad7af8ece35bc1fb51c

# Do not read implicit host credentials or emit telemetry during public downloads.
ENV HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    HF_HUB_DISABLE_XET=1

RUN python -m pip install --no-cache-dir "huggingface_hub==0.33.1"
RUN mkdir -p /opt/kronos-src /opt/kronos-models

RUN python -c "from pathlib import Path; from urllib.request import urlopen; base = 'https://raw.githubusercontent.com/shiyu-coder/Kronos/${KRONOS_SOURCE_REVISION}/'; files = ('model/kronos.py', 'model/module.py', 'model/__init__.py'); [(Path('/opt/kronos-src') / relative).parent.mkdir(parents=True, exist_ok=True) or (Path('/opt/kronos-src') / relative).write_bytes(urlopen(base + relative, timeout=60).read()) for relative in files]"
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='NeoQuasar/Kronos-small', revision='${KRONOS_SMALL_MODEL_REVISION}', allow_patterns=['config.json', 'model.safetensors'], local_dir='/opt/kronos-models/Kronos-small'); snapshot_download(repo_id='NeoQuasar/Kronos-Tokenizer-base', revision='${KRONOS_SMALL_TOKENIZER_REVISION}', allow_patterns=['config.json', 'model.safetensors'], local_dir='/opt/kronos-models/Kronos-Tokenizer-base'); snapshot_download(repo_id='NeoQuasar/Kronos-mini', revision='${KRONOS_MINI_MODEL_REVISION}', allow_patterns=['config.json', 'model.safetensors'], local_dir='/opt/kronos-models/Kronos-mini'); snapshot_download(repo_id='NeoQuasar/Kronos-Tokenizer-2k', revision='${KRONOS_MINI_TOKENIZER_REVISION}', allow_patterns=['config.json', 'model.safetensors'], local_dir='/opt/kronos-models/Kronos-Tokenizer-2k')"

# Verify every source, config, and weight byte before the artifacts enter the
# runtime image. The manifests use immutable revisions above and fail closed on
# missing or changed files.
RUN set -eux; \
    printf '%s  %s\n' \
      '0a5f90282e2039c2de0771473419715c845def154896dbd0f5747837e6241032' '/opt/kronos-src/model/kronos.py' \
      'a07edbadc0e96804c8158c021bbc6063bb7cc43b34d7fc470d5c8ff2005a409f' '/opt/kronos-src/model/module.py' \
      'f8f856ca3fedadcaac97e196be23d1aeda1c3c9ffe8903d66d43ea3bcac6240c' '/opt/kronos-src/model/__init__.py' \
      > /tmp/kronos-source.sha256; \
    sha256sum -c /tmp/kronos-source.sha256; \
    printf '%s  %s\n' \
      '2366e7ccfec76cbc19cf3c4c1b9c5d901be336ca1e83f2d2292c9bff381b77a2' '/opt/kronos-models/Kronos-Tokenizer-base/config.json' \
      '59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee' '/opt/kronos-models/Kronos-Tokenizer-base/model.safetensors' \
      '5e0f6a605d5f81b5c9b559fe5cf716a1acb041c744e6f41bd05b097b7a685396' '/opt/kronos-models/Kronos-small/config.json' \
      'b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020' '/opt/kronos-models/Kronos-small/model.safetensors' \
      '0b30a443affb03e05a876a083857de9164f899feb7b4d261da02c485c9a3e3b6' '/opt/kronos-models/Kronos-Tokenizer-2k/config.json' \
      'b97ec46b3b72160509e289183eaf7bdf5f0dac5bb9b49522f6d46638a99a8717' '/opt/kronos-models/Kronos-Tokenizer-2k/model.safetensors' \
      '70daca2cb11e3a979dd6b8ac12ee08e2aace877acf28f5b8dfb4fe5609736201' '/opt/kronos-models/Kronos-mini/config.json' \
      'a7d5f37e2e9fbd9891f7d7d4f72574512dd1f704fee14223e0a8cd0fbf54197c' '/opt/kronos-models/Kronos-mini/model.safetensors' \
      > /tmp/kronos-models.sha256; \
    sha256sum -c /tmp/kronos-models.sha256; \
    rm -rf /opt/kronos-src/.cache /opt/kronos-models/*/.cache /tmp/kronos-source.sha256 /tmp/kronos-models.sha256

FROM base AS kronos-runtime

ARG KRONOS_TORCH_VERSION=2.9.1+cpu

RUN python -m venv /opt/kronos-venv \
 && /opt/kronos-venv/bin/python -m pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      "torch==${KRONOS_TORCH_VERSION}" \
 && /opt/kronos-venv/bin/python -m pip install --no-cache-dir \
      "pandas==2.3.3" \
      "einops==0.8.1" \
      "huggingface_hub==0.33.1" \
      "safetensors==0.6.2" \
 && /opt/kronos-venv/bin/python -c "import torch; assert torch.__version__ == '2.9.1+cpu'; assert not torch.cuda.is_available()"

FROM base AS runtime

# TRADINGAGENTS_IN_DOCKER lets an update notice name the rebuild command.
# These are the Linux defaults used by tradingagents.forecast.artifact.py.
ENV PYTHONUNBUFFERED=1 \
    TRADINGAGENTS_IN_DOCKER=1 \
    HOME=/home/appuser \
    TRADINGAGENTS_SHADOW_KRONOS_PYTHON=/home/appuser/tradingagents-research/kronos-venv/bin/python \
    TRADINGAGENTS_SHADOW_KRONOS_SOURCE=/home/appuser/tradingagents-research/kronos-src \
    TRADINGAGENTS_SHADOW_KRONOS_MODELS=/home/appuser/tradingagents-research/models \
    CUDA_VISIBLE_DEVICES= \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home appuser \
 && install -d -m 0755 -o appuser -g appuser \
      /home/appuser/.tradingagents \
      /home/appuser/tradingagents-research

COPY --from=kronos-runtime --chown=appuser:appuser \
     /opt/kronos-venv /home/appuser/tradingagents-research/kronos-venv
COPY --from=kronos-download --chown=appuser:appuser \
     /opt/kronos-src /home/appuser/tradingagents-research/kronos-src
COPY --from=kronos-download --chown=appuser:appuser \
     /opt/kronos-models /home/appuser/tradingagents-research/models

# Keep the application venv free of model-only libraries.
RUN /opt/venv/bin/python -c "import importlib.util; assert all(importlib.util.find_spec(name) is None for name in ('torch', 'einops', 'safetensors'))"

USER appuser
WORKDIR /home/appuser/app

COPY --from=builder --chown=appuser:appuser /build .

EXPOSE 8000

# PostgreSQL is mandatory: the app fails fast when TRADINGAGENTS_DATABASE_URL
# is missing rather than silently persisting SQLite state.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["/opt/venv/bin/python", "-c", "import urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4); raise SystemExit(0 if response.status == 200 else 1)"]

ENTRYPOINT ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
