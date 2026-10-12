# 第三者ソフトウェアとライセンス

deepflow-swarmの独自コードは[MIT](LICENSE)で公開します。外部ソフトウェアの
ライセンスはそれぞれの著作権者に帰属し、本プロジェクトのMIT表記で置き換えません。

## 採用コンポーネント

| プロジェクト | ライセンス | 利用範囲 |
|---|---|---|
| [DeerFlow](https://github.com/bytedance/deer-flow) | MIT | Gateway APIへ接続。バックエンド本体を同梱しない |
| [OpenCode](https://github.com/anomalyco/opencode) | MIT | npm配布物をworker image構築時に取得しHTTP APIへ接続 |
| [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) | Apache-2.0 | Python SDKを依存として導入。サービスは外部で起動 |
| [Gas Town](https://github.com/gastownhall/gastown) | MIT | Refineryの統合・失敗群分割という設計を参考に独自実装。Goコードの転記なし |
| [LiteLLM](https://github.com/BerriAI/litellm) | MIT部分、enterprise/は別ライセンス | モデルGateway。enterprise/のコードを転記・再配布しない |
| [Langfuse](https://github.com/langfuse/langfuse) | MIT部分、ee/等は別ライセンス | 公式Compose設定を参考にしたセルフホスト設定。EEコードを転記しない |
| [Langfuse Python SDK](https://github.com/langfuse/langfuse-python) | MIT | トレース・score送信 |

上流の著作権表記とライセンス全文を[third_party/licenses](third_party/licenses)に
保存しています。確認したコミットと取得URLは[upstream-sources.json](third_party/upstream-sources.json)
で固定しています。OpenSandboxのApache-2.0はこのプロジェクト独自コードのMITとは別です。

LiteLLMの`enterprise/`、Langfuseの`ee/`、`web/src/ee/`、`worker/src/ee/`には
それぞれ上流が定める別の条件が適用されます。本設定はenterpriseライセンスキーや
EE機能を有効化しません。公式imageへの参照は、そのimageに含まれるすべての
ソフトウェアをMITとみなす意味ではありません。

## 配布境界

このGitリポジトリとPython wheelは独自の接続コード、設定、文書、テストを含みます。
上流バックエンド、コンテナimage、モデル重み、Node.jsやPythonの実行環境は同梱しません。
wheelに第三者ライセンス表記を含めます。依存物は`uv.lock`から別途取得し、各配布物の
LICENSE/NOTICEを維持します。直接のPython依存にはHTTPX（BSD-3-Clause）、asyncpgと
OpenSandbox（Apache-2.0）、SQLAlchemy/Pydantic/Pydantic Settings/Typer/Langfuse SDK
（MIT）が含まれます。

コンテナをビルドして再配布する場合は、その中の依存物についても著作権・ライセンス
表記を保持してください。公式imageの利用条件は上流に従います。

## その他の実行基盤

ComposeはPostgreSQL、ClickHouse、Redis、MinIOの公式配布imageを参照します。
これらは独自コードのMITの対象外です。特に[MinIOはAGPL-3.0](https://github.com/minio/minio/blob/master/LICENSE)で、
[Redisはバージョンによってライセンスが異なります](https://redis.io/legal/licenses/)。imageの複製・改変・配布を行う場合は
配布元の対応するソースとライセンス条件を確認してください。本リポジトリは
これらのバイナリを再配布しません。
