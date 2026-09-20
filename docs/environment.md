# Environment Record (frozen 2026-09-20)

## Vast.ai Instance

- Instance ID: 51715606
- Machine ID: 3754
- Host ID: 1276
- Location: Norway
- GPU: RTX 3090 (24GB)
- CUDA: 13.0
- Disk: 60GB
- Rate: $0.2889/hr
- SSH: ssh -p 35606 root@ssh7.vast.ai

## OS / Kernel

- Ubuntu 22.04.5 LTS
- Kernel 5.15.0-190-generic
- See docs/inventory/01-os.txt

## NVIDIA

- Driver: 580.173.02
- CUDA: 13.0
- GPU: NVIDIA GeForce RTX 3090
- See docs/inventory/02-nvidia-smi.txt, 03-gpu.csv

## Python

- System python: 3.10.x
- venv python: 3.10.x (uv)
- venv path: /workspace/r2t2-venv
- See docs/inventory/04-python.txt

## Dependencies

- See docs/inventory/05-pip-freeze.txt (227 packages)
- Key: vllm 0.14.0, torch 2.9.1+cu130, torchaudio 2.9.1+cu130,
  transformers 4.57.6, tokenizers 0.22.2, huggingface-hub 0.36.2,
  qwen-asr 0.0.6, fireredvad 0.0.2, sanic 25.12.1

## R2T2 Repository

- URL: https://github.com/netease-youdao/Confucius4-R2T2
- Commit: 80c22e6140bcb9166fb9906798894fc8b18c8309
- Path: /root/Confucius4-R2T2

## VAD Model

- Source: https://huggingface.co/FireRedTeam/FireRedVAD
- Path: /root/Confucius4-R2T2/checkpoints/vad/Stream-VAD/
- Files: cmvn.ark, model.pth.tar
- See docs/inventory/07-vad.txt

## Proxy Server

- Path: /root/Confucius4-R2T2/proxy_server.py
- Port: 8000
- Canonical EN-to-JA source: server/proxy_server.py
- docs/inventory/08-proxy_server.py is an archived JA-to-EN experiment.

## Onstart Script

- Path: /workspace/onstart.sh (symlinked to /root/onstart.sh)
- See docs/inventory/09-onstart.sh

## DeepL Usage

- Cap: 500000 chars/month
- Reset: 1st of each month UTC
- Usage file: /root/r2t2-data/deepl_usage.json
- See docs/inventory/10-deepl-usage.txt

## Freeze Note

At freeze time, the pipeline was working for EN->JA (English audio in,
Japanese text and audio out). JA->EN experiment failed because R2T2
does not support Japanese ASR at usable quality.

See docs/e2e-journey.md for the full construction history.
