# This image defines the CUDA 13 R2T2 proxy runtime. The official R2T2 clone,
# VAD checkpoint, model cache, and both server processes are provisioned by
# onstart.sh; no secrets are copied into this image.
FROM nvidia/cuda:13.0.0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/root/.cache/huggingface \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.10 python3-pip python3.10-venv \
      git ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN python3.10 -m pip install --no-cache-dir --upgrade pip uv \
    && uv pip install --system --torch-backend=auto \
      "vllm==0.14.0" \
      "qwen-asr==0.0.6" \
      "fireredvad==0.0.2" \
      "sanic==25.12.1" \
      librosa soundfile sox \
      "huggingface-hub==0.36.2" \
      "tokenizers==0.22.2" \
      "deepl==1.19.1" fastapi uvicorn "websockets==16.1.1"

WORKDIR /opt/r2t2-proxy
COPY proxy_server.py ./proxy_server.py

EXPOSE 8000 8272

# Start R2T2 ws_server.py and proxy_server.py through onstart.sh after the
# official repository and VAD checkpoint are available. Inject secrets only at
# runtime, for example with Docker's --env-file option.
CMD ["/bin/bash"]
