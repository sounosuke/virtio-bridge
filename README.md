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

# With encryption support
pip install virtio-bridge[crypto]
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

### Config File

Instead of passing flags every time, you can use a TOML config file:

```bash
virtio-bridge --config bridge.toml server
```

See `bridge.toml.example` for the full format. CLI flags always override config values.

### HTTP Mode

| Flag | Default | Description |
|------|---------|-------------|
| `--target` | (required) | URL to forward requests to |
| `--bridge-dir` | (required) | Path to shared bridge directory |
| `--allow-host` | `localhost,127.0.0.1,::1` | Allowed target hosts (comma-separated) |
| `--secret` | off | Shared secret for AES-256-GCM encryption |
| `--listen` | `127.0.0.1:8080` | Client listen address |
| `--timeout` | `30.0` | Response timeout (seconds) |
| `--verbose` | off | Debug logging |

### SOCKS5 Mode

| Flag | Default | Description |
|------|---------|-------------|
| `--bridge-dir` | (required) | Path to shared bridge directory |
| `--allow-host` | `localhost,127.0.0.1,::1` | Allowed destination hosts (comma-separated) |
| `--secret` | off | Shared secret for AES-256-GCM encryption |
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

## Security

virtio-bridge is designed for **trusted VM↔host communication** on a single machine. It is not intended for use across untrusted networks.

**Important: write access to the bridge directory = network access through the host.** Any process or user that can write JSON files to the bridge directory can make the host-side server/relay issue HTTP requests or TCP connections on their behalf. This is by design — virtio-bridge exists to bypass VM network restrictions via the filesystem — but you must ensure only trusted processes have write access to the bridge directory.

**What you should know:**

- Request and response data (including HTTP headers and bodies) is stored as **plaintext files** in the shared directory. Any process with access to the shared folder can read this data.
- Both HTTP server and TCP relay restrict destinations to `localhost` by default via `--allow-host`. If you need to reach other hosts, add them explicitly (e.g., `--allow-host localhost,127.0.0.1,192.168.1.100`).
- The SOCKS5 proxy binds to `127.0.0.1` by default. **Never bind to `0.0.0.0`** in untrusted environments — it would expose the proxy to the network.
- The bridge directory is created with `700` permissions where the filesystem supports it. On VirtioFS, permission enforcement depends on the host configuration.
- Request paths are validated to prevent path traversal attacks. Symlinks in the bridge directory are rejected.
- Stale request/response files are cleaned up automatically (default: 5 minutes), but may contain sensitive data until deleted.

**Recommendations:**

- Keep the bridge directory on a filesystem only accessible to the VM and host user
- Use `--allow-host` to restrict destinations to only the hosts you need (default: localhost only)
- Don't pass sensitive credentials in HTTP headers if other VMs share the same filesystem
- On shared machines, ensure other users cannot write to your bridge directory
- Use `--secret` to encrypt all data on disk with AES-256-GCM when handling sensitive data
- For maximum security, combine `--secret` with `--allow-host` and restrictive filesystem permissions

### Encryption

When `--secret` is set, all request/response files and streaming data are encrypted with AES-256-GCM. The key is derived from the shared secret via PBKDF2-HMAC-SHA256 (100,000 iterations). Both sides must use the same secret.

```bash
# Host
virtio-bridge server --target http://localhost:11434 --bridge-dir ~/.bridge --secret "my-strong-passphrase"

# VM
virtio-bridge client --listen 127.0.0.1:11434 --bridge-dir /mnt/shared/.bridge --secret "my-strong-passphrase"
```

Encrypted files use `.enc` extension instead of `.json`. Each file/chunk has a unique random nonce, preventing replay attacks. Requires `pip install virtio-bridge[crypto]`

## Testing

### Quick Integration Test (no setup required)

Run the full pipeline on a single machine — no VirtioFS, no external services:

```bash
git clone https://github.com/sounosuke/virtio-bridge.git
cd virtio-bridge
python3 -m virtio_bridge.cli integration-test
```

This spins up echo servers, bridge server/client, SOCKS5 proxy, and TCP relay internally, then runs 4 end-to-end tests (HTTP GET, HTTP POST, SOCKS5 connect, SOCKS5 error handling).

### Unit Tests

```bash
pip install pytest
pytest tests/
```

### Real Machine Test (VirtioFS)

For testing across the actual VM↔host boundary with a real service:

**HTTP mode** — on the host, start the server; in the VM, start the client:

```bash
# Host
python3 -m virtio_bridge.cli server \
  --target http://localhost:11434 \
  --bridge-dir ~/shared/.bridge

# VM
python3 -m virtio_bridge.cli client \
  --listen 127.0.0.1:11434 \
  --bridge-dir /mnt/shared/.bridge

# VM: test
curl http://localhost:11434/v1/models
```

**SOCKS5 mode** — on the host, start the TCP relay; in the VM, start the SOCKS5 proxy:

```bash
# Host
python3 -m virtio_bridge.cli tcp-relay --bridge-dir ~/shared/.bridge

# VM
python3 -m virtio_bridge.cli socks \
  --listen 127.0.0.1:1080 \
  --bridge-dir /mnt/shared/.bridge

# VM: test
curl -x socks5h://127.0.0.1:1080 http://localhost:11434/v1/models
```

## Background

This tool was born from investigating [Cowork VM's networking restrictions](https://github.com/anthropics/claude-code/issues/18671). The VM uses Apple's Virtualization.framework which blocks TCP from VM to host, but VirtioFS folder sharing works bidirectionally. We realized the filesystem itself could serve as a transport layer.

## Roadmap

- **v0.1**: HTTP relay with streaming support
- **v0.2**: Generic TCP relay (SOCKS5 proxy mode) for SSH, databases, etc.
- **v0.3**: Host allowlisting (`--allow-host`), config file support
- **v0.4 (current)**: AES-256-GCM encryption (`--secret`)
- **v0.5**: TUN/TAP for full VPN-over-filesystem (experimental)

## License

MIT
