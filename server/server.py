#!/usr/bin/env python3
"""
server.py — R2T2 WebSocket Server (Vast.ai)
============================================
  Input : 16kHz / 16bit / mono / LE PCM  (raw binary frames)
  ASR   : Confucius4-R2T2 (streaming, transformers backend)
  Trans : DeepL Free API  (Option B: hard cap @ 500,000 chars/month)
  Output: JSON text frames
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import sys
import time
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, status
import uvicorn

try:
    import deepl
except ImportError:  # pragma: no cover
    deepl = None  # 起動時チェックで弾く

# ============================================================
# 0. Config
# ============================================================
LOG = logging.getLogger("r2t2.server")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)

AUTH_TOKEN      = os.environ.get("R2T2_TOKEN", "")
MODEL_ID        = os.environ.get("R2T2_MODEL_ID", "netease-youdao/Confucius4-R2T2")
SAMPLE_RATE     = 16_000

DEEPL_KEY       = os.environ.get("DEEPL_KEY", "")
DEEPL_CAP_CHARS = int(os.environ.get("DEEPL_CAP_CHARS", "500000"))
USAGE_FILE      = Path(os.environ.get("DEEPL_USAGE_FILE", "/app/data/deepl_usage.json"))
RESET_DAY       = int(os.environ.get("DEEPL_RESET_DAY", "1"))

PUNCT_RE        = re.compile(r"[.!?。！？…]+")
MIN_WORDS       = int(os.environ.get("R2T2_MIN_WORDS", "4"))
MAX_WAIT_MS     = int(os.environ.get("R2T2_MAX_WAIT_MS", "1200"))

CTRL_CHARS      = {c for c in map(chr, range(0x20)) if c not in "\n\t"}


# ============================================================
# 1. Exceptions
# ============================================================
class DeepLCapExceeded(Exception):
    """DeepL の上限（無料枠 or 強制キャップ）を超えた。"""


class UsageFileCorrupted(Exception):
    """deepl_usage.json が壊れていて復元不能。"""


# ============================================================
# 2. DeepL Manager (Option B: hard cap)
# ============================================================
class DeepLManager:
    """
    Option B: 単一キー、累積文字数が cap を超えたら即例外。
    文字数は『原文（英語）の長さ（スペース/改行含む）』で計上。
    使用量は JSON に原子的に永続化する。
    """

    def __init__(self, key: str, cap: int, usage_file: Path, reset_day: int) -> None:
        if not key:
            raise RuntimeError("DEEPL_KEY is empty")
        if deepl is None:
            raise RuntimeError("deepl package not installed")
        self._client = deepl.Translator(key)
        self.cap = cap
        self.path = usage_file
        self.reset_day = reset_day
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock: Optional[asyncio.Lock] = None

    # ---- lock (lazy) ----
    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ---- persistence ----
    def _period(self) -> str:
        now = datetime.now(timezone.utc)
        return f"{now.year}-{now.month:02d}"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"total_chars_consumed": 0, "period": self._period()}
        try:
            raw = self.path.read_text("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("root not dict")
            n = data.get("total_chars_consumed")
            if not isinstance(n, int) or n < 0:
                raise ValueError(f"bad total_chars_consumed={n!r}")
            if not isinstance(data.get("period"), str):
                data["period"] = self._period()
            return data
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # ★5: 破損を検知したら安全側＝停止
            LOG.critical("usage file corrupted: %s", e)
            try:
                bad = self.path.with_suffix(f".bad.{int(time.time())}")
                self.path.rename(bad)
                LOG.critical("moved corrupted usage file to %s", bad)
            except OSError:
                pass
            raise UsageFileCorrupted("DeepL usage file corrupted") from e

    def _save(self, u: dict) -> None:
        # ★5: 原子的書き込み + fsync
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(u, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)
        except OSError as e:
            LOG.critical("usage save failed: %s", e)
            raise UsageFileCorrupted("cannot persist DeepL usage") from e

    def _maybe_reset(self, u: dict) -> dict:
        if u.get("period") != self._period():
            LOG.info("DeepL usage period rolled: %s -> %s",
                     u.get("period"), self._period())
            return {"total_chars_consumed": 0, "period": self._period()}
        return u

    # ---- public ----
    def remaining(self) -> int:
        try:
            u = self._maybe_reset(self._load())
        except UsageFileCorrupted:
            return 0
        return max(0, self.cap - u["total_chars_consumed"])

    async def translate(self, text: str) -> str:
        if not text.strip():
            return ""
        n = len(text)

        async with self.lock:
            u = self._maybe_reset(self._load())
            projected = u["total_chars_consumed"] + n
            if projected > self.cap:
                LOG.error("DeepL cap reached: %d + %d > %d",
                          u["total_chars_consumed"], n, self.cap)
                raise DeepLCapExceeded(
                    f"DeepL cap reached ({projected} > {self.cap})"
                )
            # ★5: API 失敗でも二重課金しないよう先に予約加算
            u["total_chars_consumed"] = projected
            self._save(u)

        # ---- network I/O (outside lock) ----
        try:
            res = await asyncio.to_thread(
                self._client.translate_text, text, target_lang="JA"
            )
            return res.text
        except deepl.QuotaExceededException as e:
            LOG.critical("DeepL server-side quota exceeded: %s", e)
            raise DeepLCapExceeded("DeepL quota exceeded") from e
        except deepl.AuthorizationException as e:
            LOG.critical("DeepL auth failed (invalid key): %s", e)
            raise DeepLCapExceeded("DeepL key invalid") from e
        except deepl.TooManyRequestsException as e:
            # ★2: 予約済み文字数は既に加算済み。再送はしない。
            LOG.warning("DeepL 429 (rate-limited): %s", e)
            return ""
        except Exception as e:
            LOG.exception("DeepL transient error: %s", e)
            return ""


# ============================================================
# 3. Streaming ASR (transformers backend)
# ============================================================
class StreamingASR:
    """
    Confucius4-R2T2 をラップ。
    入力: PCM チャンク（bytes）
    出力: 新規に確定した英語テキスト（append-only で差分のみ）

    NOTE:
      _feed_hf() の processor / generate 呼び出しは、
      モデルカードに記載の実APIに合わせて書き換えること。
    """

    def __init__(self) -> None:
        self._model = None
        self._proc = None
        self._committed = ""

    # ---- init ----
    async def lazy_init(self) -> None:
        if self._model is None:
            self._model, self._proc = await asyncio.to_thread(self._load_hf)

    def _load_hf(self):
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
        LOG.info("loading HF model: %s", MODEL_ID)
        proc = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            MODEL_ID,
            torch_dtype=torch.float16,
            device_map="cuda",
            trust_remote_code=True,
        ).eval()
        return model, proc

    # ---- feed / reset ----
    async def feed(self, pcm_bytes: bytes) -> str:
        await self.lazy_init()
        text = await asyncio.to_thread(self._feed_hf, pcm_bytes)
        if not text:
            return ""
        if text.startswith(self._committed):
            delta = text[len(self._committed):]
            self._committed = text
            return delta
        # ★2: モデルが過去を書き換えた場合は安全側で全再送
        LOG.warning("ASR emitted mutated prefix; resetting baseline")
        self._committed = text
        return text

    async def reset(self) -> None:
        self._committed = ""

    # ---- low-level inference ----
    def _feed_hf(self, pcm_bytes: bytes) -> str:
        import torch
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        # --- ★ 実モデルのAPIに合わせて書き換える ---
        inputs = self._proc(pcm, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        inputs = {k: v.to("cuda", dtype=torch.float16) for k, v in inputs.items()}
        with torch.inference_mode():
            out = self._model.generate(**inputs, max_new_tokens=64)
        return self._proc.batch_decode(out, skip_special_tokens=True)[0]


# ============================================================
# 4. Windower
# ============================================================
class Windower:
    """
    追記型の英語デルタを、句読点 or 一定時間で chunk 化して DeepL に渡す。
    """

    def __init__(self, min_words: int = MIN_WORDS, max_wait_ms: int = MAX_WAIT_MS) -> None:
        self.buf = ""
        self.min_words = min_words
        self.max_wait = max_wait_ms / 1000.0
        self._last_emit = time.monotonic()

    def push(self, delta: str) -> Optional[str]:
        if not delta:
            return None
        self.buf += delta
        if PUNCT_RE.search(self.buf):
            return self._emit()
        if (time.monotonic() - self._last_emit >= self.max_wait
                and len(self.buf.split()) >= self.min_words):
            return self._emit()
        return None

    def flush(self) -> Optional[str]:
        if self.buf.strip():
            return self._emit()
        return None

    def _emit(self) -> str:
        chunk = self.buf
        self.buf = ""
        self._last_emit = time.monotonic()
        return chunk


# ============================================================
# 5. Sanitize
# ============================================================
def sanitize_ja(text: str) -> str:
    text = "".join(ch for ch in text if ch not in CTRL_CHARS)
    text = unicodedata.normalize("NFKC", text)
    return text.strip()


# ============================================================
# 6. App + lifespan
# ============================================================
deepl_mgr: Optional[DeepLManager] = None
asr_singleton: Optional[StreamingASR] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global deepl_mgr, asr_singleton

    # ---- config checks (★1) ----
    if not AUTH_TOKEN:
        LOG.warning("R2T2_TOKEN not set — authentication DISABLED")
    if not DEEPL_KEY:
        LOG.critical("DEEPL_KEY not set — refusing to start")
        sys.exit(1)
    if DEEPL_CAP_CHARS <= 0:
        LOG.critical("DEEPL_CAP_CHARS must be > 0")
        sys.exit(1)

    # ---- DeepL init (★5) ----
    try:
        deepl_mgr = DeepLManager(DEEPL_KEY, DEEPL_CAP_CHARS, USAGE_FILE, RESET_DAY)
        LOG.info("DeepL ready. remaining=%d", deepl_mgr.remaining())
    except Exception as e:
        LOG.critical("DeepL init failed: %s", e, exc_info=True)
        sys.exit(1)

    # ---- ASR warmup (★4) ----
    asr_singleton = StreamingASR()
    try:
        await asyncio.wait_for(asr_singleton.lazy_init(), timeout=180)
        LOG.info("ASR warmup OK")
    except asyncio.TimeoutError:
        LOG.critical("ASR warmup timed out (HF download stuck?)")
        sys.exit(1)
    except Exception as e:
        LOG.critical("ASR load failed: %s", e, exc_info=True)
        sys.exit(1)

    yield

    LOG.info("shutdown")


app = FastAPI(lifespan=lifespan)


# ============================================================
# 7. Auth
# ============================================================
def _check_token(token: str) -> bool:
    if not AUTH_TOKEN:
        return True
    return hmac.compare_digest(token, AUTH_TOKEN)


async def _send(ws: WebSocket, obj: dict) -> None:
    if ws.client_state.name != "CONNECTED":
        return
    try:
        await ws.send_text(json.dumps(obj, ensure_ascii=False))
    except Exception as e:
        LOG.debug("send failed: %s", e)


# ============================================================
# 8. WebSocket endpoint
# ============================================================
@app.websocket("/stream")
async def stream(ws: WebSocket, token: str = Query(default="")):
    if not _check_token(token):
        LOG.warning("auth failed from %s", ws.client)
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    LOG.info("client connected: %s", ws.client)

    assert deepl_mgr is not None and asr_singleton is not None

    asr = StreamingASR()  # ★5: セッションごとに独立。warmup済みモデルはHFキャッシュから即ロード
    asr._model = asr_singleton._model
    asr._proc  = asr_singleton._proc

    win = Windower()

    try:
        await _send(ws, {"type": "meta", "remaining": deepl_mgr.remaining()})

        while True:
            msg = await ws.receive()

            # ---- binary: audio chunk ----
            if msg.get("bytes") is not None:
                pcm = msg["bytes"]
                if not pcm:
                    continue

                # ★2: ASR 失敗はチャンク単位でスキップ
                try:
                    delta = await asr.feed(pcm)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # OOM は空キャッシュ
                    msg_str = str(e)
                    if "out of memory" in msg_str.lower():
                        try:
                            import torch
                            torch.cuda.empty_cache()
                        except Exception:
                            pass
                        LOG.error("ASR OOM; cache cleared, skipping chunk")
                    else:
                        LOG.exception("ASR feed failed")
                    await _send(ws, {"type": "error", "where": "asr",
                                     "msg": msg_str})
                    continue

                if delta:
                    await _send(ws, {"type": "en", "text": delta})
                    chunk = win.push(delta)
                    if chunk:
                        # ★5: cap 到達は即座に致命扱い
                        try:
                            ja = await deepl_mgr.translate(chunk)
                        except DeepLCapExceeded as e:
                            LOG.error("FATAL: DeepL cap. closing WS")
                            await _send(ws, {"type": "fatal",
                                             "reason": "deepl_cap",
                                             "msg": str(e)})
                            await ws.close(code=1011, reason="deepl_cap")
                            return
                        except UsageFileCorrupted as e:
                            LOG.critical("FATAL: usage corrupted")
                            await _send(ws, {"type": "fatal",
                                             "reason": "usage_corrupted",
                                             "msg": str(e)})
                            await ws.close(code=1011, reason="usage_corrupted")
                            return
                        if ja:
                            ja = sanitize_ja(ja)
                            if ja:
                                await _send(ws, {"type": "ja",
                                                 "text": ja, "src": chunk})
                                await _send(ws, {"type": "meta",
                                                 "remaining": deepl_mgr.remaining()})

            # ---- text: control ----
            elif msg.get("text") is not None:
                try:
                    ctl = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                cmd = ctl.get("cmd")

                if cmd == "eos":
                    tail_en = win.flush()
                    if tail_en:
                        try:
                            ja = await deepl_mgr.translate(tail_en)
                        except DeepLCapExceeded as e:
                            await _send(ws, {"type": "fatal",
                                             "reason": "deepl_cap",
                                             "msg": str(e)})
                            await ws.close(code=1011, reason="deepl_cap")
                            return
                        except UsageFileCorrupted as e:
                            await _send(ws, {"type": "fatal",
                                             "reason": "usage_corrupted",
                                             "msg": str(e)})
                            await ws.close(code=1011, reason="usage_corrupted")
                            return
                        if ja:
                            ja = sanitize_ja(ja)
                            if ja:
                                await _send(ws, {"type": "ja",
                                                 "text": ja, "src": tail_en})
                    await asr.reset()
                    win = Windower()
                    await _send(ws, {"type": "meta", "event": "ready"})

                elif cmd == "ping":
                    await _send(ws, {"type": "pong", "t": time.time()})

    except WebSocketDisconnect:
        LOG.info("client disconnected: %s", ws.client)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOG.exception("unhandled error in /stream: %s", e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ============================================================
# 9. main
# ============================================================
if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, log_level="info")
