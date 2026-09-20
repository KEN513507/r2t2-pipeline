# Restore Procedure (Vast.ai)

## Prerequisites

- Vast.ai account with credit
- Docker Hub access (for pulling vastai/pytorch:latest)
- SSH key registered at https://vast.ai/console/account/
- Local repo: ~/projects/r2t2-pipeline
- Local env files: ~/.config/r2t2/server.env and client.env

## Step 1: Create Vast.ai instance

Search for RTX 3090 with CUDA 13.0 or higher:

    vastai search offers 'gpu_name=RTX_3090 num_gpus=1 cuda_vers>=13.0 reliability>0.995 disk_space>60 inet_down>=800 rented=False' -o 'dph' | head -10

Avoid known-bad machine IDs: 64274, 27610, 29027, 125534

Create:

    vastai create instance <ID> --image vastai/pytorch:latest --disk 60 --ssh

Wait for running (2-10 minutes):

    sleep 120 && vastai show instances

Get SSH URL:

    vastai ssh-url <NEW_INSTANCE_ID>

## Step 2: SSH into instance

    ssh -p <PORT> root@<HOST>

## Step 3: Set up uv and venv

    python3 -m pip install --no-cache-dir -U uv
    uv venv /workspace/r2t2-venv --python 3.10 --seed
    source /workspace/r2t2-venv/bin/activate

## Step 4: Install vLLM (the critical gate)

    uv pip install "vllm==0.14.0" --torch-backend=auto

Verify:

    /workspace/r2t2-venv/bin/python -c "import vllm, torch; print('vllm', vllm.__version__, 'cuda', torch.cuda.is_available())"

Expected: vllm 0.14.0, cuda True
If this fails, destroy and try another machine.

## Step 5: Install R2T2 dependencies

    uv pip install "qwen-asr==0.0.6" "fireredvad==0.0.2" \
        sanic librosa soundfile sox "huggingface-hub==0.36.2"

## Step 6: Clone R2T2

    cd /root
    git clone https://github.com/netease-youdao/Confucius4-R2T2.git
    cd Confucius4-R2T2
    git checkout 80c22e6140bcb9166fb9906798894fc8b18c8309

## Step 7: Download VAD model

    cd /root/Confucius4-R2T2
    mkdir -p checkpoints/vad
    hf download FireRedTeam/FireRedVAD --include "Stream-VAD/*" --local-dir checkpoints/vad

Verify:

    ls -la checkpoints/vad/Stream-VAD/

Expected: cmvn.ark, model.pth.tar

## Step 8: Place proxy_server.py

Copy from docs/inventory/08-proxy_server.py to /root/Confucius4-R2T2/proxy_server.py

Or from local repo:

    scp -P <PORT> ~/projects/r2t2-pipeline/docs/inventory/08-proxy_server.py \
        root@<HOST>:/root/Confucius4-R2T2/proxy_server.py

## Step 9: Place server.env

From local machine:

    ssh -p <PORT> root@<HOST> "mkdir -p ~/.config/r2t2"
    scp -P <PORT> ~/.config/r2t2/server.env \
        root@<HOST>:~/.config/r2t2/server.env

Required content of server.env:

    R2T2_TOKEN=<64-char hex>
    DEEPL_KEY=<deepl key with :fx suffix>
    DEEPL_CAP_CHARS=500000
    DEEPL_USAGE_FILE=/root/r2t2-data/deepl_usage.json
    R2T2_WS_URL=ws://127.0.0.1:8272/asr_stream_api_v1
    R2T2_ASR_SECRET=test0102
    LOG_LEVEL=INFO

## Step 10: Start R2T2 ws_server

    cd /root/Confucius4-R2T2
    source /workspace/r2t2-venv/bin/activate
    ./run_start_server.sh start \
        --model_path netease-youdao/Confucius4-R2T2 \
        --vad_model_path checkpoints/vad/Stream-VAD \
        --port 8272 --gpu 0

Wait for model load (first time: 5-15 min for HF download; after cache: 40 sec).

Check:

    tail -f nohup_service_ws_localhost_8272.log
    ss -lntp | grep 8272

Expected last lines:
    ASR model initialized successfully
    ASR model warmup complete
    Worker ready
    LISTEN 0.0.0.0:8272

## Step 11: Start proxy_server

    cd /root/Confucius4-R2T2
    source /workspace/r2t2-venv/bin/activate
    set -a; source ~/.config/r2t2/server.env; set +a
    mkdir -p /root/r2t2-data
    nohup python proxy_server.py > /root/proxy.log 2>&1 &

Check:

    sleep 3
    tail -10 /root/proxy.log
    ss -lntp | grep 8000

Expected:
    DeepL ready. remaining=500000
    R2T2 target: ws://127.0.0.1:8272/asr_stream_api_v1
    Uvicorn running on http://0.0.0.0:8000

## Step 12: Local side — SSH tunnel

On local machine, new terminal:

    ssh -N -L 8000:localhost:8000 -p <PORT> root@<HOST>

Keep this terminal open.

## Step 13: Local side — VOICEVOX

    docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest

Verify:

    curl -s http://127.0.0.1:50021/version

## Step 14: Local side — client.py

New terminal:

    cd ~/projects/r2t2-pipeline
    source .venv/bin/activate
    set -a; source ~/.config/r2t2/client.env; set +a
    export R2T2_WS_URL=ws://127.0.0.1:8000/stream
    python client/client.py

## Known Pitfalls

1. pip install -e . produces UNKNOWN-0.0.0
   Do not use pip install. Use uv + PYTHONPATH.

2. tokenizers version conflict
   Must be 0.22.2, not 0.23.2. If mismatch, delete dist-info manually.

3. huggingface-hub 1.x
   transformers requires <1.0. Install 0.36.2 explicitly.

4. CUDA < 12.9
   vLLM 0.14.0 requires CUDA 12.9+. Use cuda_vers>=13.0 in search.

5. Machine 64274
   Docker pull fails. Avoid.

6. R2T2 JSON header
   Must be sent before PCM. See proxy_server.py line 158-166.

7. secret_key
   Default "test0102" must be present or R2T2 rejects.

## One-command Resume (after first setup)

If using Vast.ai stop (not destroy):

    vastai start instance <ID>
    # wait 2-5 min
    ssh -p <NEW_PORT> root@<HOST>
    # R2T2 and proxy auto-start via /root/onstart.sh
