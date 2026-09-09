# 内部试点部署契约固化设计

## 目标与边界

本次交付让既有知识管理能力通过一套可复制的 Docker Compose 契约面向小范围内部试点运行。交付覆盖可配置的本机入口、HTTPS 反向代理模板、启动 profile、健康语义、无秘密部署说明、只读部署验证和可填写的知识发布验收记录。

不改变草稿、发布、回滚、RBAC 或知识读取隔离的领域行为。不公开数据库、Redis、Chroma 或 publisher 端口；不挂载 Docker socket，也不扩展 app、read proxy 或 publisher 的卷和网络权限。

## 架构与配置契约

`app` 继续只绑定 loopback，Compose 端口映射改为 `127.0.0.1:${APP_HOST_PORT:-8000}:8000`。`APP_HOST_PORT` 仅控制宿主机到 app 的入口，不能用于任何内部服务。

仓库提供 Caddy 模板。Caddy 在内部试点域名上终结 HTTPS，并反向代理至 loopback `app` 入口；应用本身不暴露到非 loopback 地址。运行手册要求内部试点设置 `AUTH_COOKIE_SECURE=true`，并由验收步骤检查浏览器 Cookie 的 `Secure`、`HttpOnly` 和 `SameSite=Lax` 属性。

部署的唯一权威配置文件是部署目录中的 `.env`。所有 Compose 命令显式传入该文件并使用固定 project name；文档禁止将同一 Compose 项目名和持久卷与另一套数据库密码组合使用。`.env.example` 只声明变量和非秘密默认值。

## 服务操作与健康语义

`knowledge-backup` 从 `knowledge-admin` profile 移至独立的 `knowledge-backup` profile，并使用显式、一次性、失败返回非零退出码的备份命令。正常的知识管理启动只创建常驻服务及必要的管理 worker，不创建 backup 容器。备份运行命令单独写入运行手册。

`/api/health` 代表 app 进程存活，Compose app healthcheck 使用该端点。`/api/ready` 继续严格表示已发布的非空知识版本可查询：首次发布前返回 503，发布后返回 200。运维文档和验收记录明确将首次知识发布视为知识问答的阻断门，但不阻塞登录、管理员上传和首次发布操作。

## 验证与失败处理

新增只读 PowerShell 验证脚本。脚本接收明确的 env 文件和 project name，且不输出变量值、不启动或停止服务、不修改卷。它验证 Compose 配置渲染、必填变量的存在、app 的 loopback 端口映射，以及关键服务的预期状态；验证失败返回非零退出码并只报告变量名或状态类别。

自动化测试覆盖：端口默认值与覆盖值、所有非 app 服务未公开端口、backup profile 隔离与明确命令、Compose healthcheck 的 liveness 语义、首次发布前和发布后的 readiness API 语义，以及部署文档和验收模板的关键契约。测试继续断言知识网络与卷隔离未变化。

## 操作流程与验收

运行手册提供从复制 `.env.example`、检查必填变量、渲染 Compose、固定项目名启动、迁移状态确认、管理员引导、Caddy 配置、HTTPS/Cookie 检查到知识管理启动的顺序命令。所有敏感值只由操作者写入 `.env`，命令和脚本不得打印其值。

试点验收记录提供可填写的字段和每项的命令、预期结果与证据位置：管理员登录、真实但脱敏的 ZIP 包上传与校验、发布、带来源的问题查询、非管理员 API 403、失败发布保持当前版本、回滚命中旧版本，以及版本 ID、包 SHA-256、操作者、镜像 digest 和证据链接。记录禁止包含密码或 token。
