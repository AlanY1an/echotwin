#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MODELS_DIR="$PROJECT_DIR/models"

mkdir -p "$MODELS_DIR"

# Silero VAD (small, ONNX)
SILERO_FILE="$MODELS_DIR/silero_vad/src/silero_vad/data/silero_vad.onnx"
if [ ! -f "$SILERO_FILE" ]; then
    echo "Downloading Silero VAD..."
    mkdir -p "$(dirname "$SILERO_FILE")"
    curl -L -o "$SILERO_FILE" \
        "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
else
    echo "Silero VAD already present: $SILERO_FILE"
fi

# SenseVoiceSmall via HuggingFace
SENSE_DIR="$MODELS_DIR/SenseVoiceSmall"
if [ ! -d "$SENSE_DIR" ] || [ -z "$(ls -A "$SENSE_DIR" 2>/dev/null)" ]; then
    echo "Downloading SenseVoiceSmall (~234MB)..."
    "$PROJECT_DIR/.venv/bin/python" -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='FunAudioLLM/SenseVoiceSmall', local_dir='$SENSE_DIR')
" || {
        echo "Failed via HuggingFace; trying ModelScope..."
        "$PROJECT_DIR/.venv/bin/python" -c "
from modelscope import snapshot_download
snapshot_download('iic/SenseVoiceSmall', local_dir='$SENSE_DIR')
"
    }
else
    echo "SenseVoiceSmall already present: $SENSE_DIR"
fi

echo
echo "Models ready in $MODELS_DIR"
ls -lh "$MODELS_DIR"

# --- Streaming ASR models (sherpa zipformer, zh + en, ~200MB total) ---
# Pre-fetched here so neither first start nor a persona language switch
# pauses for a download. Repo/file lists come from the factory table.
echo
echo "Prefetching streaming ASR models (sherpa zipformer zh + en)…"
.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
from echotwin.providers.factory import SHERPA_LANG_REPOS
for lang, (repo, files) in SHERPA_LANG_REPOS.items():
    print(f"  [{lang}] {repo}")
    snapshot_download(repo, allow_patterns=files)
print("streaming ASR models cached")
PY
