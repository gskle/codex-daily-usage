# Codex Daily Usage · Codex 用量统计

用于Codex的非官方社区插件，生成中文账户用量和各任务token报表。

- 账户主显示来自当前用户的Profile兼容统计接口，按UTC日期统计并标注获取时间。
- 自动读取本机和已在Codex注册的SSH来源，显示“项目名 · 任务名”。
- 提供每日模型调用记录、任务当天/近7天token占比，以及未归属差额。
- 后台审批单独列出；重复用量事件去重；每日明细按tokens降序排列。
- 输出独立HTML、近7天CSV、每日CSV，可选JSON；无需第三方Python运行依赖。

## 安装

### 从GitHub安装

GitHub仓库：https://github.com/gskle/codex-daily-usage

安装命令：

```bash
codex plugin marketplace add gskle/codex-daily-usage
codex plugin add codex-daily-usage@codex-usage-tools
```

在Codex中新开任务使用插件。私有仓库的使用者需要已有仓库读取权限。

### 从本地分享包安装

解压本分享包，进入包含本README的仓库根目录：

```bash
codex plugin marketplace add .
codex plugin add codex-daily-usage@codex-usage-tools
```

这是显式仓库marketplace，名称是 codex-usage-tools，插件名称是 codex-daily-usage。

## 使用示例

安装后发送：

> 使用Codex用量统计，合并本机与已配置SSH，按账户统计基准列出昨天各任务的token和占比。

> 统计2026年9月23日各任务用量，按UTC日期，并将每日明细按tokens从高到低排序。

也可以从仓库根目录直接运行：

```bash
python plugins/codex-daily-usage/scripts/codex_daily_usage.py --all-sources --profile-reference --days 30 --output outputs/codex-daily-usage.html
python plugins/codex-daily-usage/scripts/codex_daily_usage.py --local --days 7 --output outputs/local-usage.html
python plugins/codex-daily-usage/scripts/codex_daily_usage.py --ssh-host my-server --days 7 --output outputs/ssh-usage.html
```

仅日志模式不加 --profile-reference；需要本地自然日可显式指定 --utc-offset=+0800。账户模式统一UTC，UTC某日对应UTC+8地区当日08:00到次日08:00。

## 环境和数据

需要Python3.8+与Codex。SSH是可选项，需要本机OpenSSH、已信任的主机密钥、现有密钥或agent登录、远端Codex日志与Python3.8+。脚本也会寻找远端uv安装的兼容Python。

每位使用者读取自己的Codex Home（CODEX_HOME或~/.codex）。账户模式使用自己的auth.json，仅向官方chatgpt.com的Profile兼容接口发送只读请求，不跟随重定向，不记录令牌。远端只返回统计与名称元数据，不传回原始对话。

Profile兼容接口是客户端内部接口，并非公开稳定API，可能随更新失效；缺少日期或请求失败会明确标注，不当作0。账户统计不是账单；日志未必覆盖所有设备或云端工作。差额保留为未归属，不虚构任务分摊。token占比也不等于订阅周额度百分比。

分享包仅含代码、说明、合成测试和marketplace配置，不含作者的报告、会话记录或认证信息。生成报告可能含任务名和路径；outputs、work、auth及会话数据已加入.gitignore。

## 维护与再次分发

以下命令适用于仓库维护者。若再次分发，请先创建自己的空仓库，并把远端地址改为自己的仓库地址。
在本README所在目录执行：

```bash
git init -b main
git add .
git commit -m "Initial Codex usage plugin"
git remote add origin https://github.com/gskle/codex-daily-usage.git
git push -u origin main
```

需上传完整仓库，包括隐藏的.agents、.github、.gitignore和插件内的.codex-plugin目录。建议用Git提交，避免网页拖拽遗漏隐藏目录。

本仓库尚未指定开源许可证，可由发布者自行选择并添加。上传GitHub不会自动上架OpenAI公共插件目录。

## 更新

修改插件后更新插件manifest的version并推送。使用者刷新来源后重新安装：

```bash
codex plugin marketplace upgrade codex-usage-tools
codex plugin add codex-daily-usage@codex-usage-tools
```

## 检查

合成测试不会连接真实账户或SSH，也不会读取使用者实际登录：

```bash
python -m unittest discover -s tests -p "verify_*.py" -v
```

## 结构

```text
.agents/plugins/marketplace.json
.github/workflows/tests.yml
plugins/codex-daily-usage/.codex-plugin/plugin.json
plugins/codex-daily-usage/skills/codex-daily-usage/SKILL.md
plugins/codex-daily-usage/scripts/codex_daily_usage.py
tests/
README.md
.gitignore
```

安装与marketplace格式参考：[OpenAI插件打包说明](https://developers.openai.com/plugins/build/plugins)；CLI参考：[Developer commands](https://learn.chatgpt.com/docs/developer-commands)。
