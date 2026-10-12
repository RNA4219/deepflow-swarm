# DeepFlow Swarm Blueprint

## 目的

DeerFlowを指揮・計画・調査・再計画・独立レビューの実行基盤とし、コード変更をOpenSandbox上のOpenCodeへ渡すCLI-firstオーケストレーションを提供する。

## 採用境界

- DeerFlow Gateway: planner、research worker、bounded replan、独立reviewer。
- OpenCode: コード変更のagent loop。操作はsandbox内HTTP APIを通す。
- OpenSandbox: コードworkerごとの実行環境。通常は成果物取得後に破棄し、投入結果が不明な場合は照合用に保持する。
- LiteLLM: `planner` / `worker` / `reviewer` / `local_private` のmodel aliasを提供する。
- PostgreSQL: run、task、attempt、外部session ID、成果物の状態正本。
- Langfuse: trace、acceptance score、review score、人手評価の観測先。
- Refinery: 完了patchの統合キュー。統合後検証に失敗した場合は候補群を分割して再検証する。

## プロファイル

| profile | 実行方式 |
| --- | --- |
| `research` | DeerFlowの独立threadで観点別調査し、根拠付き結果を集約 |
| `implementation` | task DAGを実行し、OpenCode実装・統合検証・独立reviewを行う |
| `competition` | 独立sandboxで候補実装を生成し、同じ検証条件で選別 |
| `dependency` | 依存DAGのready taskのみを並列実行 |

空いた実行枠は他のready taskで補充します。後続taskには、間接依存も含む先行成果を
依存順で渡し、受入前の報告であることを明示します。competitionは2〜4の独立候補を
別々に受入検証し、複数案が合格した場合は計画順で一案を選びます。
統合・受入が未実施の候補や、review上限を超える差分はcompletedにしません。

## Run lifecycle

`planning -> running -> integrating -> reviewing -> completed`

失敗時はattemptのIDと失敗分類を保存する。再計画回数は設定値で制限し、通信断後は保存済みOpenCode session/message IDで照合する。結果を照合できない場合は自動再投入せず、sandboxを保持して`submission_unknown`として人に返す。

## 初期実装の境界

実行入口はforeground CLIであり、PostgreSQLへ状態を保存する。DeerFlow GatewayとOpenSandbox serviceは利用者が別途起動・運用する。OpenSandboxの所有権をGateway間で共有する分散運用、長時間runの常駐worker daemon、モデルごとの契約試験、PR作成は初期実装に含めない。
