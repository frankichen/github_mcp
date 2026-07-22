# AKShare 合规兜底

本功能用于降低对行情数据源的请求压力，并在数据源临时异常时让观察盘继续可用。

## 目标

- 不并发刷接口。
- 每只股票之间增加请求间隔。
- 连续失败后停止继续请求。
- 数据源异常时优先使用上一份本地日线 CSV。
- 报告和 latest.json 标明数据状态。

## 配置项

```json
"provider": {
  "akshare_request_delay_ms": 1500,
  "akshare_max_consecutive_failures": 3,
  "akshare_fallback_to_bars_file": true
}
```

含义：

- `akshare_request_delay_ms`：AKShare 每只股票之间的请求间隔，默认 1500 毫秒。
- `akshare_max_consecutive_failures`：连续失败多少次后停止继续请求，默认 3。
- `akshare_fallback_to_bars_file`：AKShare 拉取失败时，是否使用 `data.bars_file` 里的上一份 CSV 继续生成观察盘。

## 数据状态

日报钉钉消息和 `latest.json` 会展示数据状态：

- `fresh`：本次成功拉取新数据。
- `cached_fallback`：本次拉取失败，使用上一份本地 CSV 兜底。

如果是缓存兜底，`latest.json` 会包含 `data_status_reason`，说明触发原因。

## 使用建议

当前建议先使用：

```json
"akshare_request_delay_ms": 1500,
"akshare_max_consecutive_failures": 3,
"akshare_fallback_to_bars_file": true
```

如果数据源不稳定，可以提高请求间隔，例如：

```json
"akshare_request_delay_ms": 3000
```

## 安全边界

本功能只做降频、熔断和本地缓存兜底，不做代理池、不绕过限制、不进行高频重试。
