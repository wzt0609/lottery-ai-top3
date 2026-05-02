# 中国福利彩票 3D / 体彩排列三排列五统计预测工具

这是一个每天自动检索开奖数据、生成统计候选号码、并在开奖后做复盘优化的小程序。

重要说明：彩票开奖结果具有随机性，本工具只做历史统计、候选组合记录和复盘，不保证中奖，也不构成投注建议。

## 功能

- 云端版本每 30 分钟运行一次，持续尝试刷新公开数据并生成页面。
- 每天 20:00 后生成中国福利彩票 3D、中国体育彩票排列三、排列五的正式预测报告。
- 每天开奖后默认 22:00 再次抓取开奖结果，复盘当天候选号码表现，并小步调整模型权重。
- 手机刷新 `mobile.html` 时会触发实时检查：预测数据超过配置间隔会重新抓取生成；22:00 后还会自动尝试开奖复盘。
- 自动保存历史开奖数据到 `data/`。
- 自动保存预测报告和开奖后复盘到 `reports/`。
- 根据最近回测表现调整下一轮统计权重。
- 根据每日复盘表现迭代调整 legacy/markov/bayes/shape 模型权重。
- 数据源包含 17500 历史文本数据、中国福彩网、中国体彩网/国家彩票平台页面或接口。
- 候选评分纳入走势图常见形态：位置热度、近期性、遗漏、相邻期转移、和值、和值尾、跨度、奇偶比、大小比、012 路分布。

## 快速运行

```powershell
python .\lottery_predictor.py init
python .\lottery_predictor.py collect
python .\lottery_predictor.py predict
python .\lottery_predictor.py post-draw
```

运行后查看：

- `data/*.csv`：历史开奖数据
- `reports/prediction-YYYY-MM-DD.md`：每日预测报告
- `reports/post-draw-YYYY-MM-DD.md`：开奖后复盘
- `reports/mobile.html`：手机友好的每日最高评分 3 码页面
- `config.json`：时间、候选数量、统计权重等配置

## 手机查看链接

每天执行 `predict` 后会自动刷新 `reports/mobile.html`，页面只突出显示：

- 3 个福彩 3D 号码
- 3 个排列三号码
- 3 个排列五号码

在电脑上启动手机页面服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_mobile_server.ps1
```

控制台会输出类似下面的链接：

```text
Phone on the same Wi-Fi: http://192.168.1.23:8765/mobile.html
```

手机和电脑连接同一个 Wi-Fi 后，用手机浏览器打开这个链接即可。

## 长期运行方式一：后台常驻

```powershell
python .\lottery_predictor.py daemon
```

程序会按 `config.json` 中的 `predict_time` 和 `post_draw_time` 执行。

## 长期运行方式二：Windows 任务计划程序

先用管理员或当前用户 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1
```

它会创建两个任务：

- `LotteryPredictor-DailyPredict`：每天 20:00 运行预测
- `LotteryPredictor-PostDraw`：每天 22:00 运行开奖后复盘

如果你的地区开奖公布时间有延迟，可以修改 `config.json` 中的 `post_draw_time`，并重新运行任务安装脚本。

## 配置项

`config.json` 会在第一次运行时自动生成，常用字段：

- `predict_time`：每日预测时间，默认 `20:00`
- `post_draw_time`：开奖后复盘时间，默认 `22:00`
- `candidate_count`：每种彩票输出候选号码数量
- `history_limit`：本地保留多少期历史数据
- `weights`：频率、近期性、遗漏、转移统计的组合权重
- `web_refresh_predict_minutes`：手机页面刷新触发预测重算的最小间隔，默认 0，表示每次刷新都强制重算。
- `web_refresh_post_draw_always`：默认开启，表示每次刷新手机页面都会尝试开奖复盘，然后再生成预测页。

## 数据来源

程序会尝试使用 17500 历史文本数据、中国福利彩票官网、中国体彩网/国家彩票平台公开开奖页面或接口；如果网站结构调整导致抓取失败，会使用本地已缓存历史数据继续生成报告，并在控制台提示错误。

## 优化逻辑

每天开奖后执行 `post-draw` 时，程序会：

1. 重新抓取最新开奖结果。
2. 对当天预测候选做位置命中和完整命中复盘。
3. 用最近若干期做滚动回测，选择表现较好的统计权重。
4. 把下一轮权重写回 `config.json`。

这些优化只能让报告更贴近历史统计特征，不能消除开奖随机性。
