# virtio-bridge

HTTP relay over shared filesystem for VMs with restricted networking.

If your VM can share a folder with the host but can't make TCP connections to it, virtio-bridge lets you reach the host's HTTP services anyway — no tunnels, no external servers, just filesystem I/O.

## The Problem

VMs using Apple's Virtualization.framework (Cowork, Tart, Lima, etc.) often share a folder with the host via VirtioFS, but block TCP connections from VM to host. This means you can't reach `localhost` services (LLM servers, databases, Docker containers) from inside the VM.

Common workarounds like bore.pub or SSH tunnels require external servers and add latency. virtio-bridge solves this using only the shared filesystem.

## How It Works

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

1. **Client** (VM) runs an HTTP proxy. Your app sends requests to it like a normal server.
2. Client writes the request as a JSON file to the shared directory.
3. **Server** (host) watches for new files, reads the request, forwards it to the real service.
4. Server writes the response back as a JSON file.
5. Client reads the response and returns it to your app.

Streaming (SSE) is supported — chunks are appended to a file and read incrementally.

## Quick Start

### Install

```bash
# Both host and VM
pip install virtio-bridge
```

Or clone and install:
```bash
git clone https://github.com/sonosuke/virtio-bridge.git
cd virtio-bridge
pip install -e .
```

### Run

**On the host (Mac):**

```bash
# Forward requests to your local LLM server
virtio-bridge server \
  --target http://localhost:11434 \
  --bridge-dir ~/shared-folder/.bridge
```

**On the VM:**

```bash
# Start proxy on the same port as the target service
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

### Test the connection

```bash
# From the VM: writes a test request and waits for the server to respond
virtio-bridge test --bridge-dir /mnt/shared-folder/.bridge
```

## Use Cases

- **Local LLM inference**: Reach vllm, llama.cpp, Ollama, etc. running on the host
- **Docker services**: Access databases, APIs, and other containers on the host
- **Development servers**: Connect to webpack-dev-server, Vite, etc.
- **Any HTTP service**: If it speaks HTTP, virtio-bridge can relay it

## Platform Support

| Platform | Client (VM) | Server (Host) |
|----------|-------------|---------------|
| Linux    | inotify (fast) | polling |
| macOS    | polling     | polling |

File watching uses inotify on Linux for low-latency detection (~10ms). Falls back to polling (~100ms) where inotify isn't available.

## Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--target` | (required) | URL to forward requests to |
| `--bridge-dir` | (required) | Path to shared bridge directory |
| `--listen` | `127.0.0.1:8080` | Client listen address |
| `--timeout` | `30.0` | Response timeout (seconds) |
| `--verbose` | off | Debug logging |

## How It Compares

| Approach | External Server | Port Fixed | Streaming | Setup |
|----------|----------------|------------|-----------|-------|
| bore.pub | Yes | No | Yes | Easy |
| SSH tunnel | Yes | Yes | Yes | Medium |
| **virtio-bridge** | **No** | **Yes** | **Yes** | **Easy** |

## Roadmap

- **v1 (current)**: HTTP relay with streaming support
- **v2**: Generic TCP relay (SOCKS proxy mode) for SSH, databases, etc.
- **v3**: TUN/TAP for full VPN-over-filesystem (experimental)

## Background

This tool was born from investigating [Cowork VM's networking restrictions](https://github.com/anthropics/claude-code/issues/18671). The VM uses Apple's Virtualization.framework which blocks TCP from VM to host, but VirtioFS folder sharing works bidirectionally. We realized the filesystem itself could serve as a transport layer.

## License

MIT
