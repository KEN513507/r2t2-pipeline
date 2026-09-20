#!/bin/bash
# setup_vast.sh — One-shot setup for a fresh Vast.ai instance.
# Run inside the instance as root.
set -euo pipefail

R2T2_COMMIT="80c22e6140bcb9166fb9906798894fc8b18c8309"

echo "=== 1. uv + venv ==="
python3 -m pip install --no-cache-dir -U uv
uv venv /workspace/r2t2-venv --python 3.10 --seed
source /workspace/r2t2-venv/bin/activate

echo "=== 2. vLLM (critical) ==="
uv pip install "vllm==0.14.0" --torch-backend=auto
python -c "import vllm, torch; print('vllm', vllm.__version__, 'cuda', torch.cuda.is_available())"

echo "=== 3. R2T2 deps ==="
uv pip install "qwen-asr==0.0.6" "fireredvad==0.0.2" \
    sanic librosa soundfile sox "huggingface-hub==0.36.2"

echo "=== 4. Clone R2T2 ==="
cd /root
if [ ! -d Confucius4-R2T2 ]; then
    git clone https://github.com/netease-youdao/Confucius4-R2T2.git
fi
cd Confucius4-R2T2
git fetch
git checkout "$R2T2_COMMIT"

echo "=== 5. VAD model ==="
mkdir -p checkpoints/vad
if [ ! -f checkpoints/vad/Stream-VAD/model.pth.tar ]; then
    hf download FireRedTeam/FireRedVAD --include "Stream-VAD/*" --local-dir checkpoints/vad
fi
ls -la checkpoints/vad/Stream-VAD/

echo "=== 6. Done. Next: ==="
echo "  1. scp docs/inventory/08-proxy_server.py root@<HOST>:/root/Confucius4-R2T2/proxy_server.py"
echo "  2. scp ~/.config/r2t2/server.env root@<HOST>:~/.config/r2t2/server.env"
echo "  3. Start R2T2 and proxy (see docs/restore.md Step 10-11)"
