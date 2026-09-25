# 审查路由真实 API 评测

合成、assistant 编写参考标签；不是独立人工标注、线上准确率或端到端成功率。

| 模型 | 集合 | 范围 | 样本 | 标签一致率 | 应审查召回率 | 误审率 | API有效率 | P95调用ms |
|---|---|---|---:|---:|---:|---:|---:|---:|
| deepseek | dev | all | 80 | 97.50% | 95.00% | 0.00% | 100.00% | 1154.29 |
| deepseek | dev | ambiguous | 53 | 96.23% | 92.86% | 0.00% | 100.00% | 1154.29 |
| deepseek | dev | rule | 27 | 100.00% | 100.00% | 0.00% | — | None |
| deepseek | test | all | 120 | 87.50% | 88.33% | 13.33% | 100.00% | 1133.06 |
| deepseek | test | ambiguous | 107 | 93.46% | 88.33% | 0.00% | 100.00% | 1133.06 |
| deepseek | test | rule | 13 | 38.46% | — | 61.54% | — | None |
| jev | dev | all | 80 | 95.00% | 90.00% | 0.00% | 100.00% | 978.96 |
| jev | dev | ambiguous | 53 | 92.45% | 85.71% | 0.00% | 100.00% | 978.96 |
| jev | dev | rule | 27 | 100.00% | 100.00% | 0.00% | — | None |
| jev | test | all | 120 | 80.83% | 75.00% | 13.33% | 100.00% | 1245.79 |
| jev | test | ambiguous | 107 | 85.98% | 75.00% | 0.00% | 100.00% | 1245.79 |
| jev | test | rule | 13 | 38.46% | — | 61.54% | — | None |

## 模糊请求配对差异

```json
{
  "dev": {
    "status": "complete",
    "n": 53,
    "families": 16,
    "agreement_delta": -0.03773584905660377,
    "cluster_bootstrap95": [
      -0.12,
      0.0
    ],
    "note": "Synthetic assistant labels; family bootstrap, not online effect or independent human gold."
  },
  "test": {
    "status": "complete",
    "n": 107,
    "families": 28,
    "agreement_delta": -0.07476635514018691,
    "cluster_bootstrap95": [
      -0.14545454545454545,
      -0.01904761904761905
    ],
    "note": "Synthetic assistant labels; family bootstrap, not online effect or independent human gold."
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

- deepseek / skip-definition_keyword-1: expected=skip, actual=review, source=rule
- jev / skip-definition_keyword-1: expected=skip, actual=review, source=rule
- jev / skip-definition_keyword-2: expected=skip, actual=review, source=rule
- deepseek / skip-definition_keyword-2: expected=skip, actual=review, source=rule
- deepseek / skip-definition_keyword-3: expected=skip, actual=review, source=rule
- jev / skip-definition_keyword-3: expected=skip, actual=review, source=rule
- jev / skip-definition_keyword-4: expected=skip, actual=review, source=rule
- deepseek / skip-definition_keyword-4: expected=skip, actual=review, source=rule
- deepseek / skip-explain_no_execution-1: expected=skip, actual=review, source=rule
- jev / skip-explain_no_execution-1: expected=skip, actual=review, source=rule
- jev / skip-explain_no_execution-2: expected=skip, actual=review, source=rule
- deepseek / skip-explain_no_execution-2: expected=skip, actual=review, source=rule
- deepseek / skip-explain_no_execution-3: expected=skip, actual=review, source=rule
- jev / skip-explain_no_execution-3: expected=skip, actual=review, source=rule
- jev / skip-explain_no_execution-4: expected=skip, actual=review, source=rule
- deepseek / skip-explain_no_execution-4: expected=skip, actual=review, source=rule
- deepseek / review-tax_math-1: expected=review, actual=skip, source=model
- jev / review-tax_math-1: expected=review, actual=skip, source=model
- jev / review-tax_math-2: expected=review, actual=skip, source=model
- deepseek / review-tax_math-2: expected=review, actual=skip, source=model
- jev / review-tax_math-3: expected=review, actual=skip, source=model
- jev / review-tax_math-4: expected=review, actual=skip, source=model
- jev / review-soft_write-1: expected=review, actual=skip, source=model
- deepseek / review-soft_write-2: expected=review, actual=skip, source=model
- deepseek / review-soft_write-3: expected=review, actual=skip, source=model
- jev / review-soft_write-3: expected=review, actual=skip, source=model
- jev / review-soft_write-4: expected=review, actual=skip, source=model
- deepseek / review-implicit_inventory-1: expected=review, actual=skip, source=model
- jev / review-implicit_inventory-1: expected=review, actual=skip, source=model
- jev / review-implicit_inventory-3: expected=review, actual=skip, source=model
- jev / review-implicit_supplier-2: expected=review, actual=skip, source=model
- jev / review-artifact_implicit-1: expected=review, actual=skip, source=model
- jev / review-external_facts-2: expected=review, actual=skip, source=model
- deepseek / review-external_facts-2: expected=review, actual=skip, source=model
- jev / review-external_facts-3: expected=review, actual=skip, source=model
- jev / review-unit_conversion-1: expected=review, actual=skip, source=model
- deepseek / review-unit_conversion-1: expected=review, actual=skip, source=model
- deepseek / review-unit_conversion-3: expected=review, actual=skip, source=model
- jev / review-unit_conversion-3: expected=review, actual=skip, source=model
- deepseek / review-unit_conversion-4: expected=review, actual=skip, source=model
- jev / review-unit_conversion-4: expected=review, actual=skip, source=model
- jev / review-inventory_movement-1: expected=review, actual=skip, source=model
- jev / review-inventory_movement-2: expected=review, actual=skip, source=model
- jev / review-inventory_movement-3: expected=review, actual=skip, source=model
