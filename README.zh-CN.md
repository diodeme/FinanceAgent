# FinanceAgent

[English](README.md) | **简体中文**

面向 SEC 财报的财务分析助手。支持跨季度指标对比、数值来源追溯、财报原文分析，以及表格和报告的 Excel 导出。

[快速开始](#快速开始) · [使用示例](#使用示例) · [常用命令](#常用命令) · [常见问题](#常见问题) · [项目文档](#项目文档)

## 功能

- **季度对比**：查询已披露的财务数据，跨公司、跨财季对比并计算指标。
- **来源追溯**：从结果单元格或报告引用查看对应期间、单位、财报和计算依据。
- **财报分析**：结合结构化财务数据和 SEC 原文，由运行在 Docker 工作区中的分析 Agent 完成研究，分析方法可通过 Skill 调整。
- **结果保存与导出**：在会话中查看已完成的结果，将表格和文字报告导出为 Excel。

技术栈：**AgentScope 2.0.10**、**Python / FastAPI**、**React / TypeScript**、**PostgreSQL 16** 和 **Redis**。

> [!WARNING]
> 仅用于可信的本地开发和演示环境。`X-User-ID` 是占位身份机制，不是安全认证。后端挂载 Docker socket，拥有 Docker 管理权限。UI 和 API 端口未限定为本机监听，请勿直接暴露到公网。

## 快速开始

### 环境要求

- Git、Docker Engine 和 Docker Compose v2，使用 Linux 容器。
- Windows 请使用 WSL2，并确保 WSL 中可运行 `docker compose`。
- 能访问 SEC、模型服务，以及构建所需的镜像和依赖源。
- 一个支持工具调用的模型 API 凭证。开发环境使用 DeepSeek，在网页中配置凭证即可。

### 1. 配置

```bash
git clone https://github.com/diodeme/FinanceAgent.git
cd FinanceAgent
cp .env.example .env
```

参考 [`.env.example`](.env.example) 编辑 `.env`：

| 变量 | 配置说明 |
| --- | --- |
| `POSTGRES_USER` | 数据库用户名，可保留示例值。 |
| `POSTGRES_PASSWORD` | 请替换示例密码。当前密码直接拼入连接 URL，避免使用未编码的 URL 特殊字符。 |
| `POSTGRES_DB` | 数据库名，可保留示例值。 |
| `SEC_USER_AGENT` | 应用名和**真实联系邮箱**，例如 `"FinanceAgent/0.1 your-name@your-domain.com"`，请换成自己的真实地址。 |
| `CORS_ORIGINS` | 可选，允许的前端来源，多个来源用逗号分隔。本机可设为 `http://localhost:8080`。 |
| `ANALYSIS_WORKSPACE_DIR` | 可选，默认 `/tmp/financeagent-analysis`，路径要求见下方说明。 |

> [!IMPORTANT]
> 分析功能需要 Linux Docker daemon，并能访问 `/var/run/docker.sock`。分析工作区必须在 **Docker daemon 宿主机和后端容器中使用相同的绝对 Linux 路径，且指向同一份文件**。没有特殊需求时保留默认值；使用 Docker Desktop 或远程 daemon 时需确认挂载行为，不要填写 `E:\...` 这样的 Windows 路径。

`DEEPSEEK_*` 变量仅供可选的开发初始化脚本 [`scripts/seed.sh`](scripts/seed.sh) 使用。正常网页配置无需填写，也不会自动创建网页凭证。知识库当前禁用，无需配置 embedding。

### 2. 启动服务

```bash
docker compose up -d --build
docker compose ps
```

后端会在启动 API 前自动执行数据库迁移。全新安装不需要手动建表。

| 服务 | 地址 |
| --- | --- |
| 网页 | http://localhost:8080 |
| 后端 API | http://localhost:8000 |
| PostgreSQL | `127.0.0.1:5432` |
| Redis | `127.0.0.1:6379` |

检查后端是否就绪：

```bash
curl -H "X-User-ID: dev" http://localhost:8000/health
```

### 3. 配置模型

1. 打开 **http://localhost:8080**。
2. 在初始化页面填写服务地址 `http://localhost:8000`，用户名可填 `dev`。后续使用相同用户名可继续访问原有会话。
3. 在 **Credentials / 凭证** 页面添加模型 API 凭证。使用 DeepSeek 时，Base URL 为 `https://api.deepseek.com`。
4. 进入聊天页面，选择自动创建的 **FinanceAgent**，新建会话并选择凭证和可用模型。
5. 发送问题即可。首次分析需要准备 Docker 工作区镜像，可能等待数分钟。

## 使用示例

可在同一会话中依次提问：

```text
查一下苹果最近一个已披露财季的营收。
再对比苹果和微软最近四个已披露财季的营收和毛利率。
结合对应财报原文，分析这些变化的原因。
把刚才的对比表和分析整理成 Excel。
```

点击结果表中的数值或报告引用可查看来源。展开分析卡片可阅读报告，通过导出卡片可下载 Excel 文件。

**当前覆盖范围**：AAPL、MSFT、NVDA、AMZN、GOOGL（GOOG 别名）和 META 的 US-GAAP 合并口径财务数据，每次最多查询 8 个财季。不支持的指标和缺失数据会明确标注，不提供通用网页搜索或 RAG 流程。

**调整分析方法**：复制并编辑 [`financial-analysis/SKILL.md`](backend/finagent/skills/financial-analysis/SKILL.md)，保留 `financial-analysis` 名称。在聊天工作区的 **Skills** 面板移除旧 Skill，再上传修改后的文件夹。下一次分析使用新方法，无需重启，已保存的报告不受影响。

## 常用命令

在仓库根目录执行：

| 操作 | 命令 |
| --- | --- |
| 查看服务状态 | `docker compose ps` |
| 跟踪后端日志 | `docker compose logs -f backend` |
| 更新代码后重新构建并启动 | `docker compose up -d --build` |
| 应用后端 `.env` 修改，需确认没有分析任务运行 | `docker compose up -d --force-recreate backend` |
| 停止服务，保留数据卷 | `docker compose down` |

> [!CAUTION]
> 数据库、会话和持久化文件保存在 Docker volumes 中。`docker compose down -v` 会删除这些数据卷。请勿将 `.env`、本地数据或导出报表提交到 Git。

## 常见问题

| 问题 | 排查方法 |
| --- | --- |
| 后端无法启动 | 运行 `docker compose logs --tail=100 backend`，检查数据库配置和迁移错误。旧版数据库尚未登记迁移版本时，请遵循[迁移说明](docs/数据库迁移.md)，不要删除数据卷或强制 stamp。 |
| 网页无法连接 | 确认服务地址**可从当前浏览器访问**。从另一台机器打开网页时，不能用 `localhost` 指代服务所在机器。同时检查 `CORS_ORIGINS`。 |
| SEC 财报下载失败 | 在 `SEC_USER_AGENT` 中填写真实联系邮箱，再重新创建后端。占位邮箱会被拒绝；合规的 User-Agent 也不能保证访问成功或避免 HTTP 403。 |
| 分析工作区启动失败或较慢 | 检查 Docker socket 权限和共享的绝对工作区路径。首次构建镜像需要联网，可能耗时数分钟。 |

## 评估

五道对照题覆盖 AAPL/MSFT 财报、财年与自然年对齐，以及财务分析。FinanceAgent 与原生 Codex Agent 接收相同问题，使用同一配置的 `deepseek-flash` 模型，每题上限五分钟。标准答案从官方报表独立核验，不取自应用数据库；分别报告数值正确覆盖率、助手评分的分析质量、token 和耗时，保留失败与退化的运行记录。

结果及限制见[评估报告](docs/评估.md)，复现方式见[评测说明](eval/README.md)。Codex 是通用 Agent 基线，**不是 ChatGPT 产品本身**；这份小规模实验不证明系统全面优于 ChatGPT。

## 项目结构

```text
backend/   API、财务工具、分析 Agent、数据库迁移和测试
webui/     React 前端、nginx 配置和浏览器验收测试
db/        PostgreSQL 初始化
scripts/   可选的开发初始化脚本
eval/      固定题目、独立标准答案、执行脚本、评分及答案快照
docs/      设计、数据结构、迁移说明和验收记录
```

## 项目文档

详细项目文档目前以中文维护：

- [设计说明](docs/设计.md)
- [数据结构](docs/数据结构.md)
- [数据库迁移](docs/数据库迁移.md)
- [验收记录](docs/验收.md)
