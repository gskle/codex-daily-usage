# Codex 用量统计

非官方社区插件：以当前用户自己的Profile兼容统计作为账户显示值，并汇总本机和已注册SSH服务器的各任务日志。提供中文HTML、近7天CSV、每日CSV及可选JSON。

在此插件目录中运行：

```bash
python scripts/codex_daily_usage.py --all-sources --profile-reference --days 30 --output outputs/codex-daily-usage.html
python scripts/codex_daily_usage.py --local --days 7 --output outputs/local-usage.html
python scripts/codex_daily_usage.py --ssh-host my-server --days 7 --output outputs/ssh-usage.html
```

需要Python3.8+；SSH需要OpenSSH、现有非交互登录与远端Python3.8+。账户模式需要当前用户已有ChatGPT登录，Profile兼容接口可能随客户端升级变化。接口失败或缺日会明确显示，不按零处理。

账户模式按UTC划日，任务的账户份额来自可定位日志，后台审批及未归属差额独立列示。排序、名称映射、重复记录去重均由脚本完成。

本插件不携带作者的登录信息、SSH配置、原始日志或统计报表。使用时会读取使用者自己的这些数据；报告内可能含任务名和目录信息。

GitHub仓库结构和安装方式见仓库根目录README.md。
