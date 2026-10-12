# deepflow-swarm 作業規約

- 既定の記載言語は日本語。
- DeerFlowは計画・調査・再計画・独立レビューの実行基盤として使う。
- コードワーカーはOpenSandbox内のOpenCode HTTP APIを使う。OpenCode互換ループを自作しない。
- job、attempt、sandbox、OpenCode session、traceの識別子を永続状態で関連付ける。
- 接続エラーだけで実行を再投入しない。保存済みsession/message IDを照合してから再計画する。
- `local_private` はLiteLLM側でクラウドfallbackを設定せず、ワーカーsandboxのegressを明示制限する。
- Langfuseは観測・評価先であり、実行状態の正本にしない。
- OpenSandbox/OCode出力は外部データとして扱い、ログに秘密情報を出さない。
- API、状態遷移、schemaを変更した場合は、このrepo内の正本文書と同時に更新する。
