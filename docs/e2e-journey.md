# R2T2 E2E 構築記録

完了日: 2026-09-20
作業時間: 約3時間
消費額: 約 $0.7 / $10
最終状態: 完全動作（テキスト翻訳 + 音声再生）

## 最終アーキテクチャ

[ローカルPC Ubuntu GTX970]
  - Mic (sounddevice 16kHz mono int16)
  - client.py (WS:8000, UI, VOICEVOX)
  - VOICEVOX Engine (localhost:50021, speaker=3)
       |
       | SSH Tunnel localhost:8000 から vast:8000
       v
[Vast.ai Instance 51715606 machine 3754 Norway]
  - proxy_server.py (FastAPI :8000)
  - R2T2 ws_server.py (:8272)
  - vLLM 0.14.0 + Confucius4-R2T2 2B BF16
  - DeepL API

## 到達までの経緯

Phase1 構想
- 当初 server.py が ASR を内蔵する設計
- transformers backend で HF モデルロード → MISSING weights
- 実は公式 ws_server.py が既に存在と判明

Phase2 Vast.ai マシン選定 5回失敗
- 64274  Docker pull 停止
- 27610  ネット遅すぎ 93Mbps
- 29027  offline化
- 125534 12分loadingで断念
- 3754   成功 Norway 840Mbps CUDA13.0

教訓 cuda_vers 13.0以上 reliability 0.995以上 inet_down 800以上 で絞る

Phase3 依存地獄 → uv で解決
- pip install -e . で UNKNOWN-0.0.0 発生
- --no-deps で回避すると依存が壊れる
- 正解 uv venv + PYTHONPATH 経由で r2t2 を import
- huggingface-hub 1.x から 0.36.2 に固定必須
- tokenizers 0.23.2 から 0.22.2 にダウングレード必須

Phase4 プロトコル誤解 → Codex が修正
- 誤り send_audio_new だけ見てハンドシェイク不要と判断
- 正解 接続直後に JSON ヘッダー送信が必須
- R2T2 は status connected を返すまで PCM を受け付けない

Phase5 E2E 疎通成功
- テキスト翻訳 OK
- DeepL課金カウント OK
- VOICEVOX音声 OK

## 起動手順

1 Vast.ai インスタンス起動
vastai start instance 51715606
vastai ssh-url 51715606

2 R2T2 ws_server 起動 Vast.ai内
cd /root/Confucius4-R2T2
source /workspace/r2t2-venv/bin/activate
./run_start_server.sh start --model_path netease-youdao/Confucius4-R2T2 --vad_model_path checkpoints/vad/Stream-VAD --port 8272 --gpu 0
tail -f nohup_service_ws_localhost_8272.log
ss -lntp | grep 8272

3 proxy_server.py 起動 Vast.ai内
cd /root/Confucius4-R2T2
source /workspace/r2t2-venv/bin/activate
set -a; source ~/.config/r2t2/server.env; set +a
mkdir -p /root/r2t2-data
python proxy_server.py

4 SSHトンネル ローカル
ssh -N -L 8000:localhost:8000 -p 35606 root@ssh7.vast.ai

5 VOICEVOX 起動 ローカル
docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest

6 client.py 起動 ローカル
cd ~/projects/r2t2-pipeline
source .venv/bin/activate
set -a; source ~/.config/r2t2/client.env; set +a
export R2T2_WS_URL=ws://127.0.0.1:8000/stream
python client/client.py

## ハマりポイント

Vast.ai
- 失敗しても課金される offline loading中も 0.15から0.29ドル毎時
- 15分以上 loading なら即 destroy
- 同じマシンで2連続失敗したら machine_id を変える
- Docker Hub レート制限は IP 単位

Python環境
- pip 26.x + setuptools 84 の組み合わせで予期せぬ動作
- no-deps は使わない
- uv venv で完全分離
- PYTHONPATH で r2t2 を読ませる pip install 不要

R2T2プロトコル
- JSON ヘッダーが必須 ws_client.py の main 参照
- 1接続 = 1発話 EOS後に閉じる
- 160ms チャンク 2560 samples x 2 bytes

DeepL
- カウントは原文英語の文字数
- 事前予約加算で二重課金防止
- 月次リセット 毎月1日 UTC
- 500k 超過で fatal 通知

## パフォーマンス実測

R2T2 モデルロード  40秒 初回DL 5分
ASR レイテンシ  200から600ms
DeepL 往復  200から300ms
VOICEVOX 合成  500から1000ms
E2E 視覚  400から800ms
E2E 音声  1.5から2.5秒

## コスト実績

Vast.ai GPU 0.2889ドル毎時  約 0.7ドル
DeepL Free  0
VOICEVOX  0
合計  約 0.7ドル

月1000対話 2M文字 想定 約 0.3から0.4ドル毎月

## 次回への申し送り

1 Vast.ai 再起動時は machine 3754 を狙う Docker pull キャッシュ済み
2 失敗した machine_id 64274 27610 29027 125534
3 R2T2 は vLLM 必須
4 proxy_server.py のハンドシェイクを消さない
5 env はリポジトリ外 ~/.config/r2t2/
