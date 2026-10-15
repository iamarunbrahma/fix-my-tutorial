# Sourced at the start of every step. Sets headless defaults and starts the offline OpenAI-compatible stub.
export MPLBACKEND=Agg PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
export NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false CI=1
export OPENAI_API_KEY=sk-fixmytutorial-stub
export OPENAI_BASE_URL=http://127.0.0.1:8099/v1 OPENAI_API_BASE=http://127.0.0.1:8099/v1
if ! (exec 3<>/dev/tcp/127.0.0.1/8099) 2>/dev/null; then
  nohup python3 /opt/fmt/openai_stub.py 8099 >/tmp/openai_stub.log 2>&1 &
  for _ in $(seq 1 50); do (exec 3<>/dev/tcp/127.0.0.1/8099) 2>/dev/null && break; sleep 0.1; done
fi
mkdir -p /work && cd /work
