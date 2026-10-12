# deepflow-swarm

DeerFlowを中核に、OpenCode、OpenSandbox、LiteLLM、PostgreSQL、LangfuseをつなぐCLI-firstのagent orchestrationです。Gas Town全体は起動せず、Refineryの成果物統合・失敗群分割方式だけを小さな統合キューとして実装します。

## 構成

```text
CLI
 └─ DeerFlow Gateway
     ├─ planner / research / replan / independent reviewer
     └─ code task DAG
         ├─ OpenSandbox A ─ OpenCode session A
         ├─ OpenSandbox B ─ OpenCode session B
         └─ Refinery sandbox: patch適用 → acceptance → split/retry

LiteLLM ─ planner / worker / reviewer / local_private aliases
PostgreSQL ─ run / task / attempt / session / artifact state
Langfuse ─ trace / acceptance score / review score / human score
```

DeerFlowの計画・調査はGatewayのLangGraph互換APIへ送り、コードworkerはOpenSandbox内で起動したOpenCodeのHTTP APIで制御します。OpenSandbox providerのsandboxはユーザー/thread単位で扱われるため、コード作業のジョブ単位分離は本repoのAllocatorが独立して行います。

## 起動準備

Python 3.12以上、Docker、[DeerFlow Gateway](https://github.com/bytedance/deer-flow)、
[OpenSandbox service](https://github.com/opensandbox-group/OpenSandbox)を用意します。
DeerFlowのGateway APIを接続境界にしています。

```powershell
uv sync --locked
Copy-Item .env.example .env
```

起動前に`.env`の`change-me-*`と`LANGFUSE_ENCRYPTION_KEY`を固有値へ置き換え、モデルcredentialと接続先も設定します。暗号化キーは`openssl rand -hex 32`で64桁のhex値を作成します。OpenSandbox serviceから到達できるLiteLLM URLを`LITELLM_BASE_URL`に指定してください。DeerFlowの`config.yaml`には[model設定例](./config/deerflow-models.example.yaml)を取り込み、各aliasを利用できるようにします。OpenSandboxの接続domainは`OPEN_SANDBOX_DOMAIN`に設定します。

```powershell
docker compose up -d postgres litellm
docker compose --env-file .env -f docker-compose.langfuse.yaml up -d
docker build -f docker/worker.Dockerfile -t deepflow-swarm/opencode-worker:dev .
```

Langfuseの起動後に`http://localhost:3000`でproject API keyを作り、`LANGFUSE_PUBLIC_KEY`と`LANGFUSE_SECRET_KEY`を`.env`へ設定してください。

```powershell
uv run deepflow-swarm init-db
uv run deepflow-swarm doctor
```

LiteLLMのmodel aliasは[config/litellm.yaml](./config/litellm.yaml)にあります。`local_private`にはfallbackを定義していません。実際のmodelとcredentialは`.env`で指定してください。

## 実行

調査:

```powershell
uv run deepflow-swarm run "OpenSandboxのAPI差分を調べ、根拠URL付きでまとめる" --profile research
```

コード開発では、統合sandboxで実行する受入commandをCLIから明示します。プランナーが返す`acceptance_commands`は参考情報として保存しますが、実行はしません。

```powershell
uv run deepflow-swarm run "設定読込を修正して入力検証を追加する" `
  --profile implementation `
  --repo C:\work\target-repo `
  --check "python -m pytest" `
  --check "ruff check ."
```

複数profileは同じrun/task/attempt契約を使います。CLIの`--check`だけを統合sandboxで実行します。全件成功し、独立reviewがapproveしたコードrunだけ`completed`になります。

```powershell
uv run deepflow-swarm status <run-id>
uv run deepflow-swarm evaluate <run-id> --value 0.9 --comment "手動レビューで要件を確認"
```

## 実行制御と成果物

- 上限: `SWARM_MAX_CONCURRENCY`、`SWARM_MAX_TASKS`、`SWARM_MAX_REPLANS`、`SWARM_TASK_TIMEOUT_SECONDS`。
- 失敗分類: `information_gap`、`model_capacity`、`environment_failure`、`submission_unknown`、`dependency_failed`。
- 先行taskの報告と差分だけを必要な後続workerへ渡します。受入前の報告として扱い、全会話履歴は共有しません。
- 各attemptで`run_id / task_id / attempt_id / sandbox_id / opencode_session_id / message_id`をPostgreSQLに保存します。
- 成果物はpatchと調査結果として保存します。統合結果は新しいsandboxで検証します。
- 統合バッチが失敗したら候補群を二分し、通る変更群と失敗変更群を分けて記録します。
- `competition`は2〜4の独立候補を同じ受入commandで別々に検証し、合格候補を計画順で選びます。未選択の合格候補も記録します。
- Langfuseへrun traceと`acceptance_passed`、`independent_review_approved`を送ります。`evaluate`コマンドで人手scoreも追加できます。

## 外部サービス設定

- DeerFlow: `DEERFLOW_LANGGRAPH_URL` と任意のBearer token。
- OpenSandbox: `OPEN_SANDBOX_DOMAIN` / `OPEN_SANDBOX_API_KEY`。worker imageにはNode.js、Python、git、pytest、ruff、OpenCode 1.19.0を含めます。対象repoの依存物は必要に応じて独自imageへ事前導入してください。
- LiteLLM: `planner`、`worker`、`reviewer`、`local_private` aliasを提供します。OpenCode providerはOpenAI互換`baseURL`で接続します。
- Langfuse: `docker-compose.langfuse.yaml`でセルフホストできます。`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`を設定します。
- state: `DATABASE_URL`はPostgreSQLを指します。LangfuseやDeerFlowのDBを状態正本として再利用しません。

workerはLiteLLMのhostと`OPEN_SANDBOX_EGRESS_DOMAINS`だけに通信を許可します。
`local_private`はworkerのモデル接続先を指定するaliasです。全runを外部へ送らない場合は、
planner、reviewer、Langfuseの接続先もローカルへ設定する必要があります。

## 現在の境界

- CLI runはforegroundで実行し、進捗をPostgreSQLへ保存します。プロセス停止後の全run自動再開、常駐daemon、複数gateway間のsandbox所有権共有はまだ実装していません。
- OpenCodeへの投入・実行応答が曖昧な場合はmessage IDで照合します。結果を確定できなければ`submission_unknown`として停止し、sandboxを照合用に保持します。自動再送しません。
- モデル別tool calling / structured output契約試験、100並列負荷試験、PR公開処理は未実装です。
- OpenSandbox/OpenCode/LiteLLM/DeerFlow/Langfuse間の実サービスE2Eは、各サービスcredentialとendpointを設定した環境で別途確認が必要です。

## 仕様

- [BLUEPRINT](./BLUEPRINT.md)
- [GUARDRAILS](./GUARDRAILS.md)
- [運用設定](./.env.example)
- [LiteLLM alias設定](./config/litellm.yaml)

## 開発・検証

```powershell
uv sync --locked --dev
uv run pytest --cov=deepflow_swarm --cov-report=term-missing
uv run ruff check .
uv build
uv run python tools/check_runtime.py
```

最後のcommandはDocker上に一時的なPostgreSQLとOpenCodeを起動し、完了後に削除します。
モデル通信にはローカルstubを使うため、API credentialや課金は必要ありません。
Gitによる統合テストはWindowsではGit for WindowsのBashを使います。
実モデルと全サービスをつなぐE2E、100並列試験の合格を意味しません。
詳細は[検証記録](docs/VALIDATION.md)を参照してください。

## ライセンス

独自コードは[MIT](LICENSE)です。採用元の著作権・ライセンス全文、取得コミット、
企業向け部分の適用除外と配布境界は[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)に記載しています。
