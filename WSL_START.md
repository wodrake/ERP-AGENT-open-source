# WSL 修复版启动与回退

## 本次修复

- 从真实 LangGraph 配置读取 thread_id，缺少 ID 时跳过归档，避免 ep_unknown 覆盖历史。
- 每次模型调用动态注入当前用户 WARM 摘要，限制为 4000 字符；不再使用创建 Agent 时的静态快照。
- 记忆配置读取 .env；可选 LLM 抽取接入主链路，默认关闭，开启会产生额外模型调用。
- 规则抽取跳过否定和明确临时表达，只处理最新用户轮次，避免旧会话偏好反复覆盖。
- 新增真实框架的离线集成测试，无需付费模型、MongoDB 或 Docker。

## 本机目录

新版：`/home/administrator/projects/ERP-AGENT-open-source`

旧版：`/home/administrator/projects/ERP-AGENT`，未覆盖，原有未提交改动保留。

新版复制了旧版 .env（不加入 Git），通过 .venv 和 frontend/node_modules 符号链接复用旧版依赖。
暂时不要删除旧目录，也不要在共享环境里升级依赖。要独立部署时请新建虚拟环境并安装 requirements.txt，前端执行 npm ci。

## 启动前

先启动 Windows Docker Desktop，并在 Settings → Resources → WSL Integration 中启用 Ubuntu。
在 WSL 执行 `docker version`，应同时看到 Client 和 Server。需要 MongoDB 服务可用，且 .env 中地址正确。
如果已有 MongoDB 容器，启动原容器，不要删除已有数据库或数据卷。

本次检查时 Docker 在 WSL 不可用，因此尚未验证真实 Docker/MongoDB/模型的完整链路。

## 三个 WSL 终端分别启动

终端 1：MCP 服务

```bash
cd ~/projects/ERP-AGENT-open-source
.venv/bin/python -m src.mcp_server.server_main
```

终端 2：后端（先确认 MCP 已启动）

```bash
cd ~/projects/ERP-AGENT-open-source
.venv/bin/python -m src.api_view.web_main
```

终端 3：前端

```bash
cd ~/projects/ERP-AGENT-open-source/frontend
npm run dev
```

浏览器访问 http://localhost:3000 。不要同时启动旧版的同端口服务。

## 验证记忆

先执行离线测试：

```bash
cd ~/projects/ERP-AGENT-open-source
.venv/bin/python -m src.test.test_memory_layer
.venv/bin/python -m unittest src.test.test_memory_integration -v
```

网页使用同一用户：在会话 A 说“以后用柱状图”，新建会话 B 请求展示数据，确认偏好生效。
再在不同会话完成两个任务，检查历史记忆能分别检索，不会全部覆盖为 ep_unknown。
新用户不应看到上一用户的记忆。已有 ep_unknown 中被覆盖的数据不能仅靠修复代码恢复。

## Git 版本与安全回退

开发分支：`codex/memory-integration-fixes`。
修复前快照标签：`memory-before-fixes-20260917`。
修复后标签：`memory-fixed-20260917`。

```bash
git log --oneline -5
git diff memory-before-fixes-20260917 memory-fixed-20260917 --stat
```

若要对照修复前版本，先停服务，再创建单独工作目录，不会抹掉当前修改：

```bash
git worktree add ../ERP-AGENT-memory-before memory-before-fixes-20260917
```

新工作目录需自行配置 .env 和依赖。代码回退不会回滚 MongoDB 数据；重要数据请单独备份。
本次仅作本地 Git 管理，没有推送 GitHub。
