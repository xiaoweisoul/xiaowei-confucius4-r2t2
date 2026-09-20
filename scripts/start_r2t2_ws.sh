#!/usr/bin/env bash
# R2T2 流式 ASR 服务启动脚本(systemd 入口)。
# 参照 funasr_nano_2512/xiaowei-funasr/scripts/start_xiaowei_realtime_ws.sh 的环境变量覆盖模式:
# 所有配置项可用 R2T2_* 环境变量覆盖,不传时用本脚本内置默认值。
# 例子:临时用另一份代码起测试实例时,执行
#   R2T2_SERVER_DIR=/tmp/xxx R2T2_PORT=18273 ./start_r2t2_ws.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_DIR="$(cd "${REPO_DIR}/.." && pwd)"

PYTHON_BIN="${R2T2_PYTHON_BIN:-${ROOT_DIR}/venvs/r2t2/bin/python}"
# 服务端代码目录(上游 github.com/netease-youdao/Confucius4-R2T2 clone + 本地缺陷修复)。
# 历史上放在 /tmp/r2t2_repo,重启机器会丢;现固定在持久盘 server/ 下。
SERVER_DIR="${R2T2_SERVER_DIR:-${ROOT_DIR}/server}"
PORT="${R2T2_PORT:-18272}"
MODEL="${R2T2_MODEL:-${ROOT_DIR}/models/Confucius4-R2T2}"
VAD_MODEL_PATH="${R2T2_VAD_MODEL_PATH:-${ROOT_DIR}/models/vad/Stream-VAD}"

# GPU 选择:与 funasr(10095)/hojo(10097) 同卡共存,默认固定物理卡 0。
# ws_server.py 内部 device 交给 vllm 按 CUDA_VISIBLE_DEVICES 解析。
export CUDA_VISIBLE_DEVICES="${R2T2_CUDA_VISIBLE_DEVICES:-0}"
# ws_server.py 以 `import r2t2` 方式引用同目录包,必须把代码目录加进 PYTHONPATH。
export PYTHONPATH="${SERVER_DIR}"
export PYTHONUNBUFFERED="1"

if [[ ! -f "${SERVER_DIR}/ws_server.py" ]]; then
  echo "error: ${SERVER_DIR}/ws_server.py 不存在,请先部署服务端代码(README 第 4 节)" >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${SERVER_DIR}/ws_server.py" \
  -p "${PORT}" \
  -m "${MODEL}" \
  --vad_model_path "${VAD_MODEL_PATH}"