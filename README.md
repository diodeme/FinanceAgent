# FinanceAgent

基于 AgentScope 的财务分析助手，支持 SEC 财报查询、跨季度指标对比、来源追溯、财报原文分析和 Excel 导出。后端使用 Python / FastAPI，前端使用 React，数据存储使用 PostgreSQL 和 Redis。

## 环境要求

- Git、Docker Engine 和 Docker Compose v2；使用 Linux 容器。
- Windows 建议在 WSL2 中执行下方命令，并确保 WSL 中可运行 `docker compose`。
- 能访问 SEC、模型 API 及构建所需的镜像/依赖源。
- 一个支持工具调用的模型 API 凭证；开发环境使用 DeepSeek，在网页中配置即可。

> 仅用于可信的本地开发/演示环境。用户名通过 `X-User-ID` 传递，不是正式身份认证；后端挂载 Docker socket 以创建分析容器。当前 UI/API 端口未限定为本机监听，请勿直接暴露到公网。

## 1. 配置

```bash
git clone https://github.com/diodeme/FinanceAgent.git
cd FinanceAgent
cp .env.example .env
```

编辑 `.env`，主要配置如下：

| 变量 | 说明 |
| --- | --- |
| `POSTGRES_USER` | 数据库用户名，可保留示例值 |
| `POSTGRES_PASSWORD` | 数据库密码，请替换示例值；当前连接 URL 直接拼接密码，避免使用未编码的 URL 特殊字符 |
| `POSTGRES_DB` | 数据库名，可保留示例值 |
| `SEC_USER_AGENT` | SEC 请求身份，填写应用名及真实联系邮箱，例如 `"FinanceAgent/0.1 your-name@your-domain.com"`，请换成自己的真实地址 |
| `CORS_ORIGINS` | 可选，允许的前端来源，多个来源用逗号分隔；本机可设为 `http://localhost:8080` |
| `ANALYSIS_WORKSPACE_DIR` | 可选，分析临时目录，默认 `/tmp/financeagent-analysis` |

获取 SEC 财报原文需要有效的 `SEC_USER_AGENT`；占位邮箱不可用，合规配置也不能保证 SEC 不返回 403。

分析工作区路径必须是 **Docker daemon 宿主机与后端容器中相同的绝对 Linux 路径**，两边需要访问同一份文件。通常保留默认值即可；使用 Docker Desktop 或远程 daemon 时需确认路径映射，不要填写 `E:\...` 这样的 Windows 路径。

`.env` 中的 `DEEPSEEK_*` 仅供可选的 `scripts/seed.sh` 初始化脚本使用，正常网页使用无需填写，也不会自动导入网页凭证。知识库当前未启用，无需配置 embedding。

## 2. 启动

在仓库根目录执行：

```bash
docker compose up -d --build
docker compose ps
```

首次启动会构建镜像并自动执行数据库迁移，不需要手动建表。

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

如果启动失败，先查看日志：

```bash
docker compose logs --tail=100 backend
```

已有旧版数据库但尚未登记迁移版本时，请参考 [数据库迁移说明](docs/数据库迁移.md)，不要通过删除数据卷或强制 stamp 来解决。

## 3. 首次使用

1. 打开 http://localhost:8080。
2. 在初始化页面填写服务地址 `http://localhost:8000`，用户名可填 `dev`。后续使用相同用户名可继续访问原有会话。
3. 在 **Credentials / 凭证** 页面添加模型 API Key。使用 DeepSeek 时，Base URL 为 `https://api.deepseek.com`。
4. 进入聊天页面，选择自动创建的 **FinanceAgent**，新建会话并选择可用的凭证和模型。
5. 发送问题即可。首次运行分析时需要准备工作区镜像，可能等待数分钟。

可在同一会话中依次尝试：

```text
查一下苹果最近一个已披露财季的营收。
再对比苹果和微软最近四个已披露财季的营收和毛利率。
结合对应财报原文，分析这些变化的原因。
把刚才的对比表和分析整理成 Excel。
```

点击结果表中的数值或报告引用可查看来源；分析报告支持展开阅读，Excel 可从导出卡片下载。

当前支持的公司为 AAPL、MSFT、NVDA、AMZN、GOOGL（GOOG 别名）和 META，一次最多查询 8 个财季。缺失指标会明确标注；不提供通用网页搜索。

分析方法使用 `financial-analysis` Skill。需要调整时，可复制并编辑 `backend/finagent/skills/financial-analysis/SKILL.md`，保留名称，在聊天工作区的 Skills 面板移除旧版本后上传修改后的文件夹；下一次分析生效，无需重启。

## 常用操作

```bash
# 查看服务状态
docker compose ps

# 跟踪后端日志
docker compose logs -f backend

# 更新代码后重新构建并启动
docker compose up -d --build

# 修改 .env 后，在没有分析任务运行时重新创建后端
docker compose up -d --force-recreate backend

# 停止服务，保留数据卷
docker compose down
```

数据库、会话和持久化文件保存在 Docker volumes 中。**不要使用 `docker compose down -v`，除非确定要删除这些数据。** `.env`、本地数据及导出的报表不提交到仓库。

## 更多文档

- [设计说明](docs/设计.md)
- [数据结构](docs/数据结构.md)
- [数据库迁移](docs/数据库迁移.md)
- [验收记录](docs/验收.md)
