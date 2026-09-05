#!/bin/bash
# Provisioning-script voor de Vast.ai-instance.
# Idempotent: veilig om meerdere keren te draaien (eerste start, resume, handmatig).
# Alles wat er al staat wordt overgeslagen; alles wat al draait wordt niet dubbel gestart.

export LLAMA_CACHE=/workspace/models          # model blijft bewaard bij "stop"
export BOT_WORKDIR="${BOT_WORKDIR:-/workspace}"
MODEL_HF="douyamv/Qwen3.8-27B-abliterated-GGUF:Q6_K"
PORT=18081
REPO="https://github.com/larsakuyper-tech/Qwen-telegram-bot.git"
LLAMA_BIN=/workspace/llama.cpp/build/bin/llama-server

exec >> /workspace/setup.log 2>&1
echo "===== onstart $(date) ====="

# Env vars ook zichtbaar in SSH/Jupyter-terminals
env | grep -E '^(TG_|LLM_|BOT_|LLAMA_)' >> /etc/environment || true
mkdir -p /workspace/models
cd /workspace

# --- 1. bot-code ophalen of bijwerken ---
if [ -d /workspace/bot/.git ]; then
  echo "bot: git pull"
  (cd /workspace/bot && git pull -q) || true
else
  echo "bot: git clone"
  git clone -q "$REPO" /workspace/bot
fi

# --- 2. llama.cpp bouwen, alleen als het binary nog niet bestaat ---
if [ ! -x "$LLAMA_BIN" ]; then
  echo "llama.cpp: bouwen (eenmalig, ~5-10 min)"
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq git cmake build-essential >/dev/null 2>&1
  if [ ! -d /workspace/llama.cpp ]; then
    git clone -q --depth 1 https://github.com/ggml-org/llama.cpp /workspace/llama.cpp
  fi
  cd /workspace/llama.cpp
  cmake -B build -DGGML_CUDA=ON
  cmake --build build --config Release -j "$(nproc)" --target llama-server
  cd /workspace
  if [ ! -x "$LLAMA_BIN" ]; then
    echo "FOUT: bouwen mislukt, zie hierboven"
    exit 1
  fi
else
  echo "llama.cpp: al gebouwd, overslaan"
fi

# --- 3. llama-server starten (downloadt het model de eerste keer) ---
if pgrep -f "llama-server" >/dev/null; then
  echo "llama-server: draait al"
else
  echo "llama-server: starten op poort $PORT"
  nohup "$LLAMA_BIN" \
    -hf "$MODEL_HF" \
    -ngl 99 -c 32768 --jinja \
    --host 127.0.0.1 --port "$PORT" \
    > /workspace/llama.log 2>&1 &
fi

# --- 4. bot starten ---
if pgrep -f "python3 bot.py" >/dev/null; then
  echo "bot: draait al"
else
  echo "bot: starten"
  cd /workspace/bot
  pip install -q -r requirements.txt
  nohup python3 bot.py > /workspace/bot.log 2>&1 &
fi

echo "===== klaar $(date) — logs: /workspace/llama.log en /workspace/bot.log ====="
