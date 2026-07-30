# ECLIPTICA ログ解析メモ

VRChatワールド「ECLIPTICA」がDebug.Logとして出力する、ゲームプレイ関連のログパターンをまとめたもの。
新しい実ログを調べるたびにここへ追記していく。

## 実装済み（アプリで使用中）

| パターン | 意味 |
|---|---|
| `ECLIPTICA - now in stage: Stage_XXX on phase: N.NN as class: YYY` | ステージ移動・ラン進行度・クラス |
| `ECLIPTICA - now fighting boss: BossName(Clone) on phase: N.NN` | ボス戦開始 |
| `Tracking boss as defeated in-run.` | ボス撃破 |
| `ECLIPTICA - now in lobby` | ラン終了（クリア or 全滅） |
| `ECLIPTICA - now in intermission` | ボス撃破後の小休止。**この時点でHISTORYを確定させる**（次のステージ読み込みまで待たない） |
| `ECLIPTICA (MASTER Setting\|saving\|loaded) SESSION ID (to)? (\d+)` | セッション（ラン）識別ID。変わったら新しいランとみなしHISTORYを自動リセット。同じセッション中にアプリを再起動した場合は`state.json`から履歴を復元する |
| `Joining wrld_XXXX:...` | VRChatインスタンスID |
| `Initialized PlayerAPI "XXX" is local` | 自分の表示名 |
| `Dealing (\d+) (STRIKE\|NON-STRIKE) damage` | 与ダメージ |
| `damage has been taken: (\d+), from source: (\S+)` | 被ダメージ（発生源名付き） |
| `Boss XXX dead, personal damage dealt:` + `STRIKE DMG: N` + `NON-STRIKE DMG: N` | ゲーム自身が集計するボス単体への合計ダメージ。HISTORY・PARTY両方の合計ダメージ横に`(N)`で表示、答え合わせ用。**同じ組み合わせが0/0で十数回連続で繰り返されることがある**（後片付け処理のエコーと思われる）。足し算方式で集計しているので0の重複は結果に影響しない |
| `ownership of XXX transferred to YYY` | VRChatのネットワークオブジェクト所有権移動ログ。雑魚名・ボス名（`(Clone)`無しの生の名前、`now fighting boss:`のボス名と一致）の両方で出現し、実質的なターゲッティング先を表しているらしい。**現在のステージのボス名と一致した場合のみ**、`YYY`が自分の表示名になった時点を「自分がボスに狙われている」として記録し、他人に移った時点で解除して警告バナーを表示（雑魚のownership移動は無視）。ステージ切り替わり・休憩所(intermission)入り・ロビー帰還時にもリセットする。ダメージ行そのものへのターゲット紐付けではないため、雑魚/ボスのダメージ仕分けには使えない |

## 確認したが使えなかったもの

- **`spawn token, True/False, N`** — 1ステージにつき3行固定で出るが、これは**そのステージで何個トークンを生成するかの設定値**。実際に何個拾ったかのログは存在しない（拾得イベント自体がDebug.Logに出力されていないことを実際にプレイして確認済み）。

## 新しく見つかった、まだ未検証の要素

- **`[Behaviour] Initialized PlayerAPI "XXX" is remote`** — 自分以外のプレイヤーの表示名。`is local`の他人版。**他プレイヤーの名前をログから直接取得できる**ということなので、パーティ共有機能で「サーバーに頼らずログだけで同席者を把握する」使い方ができるかもしれない（要検討）。
- **`Initializing Enemy POOL ID(\d+) as ENEMY ID (\d+)`** — 敵の出現管理ログ。ENEMY IDは数値のみで敵名は分からないため、単体では活用しにくい（ID→敵名の対応表が別途分かれば、同時出現数のカウントなどに使えるかも）。
- **`ECLIPTICA session ID does not match the active session.`** — セッション不整合の警告（1回だけ確認）。ラン状態がおかしくなった時の診断表示に使えるかもしれない。
- **`ECLIPTICA loaded blank session ID.`** — セッション未割り当て状態（起動直後などに出る）。
- **`BundlesManager: Send message for bundle 'Community Spirit Reward' ignored! Reason: Already Sent/Seen`** — ボス撃破直後に出る、報酬/実績通知っぽいメッセージ。「無視された」理由付きで出るので、本来は何らかの通知UIに繋がっていると思われる。報酬名（`Community Spirit Reward`）が取れているので、達成した報酬の種類を表示する機能に使えるかもしれない。
- **クラスは現時点で8種類確認済み**: Spellsword, Gunmancer, Twinmage, Fistmage, Spellhammer, Shieldmage, Thaumaturge, Nekomancer（新セッション開始直後の`Hall of Beginnings`入場時に固定され、同一セッション中は変わらない）。

## 確認したが存在しなかったもの

コンボ、倍率、バフ/デバフ、クールダウン、スキル名、通貨/スコア、キル数、回復、リバイブ、拾得(ピックアップ)、ショップ/アップグレード、ミス/ブロック/回避/パリィ、HP%、プレイヤー自身の死亡回数カウンタ — これらのキーワードでは意味のあるログは見つからなかった。
