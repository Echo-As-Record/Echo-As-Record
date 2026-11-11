FROM node:24.11.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ffmpeg git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --break-system-packages uv==0.9.9 \
    && npm install -g @openai/codex@0.155.1
# Build context is experiments/codex_baselines; never COPY local-data or credentials.
COPY src/ear_baselines/meter.py src/ear_baselines/openai_prices.json src/ear_baselines/isolation.py /opt/exam/
WORKDIR /work
