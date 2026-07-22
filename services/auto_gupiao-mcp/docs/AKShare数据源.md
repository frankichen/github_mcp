# AKShare 数据源

本项目支持通过本地 Python 脚本调用 AKShare，再把结果输出成 Go 程序可读取的 CSV。

## 运行前准备

本地需要安装 Python，并安装 AKShare 和 pandas。

## 常用命令

拉取实时快照并选股，AKShare 默认使用 `basic` 策略档位：

```bash
go run ./cmd/autogupiao -mode select -source akshare -top 5 -cash 10000 -min-score 60
```

如果实时快照字段不完整导致候选为空，可以降低阈值：

```bash
go run ./cmd/autogupiao -mode select -source akshare -top 5 -cash 10000 -min-score 45
```

Windows 上如果系统 `python` 或 `py` 不可用，可以通过 `-python` 指定可执行文件完整路径：

```powershell
go run ./cmd/autogupiao -mode select -source akshare -python "C:\path\to\python.exe" -top 5 -cash 10000 -min-score 45
```

启用历史指标增强。该模式会先拉实时快照，再对初筛后的股票拉历史日线并回填 MA、RSI、5 日涨幅、20 日涨幅。因为会多次请求 AKShare，速度会明显变慢：

```bash
go run ./cmd/autogupiao -mode select -source akshare -akshare-enrich -akshare-enrich-limit 20 -top 5 -cash 10000 -min-score 45
```

拉取单只股票历史日线并输出 CSV：

```bash
go run ./cmd/autogupiao -mode fetch-bars -source akshare -code 000001 -start-date 20250101 -end-date 20250518 -output data/bars/000001.csv
```

使用导出的日线做回测：

```bash
go run ./cmd/autogupiao -mode backtest -input data/bars/000001.csv -cash 10000 -top 1 -min-score 60
```

直接运行 Python 桥接脚本：

```bash
python scripts/akshare_fetch.py --type spot
python scripts/akshare_fetch.py --type bars --code 000001 --start-date 20250101 --end-date 20250518
```

## CLI 参数

- `-source akshare`：使用 AKShare 数据源。
- `-python`：指定 Python 可执行文件路径，例如 Windows 上可传 bundled Python 路径。
- `-akshare-script`：指定 Python 桥接脚本路径。
- `-akshare-cache`：指定 AKShare CSV 缓存目录，默认 `data/cache/akshare`。
- `-refresh-cache`：跳过缓存并重新拉取。
- `-akshare-enrich`：启用历史指标增强。
- `-akshare-enrich-limit`：控制最多增强多少只股票，默认 30。
- `-akshare-lookback-days`：历史指标回看自然日，默认 140。
- `-strategy-profile basic`：使用免费快照适配的基础策略档位。
- `-strategy-profile full`：使用完整数据评分档位。

## 缓存说明

AKShare 实时快照和历史日线会缓存到本地目录，例如：

```text
data/cache/akshare/spot_20260519.csv
data/cache/akshare/bars_000001_20250101_20250518.csv
```

缓存目录已被 `.gitignore` 忽略，不应提交到 GitHub。

## 说明

AKShare 上游字段可能随版本变化。脚本会尽量兼容中文字段名，并把缺失字段填为空值或 0，避免主程序直接崩溃。

当前 AKShare 桥主要用于低成本数据验证。正式模拟盘或实盘前，仍需要增加数据质量校验、异常熔断和更稳定的行情源。
