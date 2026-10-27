# Skills

可复用的 Agent Skills，采用 MIT 许可证。当前包含 [x-research](x-research/SKILL.md)：通过 SocialData 查询 X（Twitter）的公开内容。

## 安装与配置

下载仓库，把整个 `x-research` 文件夹复制到你的 Agent 支持的技能目录，保留文件的相对位置和随包许可证。也可以直接运行脚本，无需 SocialData MCP。

在 Claude 等带网络允许列表的沙箱中运行时，需要放行 `api.socialdata.tools`。被拦截时脚本会提示“请求被网络代理或沙箱拦截”，这不是密钥问题。

需要 Python 3.10+、网络访问和自己的 SocialData API key。SocialData 是独立的付费第三方服务，本仓库不提供额度。脚本使用标准库；可选 IANA 命名时区需要系统时区数据或 `tzdata`。

密钥按以下顺序读取，日志会写明使用了哪个来源（不打印密钥）：

1. `--env-file` 指定的 UTF-8 文件，内容为 `SOCIALDATA_API_KEY=你的实际密钥`；显式指定时优先于环境变量，文件不存在或没有密钥会直接报错。
2. 环境变量 `SOCIALDATA_API_KEY`。
3. 默认文件 `~/.config/socialdata/key.env`（仅当存在时）。Agent 的终端通常读不到你本机 shell 的环境变量，推荐用这种方式。

不要把真实密钥粘贴到聊天、命令历史或提交记录。

下列交互输入不会把密钥值写入命令文本，只设置当前终端的环境变量，并不代表加密存储。

macOS / Linux（bash）：

```bash
read -r -s -p "SocialData API key: " SOCIALDATA_API_KEY
export SOCIALDATA_API_KEY
printf '\n'
python3 x-research/socialdata_x.py user nvidia --max 5 --format json --out outputs/nvidia.json
unset SOCIALDATA_API_KEY
```

Windows PowerShell：

```powershell
$socialDataSecret = Read-Host 'SocialData API key' -AsSecureString
$socialDataCredential = [System.Net.NetworkCredential]::new('', $socialDataSecret)
$env:SOCIALDATA_API_KEY = $socialDataCredential.Password
python x-research/socialdata_x.py user nvidia --max 5 --format json --out outputs/nvidia.json
Remove-Item Env:SOCIALDATA_API_KEY
$socialDataCredential = $null
$socialDataSecret = $null
```

Agent 的非交互终端应读取预先配置的变量或本地密钥文件，不应运行上述交互步骤。配置示例见 [.env.example](.env.example)；脚本不会自动读取示例或保存密钥。

## 查询示例

从仓库根目录执行；Windows 使用 `python`，macOS/Linux 可使用 `python3`。技能安装后将路径换成实际技能目录。

```bash
python x-research/socialdata_x.py search 'AI min_faves:50 lang:en' --max 50
python x-research/socialdata_x.py user nvidia OpenAI --since-hours 24 --no-replies
python x-research/socialdata_x.py tweet 'https://x.com/example/status/1234567890123456789' --full
python x-research/socialdata_x.py profile nvidia OpenAI --format json
python x-research/socialdata_x.py search -h
python x-research/socialdata_x.py user nvidia OpenAI --timeline --since-hours 24 --dry-run
```

`--dry-run` 不读取密钥、不发请求、不写文件，只输出最终查询语句、估算费用和是否在预算内，适合 Agent 先向用户确认。

推文链接示例为占位内容，需替换为实际推文。每个子命令支持 `-h`。

## 预算与完整性

整次运行默认预算 **$0.20**，当前按每个返回对象 $0.0002 估算。多账号、搜索批次和线程等区块共用预算，每次请求及重试前预留额度。过滤或截断不减少已返回数据的费用；默认取 50 条通常收到约 60 条的页面数据，估算约 $0.012。[官方计价](https://docs.socialdata.tools/getting-started/pricing/)

需要更高预算时，先确认费用，再显式指定金额及确认标志：

```bash
python x-research/socialdata_x.py user nvidia OpenAI --per-account --max 1000 --budget-usd 0.50 --yes
```

时间线带过滤条件时，为凑够条数可能多翻页，日志会同时给出“最多约 $X”的上限估算。DNS 失败、连接被拒、代理拦截时请求没有发出，不计入费用；成功状态码但内容不是有效 JSON 时计一次并停止，不重试。

`--yes` 不取消预算上限。下一页通常预留 20 条，发现更大页后提高预留；API 不保证最大页大小，意外大页仍可能造成一页超出，随后停止。网络响应不确定的请求按预留量保守计费，空结果按可能的公平使用费用估算。此预算是本地估算控制，**不是服务商账单或绝对扣费保证**；共用密钥的其他调用不在统计内。

退出码：`0` 完成；`1` 错误；`2` 预算或分页保护停止，结果不完整；`130` 用户中断。JSON 包含 `meta.complete`、`stop_reasons`、`notes`、`estimated_cost_usd`、`uncertain_cost_usd`。批量查询推文时，未返回的 ID 会写入 `stop_reasons` 并标记为不完整；`--full` 遇到没有长文的推文时写入 `notes`，不影响线程、回复和引用的抓取。`count`/`unique_tweet_count` 是不同推文数，`record_count` 包括全文等额外记录。完成只指本次流程，不保证检索覆盖全部 X 内容。

`--out` 在成功和失败时输出已获取数据，并生成 `<out>.checkpoint.jsonl`，逐次记录原始响应、请求参数、游标和估算消耗，不记录密钥或请求头。CSV/JSONL 另有 `<out>.meta.json`。检查点可用于找回已收到的数据和诊断，不实现自动续传。结果用临时文件加替换写入；同名输出和检查点会被新运行覆盖，保留旧结果时应使用新文件名。检查点包含用户查询的数据，请按自己的数据管理要求保存。

## 时间与格式

默认 `--tz UTC+08:00` 为固定偏移，不需要外部包。IANA 命名时区支持历史夏令时，例如 `Asia/Shanghai`、`America/New_York`。Windows 通常需安装数据：

```bash
python -m pip install tzdata
```

未知时区或缺少数据会在请求前报错。固定偏移不处理历史夏令时，有此需求应使用命名时区。

- JSON 保留元数据与区块，`--raw` 保留 API 对象。
- JSONL 每行统一为 `{"section": "tweets", "data": {...}}`，raw 模式也如此；资料使用 `users` 区块。
- `raw_text` 是 API 原文；`display_text` 和兼容字段 `text` 为整理后的文本，Markdown 展示阅读文本。
- CSV 默认 `--csv-mode safe`，公式前缀加制表符并加引号，适合表格查看，会改变文本。不同表格软件可能有差异；保留原文优先 JSON。仅程序导入时可用 `--csv-mode raw`，不要直接用表格软件打开不可信内容。

相较早期版本，目录由 `X-Research` 改为 `x-research`，移除 `/data` 和 Notion 下载依赖；JSONL 增加 `section/data` 包装；全文不再重复累计不同推文数；大任务需显式预算，单独 `--yes` 不再放开限制。

## 验证与贡献

```bash
python -m unittest discover -s tests -v
```

检查模拟 API 响应，不需要密钥、不访问 SocialData、不消耗额度，覆盖预算、分页、失败保存、网络拦截与计费口径、批量缺失检测、密钥来源优先级、查询长度、dry-run、时区、CSV、JSONL、原文及参数验证。GitHub Actions 在 Windows/Linux 和多个 Python 版本执行同样检查。真实接口验证需自行配置测试账户及限定预算。

问题反馈附参数、退出码和脱敏日志，不要上传密钥、环境文件或未经检查的结果。

## 许可证

[MIT](LICENSE)。代码许可不替代 SocialData 或 X 对服务和数据内容的相关条款。
