#!/usr/bin/env python3
"""VOICEVOX synthesis and local playback smoke test."""

import io
import os
import sys
import time

import pygame
import requests

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "3"))
PHRASES = [
    "こんにちは、テストです。",
    "私は車を買いたいです。",
    "今日はいい天気ですね。",
]


def check_alive() -> bool:
    try:
        response = requests.get(f"{VOICEVOX_URL}/version", timeout=3)
        response.raise_for_status()
        print(f"VOICEVOX version: {response.text.strip()}")
        return True
    except requests.RequestException as exc:
        print(f"VOICEVOX unreachable: {exc}")
        print(f"URL: {VOICEVOX_URL}")
        return False


def synthesize(text: str) -> bytes:
    query = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": SPEAKER},
        timeout=10,
    )
    query.raise_for_status()
    synthesis = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": SPEAKER},
        json=query.json(),
        timeout=30,
    )
    synthesis.raise_for_status()
    return synthesis.content


def main() -> int:
    if not check_alive():
        return 1

    pygame.mixer.pre_init(frequency=24000, size=-16, channels=1, buffer=512)
    pygame.mixer.init()
    print("pygame mixer ready\n")

    for index, phrase in enumerate(PHRASES, 1):
        print(f"[{index}/{len(PHRASES)}] {phrase}")
        started = time.monotonic()
        wav = synthesize(phrase)
        print(f"  synthesis: {(time.monotonic() - started) * 1000:.0f} ms, {len(wav)} bytes")
        channel = pygame.mixer.Sound(file=io.BytesIO(wav)).play()
        while channel.get_busy():
            time.sleep(0.03)
        print("  playback done\n")

    print("TTS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
