# JEV 审查路由适配与离线配对评测

## 运行位置与回滚

WSL 项目：`/home/administrator/projects/ERP-AGENT-open-source`。
开发分支：`codex/jev-review-evaluation`。
改造前标签：`baseline/pre-jev-20260925`（`c317699`）。
本次只扩展模糊请求路由模型的选择，不改原 `review_policy.py`、执行门控或 HITL。
默认仍为 DeepSeek。修改配置后需重启后端，让已有 Agent Session 重建。

## 配置

在项目根目录未提交的 `.env` 中设置：

```dotenv
REVIEW_ROUTER_PROVIDER=deepseek
TYPESAFE_API_KEY=填写自己的密钥
TYPESAFE_MODEL=jev-1.13.0
```

想试用 JEV 时将 `REVIEW_ROUTER_PROVIDER` 改为 `jev`；回退时改回 `deepseek`。
这不切换主 Agent 或 Grader，也不关闭人工审批。勿提交 `.env`，勿在日志打印密钥。
缺 Key、超时、HTTP 错误、无效结构仍走原保守审查。默认不重试、不自动降级二次调用 DeepSeek。

JEV 使用官方 HTTPS 端点，不接受配置任意 URL，不跟随重定向；适配器不读取环境代理。
如果网络必须经代理，需另外设计受信代理配置，勿将 Key 发送到不明代理。
同步入口使用 HTTP 超时；实际异步主链路另有原 `asyncio.wait_for` 总时限。
没有新增 SDK 依赖，复用项目 `httpx`。

## 为什么是适配器而非替换聊天模型

JEV 接收相同的有界 recent_messages/plan/todos。
一次请求独立询问 7 个问题：decision、task_type 两个 Choice，以及五个检查维度 Noul。
各问题独立评估，再映射为现有 `ReviewRoute`；`reason` 是适配器生成的决策说明，不是模型的自然语言理由。
Choice 的 confidence 不能当业务准确率。首轮阈值为 0.0，不额外覆盖返回的 choice；uncertain 仍保守 review。
Noul 检查项固定以 0.5 纳入。检查项质量未单独标注，当前评测只衡量 review/skip 路由标签。

## 复现

```bash
cd /home/administrator/projects/ERP-AGENT-open-source
.venv/bin/python -m unittest src.test.test_jev_router src.test.test_hybrid_review -q
# 真实调用，消耗两家 API 额度；先少量连通性验证
.venv/bin/python -m evals.review_routing.run --smoke --output evals/review_routing/results/my-smoke
# 一次完整运行；不执行业务工具，每个模型最多 200 次调用
.venv/bin/python -m evals.review_routing.run --output evals/review_routing/results/my-paired
```

每个样本两种模型交错运行，默认总并发 2；不要与高负载任务同时跑延迟测试。
相同参数和输出目录可续跑，已有结果不再次请求；更改配置/模型/样本请使用新目录。
401/402/403 停止后续任务，最多可能有其他已在途任务；429/529 无重试，计入失败。
进程强行终止后若末行不完整，应保留原日志、检查末行后再恢复，不能偷偷删除失败记录。
数据生成器只用于首次生成，拒绝覆盖已冻结 JSONL。标签版本、方法与局限见 [LABELING.md](LABELING.md)。

## 输出与结论边界

- `manifest.json`：数据哈希、模型配置、样本 ID、Git 基线。
- `results.jsonl`：真实请求逐题结果，含回退、耗时、模型版本与用量；不保存密钥。
- `summary.json` / `REPORT.md`：开发/测试集分别报告整体、模糊与规则子集指标。
- `paired_ambiguous`：按场景组 bootstrap 的标签一致率差异区间。

API 有效率与标签一致率是不同指标。完整链路任务成功率、实际费用与中文生产分布上的质量尚需独立测量。
这是一轮合成场景测试，不能据此承诺线上提升或对 200 个变体声称独立统计显著性。
人工复核与新增真实脱敏样本，应形成新版本与新的冻结测试集，不为改善分数修改 v1。

## 官方资料

- https://docs.typesafe.ai/introduction/quickstart
- https://docs.typesafe.ai/api
- https://docs.typesafe.ai/introduction
