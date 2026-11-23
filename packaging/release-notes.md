## Download

| Where | What to get |
|---|---|
| **Windows** | `detok-windows-x86_64.exe`: double-click it, the UI opens in your browser |
| **Linux desktop** | `detok-linux-x86_64`: `chmod +x detok-linux-x86_64 && ./detok-linux-x86_64` |
| **Server / homelab** | Docker image `ghcr.io/zgenny/detok` (amd64 + arm64, see `docker-compose.yml` in the README) |
| **Python** | `pip install detok-*.whl` |

On first launch Detok downloads Chromium (~150 MB, only once).
On Windows, SmartScreen may warn that the executable isn't signed: "More info" → "Run anyway".

Each file has a matching `.sha256` next to it to verify its integrity.
