# qwen3.5:27b — GPU box bootstrap

Rent a GPU box, run one script, and it is ready: Ollama serving **qwen3.5:27b**, GPU monitoring running,
and the central server (llms.bytepaths.io) able to tunnel in for monitoring and the Qwen browser agent.

## Use it on a new box

On the GPU box (Vast.ai SSH session, as root):

```bash
git clone https://github.com/gz55555/qwen.git && cd qwen && ./setup.sh
```

Re-running is safe: every step checks first and skips what is already done.

At the end it prints **one command for the central server**, like:

```
/opt/ai-monitoring/scripts/gpu-up.sh --host 1.2.3.4 --port 41234 --fingerprint SHA256:… --instance-id vast-01 --model qwen3.5:27b --yes
```

Run it on the central server (or paste it to Claude there). It repoints the monitoring tunnel, the Qwen-agent
tunnel, the Prometheus targets and the post-GPU hooks, then waits until live GPU telemetry arrives.

> Optional: if the central registration endpoint is enabled, pass `--token-file F` (or set `LLMMON_REGISTER_TOKEN`)
> and `./setup.sh` does this last step itself.
>
> Fingerprint check: the central server compares the box's host key exactly, so copy the printed command
> rather than retyping it (`0`/`O` and `l`/`1` are easy to confuse).

## What `setup.sh` does

| step | what | notes |
|---|---|---|
| 1 preflight | GPU, disk, supervisor, python venv, sshd forwarding | installs missing packages with apt |
| 2 SSH access | adds the central server's **public** keys to `/root/.ssh/authorized_keys` | the monitoring and agent keys are restricted (tunnel to one port only, no shell). A guard program (`llmmon-keys-guard`) re-adds them every 30 s if something rewrites the file, and never touches other keys |
| 3 Ollama | installs Ollama (pinned version) and runs it under supervisor on `127.0.0.1:11434` | an Ollama that is already running is **never restarted** |
| 4 model | `ollama pull qwen3.5:27b` if missing, then loads it into VRAM | skipped if you copied `/root/.ollama` from the old box |
| 5 monitoring | llmmon agent: GPU exporter (`:9835`) and container node_exporter (`:9100`), loopback only | checksummed bundle; node_exporter is downloaded and verified against a pinned hash |
| 6 register | prints the central command, or registers itself if you give it a token | the host key fingerprint is read from the box itself |

Log: `/var/log/llmmon/setup-*.log`

## Moving data from the old box (optional, saves the download)

```bash
# on the NEW box, if the old one is still reachable:
rsync -a --info=progress2 -e "ssh -p OLD_PORT" root@OLD_IP:/root/.ollama/ /root/.ollama/
./setup.sh      # sees the model is present, skips the pull
```

## Fully hands-off (Vast "on-start script")

Paste this into the instance template's on-start field so a new box sets itself up on boot:

```bash
cd /root && [ -d qwen ] || git clone https://github.com/gz55555/qwen.git; cd qwen && git pull -q; ./setup.sh --no-register > /var/log/qwen-setup.log 2>&1 &
```

## Options

```
./setup.sh --no-warm              # don't load the model into VRAM
./setup.sh --model other:tag      # override model.env for one run
./setup.sh --host IP --port PORT  # if Vast's PUBLIC_IPADDR / VAST_TCP_PORT_22 are not visible
./setup.sh --no-register          # never ask for the token, just print the central command
```

## Another model

Copy this repo and edit `model.env` (`MODEL`, `MODEL_SIZE_HINT`, `NUM_CTX`). Everything else is model-agnostic.
On the central server, `scripts/build-gpu-repo.sh DIR --model NAME` regenerates `setup.sh`, `bootstrap/` and `model.env`
from the current templates.

## Security

- This repo contains **public keys only**; no private keys, passwords or tokens.
- Ollama and the exporters listen on 127.0.0.1 only. The central server reaches them through SSH tunnels
  that are pinned to this box's host key.
- To revoke the central server's access: `/opt/llmmon-agent/scripts/uninstall.sh`. That removes the
  monitoring and agent keys and the guard; it keeps the admin key.
