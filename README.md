# AI 新闻日报 Bot

自动抓取 AI & 内容赛道 RSS 新闻，经 DeepSeek 初筛 + 深度分析后，每日推送至飞书文档。

## 架构

```
RSS 抓取 (aiohttp + feedparser)
    ↓
去重 (difflib 标题相似度)
    ↓
初筛 (DeepSeek Flash，关闭思考 — 双路结构化 JSON)
    ↓
深度分析 (DeepSeek V4 Pro，高强度思考 — 结构化 JSON 日报)
    ↓
飞书文档 (lark-oapi)
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 3. 配置 RSS 源
# 编辑 config/rss_sources.yaml，填入 RSS 源列表

# 4. 运行
python src/main.py
```

## 配置

| 文件 | 说明 |
|------|------|
| `.env` | API 密钥（不提交 Git） |
| `config/rss_sources.yaml` | RSS 源列表 |
| `config/prompts/screening.txt` | DeepSeek 初筛 Prompt |
| `config/prompts/analysis.txt` | DeepSeek 深度分析 Prompt |
| `config/settings.py` | 模型参数、截断长度等 |

## 运行与恢复

GitHub Actions 执行 `.github/workflows/daily_report.yml`；本地代码只有手动运行时才执行。
初筛双路均使用 `deepseek-flash`（关闭思考），深度分析使用 `deepseek-v4-pro`（开启思考，`high` 强度）。
3 分及以上进入候选，4 分及以上进入日报。没有达标新闻与模型调用失败分开处理：调用失败令任务报错，不覆盖旧日报。
日报替换先写入新内容，再移除旧段落；读取文档时遍历全部分页。

每次定时运行把原始新闻、初筛结果、分析结果保存为 Actions artifact，保留 30 天。
历史 RSS 可能不再包含旧文章，回填时优先恢复 artifact 或当天初筛表的原始链接。
`python src/backfill.py --start-date YYYY-MM-DD --end-date YYYY-MM-DD` 可回填仍在 RSS 中的日期；默认保留已有日期。
用 `--articles-dir PATH` 从原始新闻 JSON 归档恢复；需要替换已有日报时显式添加 `--replace-existing`。无达标新闻时仍保留已有报告。

回归检查：`python -m unittest discover -s tests -v`。

## 定时运行

GitHub Actions: 每天 UTC 03:50（北京时间 11:50）自动运行。
也可在 Actions 页面手动触发（`workflow_dispatch`）。

在 GitHub 仓库 Settings → Secrets 中配置：
- `DEEPSEEK_API_KEY`
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
