# Confucius4-R2T2 评测与部署试验(xiaowei2)

网易有道 [Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2)
(2B 真流式 ASR)在 xiaowei2 机器上的部署试验与精度评测,对比基线为本机现网
serving 的 funasr_nano_2512(Fun-ASR-Nano,0.8B)。

## 结论速览

- **精度**:同一 312 条评测集、同一口径,clean CER 1.38% vs funasr 3.22%,
  各噪声组普遍 2~3 倍提升,纯噪声幻觉 0 字/条 vs 6.6 字/条。
- **延迟**:首字约 0.4s(funasr 现网 P50 1.67s),160ms 块单轮推理 17~30ms,
  真流式追加输出。
- **风险**:模型权重是网易自定义许可(非 Apache-2.0),商用前需法务审查;
  官方 ws_server.py 有 3 个必须先修的缺陷(下文)。

详细数据与部署位置见 [REPORT.md](REPORT.md)。

## 目录

```
├── REPORT.md              # 完整评测报告(精度/延迟/缺陷清单/部署命令)
├── r2t2-ws.service        # systemd 单元(参照 funasr-ws.service)
├── scripts/
│   ├── start_r2t2_ws.sh   # 服务启动脚本(systemd ExecStart 入口,支持 R2T2_* 覆盖)
│   ├── eval_r2t2.py       # 312 条评测脚本,与 hojo eval_run.py 同口径
│   └── r2t2_latency_probe.py  # 实时节奏延迟探针
├── eval_results/
│   ├── r2t2.md            # 最终评测结果表(2026-09-19)
│   └── r2t2.json          # 逐条原始结果(REF/HYP/CER)
└── patches/
    └── 0001-ws-server-budget-and-deploy-fixes.patch  # 官方服务端修复补丁
```

## 服务部署(systemd)

```bash
cp r2t2-ws.service /etc/systemd/system/ && systemctl daemon-reload
systemctl enable --now r2t2-ws     # 开机自启 + 崩溃自动拉起
journalctl -u r2t2-ws -f           # 或看 logs/r2t2_ws.log
```

## 官方代码缺陷与补丁

评测过程中定位了官方 `ws_server.py` 的 3 个缺陷,均已在
[patches/](patches/) 中以 git diff 形式给出,应用方式:

```bash
cd Confucius4-R2T2 && git apply 0001-ws-server-budget-and-deploy-fixes.patch
```

1. **v1 路由 language=None 时丢整轮文本**:`streaming_transcribe_no_reset`
   在模型输出无 `<asr_text>` 标签且未显式指定语言时清空该轮解码结果
   (r2t2_asr.py `if not has_tag and state.force_language == None` 分支),
   噪声场景频发(实测 white_5db 13/30 条整句为空)。
   **绕过**:客户端 header 显式传 `"language": "Chinese"`,不传 "zhen"。
2. **EOS 收尾 token 预算过小**:`max(1, int(320/1280))=1`,LSP 未提交尾部
   全部丢失。补丁改为 `max(8, ...)`。
3. **每轮解码预算 floor 过小**:160ms 块下中文翻倍后仅 4,首 token 犹豫时
   文本无法起步。补丁 floor 4→8。

另注意:官方默认 `gpu_memory_utilization=0.95` 会占满整卡、`max_model_len=65536`
在低显存预算下启动失败,补丁一并改为 0.20 / 8192(单卡与 funasr 共存场景)。

## 评测方法

- 数据:`/mnt/asr/hojo_asr_multi_v1/eval_dataset`(clean 30 条 + 白噪/粉噪/
  嘈杂人声各 5/10/20dB × 30 条 + 纯噪声 12 条),TTS 生成,与 funasr 2026-09-06
  基准完全同集。
- 口径:CER = 字级编辑距离/参考字数,规范化去标点、数字归一(与
  hojo eval_run.py 一致);客户端实时节奏推流,EOS 前补 1.5s 静音让 LSP
  尾部 token 落地。
- 环境模型权重不入库,只记录脚本与结果。模型从 ModelScope 下载:
  `netease-youdao/Confucius4-R2T2`,VAD 用 `FireRedTeam/FireRedVAD`(Stream-VAD)。
