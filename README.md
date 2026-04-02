# AKShare 多来源快讯 -> 微信群 HAO

这个脚本会每 150 秒拉取一次 AKShare 快讯接口，把程序启动时间之后的新消息整理后发到微信群 `HAO`。

当前消息模板：

```text
市场热点
1.  国内77家沥青企业产能利用率21.8%，环比降1.2%；
2.  期货BU2606合约收盘4454，跌幅0.83%；
3.  安徽某拌合站本周新增3万吨采购订单
```

说明：

- 不输出时间
- 不输出来源
- 固定标题为 `市场热点`
- 每条内容按编号列表输出

支持的资讯源：

- `cls`: 财联社快讯 `stock_info_global_cls`
- `sina`: 新浪财经全球快讯 `stock_info_global_sina`
- `em`: 东方财富资讯 `stock_info_global_em`
- `ths`: 同花顺资讯 `stock_info_global_ths`
- `futu`: 富途快讯 `stock_info_global_futu`

默认启用：

```env
AKSHARE_NEWS_SOURCES=cls,sina
```

如果想继续加来源，可以改成：

```env
AKSHARE_NEWS_SOURCES=cls,sina,em,ths,futu
```

运行：

```powershell
python wxbot.py
```

每日 GitHub + ClawHub 通报：

```powershell
python daily_digest_bot.py
```

立刻试发一份：

```powershell
python daily_digest_bot.py --once
```

默认会在每天 `09:00` 发送到 `HAO` 群，配置项在 [`.env`](C:\Users\panjiayuan\Documents\New project\.env)：

```env
DAILY_DIGEST_TARGET=HAO
DAILY_REPORT_TIME=09:00
GITHUB_TOP_N=5
CLAWHUB_TOP_N=5
CLAWHUB_TOPIC_QUERIES=agent,github,search,automation,news,wechat
```
