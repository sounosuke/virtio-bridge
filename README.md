# virtio-bridge

HTTP and TCP relay over shared filesystem for VMs with restricted networking.

If your VM can share a folder with the host but can't make TCP connections to it, virtio-bridge lets you reach the host's services anyway — no tunnels, no external servers, just filesystem I/O.

## The Problem

VMs using Apple's Virtualization.framework (Cowork, Tart, Lima, etc.) often share a folder with the host via VirtioFS, but block TCP connections from VM to host. This means you can't reach `localhost` services (LLM servers, databases, Docker containers) from inside the VM.

Common workarounds like bore.pub or SSH tunnels require external servers and add latency. virtio-bridge solves this using only the shared filesystem.

## How It Works

### HTTP Mode (v1)

For when you need to reach a specific HTTP service:

```
VM (client)                Shared Folder              Host (server)
┌──────────────┐          ┌──────────────┐          ┌──────────────┐
│ Your app     │          │ .bridge/     │          │ localhost    │
│  curl, etc.  │          │  requests/   │          │  :11434      │
│      │       │          │  responses/  │          │  (your svc)  │
│      ▼       │          │              │          │      ▲       │
│ HTTP proxy   │──write──▶│  {id}.json   │◀─watch───│  forwarder   │
│ :11434       │◀──read───│  {id}.json   │──write──▶│      │       │
└──────────────┘          └──────────────┘          └──────────────┘
```

### SOCKS5 Mode (v2)

For when you need to reach any TCP service (databases, SSH, etc.):

```
VM (socks)                 Shared Folder              Host (tcp-relay)
┌──────────────┐          ┌──────────────┐          ┌──────────────┐
│ Your app     │          │ .bridge/tcp/ │          │ any host:port│
│  psql, ssh   │          │  {conn}/     │          │  PostgreSQL  │
│      │       │          │   connect    │          │  SSH, Redis  │
│      ▼       │          │   upstream   │          │      ▲       │
│ SOCKS5 proxy │──write──▶│   downstream │◀─watch───│  TCP relay   │
│ :1080        │◀──read───│              │──write──▶│              │
└──────────────┘          └──────────────┘          └──────────────┘
```

## Quick Start

### Install

```bash
# Basic install (polling-based file watching)
pip install virtio-bridge

# With native file watching (recommended for macOS host)
pip install virtio-bridge[watch]
```

Or clone and install:
```bash
git clone https://github.com/sounosuke/virtio-bridge.git
cd virtio-bridge
pip install -e ".[watch]"
```

### HTTP Mode

**On the host (Mac):**

```bash
virtio-bridge server \
  --target http://localhost:11434 \
  --bridge-dir ~/shared-folder/.bridge
```

**On the VM:**

```bash
virtio-bridge client \
  --listen 127.0.0.1:11434 \
  --bridge-dir /mnt/shared-folder/.bridge
```

**From your app (inside VM):**

```bash
# Works exactly like talking to localhost
curl http://localhost:11434/v1/models

# Streaming works too
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

### SOCKS5 Mode

**On the host (Mac):**

```bash
virtio-bridge tcp-relay --bridge-dir ~/shared-folder/.bridge
```

**On the VM:**

```bash
virtio-bridge socks \
  --listen 127.0.0.1:1080 \
  --bridge-dir /mnt/shared-folder/.bridge
```

**From your app (inside VM):**

```bash
# Connect to any TCP service on the host via SOCKS5
curl --socks5 127.0.0.1:1080 http://localhost:5432/  # PostgreSQL
ssh -o ProxyCommand='nc -X 5 -x 127.0.0.1:1080 %h %p' user@localhost  # SSH

# Or set the environment variable for everything
export ALL_PROXY=socks5://127.0.0.1:1080
curl http://localhost:6379/  # Redis, etc.
```

### Test the connection

```bash
# HTTP mode: writes a test request and waits for the server to respond
virtio-bridge test --bridge-dir /mnt/shared-folder/.bridge
```

## Use Cases

- **Local LLM inference**: Reach vllm, llama.cpp, Ollama, etc. running on the host
- **Docker services**: Access databases, APIs, and other containers on the host
- **Development servers**: Connect to webpack-dev-server, Vite, etc.
- **Database access**: Connect to PostgreSQL, MySQL, Redis via SOCKS5
- **SSH tunneling**: SSH to the host or through it via SOCKS5
- **Any TCP service**: If it speaks TCP, SOCKS5 mode can relay it

## Platform Support

| Platform | Client/SOCKS (VM) | Server/TCP-relay (Host) |
|----------|-------------------|-------------------------|
| Linux    | inotify (fast)    | inotify (fast)          |
| macOS    | watchdog/polling  | watchdog/polling        |

File watching priority: inotify (Linux) → watchdog (macOS/Linux, `pip install virtio-bridge[watch]`) → polling fallback (~100ms).

## Configuration

### HTTP Mode

| Flag | Default | Description |
|------|---------|-------------|
| `--target` | (required) | URL to forward requests to |
| `--bridge-dir` | (required) | Path to shared bridge directory |
| `--listen` | `127.0.0.1:8080` | Client listen address |
| `--timeout` | `30.0` | Response timeout (seconds) |
| `--verbose` | off | Debug logging |

### SOCKS5 Mode

| Flag | Default | Description |
|------|---------|-------------|
| `--bridge-dir` | (required) | Path to shared bridge directory |
| `--listen` | `127.0.0.1:1080` | SOCKS5 listen address |
| `--verbose` | off | Debug logging |

## How It Compares

| Approach | External Server | Port Fixed | Streaming | Any TCP | Setup |
|----------|----------------|------------|-----------|---------|-------|
| bore.pub | Yes | No | Yes | No | Easy |
| SSH tunnel | Yes | Yes | Yes | Yes | Medium |
| **virtio-bridge** | **No** | **Yes** | **Yes** | **Yes** | **Easy** |

## Running Both Modes

You can run HTTP mode and SOCKS5 mode simultaneously with the same bridge directory. On the host side, run both:

```bash
virtio-bridge server --target http://localhost:11434 --bridge-dir ~/shared/.bridge &
virtio-bridge tcp-relay --bridge-dir ~/shared/.bridge &
```

## Background

This tool was born from investigating [Cowork VM's networking restrictions](https://github.com/anthropics/claude-code/issues/18671). The VM uses Apple's Virtualization.framework which blocks TCP from VM to host, but VirtioFS folder sharing works bidirectionally. We realized the filesystem itself could serve as a transport layer.

## Roadmap

- **v1**: HTTP relay with streaming support
- **v2 (current)**: Generic TCP relay (SOCKS5 proxy mode) for SSH, databases, etc.
- **v3**: TUN/TAP for full VPN-over-filesystem (experimental)

## License

MIT
