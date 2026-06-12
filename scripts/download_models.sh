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

# --- Phase 2: 流式 paraformer(可选;首次运行 funasr_stream 时也会自动下载)---
echo
echo "Prefetching paraformer-zh-streaming (modelscope cache, ~900MB)…"
.venv/bin/python - <<'PY'
from modelscope import snapshot_download
snapshot_download("iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online")
print("paraformer-zh-streaming cached")
PY
