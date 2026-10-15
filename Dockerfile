FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv==0.5.9
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY rot_index ./rot_index
RUN uv sync --frozen --no-dev && mkdir -p /data && chmod 777 /data
ENV DATA_DIR=/data SANDBOX_BACKEND=contree PUBLIC_DEMO=true PATH="/app/.venv/bin:$PATH"
EXPOSE 7860
CMD ["uvicorn", "fixmytutorial.web.app:app", "--host", "0.0.0.0", "--port", "7860", "--no-proxy-headers"]
