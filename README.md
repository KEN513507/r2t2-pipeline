# R2T2 English-to-Japanese Real-Time Translation Pipeline

Realtime EN to JA translation.

- ASR: Confucius4-R2T2 (streaming, transformers)
- MT: DeepL Free API (Option B: 500k chars/month hard cap)
- TTS: VOICEVOX (local CPU, speaker=3 zundamon)
- Transport: WebSocket
- Server: Vast.ai

## Layout

    client/    Local Linux client (mic, WS, VOICEVOX)
    server/    Vast.ai WebSocket ASR+MT server (Docker)
    docs/      Architecture
    scripts/   Helper launchers

## Secrets: NOT in this repository

All secrets live in ~/.config/r2t2/

    ~/.config/r2t2/
      server.env    (chmod 600)
      client.env    (chmod 600)
      .token        (chmod 600)

### Setup

    mkdir -p ~/.config/r2t2 && chmod 700 ~/.config/r2t2
    openssl rand -hex 32 | tee ~/.config/r2t2/.token
    chmod 600 ~/.config/r2t2/.token

Create ~/.config/r2t2/server.env:

    R2T2_TOKEN=<same as .token>
    R2T2_MODEL_ID=netease-youdao/Confucius4-R2T2
    R2T2_MIN_WORDS=4
    R2T2_MAX_WAIT_MS=1200
    DEEPL_KEY=<your real key>
    DEEPL_CAP_CHARS=500000
    DEEPL_USAGE_FILE=/app/data/deepl_usage.json
    LOG_LEVEL=INFO

Create ~/.config/r2t2/client.env:

    R2T2_WS_URL=ws://<vast_ai_ip>:8000/stream
    R2T2_TOKEN=<same as .token>
    R2T2_SILENCE_RMS=300
    R2T2_SILENCE_CHUNKS=4
    VOICEVOX_URL=http://127.0.0.1:50021
    VOICEVOX_SPEAKER=3
    LOG_LEVEL=INFO

    chmod 600 ~/.config/r2t2/*.env ~/.config/r2t2/.token

## Server (Vast.ai)

    cd server
    docker build -t r2t2-server .
    ../scripts/run-server-docker.sh

## Client (Local Linux)

VSCode: F5 -> "Python: client.py (local)"

Shell:

    ./scripts/run-client.sh

## Security

- No .env in repository
- Docker image does not embed secrets (injected via --env-file)
- ~/.config/r2t2/* is chmod 600
