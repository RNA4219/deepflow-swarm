# Guardrails

- 外部workerの`success`報告だけでrunを完了にしない。コード成果物は別sandboxで固定したacceptance commandを実行する。
- acceptance commandが一件もないコードrunは`completed`にしない。
- workerごとに独立sandboxと独立checkoutを割り当てる。同じcheckoutを同時編集させない。
- model出力をshellへ直接展開しない。実行するacceptance commandは利用者がCLIの`--check`で指定したものに限り、隔離sandbox内で実行する。
- `local_private` model aliasにクラウドfallbackを定義しない。private処理はLiteLLM側の経路とegress設定で制限する。
- OpenCodeの投入・実行応答が曖昧な場合、同じmessageをblind retryしない。message IDを照合し、不明ならsandboxを保持して`submission_unknown`として停止する。
- Langfuseは分析先であり、task stateやsandbox ownershipの決定に使わない。
- sandboxにはホストの`.env`、`.git`、依存キャッシュを送らない。公開鍵・credentialは設定ファイルでなく環境から渡す。
- run終端後のsandbox破棄に失敗した場合はログへIDを残す。取得済みの成果物を破棄したり、同じ作業を自動で再投入したりせず、TTLによる失効を待つ。
