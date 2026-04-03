# NewsAlert

这个项目包含两个微信机器人：

- `wxbot.py`：3 秒轮询 AKShare 新闻并发送到 `HAO` 群
- `daily_digest_bot.py`：每次启动生成一份 GitHub + ClawHub 日报并发送到 `HAO` 群

## 新闻机器人

启动：

```powershell
python wxbot.py
```

或直接双击：

```text
start_news_bot.bat
```

当前新闻模板：

```text
市场热点
1.  内容一；
2.  内容二；
3.  内容三
```

## 每日日报机器人

启动：

```powershell
python daily_digest_bot.py
```

或直接双击：

```text
start_daily_digest.bat
```

当前行为：

- 每次启动 `daily_digest_bot.py`，就生成并发送一次日报
- 发送完成后直接退出
- `--once` 仍可用，但只是兼容参数

手动试发：

```powershell
python daily_digest_bot.py --once
```

## 主要配置

配置文件：

```text
.env
```

常用项：

```env
WECHAT_TARGET=HAO
POLL_INTERVAL_SECONDS=3
NEWS_HISTORY_LIMIT=100

DAILY_DIGEST_TARGET=HAO
GITHUB_TOP_N=5
CLAWHUB_TOP_N=5
CLAWHUB_TOPIC_QUERIES=agent,github,search,automation,news,wechat
```

## 启动脚本

- `start_news_bot.bat`
- `start_daily_digest.bat`
