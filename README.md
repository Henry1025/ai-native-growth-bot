# AI 新闻日报 Bot

自动抓取 AI & 内容赛道 RSS 新闻，经 Kimi 初筛 + 深度分析后，每日推送至飞书文档。

## 架构

```
RSS 抓取 (aiohttp + feedparser)
    ↓
去重 (difflib 标题相似度)
    ↓
初筛 (Kimi K 2.0 — 结构化 JSON)
    ↓
深度分析 (Kimi 2.5 — Markdown 日报)
    ↓
飞书文档 (lark-oapi)
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 MOONSHOT_API_KEY

# 3. 配置 RSS 源
# 编辑 config/rss_sources.yaml，填入 RSS 源列表

# 4. 运行
python src/main.py
```

## 配置

| 文件 | 说明 |
|------|------|
| `.env` | API 密钥（不提交 Git） |
| `config/rss_sources.yaml` | RSS 源列表（待 @haochen 补充） |
| `config/prompts/screening.txt` | Kimi 初筛 Prompt |
| `config/prompts/analysis.txt` | Kimi 深度分析 Prompt |
| `config/settings.py` | 模型参数、截断长度等 |

## 定时运行

GitHub Actions: 每天 UTC 03:00（北京时间 11:00）自动运行。
也可在 Actions 页面手动触发（`workflow_dispatch`）。

在 GitHub 仓库 Settings → Secrets 中配置：
- `MOONSHOT_API_KEY`
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
