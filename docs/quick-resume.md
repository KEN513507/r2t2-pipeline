# Vast.ai quick resume

The instance keeps its persistent volume while stopped. `/workspace/onstart.sh`
starts R2T2 and the proxy whenever Vast.ai starts the instance. The script reads
`/root/.config/r2t2/server.env` without printing it.

## Resume locally

```bash
./scripts/start-all.sh
```

The script starts instance `51715606` by default, waits until Vast.ai reports it
as running, obtains the current SSH URL, creates the local `8000` tunnel, starts
VOICEVOX if needed, then launches `client.py`.

Set `R2T2_VAST_INSTANCE_ID` to use another instance:

```bash
R2T2_VAST_INSTANCE_ID=12345678 ./scripts/start-all.sh
```

## Stop the remote GPU

Exit the client, then run:

```bash
./scripts/stop-vast.sh
```

This invokes `vastai stop instance`; it does not destroy the instance or its
storage volume.

## Service logs after resume

```bash
ssh -p <current-port> root@<current-host> 'tail -50 /var/log/onstart.log'
```

The current SSH endpoint is available from:

```bash
.venv/bin/vastai ssh-url 51715606
```
