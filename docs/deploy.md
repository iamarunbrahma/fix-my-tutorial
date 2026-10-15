# Deploying Fix My Tutorial

## Production: Hugging Face Space + Token Factory Sandboxes

1. `uv run python -m fixmytutorial.rot_index sync` (first time ever: add `--bootstrap`). This merges and uploads local runs and the spend ledger, so the Space restores the true lifetime spend on boot.
2. `uv run python scripts/deploy_space.py`. It creates the Space and sets secrets and variables (`SANDBOX_BACKEND=contree`), then uploads the app.
3. Check `/healthz`, then `/admin/ledger?token=...` (lifetime spend should match the local ledger), then run one fixture tutorial.

Tutorial code runs inside Token Factory Sandboxes microVMs, never on the Space itself.

## Fallback: Docker sandbox on a Linux VM

Use this only if Sandboxes access is unavailable. Tutorial code is untrusted, so the sandbox network must not reach the host, the LAN or the cloud metadata service. The app refuses to run tutorials until an in-container probe shows these ranges are unreachable.

```bash
# 1. Dedicated bridge with container-to-container traffic disabled
docker network create --driver bridge \
  --opt com.docker.network.bridge.name=fmt0 \
  --opt com.docker.network.bridge.enable_icc=false fmt-sandbox

# 2. Block private, carrier-grade NAT and link-local (metadata) destinations for forwarded traffic
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  sudo iptables -I DOCKER-USER -i fmt0 -d "$net" -j DROP
done

# 3. Block sandbox traffic to the host itself (the bridge gateway)
sudo iptables -I INPUT -i fmt0 -j DROP

# 4. Persist the rules (Ubuntu/Debian)
sudo apt-get install -y iptables-persistent && sudo netfilter-persistent save
```

Run the app with `SANDBOX_BACKEND=docker`, `DOCKER_NETWORK=fmt-sandbox`, and **without** `DOCKER_ALLOW_PRIVATE_EGRESS`. On the first tutorial run, the backend probes the network from inside a container and refuses to continue if any private address answers.

`DOCKER_ALLOW_PRIVATE_EGRESS=true` exists only for running our own fixture tutorials on a developer laptop. The Rot Index batch refuses to run with it set.
