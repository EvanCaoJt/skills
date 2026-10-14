---
name: X-Research
description: "通过 SocialData API 抓取 X（Twitter）推文，内置脚本负责分页、重试和费用估算。支持：账号最新/历史推文（多账号合并）、关键词与高级搜索（from:、since:、min_faves: 等）、单条推文及其线程/回复/引用、X 长文与长帖全文、列表时间线、账号资料。用户提到 SocialData，或要求抓取/拉取/监控 X（推特）账号的推文、按话题或条件搜索推文、读取推文链接或 thread、为选题和投研收集 X 上的一手内容时使用。"
---

## 前置条件

- **脚本**：本技能目录下的 `socialdata_x.py`（只用 Python 3 标准库，沙箱需能联网）。`/data/socialdata_x.py` 已存在就直接用；否则用 `notion_load_page` 加载本页，把「文件」属性里的 `file://…` 地址原样传给 `notion_view_file_url`，文件会下载进沙箱并返回 `sandbox_path`，再 `cp` 到 `/data/socialdata_x.py`。
- **密钥**：读环境变量 `SOCIALDATA_API_KEY`，其次读 `/data/.socialdata.env`。两处都没有就向用户索取，密钥在 SocialData 控制台的 API keys 页面。每个 bash 调用可能是新 shell，所以把密钥写在命令前面：`SOCIALDATA_API_KEY=你的密钥 python3 /data/socialdata_x.py ...`。不要在回复里复述密钥，不要写进 Notion 页面；只有用户明确同意，才保存到 `/data/.socialdata.env`（`chmod 600`）。

## 费用与限速

- 每条推文或每份资料 $0.0002（$0.2/千条），每页约 20 条；查询单个对象（一条推文、`--full`、`--timeline` 先查的账号资料）每次也是 $0.0002。
- 默认 `--max 50`，约 $0.01。`--max` 超过 1000（约 $0.2）脚本会拒绝运行：先把估算花费告诉用户，确认后加 `--yes`。
- 限速 120 次/分钟，脚本自动节流，并对 429/5xx 重试 3 次。402 是余额不足：请用户去 SocialData 控制台充值，不要反复重试。
- 每次运行结束，stderr 会打印「估算花费」，汇报时带上。

## 命令速查

格式：`python3 /data/socialdata_x.py 子命令 参数`，`-h` 查看全部参数。

| 需求 | 命令 |
| --- | --- |
| 账号最近推文 | `user elonmusk nvidia --since-hours 24 --no-replies` |
| 多账号合并，取最新 N 条 | `user a b c d e --max 60`（每 20 个账号合成一次搜索） |
| 每个账号各取 N 条 | `user a b --per-account --max 5` |
| 单账号完整时间线 | `user a --timeline --since 2026-10-01` |
| 话题、关键词、条件 | `search '"HBM4" (CoWoS OR CPO) min_faves:50 lang:en' --type Top --since 2026-09-01` |
| 只要上次之后的新推文 | 在 `user` 或 `search` 后加 `--since-id LAST_ID` |
| 指定推文 | `tweet URL_OR_ID` |
| 线程、回复、引用 | `tweet URL --thread --comments 20 --quotes 10` |
| 长文、被截断的长帖 | `tweet URL --full` |
| 列表时间线 | `list LIST_ID_OR_URL` |
| 账号资料、核对账号名 | `profile a b` |

常用参数：`--max`；`--since` / `--until`（按 `--tz` 解释，默认 Asia/Shanghai，如 `2026-10-06T08:00`，`--until` 不含）；`--min-faves`；`--lang`；`--originals`（只要原创）；`--format md/json/jsonl/csv`；`--out 文件`；`--sort new/old/likes`；`--raw`（json 输出 API 原始对象）。`search` 支持 X 全部高级操作符（`from:`、`to:`、`"短语"`、`OR`、`-词`、`filter:media`、`url:` 等）。

## 执行流程

1. 确定账号或话题、时间范围、条数。用户没说的用默认值，并在汇报里写明，不要为此追问。
2. 按「费用与限速」估算；预计超过约 $0.2 先问用户。
3. 运行脚本。结果超过约 40 条，或还要二次处理，就加 `--out /data/x_tweets.md`（或 `--format json --out ...`）再读文件，别把几百条原文直接打印进对话。
4. 看 stderr 的条数和估算花费。0 条时先用 `profile` 核对账号名，再检查时间范围和过滤条件；受保护账号抓不到。
5. 汇报：第一句给结论（抓到几条、时间范围、花费），再给内容。用户要分析时才归纳。

## 输出规范

- 推文原文逐字保留，不改数字，不补推文里没有的事实；翻译、摘要、判断另起一行并标明是译文或解读。
- 每条保留作者、时间、互动数据和原帖链接；引用推文、转推要标出，转推正文取自原帖。
- 存入 Notion 时按推文 URL 去重（先查已有行），字段至少含链接、作者、发布时间、正文、点赞、转发、浏览。

## 易错点

- `-filter:retweets` 会连引用推文一起排除：要「原创」用 `--originals`，只去掉回复用 `--no-replies`。搜索默认已排除原生转推。
- `--timeline` 默认丢弃转推（`--with-retweets` 保留），置顶帖脚本已处理。评论接口只返回顶层推文的回复。
- 正文只有标题加链接（输出会提示「含 X 长文」），或文末被截断（…）：用 `--full` 取全文。
- 搜索走 X 站内搜索，个别推文可能搜不到；要尽量完整的单账号历史，用 `--timeline`。
- 推文 ID 一律按字符串处理。
- 脚本不覆盖粉丝/关注列表（按页计费很贵）、Spaces、社群、Monitor/Webhook；需要时读 [docs.socialdata.tools/llms.txt](https://docs.socialdata.tools/llms.txt)。
