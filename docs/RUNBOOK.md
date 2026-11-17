# 运行手册 ledgerbank

## 启动与停止

```bash
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:18206/api/health
docker compose down -v --remove-orphans
```

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `ledgerbank-data` 保存批次、流水行、候选与匹配。重建后从「载入样例」重新开始。

## 主流程

1. `POST /api/demo/run`：读取两份脏样例，采信、质量、候选、匹配一次跑完。
2. `GET /api/quarantine`：查看隔离行与原因分布。
3. `GET /api/candidates`：查看已生成候选。
4. `GET /api/matches`：查看匹配组与手续费分摊。
5. `GET /api/differences`：查看两侧守恒与差异归因。
6. `GET /api/export/report.csv` 与 `GET /api/export/manifest.json`：导出。

## 净额结算

```bash
# 一键样例：导入往来凭证与到账清单并执行结算
curl -s -X POST http://127.0.0.1:18206/api/settlements/demo
# 自有文件：两侧分别整侧替换导入
curl -s -X POST http://127.0.0.1:18206/api/settlements/import \
  -F side=vouchers -F file=@sample_data/settle_vouchers.csv
curl -s -X POST http://127.0.0.1:18206/api/settlements/import \
  -F side=receipts -F file=@sample_data/settle_receipts.csv
# 执行 / 查看 / 下载 / 版本恢复
curl -s -X POST http://127.0.0.1:18206/api/settlements/apply
curl -s http://127.0.0.1:18206/api/settlements
curl -s -OJ http://127.0.0.1:18206/api/settlements/export.csv
curl -s http://127.0.0.1:18206/api/settlements/versions
curl -s -X POST http://127.0.0.1:18206/api/settlements/restore/<batch_id>
```

- 同一输入重试命中同一批次号，事务内先删后插，不会重复冲销。
- 输入变化后执行会生成新版本；页面与 CSV 始终读当前版本，历史版本可恢复。
- 结算输入整侧替换：到账清单要包含本期全部到账，重导同一文件结果不变。

## 单步重跑

```bash
curl -s -X POST http://127.0.0.1:18206/api/candidates
curl -s -X POST http://127.0.0.1:18206/api/matches
```

同一输入重跑命中同一 `run_id`，先删后插，行数与合计不变。

## 导入自有文件

```bash
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=bank -F file=@sample_data/bank_statement_dirty.csv
```

支持 UTF-8（含 BOM）、UTF-8 无 BOM、GB18030；列名按 `GET /api/contract` 的别名表映射。

## 失败处理

- 400：参数或文件不可用，页面顶部提示原因，保留已填内容。
- 404：批次或样例不存在，先执行 `POST /api/demo/run`。
- 409：还没有候选就求解匹配，先执行 `POST /api/candidates`；结算两侧输入未齐就执行，先导入往来凭证与到账清单。
- 隔离行不是失败：按原因码修正源文件后重新导入。
- 结算导入是严格模式：任何一行金额、日期、类型不合格或编号重复，整批拒绝并指明行号，修正后重新导入。

## 日志

```bash
docker compose logs -f app
```
