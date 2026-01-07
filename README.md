# AI Strategy Agent

这是一个基于 LLM 的智能股票交易策略代理系统。它结合了自动化流程、市场数据扫描、AI 分析和决策制定，旨在辅助或自动执行交易策略。

## 主要功能

*   **全自动交易流程**：
    *   **早盘策略 (Pre-Market)**：自动扫描热门股票，获取新闻资讯，利用 AI 分析师生成报告，并由 AI 决策者制定买入计划。
    *   **尾盘风控 (Pre-Close)**：监控当前持仓，结合最新行情进行风险评估，自动触发卖出或减仓信号。
*   **AI 驱动分析**：
    *   **Analyst Agent**：分析个股的基本面、技术面和新闻情绪。
    *   **Decision Maker Agent**：基于分析报告和账户资金状况，制定最终交易决策（买入/卖出/持仓），并包含资金管理功能。
*   **多源数据支持**：集成 `Tushare` 获取行情数据，`AkShare` (经由 NewsClient) 获取新闻数据。
*   **通知推送**：通过钉钉机器人 (DingTalk) 实时推送策略报告和交易执行结果。
*   **风险控制**：支持单只股票最大仓位限制、止盈止损逻辑（通过 AI 分析实现）。

## 项目结构

```
.
├── agents/             # AI 代理模块 (分析师、决策者)
├── core/               # 核心组件 (扫描器、交易执行、消息推送、数据库模型)
├── data/               # 本地数据存储 (如 SQLite 数据库)
├── logs/               # 运行日志
├── prompts/            # LLM Prompt 模板 (Jinja2)
├── config.yaml         # 策略配置文件
├── main.py             # 主程序入口
└── requirements.txt    # 依赖列表
```

## 快速开始

### 1. 环境准备

确保已安装 Python 3.8+。

```bash
# 创建并激活虚拟环境
python -m venv venv
source venv/bin/activate  # macOS/Linux
# venv\Scripts\activate   # Windows

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置文件

复制 `.env.example` (如果存在) 或创建 `.env` 文件，配置必要的 API Token：

```ini
# .env
TUSHARE_TOKEN=your_tushare_token
DING_ROBOT_ACCESS_TOKEN=your_dingtalk_token
DING_ROBOT_SECRET=your_dingtalk_secret
LLM_API_KEY=your_llm_api_key1,your_llm_api_key2  # 支持多key轮询
LLM_BASE_URL=your_llm_base_url
```

修改 `config.yaml` 调整策略参数：
*   `watchlist`: 默认关注股票池
*   `max_position_per_stock`: 单只股票最大仓位限制

### 3. 运行

**手动运行测试模式 (不实际交易，仅生成报告):**

```bash
# 运行早盘流程
python main.py --pre-market --test

# 运行尾盘风控
python main.py --pre-close --test
```

**启动定时任务:**

直接运行主程序，将启动 `APScheduler` 定时任务：

```bash
python main.py
```

## 注意事项

*   本项目仅供学习和研究使用，不构成任何投资建议。
*   `data/` 和 `logs/` 目录默认已被 `.gitignore` 忽略，请勿上传敏感数据。
