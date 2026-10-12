# 公開前の検証記録

実施日: 2026-10-12。ローカル環境: Windows、Python 3.13.7、Docker Engine 29.0.1。
外部モデルのAPI credentialは使用していません。

最終ローカル実行は48件成功（9.01秒）、行coverageは75%でした。Ruffの検査と
フォーマット検査、wheel/sdistの生成、配布物内のライセンス9件の同梱も確認しました。
GitHub CIでPython 3.12/3.13と実コンテナの確認を継続します。

公開時のLinux CIで、一時path変換が生成した`/tmp`を再度変換するテスト補助コードの
不具合を検出し、一回の置換で処理するよう修正しました。修正後はWindowsのGit統合6件が
6.29秒で成功し、LinuxのPython 3.13コンテナでも48件が2.89秒で成功しました。

## 検証範囲

- DAGの循環・未定義依存・重複ID・パスとして不正なIDを拒否する。
- 空いた実行枠を補充し、依存の失敗を孫taskまで伝播する。
- 共通の祖先を持つtaskへ、先行差分を依存順で一度ずつ渡す。
- workerの成功報告だけでコードrunを完了扱いにしない。
- OpenCodeの中間出力・retry状態・モデルエラーを成功と扱わない。
- 投入応答が失われた場合、保存済みmessage IDを照合し、再送しない。
- 照合できない場合は`submission_unknown`を返し、sandboxを保持する。
- 実際のGitで新規・バイナリ差分を回収して別checkoutへ適用する。
- 統合後の固定受入条件で不合格を検出し、合格する変更を切り分ける。
- 競合候補を別々に検証して一案を選び、未選択候補を記録する。
- Langfuse障害がjobを失敗させず、job自体の例外が観測処理に隠されない。
- DeerFlowのSSE errorと、上限を超えるreview差分を拒否する。
- 秘密情報・環境file・依存directoryをworkspace archiveに含めない。
- Compose設定、CLI名、wheel/sdist生成を確認する。

## 実コンテナによる契約確認

`uv run python tools/check_runtime.py`で一時コンテナを起動し、終了時に削除します。

PostgreSQL 17ではschema作成、run/task/attemptの遷移、sandbox/session/message IDの
対応保存、成果物の保存・取得を確認しました。

OpenCode 1.19.0では`/global/health`、session作成、`prompt_async`、message IDの
照合とpollingを確認しました。Chat Completionsのローカルstubへ接続し、assistantの
完了出力を取得しました。不正なproviderについては成功扱いにせず失敗を取得しました。
この版には`@ai-sdk/openai-compatible`が組み込まれているため、そのproviderを固定して使います。

Composeの2ファイルは`--env-file .env.example config --quiet`が成功しました。
Langfuse全サービスとOpenSandbox serviceの起動はこの確認範囲に含みません。

## 不具合修正と最適化

| 確認した問題 | 修正 |
|---|---|
| 中間出力やretryを完了と誤認 | idleとassistant完了時刻・parent ID・errorを確認 |
| 間接依存の差分が届かない | 祖先taskを依存順に列挙して重複を除去 |
| 依存失敗の連鎖でschedulerが例外 | blocked状態を連鎖が止まるまで伝播 |
| 成功報告に成果物がなくても完了扱い | `artifact_missing`として不合格 |
| 競合候補を同時適用 | 同条件で別々に検証し合格一案を選択 |
| cleanup例外が取得済み成果物を破棄 | cleanupの失敗を記録し成果物を維持。sandboxはTTLで失効 |
| review差分を黙って切り詰める | 上限超過を不合格にし全差分確認を要求 |
| 観測サービス障害でjobを失敗扱い | 観測処理の例外を状態管理から分離 |
| 統合失敗時に同じ全群を再検証 | 既知の失敗結果を再利用して二分を開始 |

模擬taskの比較は`uv run python tools/benchmark_scheduler.py`で再現できます。
同時実行2、所要時間0.20/0.02/0.02/0.20秒の4taskを5回ずつ実行し、
旧バッチ待ち方式は平均0.4095秒、修正後は0.2661秒で、所要時間は35.0%短縮しました。
DB待ち、モデル呼び出し、sandbox作成の時間は含みません。

Gitの初回確認で38.37秒かかった段階では、Windowsのdrive表記をtarがリモートhostと
解釈するテスト環境の問題を確認しました。同等条件の成功基準は当初なく、所要時間の
見込みは不確実でした。Git Bash用のpath変換を修正後、同じ6件は6.45秒で成功しました。
製品のLinux sandbox実装の遅さを示すものではありません。

## 残る確認

実モデルのtool calling・structured output、DeerFlow/OpenSandbox/LiteLLM/Langfuseを
すべてつないだE2E、実モデルでの4→8→16…の負荷、Gateway停止からの全run再開、
100並列、予算終了時の全外部処理停止は未確認です。foreground CLI、再計画・件数・
時間の上限、状態保存を実装していますが、常駐再開サービスや金額/token予算は未実装です。
検証済みの範囲と、構成案で要求された今後の範囲を区別してください。
