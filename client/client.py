#!/usr/bin/env python3
"""
client.py — R2T2 Local Client (Linux + GTX 970)
================================================
  - Mic capture : sounddevice / 16kHz / 16bit / mono / 200ms chunk
  - Transport   : WebSocket (raw binary for audio, JSON for control)
  - UI          : CUITwoLine (差し替え可能 via UIBase)
  - TTS         : VOICEVOX Engine (localhost:50021, speaker=3)
  - Player      : pygame mixer (音声優先・古いものドロップ)
  - EOS         : 無音 800ms で {"cmd":"eos"}
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from typing import Optional

import numpy as np
import pygame
import requests
import sounddevice as sd
import websockets
from websockets.exceptions import (
    ConnectionClosed,
    WebSocketException,
)

# ============================================================
# 0. Config
# ============================================================
LOG = logging.getLogger("r2t2.client")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

WS_URL_DEFAULT   = os.environ.get("R2T2_WS_URL", "ws://127.0.0.1:8000/stream")
AUTH_TOKEN       = os.environ.get("R2T2_TOKEN", "")

SAMPLE_RATE      = 16_000
CHUNK_MS         = 200
CHANNELS         = 1
DTYPE            = "int16"
INPUT_DEVICE_ENV = os.environ.get("R2T2_INPUT_DEVICE") or None
BLOCK_SIZE       = int(SAMPLE_RATE * CHUNK_MS / 1000)

SILENCE_RMS      = int(os.environ.get("R2T2_SILENCE_RMS", "300"))
SILENCE_CHUNKS   = int(os.environ.get("R2T2_SILENCE_CHUNKS", "4"))

VOICEVOX_URL     = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
SPEAKER_ID       = int(os.environ.get("VOICEVOX_SPEAKER", "3"))

TX_QUEUE_MAX     = 32
TTS_QUEUE_MAX    = 2
PLAY_QUEUE_MAX   = 2

WS_MAX_RETRIES   = 5


# ============================================================
# 1. Exceptions
# ============================================================
class FatalServerError(RuntimeError):
    """サーバーから fatal 通知を受信。即座に全停止すべき。"""


# ============================================================
# 2. UI
# ============================================================
class UIBase:
    """UI 差し替え用の抽象。Tkinter 化するならこれを継承する。"""
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def render_en(self, text: str) -> None: ...
    def render_ja(self, text: str) -> None: ...
    def set_status(self, text: str) -> None: ...


class CUITwoLine(UIBase):
    """ターミナル 2段表示（ANSI リドロー）。"""

    def __init__(self) -> None:
        self.en = ""
        self.ja = ""
        self.status = "init..."
        self._cursor_hidden = False

    def start(self) -> None:
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()
        self._cursor_hidden = True
        self._redraw()

    def stop(self) -> None:
        if self._cursor_hidden:
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()
        self._cursor_hidden = False

    def _redraw(self) -> None:
        sys.stdout.write("\033[H\033[J")
        sys.stdout.write(f"\033[1;36mEN\033[0m: {self.en}\n")
        sys.stdout.write(f"\033[1;33mJA\033[0m: {self.ja}\n")
        sys.stdout.write(f"\n\033[2m[{self.status}]\033[0m\n")
        sys.stdout.flush()

    def render_en(self, text: str) -> None:
        self.en = text
        self._redraw()

    def render_ja(self, text: str) -> None:
        self.ja = text
        self._redraw()

    def set_status(self, text: str) -> None:
        self.status = text
        self._redraw()

    def clear(self) -> None:
        self.en = ""
        self.ja = ""
        self._redraw()


# ============================================================
# 3. Mic
# ============================================================
def pick_input_device(preferred: Optional[str]) -> int:
    """★3: 見つからなければ候補を提示して raise。"""
    try:
        devices = sd.query_devices()
    except Exception as e:
        raise RuntimeError(f"sounddevice enumeration failed: {e}") from e

    if preferred:
        try:
            idx = int(preferred)
            info = sd.query_devices(idx)
            if info["max_input_channels"] < 1:
                raise ValueError(f"device {idx} has no input channel")
            return idx
        except Exception as e:
            LOG.warning("preferred device %s unusable: %s", preferred, e)

    try:
        default_idx = sd.default.device[0]
        if default_idx is not None and default_idx >= 0:
            return int(default_idx)
    except Exception:
        pass

    for i, d in enumerate(devices):
        if d["max_input_channels"] >= 1:
            LOG.info("auto-selected input device [%d] %s", i, d["name"])
            return i

    lines = "\n".join(f"  [{i}] {d['name']}" for i, d in enumerate(devices))
    raise RuntimeError(f"no input device found. candidates:\n{lines}")


class MicStreamer:
    def __init__(self, loop: asyncio.AbstractEventLoop,
                 tx_q: "asyncio.Queue[bytes]", device: int) -> None:
        self.loop = loop
        self.tx_q = tx_q
        self.device = device
        self.stream: Optional[sd.RawInputStream] = None

    def start(self) -> None:
        try:
            self.stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=CHANNELS,
                dtype=DTYPE,
                device=self.device,
                callback=self._cb,
            )
            self.stream.start()
            LOG.info("mic started (device=%d, block=%d)", self.device, BLOCK_SIZE)
        except Exception as e:
            raise RuntimeError(f"mic start failed: {e}") from e

    def _cb(self, indata, frames, time_info, status) -> None:
        if status:
            LOG.debug("mic status: %s", status)
        self.loop.call_soon_threadsafe(self._push, bytes(indata))

    def _push(self, data: bytes) -> None:
        try:
            self.tx_q.put_nowait(data)
        except asyncio.QueueFull:
            try:
                self.tx_q.get_nowait()  # ★2: 最古ドロップ
            except asyncio.QueueEmpty:
                pass
            try:
                self.tx_q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def stop(self) -> None:
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


# ============================================================
# 4. Silence / EOS
# ============================================================
class SilenceDetector:
    def __init__(self, threshold: int, chunks: int) -> None:
        self.threshold = threshold
        self.chunks = chunks
        self._count = 0
        self._in_speech = False

    def push(self, pcm: bytes) -> bool:
        arr = np.frombuffer(pcm, dtype=np.int16)
        if arr.size == 0:
            return False
        rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        if rms >= self.threshold:
            self._count = 0
            self._in_speech = True
            return False
        if not self._in_speech:
            return False
        self._count += 1
        if self._count >= self.chunks:
            self._in_speech = False
            self._count = 0
            return True
        return False


# ============================================================
# 5. VOICEVOX TTS
# ============================================================
class VoicevoxTTS:
    def __init__(self, url: str, speaker: int,
                 autostart_cmd: Optional[list[str]] = None) -> None:
        self.url = url.rstrip("/")
        self.speaker = speaker
        self.session = requests.Session()
        self.autostart_cmd = autostart_cmd
        self._proc: Optional[subprocess.Popen] = None
        self._last_health = 0.0
        self._healthy = False

    def ensure_alive(self) -> bool:
        now = time.monotonic()
        if now - self._last_health < 5.0:
            return self._healthy
        self._last_health = now
        try:
            r = self.session.get(f"{self.url}/version", timeout=2)
            r.raise_for_status()
            self._healthy = True
            return True
        except requests.RequestException:
            self._healthy = False
            if self.autostart_cmd and self._proc is None:
                LOG.warning("VOICEVOX down, attempting autostart")
                try:
                    self._proc = subprocess.Popen(
                        self.autostart_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception as e:
                    LOG.error("autostart failed: %s", e)
            return False

    def synth(self, text: str) -> Optional[bytes]:
        if not self.ensure_alive():
            LOG.warning("VOICEVOX unavailable; skipping TTS")
            return None
        try:
            q = self.session.post(
                f"{self.url}/audio_query",
                params={"text": text, "speaker": self.speaker},
                timeout=10,
            )
            q.raise_for_status()
            w = self.session.post(
                f"{self.url}/synthesis",
                params={"speaker": self.speaker},
                json=q.json(),
                timeout=30,
            )
            w.raise_for_status()
            return w.content
        except requests.Timeout:
            LOG.warning("VOICEVOX timeout for %r", text[:40])
            return None
        except requests.RequestException as e:
            LOG.warning("VOICEVOX error: %s", e)
            self._healthy = False
            return None
        except (ValueError, KeyError) as e:
            LOG.error("VOICEVOX malformed response: %s", e)
            return None

    def shutdown(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ============================================================
# 6. Audio player
# ============================================================
class AudioPlayer:
    def __init__(self) -> None:
        self._ok = False
        try:
            pygame.mixer.pre_init(frequency=24_000, size=-16, channels=1, buffer=512)
            pygame.mixer.init()
            self._ok = True
            LOG.info("pygame mixer ready")
        except pygame.error as e:
            LOG.warning("pygame mixer init failed: %s (音声は無効)", e)
            self._ok = False

    async def play(self, wav_bytes: bytes) -> None:
        if not self._ok:
            return
        try:
            snd = pygame.mixer.Sound(file=io.BytesIO(wav_bytes))
        except pygame.error as e:
            LOG.warning("Sound load failed: %s", e)
            return
        try:
            ch = snd.play()
        except pygame.error as e:
            LOG.warning("play failed: %s", e)
            return
        if ch is None:
            return
        try:
            while ch.get_busy():
                await asyncio.sleep(0.03)
        except pygame.error as e:
            LOG.warning("playback interrupted: %s", e)

    def stop_all(self) -> None:
        if not self._ok:
            return
        try:
            pygame.mixer.stop()
        except pygame.error:
            pass


# ============================================================
# 7. Drop-old queue
# ============================================================
class DropOldQueue:
    def __init__(self, maxsize: int) -> None:
        self._q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)

    def qsize(self) -> int:
        return self._q.qsize()

    def empty(self) -> bool:
        return self._q.empty()

    def put_drop_old(self, item: object) -> None:
        if self._q.full():
            try:
                self._q.get_nowait()
                LOG.debug("queue full: dropped oldest")
            except asyncio.QueueEmpty:
                pass
        try:
            self._q.put_nowait(item)
        except asyncio.QueueFull:
            LOG.debug("queue still full; discarding new item")

    async def get(self) -> object:
        return await self._q.get()


# ============================================================
# 8. WS connect with retry
# ============================================================
def build_ws_url(base: str, token: str) -> str:
    if not token:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}token={token}"


async def connect_with_retry(url: str, *, max_retries: int = WS_MAX_RETRIES):
    delay = 0.5
    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            ws = await websockets.connect(
                url,
                max_size=2 ** 20,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )
            LOG.info("ws connected (attempt %d)", attempt)
            return ws
        except (OSError, WebSocketException) as e:
            last_err = e
            LOG.warning("ws connect failed (%d/%d): %s",
                        attempt, max_retries, e)
            if attempt == max_retries:
                break
            wait = delay + random.uniform(0, delay * 0.3)
            await asyncio.sleep(wait)
            delay = min(delay * 2, 15.0)
    raise RuntimeError(f"ws connect failed after {max_retries} attempts: {last_err}")


# ============================================================
# 9. Workers
# ============================================================
async def tx_worker(ws, tx_q: asyncio.Queue, silence: SilenceDetector,
                    stop_evt: asyncio.Event) -> None:
    try:
        while not stop_evt.is_set():
            try:
                pcm = await asyncio.wait_for(tx_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await ws.send(pcm)
            except ConnectionClosed as e:
                LOG.warning("tx: connection closed: %s", e)
                return
            if silence.push(pcm):
                try:
                    await ws.send(json.dumps({"cmd": "eos"}))
                    LOG.info("EOS sent")
                except ConnectionClosed:
                    return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOG.exception("tx_worker crashed: %s", e)
    finally:
        stop_evt.set()


async def rx_worker(ws, ui: UIBase, synth_q: DropOldQueue,
                    en_buf: list, ja_buf: list,
                    stop_evt: asyncio.Event) -> None:
    try:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = msg.get("type")
            if t == "en":
                text = msg.get("text", "")
                if not text:
                    continue
                en_buf.append(text)
                ui.render_en("".join(en_buf))
            elif t == "ja":
                text = msg.get("text", "")
                if not text:
                    continue
                ja_buf.append(text)
                ui.render_ja("".join(ja_buf))
                synth_q.put_drop_old(text)
            elif t == "meta":
                if msg.get("event") == "ready":
                    en_buf.clear()
                    ja_buf.clear()
                    ui.render_en("")
                    ui.render_ja("")
                    ui.set_status("ready")
                rem = msg.get("remaining")
                if rem is not None:
                    ui.set_status(f"ready | DeepL残: {rem:,} 文字")
            elif t == "fatal":
                reason = msg.get("reason", "unknown")
                LOG.critical("FATAL from server: %s", reason)
                ui.set_status(f"⚠ FATAL: {reason} — サービス停止")
                # ★5: 伝播して amain を抜けさせる
                raise FatalServerError(reason)
            elif t == "error":
                LOG.warning("server error: %s", msg)
    except ConnectionClosed:
        LOG.warning("rx: connection closed")
    except FatalServerError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOG.exception("rx_worker crashed: %s", e)
    finally:
        stop_evt.set()


async def synth_worker(tts: VoicevoxTTS, synth_q: DropOldQueue,
                       play_q: DropOldQueue, stop_evt: asyncio.Event) -> None:
    try:
        while not stop_evt.is_set():
            try:
                text = await asyncio.wait_for(synth_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            wav = await asyncio.to_thread(tts.synth, text)
            if wav:
                play_q.put_drop_old(wav)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOG.exception("synth_worker crashed: %s", e)
    finally:
        stop_evt.set()


async def play_worker(player: AudioPlayer, play_q: DropOldQueue,
                      stop_evt: asyncio.Event) -> None:
    try:
        while not stop_evt.is_set():
            try:
                wav = await asyncio.wait_for(play_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not isinstance(wav, (bytes, bytearray)):
                continue
            # ★4: 既に新しい WAV が待っていれば古い方を捨てる
            if play_q.qsize() > 0:
                LOG.debug("dropping stale audio before playback")
                continue
            play_task = asyncio.create_task(player.play(wav))
            while not play_task.done():
                if play_q.qsize() > 0:
                    # 新しい音声到着 → 割り込み
                    player.stop_all()
                    break
                await asyncio.sleep(0.03)
            try:
                await play_task
            except Exception:
                pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOG.exception("play_worker crashed: %s", e)
    finally:
        stop_evt.set()


# ============================================================
# 10. main
# ============================================================
async def amain() -> int:
    # ---- config check (★1) ----
    if not AUTH_TOKEN:
        LOG.warning("R2T2_TOKEN not set")

    # ---- UI ----
    ui: UIBase = CUITwoLine()
    ui.start()

    # ---- TTS ----
    tts = VoicevoxTTS(VOICEVOX_URL, SPEAKER_ID)
    if not tts.ensure_alive():
        ui.set_status(f"VOICEVOX未接続: {VOICEVOX_URL} (テキストのみ)")

    # ---- player ----
    player = AudioPlayer()

    # ---- queues ----
    tx_q: asyncio.Queue = asyncio.Queue(maxsize=TX_QUEUE_MAX)
    synth_q = DropOldQueue(TTS_QUEUE_MAX)
    play_q = DropOldQueue(PLAY_QUEUE_MAX)

    # ---- state ----
    en_buf: list = []
    ja_buf: list = []
    stop_evt = asyncio.Event()

    # ---- signal handling (★2) ----
    loop = asyncio.get_running_loop()

    def _on_signal(signum):
        LOG.info("signal %s received", signum)
        stop_evt.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda s, _f: _on_signal(s))
            except Exception:
                pass

    # ---- mic device pick ----
    try:
        device_idx = pick_input_device(INPUT_DEVICE_ENV)
    except RuntimeError as e:
        ui.set_status(f"mic error: {e}")
        LOG.critical("mic device error: %s", e)
        ui.stop()
        tts.shutdown()
        return 2

    mic = MicStreamer(loop, tx_q, device_idx)
    silence = SilenceDetector(SILENCE_RMS, SILENCE_CHUNKS)

    # ---- connect ----
    ws_url = build_ws_url(WS_URL_DEFAULT, AUTH_TOKEN)
    ui.set_status(f"connecting to {ws_url}")

    ws = None
    try:
        ws = await connect_with_retry(ws_url)
        ui.set_status("connected")

        try:
            mic.start()
        except RuntimeError as e:
            ui.set_status(f"mic start error: {e}")
            LOG.critical("mic start error: %s", e)
            return 2

        tasks = [
            asyncio.create_task(
                tx_worker(ws, tx_q, silence, stop_evt), name="tx"),
            asyncio.create_task(
                rx_worker(ws, ui, synth_q, en_buf, ja_buf, stop_evt), name="rx"),
            asyncio.create_task(
                synth_worker(tts, synth_q, play_q, stop_evt), name="synth"),
            asyncio.create_task(
                play_worker(player, play_q, stop_evt), name="play"),
            asyncio.create_task(stop_evt.wait(), name="stop"),
        ]

        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )

        # fatal が来たか確認
        exit_code = 0
        for t in done:
            if t.get_name() == "stop":
                continue
            exc = t.exception()
            if isinstance(exc, FatalServerError):
                LOG.critical("FatalServerError: %s", exc)
                exit_code = 3
            elif exc is not None:
                LOG.error("task %s died: %r", t.get_name(), exc)
                exit_code = 4

        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    except RuntimeError as e:
        LOG.critical("connection failed: %s", e)
        ui.set_status(f"connection failed: {e}")
        exit_code = 5
    except FatalServerError as e:
        LOG.critical("fatal: %s", e)
        exit_code = 3
    except Exception as e:
        LOG.exception("unhandled in amain: %s", e)
        exit_code = 1
    finally:
        # ★5: 後始末を確実に
        try:
            mic.stop()
        except Exception:
            pass
        try:
            player.stop_all()
        except Exception:
            pass
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        try:
            tts.shutdown()
        except Exception:
            pass
        ui.set_status("stopped")
        ui.stop()

    return exit_code


def main() -> None:
    try:
        rc = asyncio.run(amain())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
