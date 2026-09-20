# R2T2 Architecture

## End-to-End Flow

Client captures 16kHz PCM, streams via WebSocket to Vast.ai server.
Server runs Confucius4-R2T2 ASR, then sends English deltas to client.
Windower aggregates into sentences, DeepL translates to Japanese.
Client renders JA and synthesizes speech via local VOICEVOX.

## DeepL Stop Policy (Option B)

- Cap: 500,000 chars (source English length)
- Reset: 1st of each month 00:00 UTC (automatic)
- On exceed: server sends {"type":"fatal","reason":"deepl_cap"}
  then closes WebSocket with code 1011. Client exits immediately.

## Message Types

Server -> Client (text frames):

    {"type":"en","text":"..."}                  incremental English
    {"type":"ja","text":"...","src":"..."}      translated Japanese
    {"type":"meta","remaining":N}                DeepL chars left
    {"type":"meta","event":"ready"}              EOS acknowledged
    {"type":"error","where":"asr","msg":"..."}   non-fatal
    {"type":"fatal","reason":"deepl_cap"}        fatal

Client -> Server:

    [binary]                    16kHz mono int16 PCM chunk
    {"cmd":"eos"}               end of speech
    {"cmd":"ping"}              keepalive
