# PyTorch公式 CUDA 12.1 / Ubuntu 22.04 最適化イメージ
FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

# システム依存（音声デコード用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    git \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV HF_HOME=/root/.cache/huggingface \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN mkdir -p /app/data

# 依存だけコピーしてレイヤキャッシュを効かせる
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# ソースのみコピー（.env は絶対に COPY しない）
COPY server.py /app/server.py

EXPOSE 8000

# 認証情報は実行時に --env-file で注入する
CMD ["python", "server.py"]
