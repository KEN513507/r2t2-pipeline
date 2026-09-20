"""R2T2 WebSocket proxy + DeepL translation.

Verified protocol (from official ws_client.py):
  client -> R2T2 : JSON request header, then raw PCM int16 binary frames
  client -> R2T2 : "YOUDAO_ONETIME_ASR_STREAM_EOS"
  R2T2 -> client : {"status":"success","msg":{"text":"delta","reset":bool}}
  R2T2 closes the WS after EOS (code 1000).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import websockets
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, status

LOG = logging.getLogger("r2t2.proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

ASR_URL = os.getenv("R2T2_WS_URL", "ws://127.0.0.1:8272/asr_stream_api_v1")
EOS = "YOUDAO_ONETIME_ASR_STREAM_EOS"
CAP = int(os.getenv("DEEPL_CAP_CHARS", "500000"))
USAGE_FILE = Path(os.getenv("DEEPL_USAGE_FILE", "/root/r2t2-data/deepl_usage.json"))
MIN_WORDS = int(os.getenv("R2T2_MIN_WORDS", "4"))
MAX_WAIT = int(os.getenv("R2T2_MAX_WAIT_MS", "1200")) / 1000
PUNCT_RE = re.compile(r"[.!?。！？…]+")


class CapExceeded(Exception):
    pass


class UsageError(Exception):
    pass


class Translator:
    def __init__(self, key: str):
        import deepl
        self.deepl = deepl
        self.client = deepl.Translator(key)
        self.lock = asyncio.Lock()
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)

    def period(self):
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def read(self):
        if not USAGE_FILE.exists():
            return {"period": self.period(), "total_chars_consumed": 0}
        try:
            data = json.loads(USAGE_FILE.read_text("utf-8"))
            n = data["total_chars_consumed"]
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise ValueError("invalid count")
            if not isinstance(data["period"], str):
                raise ValueError("invalid period")
            if data["period"] != self.period():
                return {"period": self.period(), "total_chars_consumed": 0}
            return data
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UsageError("DeepL usage file is invalid") from exc

    def remaining(self):
        return max(0, CAP - self.read()["total_chars_consumed"])

    def reserve(self, length: int):
        data = self.read()
        if data["total_chars_consumed"] + length > CAP:
            raise CapExceeded("DeepL character cap reached")
        data["total_chars_consumed"] += length
        tmp = USAGE_FILE.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(USAGE_FILE)
        except OSError as exc:
            raise UsageError("DeepL usage could not be saved") from exc

    async def translate(self, source: str):
        if not source.strip():
            return ""
        async with self.lock:
            self.reserve(len(source))
        try:
            result = await asyncio.to_thread(
                self.client.translate_text, source, target_lang="JA"
            )
            return unicodedata.normalize("NFKC", result.text).strip()
        except (self.deepl.QuotaExceededException, self.deepl.AuthorizationException) as exc:
            raise CapExceeded("DeepL rejected the request") from exc
        except Exception:
            LOG.exception("DeepL translation failed")
            return ""


translator: Translator | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global translator
    key = os.getenv("DEEPL_KEY", "")
    token = os.getenv("R2T2_TOKEN", "")
    if not key or not token:
        raise RuntimeError("DEEPL_KEY and R2T2_TOKEN must be set")
    if CAP <= 0:
        raise RuntimeError("DEEPL_CAP_CHARS must be positive")
    translator = Translator(key)
    LOG.info("DeepL ready. remaining=%d", translator.remaining())
    LOG.info("R2T2 target: %s", ASR_URL)
    yield


app = FastAPI(lifespan=lifespan)


async def send(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def translate_chunk(ws: WebSocket, source: str):
    assert translator is not None
    if not source.strip():
        return
    try:
        ja = await translator.translate(source)
    except (CapExceeded, UsageError) as exc:
        reason = "deepl_cap" if isinstance(exc, CapExceeded) else "usage_corrupted"
        await send(ws, {"type": "fatal", "reason": reason, "msg": str(exc)})
        raise
    if ja:
        await send(ws, {"type": "ja", "text": ja, "src": source})
        await send(ws, {"type": "meta", "remaining": translator.remaining()})


async def r2t2_turn(ws: WebSocket, first_audio: bytes):
    """One R2T2 connection handles one utterance and closes after EOS."""
    LOG.info("opening R2T2 connection: %s", ASR_URL)
    async with websockets.connect(ASR_URL, ping_interval=None, max_size=None) as upstream:
        await upstream.send(json.dumps({
            "channels": 1,
            "sample_rate": 16000,
            "requestId": str(uuid.uuid4()),
            "language": "English",
            "use_vad": False,
            "secret_key": os.getenv("R2T2_ASR_SECRET", "test0102"),
            "mode": "slow",
        }))
        greeting = json.loads(await asyncio.wait_for(upstream.recv(), timeout=15))
        if greeting.get("status") != "connected":
            raise RuntimeError("R2T2 rejected request header")
        LOG.info("R2T2 accepted request header")

        # --- sender: forward PCM, then EOS on client cmd ---
        async def forward():
            await upstream.send(first_audio)
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect()
                if message.get("bytes") is not None:
                    if message["bytes"]:
                        await upstream.send(message["bytes"])
                elif message.get("text") is not None:
                    try:
                        cmd = json.loads(message["text"]).get("cmd")
                    except (ValueError, AttributeError):
                        continue
                    if cmd == "eos":
                        await upstream.send(EOS)
                        LOG.info("sent EOS to R2T2")
                        return
                    if cmd == "ping":
                        await send(ws, {"type": "pong", "t": time.time()})

        sender = asyncio.create_task(forward())
        buffer = ""
        last_emit = time.monotonic()
        fatal = None

        try:
            async for raw in upstream:
                try:
                    packet = json.loads(raw)
                except ValueError:
                    continue
                if packet.get("status") == "error":
                    LOG.error("R2T2 error packet: %s", packet)
                    raise RuntimeError("R2T2 returned an error")
                msg = packet.get("msg")
                if packet.get("status") != "success" or not isinstance(msg, dict):
                    continue
                delta = msg.get("text", "")
                if isinstance(delta, str) and delta:
                    await send(ws, {"type": "en", "text": delta})
                    buffer += delta
                    should_emit = bool(PUNCT_RE.search(buffer)) or (
                        len(buffer.split()) >= MIN_WORDS
                        and time.monotonic() - last_emit >= MAX_WAIT
                    )
                    if should_emit:
                        try:
                            await translate_chunk(ws, buffer)
                        except (CapExceeded, UsageError) as exc:
                            fatal = exc
                            break
                        buffer = ""
                        last_emit = time.monotonic()
                if msg.get("reset") and buffer.strip():
                    try:
                        await translate_chunk(ws, buffer)
                    except (CapExceeded, UsageError) as exc:
                        fatal = exc
                        break
                    buffer = ""
        finally:
            if not sender.done():
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

        if fatal is not None:
            raise fatal
        if buffer.strip():
            await translate_chunk(ws, buffer)
        await send(ws, {"type": "meta", "event": "ready"})


@app.websocket("/stream")
async def stream(ws: WebSocket, token: str = Query(default="")):
    expected = os.getenv("R2T2_TOKEN", "")
    if expected and not hmac.compare_digest(token, expected):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    LOG.info("client connected: %s", ws.client)
    assert translator is not None
    try:
        await send(ws, {"type": "meta", "remaining": translator.remaining()})
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes"):
                await r2t2_turn(ws, message["bytes"])
            elif message.get("text"):
                try:
                    cmd = json.loads(message["text"]).get("cmd")
                except (ValueError, AttributeError):
                    continue
                if cmd == "eos":
                    await send(ws, {"type": "meta", "event": "ready"})
                elif cmd == "ping":
                    await send(ws, {"type": "pong", "t": time.time()})
    except WebSocketDisconnect:
        LOG.info("client disconnected")
    except (CapExceeded, UsageError):
        pass
    except Exception:
        LOG.exception("stream failed")
        await send(ws, {"type": "error", "where": "asr", "msg": "R2T2 connection failed"})
    finally:
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    uvicorn.run("proxy_server:app", host="0.0.0.0", port=8000, log_level="info")
