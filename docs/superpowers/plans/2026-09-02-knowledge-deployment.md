# 知识库部署实施计划

> **面向代理执行者：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐项执行本计划；每个步骤使用 `- [ ]` 跟踪。

**目标：** 建成版本化、仅查询已发布版本的知识服务；由管理员 Compose 任务构建，通过只读代理提供查询，并支持经过验证的备份、恢复和回滚。

**架构：** Chroma 是知识索引卷唯一持有者，仅通过 HTTP 访问。indexer 校验并用单条 registry upsert 发布 release descriptor；read-proxy 在转发应用请求前验证 descriptor，且只允许当前版本读取。Redis 串行化发布操作，不可变 artifact 事件保存审计证据。

**技术栈：** Python 3.12、FastAPI/ASGI、httpx、chromadb 1.5.9、Redis、Docker Compose、pytest、Ruff、POSIX shell、PowerShell。

---

## 文件结构

- 新建：`src/metro_agent/knowledge/config.py`、`chroma_client.py`、`source_validation.py`、`releases.py`、`publication_lock.py`。
- 新建：`src/metro_agent/tools/knowledge_indexer.py`、`src/metro_agent/knowledge_read_proxy.py`。
- 修改：`src/metro_agent/tools/Knowledge_RAGtools.py`、`knowledge_index_registry.py`、`chunk_artifacts.py`、`build_obsidian_index.py`、`readiness.py`、`llama_config.py`、`compose.yml`、`.env.example`、`pyproject.toml`。
- 新建：`deploy/operations/knowledge-admin.sh`、`deploy/operations/knowledge-admin.ps1`、`deploy/operations/with-knowledge-publication-lock.py`、`fixtures/knowledge-e2e/rules.md`、`fixtures/knowledge-e2e/release-smoke-queries.jsonl`。
- 修改：`deploy/operations/backup-all.sh`、`deploy/operations/restore-all.sh`、`deploy/operations/chroma-owner-counts.py`、`README.md`、`docs/operations/coordinated-backup-restore.md`、`docs/operations/internal-pilot-auth-acceptance.md`。
- 新建测试：`tests/test_knowledge_config.py`、`test_knowledge_source_validation.py`、`test_knowledge_releases.py`、`test_knowledge_publication_lock.py`、`test_knowledge_indexer.py`、`test_knowledge_read_proxy.py`、`test_knowledge_readiness.py`、`test_knowledge_e2e_contract.py`。

### 任务 1：固定并配置仅 HTTP 的知识运行时

**文件：**
- 新建：`src/metro_agent/knowledge/__init__.py`、`config.py`、`chroma_client.py`
- 修改：`pyproject.toml`、`.env.example`、`src/metro_agent/llama_config.py`
- 测试：`tests/test_knowledge_config.py`、`tests/test_project_metadata.py`

- [ ] **步骤 1：先写失败的设置契约测试**

```python
def test_production_rejects_e2e_embedding(monkeypatch):
    monkeypatch.setenv("METRO_AGENT_ENV", "production")
    monkeypatch.setenv("KNOWLEDGE_E2E", "1")
    with pytest.raises(ValueError, match="KNOWLEDGE_E2E"):
        KnowledgeSettings.from_environment()

def test_runtime_uses_pinned_http_chroma():
    assert '"chromadb==1.5.9"' in PROJECT.read_text(encoding="utf-8")
    assert "PersistentClient" not in RAG_TOOLS.read_text(encoding="utf-8")
```

- [ ] **步骤 2：运行测试确认 RED**

运行：`pytest tests/test_knowledge_config.py tests/test_project_metadata.py -q -p no:cacheprovider`

预期：因设置类和精确依赖 pin 均不存在而失败。

- [ ] **步骤 3：实现最小设置和 client 工厂**

```python
@dataclass(frozen=True)
class KnowledgeSettings:
    chroma_host: str
    chroma_port: int
    artifact_root: Path | None
    source_root: Path | None
    e2e: bool

def get_chroma_client(settings: KnowledgeSettings | None = None):
    value = settings or KnowledgeSettings.from_environment()
    return chromadb.HttpClient(host=value.chroma_host, port=value.chroma_port)
```

将依赖改为 `chromadb==1.5.9`；删除硬编码 `KNOWLEDGE_PATH`；在 `.env.example` 记录知识库六项环境变量。生产环境拒绝 `KNOWLEDGE_E2E=1`。

- [ ] **步骤 4：运行 GREEN 并提交**

运行：`pytest tests/test_knowledge_config.py tests/test_project_metadata.py -q -p no:cacheprovider`

预期：通过。

```bash
git add pyproject.toml .env.example src/metro_agent/knowledge src/metro_agent/llama_config.py tests/test_knowledge_config.py tests/test_project_metadata.py
git commit -m "feat: configure pinned knowledge runtime"
```

### 任务 2：校验不可变知识源输入

**文件：**
- 新建：`src/metro_agent/knowledge/source_validation.py`
- 测试：`tests/test_knowledge_source_validation.py`

- [ ] **步骤 1：先写失败的资料和烟囱查询测试**

```python
def test_expired_document_rejects_entire_source(tmp_path):
    write_markdown(tmp_path / "rule.md", expires_at="2026-09-01")
    write_smoke_queries(tmp_path, "rule.md")
    with pytest.raises(SourceValidationError, match="rule.md.*expires_at"):
        validate_knowledge_source(tmp_path, today=date(2026, 9, 2))

def test_source_hash_includes_smoke_query_file(tmp_path):
    write_markdown(tmp_path / "rule.md")
    write_smoke_queries(tmp_path, "rule.md", query="a")
    first = validate_knowledge_source(tmp_path).source_tree_sha256
    write_smoke_queries(tmp_path, "rule.md", query="b")
    assert validate_knowledge_source(tmp_path).source_tree_sha256 != first
```

- [ ] **步骤 2：运行 RED**

运行：`pytest tests/test_knowledge_source_validation.py -q -p no:cacheprovider`

预期：因校验器不存在而失败。

- [ ] **步骤 3：实现安全校验与确定性哈希**

```python
REQUIRED_FIELDS = {"owner", "source", "updated", "effective_date", "expires_at", "risk_level"}
RISK_LEVELS = {"general", "controlled", "high"}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_DOCUMENTS = 10_000

def validate_knowledge_source(root: Path, *, today: date | None = None) -> ValidatedSource:
    root = require_real_directory(root)
    documents = sorted(iter_markdown_files(root), key=lambda value: value.as_posix())
    validate_smoke_queries(root / "release-smoke-queries.jsonl", root)
    for document in documents:
        validate_metadata(parse_front_matter(document), document, today or shanghai_today())
    return ValidatedSource(root, tuple(documents), hash_source_tree(root, documents))
```

使用 `yaml.safe_load`；拒绝链接、junction、解析失败、未知风险等级、错误日期、过期资料、非 `.md`、超过 10 MiB 文件和超过 10,000 份文档。要求正整数 `minimum_matches` 与精确 POSIX `expected_source`；哈希排序后的资料路径/内容及 smoke-query 文件。

- [ ] **步骤 4：运行 GREEN 并提交**

运行：`pytest tests/test_knowledge_source_validation.py -q -p no:cacheprovider`

预期：路径、大小、数量、日期、元数据和 smoke 路径失败案例均通过。

```bash
git add src/metro_agent/knowledge/source_validation.py tests/test_knowledge_source_validation.py
git commit -m "feat: validate versioned knowledge sources"
```

### 任务 3：实现发布 descriptor、registry 切换、Redis 锁和 indexer CLI

**文件：**
- 新建：`src/metro_agent/knowledge/releases.py`、`publication_lock.py`、`src/metro_agent/tools/knowledge_indexer.py`
- 修改：`src/metro_agent/tools/knowledge_index_registry.py`、`chunk_artifacts.py`、`build_obsidian_index.py`
- 测试：`tests/test_knowledge_releases.py`、`test_knowledge_publication_lock.py`、`test_knowledge_indexer.py`

- [ ] **步骤 1：先写失败的发布和锁测试**

```python
def test_publish_writes_current_and_previous_in_one_record(client, tmp_path):
    publish_validated_release(client, descriptor("first"), tmp_path)
    publish_validated_release(client, descriptor("second"), tmp_path)
    pointer = read_release_pointer(client)
    assert (pointer.current_build_id, pointer.previous_build_id) == ("second", "first")

def test_second_operator_cannot_acquire_lock(redis_client):
    with PublicationLock(redis_client, "knowledge:publication", ttl_seconds=900):
        with pytest.raises(PublicationLockedError):
            PublicationLock(redis_client, "knowledge:publication", ttl_seconds=900).acquire()
```

- [ ] **步骤 2：运行 RED**

运行：`pytest tests/test_knowledge_releases.py tests/test_knowledge_publication_lock.py tests/test_knowledge_indexer.py -q -p no:cacheprovider`

预期：因 release 和 CLI API 不存在而失败。

- [ ] **步骤 3：实现固定发布操作**

```python
@dataclass(frozen=True)
class ReleasePointer:
    collection_name: str
    artifact_sha256: str
    previous_collection_name: str | None
    previous_artifact_sha256: str | None

def publish_pointer(client, pointer: ReleasePointer) -> None:
    client.get_or_create_collection(INDEX_REGISTRY_COLLECTION_NAME).upsert(
        ids=[PUBLISHED_INDEX_ID], documents=["published index"], embeddings=[[0.0]],
        metadatas=[asdict(pointer)],
    )
```

在切换前创建不可变 `releases/<build-id>/validated.json`；单条 upsert 发布；使用 UUID `started/succeeded/failed` 操作事件。锁使用 `SET ... NX PX`、带 token 的 compare-and-PEXPIRE 与 compare-and-delete。CLI 仅提供 `build-and-publish`、`status`、`rollback`、`verify`；`--force-rebuild` 仅接受五个已确认原因。输入/版本相同返回 no-op；旧模块委托给新 CLI。

- [ ] **步骤 4：运行 GREEN 并提交**

运行：`pytest tests/test_knowledge_releases.py tests/test_knowledge_publication_lock.py tests/test_knowledge_indexer.py -q -p no:cacheprovider`

预期：预检不改指针、源变更失败、no-op、强制原因、回滚交换、续租失败和中断事件均通过。

```bash
git add src/metro_agent/knowledge src/metro_agent/tools/knowledge_index_registry.py src/metro_agent/tools/chunk_artifacts.py src/metro_agent/tools/build_obsidian_index.py src/metro_agent/tools/knowledge_indexer.py tests/test_knowledge_releases.py tests/test_knowledge_publication_lock.py tests/test_knowledge_indexer.py
git commit -m "feat: add governed knowledge publication"
```

### 任务 4：实现经 artifact 校验的 Chroma 只读代理

**文件：**
- 新建：`src/metro_agent/knowledge_read_proxy.py`
- 测试：`tests/test_knowledge_read_proxy.py`

- [ ] **步骤 1：先写失败的白名单和脱敏测试**

```python
def test_proxy_rejects_upsert_without_contacting_chroma(proxy_client, upstream):
    response = proxy_client.post("/api/v2/tenants/default_tenant/databases/default_database/collections/x/upsert", json={})
    assert response.status_code == 403
    assert upstream.requests == []

def test_proxy_allows_query_for_current_collection_only(proxy_client, upstream):
    response = proxy_client.post(PUBLISHED_QUERY_PATH, json={"query_embeddings": [[0.0]]})
    assert response.status_code == 200
    assert upstream.requests[0].path == PUBLISHED_QUERY_PATH
```

- [ ] **步骤 2：运行 RED**

运行：`pytest tests/test_knowledge_read_proxy.py -q -p no:cacheprovider`

预期：因代理不存在而失败。

- [ ] **步骤 3：实现路由与 artifact 门禁**

```python
async def proxy(request: Request) -> Response:
    route = classify_chroma_route(request.method, request.url.path)
    release = await release_verifier.current_release()
    if route is None or not route.is_read or not release.allows(route):
        return JSONResponse({"detail": "knowledge route forbidden"}, status_code=403)
    body = await read_body_at_most(request, 1024 * 1024)
    return await forward(request, body, connect_timeout=2.0, read_timeout=10.0)
```

仅允许 Chroma 1.5.9 identity、heartbeat、固定 tenant/database GET、registry GET/POST-get，以及当前 collection 的 GET/count/POST-get/POST-query。代理只读 artifact 卷，转发前校验指针 descriptor 摘要，只映射当前 collection UUID。上游超时返回 503；日志不得保存正文、向量、chunk、Cookie、头或 URL 参数。

- [ ] **步骤 4：运行 GREEN 并提交**

运行：`pytest tests/test_knowledge_read_proxy.py -q -p no:cacheprovider`

预期：每条允许路由、所有写入类别、历史版本拒绝、请求体、超时、坏 descriptor 和日志脱敏均通过。

```bash
git add src/metro_agent/knowledge_read_proxy.py tests/test_knowledge_read_proxy.py
git commit -m "feat: proxy published knowledge reads"
```

### 任务 5：接入应用就绪检查与 Compose 隔离

**文件：**
- 修改：`src/metro_agent/tools/Knowledge_RAGtools.py`、`readiness.py`、`api.py`、`compose.yml`、`.env.example`
- 新建：`deploy/operations/knowledge-admin.sh`、`knowledge-admin.ps1`
- 测试：`tests/test_knowledge_readiness.py`、`test_readiness.py`、`test_auth_api.py`、`test_compose_auth.py`

- [ ] **步骤 1：先写失败的集成测试**

```python
def test_readiness_is_503_when_no_valid_published_release(auth_api):
    checker = lambda: (_ for _ in ()).throw(KnowledgeUnavailableError())
    with TestClient(auth_api(readiness_checker=checker)) as client:
        assert client.get("/api/ready").status_code == 503

def test_compose_keeps_app_off_the_chroma_backend():
    services = _compose()["services"]
    assert "knowledge_backend" not in services["app"]["networks"]
    assert "ports" not in services["chroma"]
```

- [ ] **步骤 2：运行 RED**

运行：`pytest tests/test_knowledge_readiness.py tests/test_readiness.py tests/test_auth_api.py tests/test_compose_auth.py -q -p no:cacheprovider`

预期：因应用直接使用本地持久化目录、内部服务不存在而失败。

- [ ] **步骤 3：切换运行时和 Compose**

```yaml
chroma:
  image: chromadb/chroma:1.5.9
  volumes: ["knowledge_chroma_data:/chroma/chroma"]
  networks: [knowledge_backend]
knowledge-read-proxy:
  command: ["uvicorn", "metro_agent.knowledge_read_proxy:app", "--host", "0.0.0.0", "--port", "8000"]
  volumes: ["knowledge_artifact_data:/var/lib/metro-agent/knowledge-artifacts:ro"]
  networks: [knowledge_frontend, knowledge_backend]
```

应用经 `knowledge-read-proxy` 使用 `HttpClient`，在使用缓存查询引擎前重新解析 registry，并移除本地 Chroma/artifact 读取。为 `DependencyReadinessChecker` 新增知识依赖。indexer 放进 `knowledge-admin` profile，知识源只读、artifact 可写，连 Redis/backend；app 只连 frontend。包装器采集 `$USER` 或 `$env:USERNAME`，拒绝其他子命令。

- [ ] **步骤 4：运行 GREEN 并提交**

运行：`pytest tests/test_knowledge_readiness.py tests/test_readiness.py tests/test_auth_api.py tests/test_compose_auth.py -q -p no:cacheprovider`

预期：通过；liveness 仍为 200，知识不可用时 readiness 为 503。

```bash
git add src/metro_agent/tools/Knowledge_RAGtools.py src/metro_agent/readiness.py src/metro_agent/api.py compose.yml .env.example deploy/operations/knowledge-admin.sh deploy/operations/knowledge-admin.ps1 tests/test_knowledge_readiness.py tests/test_readiness.py tests/test_auth_api.py tests/test_compose_auth.py
git commit -m "feat: deploy isolated knowledge services"
```

### 任务 6：备份、恢复、演练与最终验证

**文件：**
- 新建：`deploy/operations/with-knowledge-publication-lock.py`、`fixtures/knowledge-e2e/rules.md`、`release-smoke-queries.jsonl`
- 修改：`backup-all.sh`、`restore-all.sh`、`chroma-owner-counts.py`、`README.md`、两份 operations 文档
- 测试：`tests/test_knowledge_e2e_contract.py`

- [ ] **步骤 1：先写失败的备份/profile 契约测试**

```python
def test_backup_archives_complete_knowledge_release_unit():
    script = BACKUP.read_text(encoding="utf-8")
    assert "knowledge-chroma.tar.gz" in script
    assert "knowledge-artifacts.tar.gz" in script
    assert "knowledge:publication" in script

def test_e2e_profile_uses_only_deterministic_embedding():
    service = _compose()["services"]["knowledge-e2e"]
    assert service["environment"]["KNOWLEDGE_E2E"] == "1"
    assert "DEEPSEEK_API_KEY" not in service["environment"]
```

- [ ] **步骤 2：运行 RED**

运行：`pytest tests/test_knowledge_e2e_contract.py -q -p no:cacheprovider`

预期：因备份仍只覆盖旧本地 Chroma 路径、e2e profile 不存在而失败。

- [ ] **步骤 3：实现一致恢复与确定性 profile**

```sh
docker compose stop app knowledge-read-proxy chroma
docker compose run --rm --no-deps app python deploy/operations/with-knowledge-publication-lock.py backup
docker compose run --rm --no-deps -v "$BACKUP_DIR:/backup" chroma sh -c 'tar -C /chroma -czf /backup/knowledge-chroma.tar.gz chroma'
docker compose run --rm --no-deps -v "$BACKUP_DIR:/backup" knowledge-indexer verify-restored-release --write /backup/knowledge-release.json
```

`with-knowledge-publication-lock.py` 在备份子命令期间调用同一 token 校验 `PublicationLock`。归档整个知识 Chroma 与 artifact 根，连同 release 验证写入 `SHA256SUMS`；恢复两者后、启动 app 前验证 pointer/descriptor。新增 `knowledge-e2e` profile，仅以显式门禁的确定性 embedder 和合规 fixture 运行。文档说明加密由备份目标负责，首次部署和 Chroma 升级必须隔离恢复演练。

- [ ] **步骤 4：运行 GREEN 与完整验证**

运行：`pytest -m "not live" -q -p no:cacheprovider`

预期：零失败。

运行：`ruff check src tests`

预期：`All checks passed!`。

运行：`docker compose --profile knowledge-e2e up -d --build --wait`

预期：Chroma、代理、Redis、app 均健康，Chroma 不开放宿主机端口。

运行：`docker compose --profile knowledge-e2e run --rm knowledge-indexer build-and-publish`

预期：退出码 0 并输出一个 published build ID；使用允许的 force reason 建立第二版后，`rollback` 与 `verify` 能恢复首版来源和 build ID。

- [ ] **步骤 5：提交验收证据**

```bash
git add deploy/operations/with-knowledge-publication-lock.py deploy/operations/backup-all.sh deploy/operations/restore-all.sh deploy/operations/chroma-owner-counts.py docs/operations/coordinated-backup-restore.md docs/operations/internal-pilot-auth-acceptance.md fixtures/knowledge-e2e README.md tests/test_knowledge_e2e_contract.py
git commit -m "test: rehearse knowledge release recovery"
```
