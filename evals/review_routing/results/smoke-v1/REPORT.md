# 审查路由真实 API 评测

合成、assistant 编写参考标签；不是独立人工标注、线上准确率或端到端成功率。

| 模型 | 集合 | 范围 | 样本 | 标签一致率 | 应审查召回率 | 误审率 | API有效率 | P95调用ms |
|---|---|---|---:|---:|---:|---:|---:|---:|
| deepseek | dev | all | 2 | 100.00% | — | 0.00% | 100.00% | 1015.58 |
| deepseek | dev | ambiguous | 2 | 100.00% | — | 0.00% | 100.00% | 1015.58 |
| deepseek | dev | rule | 0 | — | — | — | — | None |
| deepseek | test | all | 0 | — | — | — | — | None |
| deepseek | test | ambiguous | 0 | — | — | — | — | None |
| deepseek | test | rule | 0 | — | — | — | — | None |
| jev | dev | all | 2 | 100.00% | — | 0.00% | 100.00% | 744.79 |
| jev | dev | ambiguous | 2 | 100.00% | — | 0.00% | 100.00% | 744.79 |
| jev | dev | rule | 0 | — | — | — | — | None |
| jev | test | all | 0 | — | — | — | — | None |
| jev | test | ambiguous | 0 | — | — | — | — | None |
| jev | test | rule | 0 | — | — | — | — | None |

## 模糊请求配对差异

```json
{
  "dev": {
    "status": "complete",
    "n": 2,
    "families": 1,
    "agreement_delta": 0.0,
    "cluster_bootstrap95": [
      0.0,
      0.0
    ],
    "note": "Synthetic assistant labels; family bootstrap, not online effect or independent human gold."
  },
  "test": {
    "status": "incomplete"
  }
}
```

## 解释边界

- 超时与无效输出按线上现有策略回退 review，纳入一致率；API 有效率另列。
- all 为整套前置路由，ambiguous 为真正进入模型的请求，rule 为相同规则处理的请求。
- 每组含相关改写，配对区间按场景组重采样；单条 Wilson 区间仅作为参考。
- 未以测试结果调整提示词、标签或阈值；无自动启用新模型。
- 未获取计费账单，仅保存 API 返回的 token 用量；费用不估造。

## 不一致样本（仅 ID，原文在冻结数据集）
