#!/bin/bash
# Wordt bij elke start van de Vast.ai-instance uitgevoerd (via het On-start commando in je template).
# Doet: env vars doorzetten, llama.cpp bouwen (eenmalig), model downloaden (eenmalig), server + bot starten.

set -e
export LLAMA_CACHE=/workspace/models        # modelcache op /workspace: blijft bewaard bij "stop"
export BOT_WORKDIR=${BOT_WORKDIR:-/workspace}
MODEL_HF="douyamv/Qwen3.8-27B-abliterated-GGUF:Q6_K"

# 1. Env vars ook zichtbaar maken in SSH/tmux-sessies (Vast.ai doet dit niet vanzelf)
env | grep -E '^(TG_|LLM_|BOT_|LLAMA_)' >> /etc/environment || true

mkdir -p /workspace/models /workspace/bot
cd /workspace

# 2. llama.cpp met CUDA bouwen — alleen als hij er nog niet staat
if [ ! -x /workspace/llama.cpp/build/bin/llama-server ]; then
  echo "== llama.cpp bouwen (eenmalig, ~5-10 min) =="
  apt-get update -qq && apt-get install -y -qq git cmake build-essential libcurl4-openssl-dev >/dev/null
  git clone --depth 1 https://github.com/ggml-org/llama.cpp /workspace/llama.cpp
  cd /workspace/llama.cpp
  cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=ON >/dev/null
  cmake --build build --config Release -j "$(nproc)" --target llama-server >/dev/null
  cd /workspace
fi

# 3. llama-server starten (downloadt het model bij de eerste keer naar $LLAMA_CACHE)
echo "== llama-server starten =="
nohup /workspace/llama.cpp/build/bin/llama-server \
  -hf "$MODEL_HF" \
  -ngl 99 -c 32768 --jinja \
  --host 127.0.0.1 --port 8080 \
  > /workspace/llama.log 2>&1 &

# 4. Bot-dependencies + bot starten
echo "== bot starten =="
cd /workspace/bot
pip install -q -r requirements.txt
nohup python3 bot.py > /workspace/bot.log 2>&1 &

echo "== klaar: logs in /workspace/llama.log en /workspace/bot.log =="
