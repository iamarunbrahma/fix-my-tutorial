# Deploying Fix My Tutorial

## Production: Render + Token Factory Sandboxes

The public demo is a Render web service (`fix-my-tutorial`, Starter plan, Frankfurt), built from this repo's Dockerfile.
Tutorial code never runs on it: every step runs in a Token Factory Sandboxes microVM. Hugging Face Docker Spaces now need a
PRO subscription, so the demo moved to Render; the private Hugging Face dataset still holds its runs and spend ledger.

1. `uv run python -m fixmytutorial.sandbox.prepare` and put the two image IDs in `.env` (`CONTREE_PYTHON_IMAGE`,
   `CONTREE_NODE_IMAGE`). Re-run it whenever `src/fixmytutorial/sandbox/assets/` or `TOOLS_SETUP` changes.
2. `uv run python -m fixmytutorial.rot_index sync` (first time ever: add `--bootstrap`). This uploads local runs and their
   events so the demo can show Rot Index reports. The laptop never uploads spend: the demo owns the remote `ledger.json`
   and its own budget ($15 lifetime, $7.50 held until 1 December), the laptop keeps a separate local budget ($11).
3. Service environment: `NEBIUS_API_KEY`, `NEBIUS_PROJECT_ID`, `TAVILY_API_KEY`, `ADMIN_TOKEN`, `CONTREE_PYTHON_IMAGE`,
   `CONTREE_NODE_IMAGE`, `HF_TOKEN` (the fine-grained dataset token, never the laptop token), `SANDBOX_BACKEND=contree`,
   `PORT=7860`, `PUBLIC_BASE_URL=https://fix-my-tutorial.onrender.com`. The Dockerfile sets `PUBLIC_DEMO=true`.
4. Auto-deploy is off: deploy with `render deploys create <service id> --confirm` after pushing the branch.
5. Check `/healthz`, then `/admin/ledger?token=...` (`reserve.held` is true until 1 December, with `caps.public` at
   $7.50), then run one tutorial through the home page.
6. Open every "Tutorials we already checked" card on the home page and every report link on `/index`. These saved
   reports are what visitors and judges still see when public runs are paused.

The `render-guard` workflow (every 3 hours, `RENDER_API_KEY` repo secret) suspends the service on or after 2026-12-16,
at 30 GB of monthly bandwidth, on any plan other than Starter, or when scaled past one instance, so Render never bills
more than the Starter plan the account credit covers. Scheduled workflows only run from the default branch.

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

Run the app with `SANDBOX_BACKEND=docker`, `DOCKER_NETWORK=fmt-sandbox`, and **without** `DOCKER_ALLOW_PRIVATE_EGRESS`. The container image sets `PUBLIC_DEMO=true`, so it uses the demo budget and keeps its spend ledger in the Hub dataset: pass the dataset-scoped `HF_TOKEN`, or public runs stay paused. On the first tutorial run, the backend probes the network from inside a container and refuses to continue if any private address answers.

`DOCKER_ALLOW_PRIVATE_EGRESS=true` exists only for running our own fixture tutorials on a developer laptop. The Rot Index batch refuses to run with it set.
