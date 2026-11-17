#!/usr/bin/env bash
# Installs the GNOME extension and the local helper for the current user. No sudo needed.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
data="$HOME/.local/share/stella"
uuid="stella@023b.github.io"

echo "extension -> ~/.local/share/gnome-shell/extensions/$uuid"
mkdir -p "$HOME/.local/share/gnome-shell/extensions"
rm -rf "$HOME/.local/share/gnome-shell/extensions/$uuid"
cp -r "$here/extension/$uuid" "$HOME/.local/share/gnome-shell/extensions/"

echo "helper -> $data"
mkdir -p "$data/kokoro"
cp "$here/helper/stella_helper.py" "$data/"
[ -d "$data/venv" ] || python3 -m venv "$data/venv"
"$data/venv/bin/pip" install -q -r "$here/helper/requirements.txt"

if [ ! -f "$data/kokoro/kokoro-v1.0.onnx" ]; then
  echo "downloading the Kokoro voice model (~340 MB)"
  base="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
  curl -L --fail -o "$data/kokoro/kokoro-v1.0.onnx" "$base/kokoro-v1.0.onnx"
  curl -L --fail -o "$data/kokoro/voices-v1.0.bin" "$base/voices-v1.0.bin"
fi

mkdir -p "$HOME/.config/systemd/user"
cp "$here/helper/stella-helper.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now stella-helper.service

cat <<MSG

Done. Next:
  1. Import n8n/stella-voice.json into n8n, set the Settings node (Ollama URL, model, town), then publish it.
  2. If n8n is not on this machine, put its webhook URL in ~/.config/stella/config.json:
       {"n8n": "http://<n8n-host>:5678/webhook/stella/voice"}
  3. Log out and in (Wayland), then: gnome-extensions enable $uuid
MSG
