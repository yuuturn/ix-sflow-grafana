# Zabbix による decapd 監視（ホスト外からの監視）

Zabbix は **zabbix**、監視対象は **collector** の `decapd` / `sfacctd` /
PostgreSQL / Grafana。Zabbix サーバが別ホストから collector のエージェントに
問い合わせる構成（パッシブチェック）。

---

## 1. 前提条件（実測で確認済み）

| 項目 | 状態 |
|------|------|
| collector の Zabbix エージェント | `zbx-agent2-collector-zabbix-agent2-1` が稼働、`ZBX_HOSTNAME=collector` |
| エージェントの待受 | `0.0.0.0:10050` |
| Zabbix サーバ → collector:10050 | 到達可能 |
| ホスト root のマウント | エージェントに `/:/hostfs` があるため**ホストのファイルを読める** |
| コンテナ内コマンド | `cat` のみ（`awk` / `grep` / `sed` なし） |

### ⚠ 重要な制約: パッシブチェックの送信元制限

エージェントは `Server=` に登録されたアドレスからの接続しか受け付けません。
collector の設定は `Server=zabbix.example.net`（= Tailscale の
`192.0.2.21` に解決）です。

```
zabbix → 192.0.2.10:10050   … ❌ Connection reset by peer（拒否）
zabbix → 192.0.2.20:10050  … ✅ 取得できる（Tailscale 経由）
```

**Zabbix サーバは collector を Tailscale の IP / ホスト名で監視してください。**
LAN 側 IP を使う場合は、collector のエージェントに
`ZBX_PASSIVESERVERS=192.0.2.11` を追加して再起動が必要です。

> アクティブチェック（エージェント → サーバ:10051）は既に動作しているため、
> アイテム種別を「Zabbix エージェント（アクティブ）」にしても同じキーが使えます。
> その場合は上記の送信元制限は関係ありません。

---

## 2. 仕組み

`vfs.file.contents` でホスト上のファイルを読むだけなので、
**エージェント側の設定変更は一切不要**です。

```
collector:
  decapd ──書き込み──▶ /var/lib/traffic-viz/decapd-stats.json
                              │
  traffic-viz-health.timer（1分ごと）
    └─ scripts/health.sh ──▶ /var/lib/traffic-viz/metrics/<metric>  (1ファイル1値)
                              │
zbx-agent2 (collector) ── /hostfs 経由で読む ──▶ Zabbix サーバ (zabbix)
```

- メトリクスは **1ファイル1値**。追加してもエージェント再起動は不要
- すべてのファイルを毎回書き直すので、**ファイルの mtime が監視自体の生存確認**になる

```bash
# 手動で流す
sudo /opt/traffic-viz/scripts/health.sh
ls /var/lib/traffic-viz/metrics/
sudo systemctl list-timers traffic-viz-health.timer
```

---

## 3. アイテム定義

ホスト「collector」に追加するアイテム。**キーはすべて以下の形式**です。

```
vfs.file.contents[/hostfs/var/lib/traffic-viz/metrics/<メトリクス名>]
```

| メトリクス名 | 型 | 更新間隔 | 内容 / 正常値 |
|-------------|-----|---------|--------------|
| `trafficviz_decapd_active` | 数値(整数) | 60s | decapd が active なら 1 |
| `trafficviz_sfacctd_active` | 数値(整数) | 60s | sfacctd が active なら 1 |
| `trafficviz_decapd_flush_age` | 数値(整数) | 60s | **収集ループのハートビート**からの秒数（正常 < 90） |
| `trafficviz_decapd_write_age` | 数値(整数) | 60s | 最後の DB 書き込みからの秒数（**参考値**。MAP-E 通信が無いと増えるのでアラートに使わない） |
| `trafficviz_decapd_uptime` | 数値(整数) | 60s | プロセス稼働秒数 |
| `trafficviz_decapd_datagrams` | 数値(整数) | 60s | 受信した sFlow データグラム累計 |
| `trafficviz_decapd_flow_samples` | 数値(整数) | 60s | フローサンプル累計 |
| `trafficviz_decapd_mape_samples` | 数値(整数) | 60s | MAP-E デキャップ成功累計 |
| `trafficviz_decapd_decode_errors` | 数値(整数) | 60s | デコード失敗累計（0 が正常） |
| `trafficviz_decapd_relay_errors` | 数値(整数) | 60s | pmacct への中継失敗累計（0 が正常） |
| `trafficviz_decapd_flush_errors` | 数値(整数) | 60s | DB 書き込み失敗累計（0 が正常） |
| `trafficviz_decapd_pending_keys` | 数値(整数) | 60s | 未書き込みの集約キー数 |
| `trafficviz_decapd_relay_enabled` | 数値(整数) | 60s | 中継モードなら 1 |
| `trafficviz_sflow_ingest_age` | 数値(整数) | 60s | pmacct の最終更新からの秒数（正常 < 120） |
| `trafficviz_rollup_age` | 数値(整数) | 60s | 最新 5分バケットの開始からの秒数（正常 < 330） |
| `trafficviz_db_ok` | 数値(整数) | 60s | DB に問い合わせできたら 1 |
| `trafficviz_flows_1h` | 数値(整数) | 60s | 直近1時間の生フロー行数 |
| `trafficviz_decap_rows` | 数値(整数) | 60s | `flows_decap` の総行数 |
| `trafficviz_ip_asn_rows` | 数値(整数) | 60s | ASN 辞書のエントリ数 |
| `trafficviz_tv-postgres_running` | 数値(整数) | 60s | コンテナ稼働 1/0 |
| `trafficviz_tv-grafana_running` | 数値(整数) | 60s | コンテナ稼働 1/0 |
| `trafficviz_check_ok` | 数値(整数) | 60s | 常に 1（監視自体の生存確認用） |

### 監視自体の停止を検知するアイテム

値が古いままでもファイルは存在し続けるため、**mtime** を別途見ます。

```
キー: vfs.file.time[/hostfs/var/lib/traffic-viz/metrics/trafficviz_check_ok,modify]
型:   数値(整数)   更新間隔: 60s
```

> `vfs.file.time` の第2引数は `modify` / `access` / `change` です
> （`mtime` は「Invalid second parameter」になります）。

---

## 4. トリガー定義

| 名前 | 条件式 | 深刻度 | 意図 |
|------|--------|--------|------|
| decapd 停止 | `last(/collector/trafficviz_decapd_active)<>1` |  Disaster | sFlow 入口の停止＝全可視化停止 |
| sfacctd 停止 | `last(/collector/trafficviz_sfacctd_active)<>1` | High | 集約側の停止 |
| sFlow 受信停止 | `last(/collector/trafficviz_sflow_ingest_age)>300` | High | 装置・経路・decapd のいずれかが異常 |
| decapd 停止（ハートビート） | `last(/collector/trafficviz_decapd_flush_age)>180` | High | 収集ループが止まっている（3サイクル連続で欠落） |
| DB 接続不可 | `last(/collector/trafficviz_db_ok)<>1` | High | PostgreSQL 障害 |
| ロールアップ停止 | `last(/collector/trafficviz_rollup_age)>900` | Warning | タイマー停止（ダッシュボードが更新されない） |
| デコードエラー | `last(/collector/trafficviz_decapd_decode_errors)>0` | Warning | サンプル形式の変化など |
| 中継エラー | `last(/collector/trafficviz_decapd_relay_errors)>0` | Warning | pmacct へ渡せていない |
| DB 書き込みエラー | `last(/collector/trafficviz_decapd_flush_errors)>0` | High | 権限・容量・接続の問題 |
| 監視自体の停止 | `last(/collector/trafficviz_check_ok_mtime)>300` | High | health.sh / タイマーの停止 |
| コンテナ停止 | `last(/collector/trafficviz_tv-postgres_running)<>1` | High | DB コンテナ停止 |
| コンテナ停止 | `last(/collector/trafficviz_tv-grafana_running)<>1` | Warning | Grafana 停止 |

> `trafficviz_check_ok_mtime` は `vfs.file.time[...]` アイテムの名前です。

### 誤検知を避けるためのヒント

- `trafficviz_rollup_age` は 5分バケットの**開始時刻**基準なので、
  正常時でも 0〜300 秒の幅で振れます。実測の最大は 474 秒だったため、
  閾値は **900 秒**（15分）に設定しています
- `trafficviz_decapd_mape_samples` は **0 の時間帯があって正常**です
  （MAP-E の通信が無ければ増えない）。アラートには使わず、
  グラフで傾向を見る用途に向きます
- 再起動直後は `flush_age` が一時的に大きくなります
  （初回 flush まで最大60秒）。`5m` の継続条件を付けると安定します

---

## 5. 設定手順

### 5-A. 自動登録（推奨・実施済み）

`scripts/zabbix-setup.py` がアイテム24個とトリガー12個を作成します。
**冪等**なので、定義を変えて再実行しても重複しません（キー／説明で突き合わせて更新）。

```bash
# Zabbix サーバ上で実行
ZABBIX_TOKEN=<APIトークン> python3 scripts/zabbix-setup.py
ZABBIX_TOKEN=<APIトークン> python3 scripts/zabbix-setup.py --dry-run   # 事前確認
```

環境変数:

| 変数 | 既定値 | 用途 |
|------|--------|------|
| `ZABBIX_TOKEN` | （必須） | API トークン |
| `ZABBIX_API` | `http://127.0.0.1:8081/api_jsonrpc.php` | API エンドポイント |
| `ZABBIX_HOST` | `collector` | 対象ホストの技術名 |

> **トリガー式はアイテムの「名前」ではなく「キー」を参照します。**
> キーが `vfs.file.contents[...]` なので式が長くなりますが、これが正しい形式です。
> ```
> last(/collector/vfs.file.contents[/hostfs/var/lib/traffic-viz/metrics/trafficviz_decapd_active])<>1
> ```

### 5-B. 手動登録（Zabbix Web UI）

1. **データ収集 → ホスト** で `collector` を開く
   （未登録ならエージェントの `ZBX_HOSTNAME=collector` と一致させる）
2. **アイテム → アイテムの作成**
   - 名前: `trafficviz decapd active`
   - タイプ: `Zabbix エージェント`（パッシブ）
   - キー: 上表のとおり
   - 情報の型: `数値（符号なし）`
   - 更新間隔: `60s`
3. §3 の表のぶんだけ繰り返す（`vfs.file.time` のものは型 `数値（符号なし）`）
4. **トリガー → トリガーの作成** で §4 の式を登録
5. 動作確認: アイテムの「最新データ」に値が出る／`テスト` ボタンで取得できる

> ホスト `collector` のインターフェースには **Tailscale の IP（192.0.2.20）** を
> 設定してください。LAN IP だとパッシブチェックが拒否されます。

---

## 5-C. 登録結果の確認

```bash
python3 scripts/zabbix-setup.py --dry-run    # 差分がないことを確認
```

API での確認例（`state=0` なら正常に値を取得できている）:

```json
{"jsonrpc":"2.0","method":"item.get","params":{
   "host":"collector","search":{"key_":"trafficviz"},
   "output":["name","state","error","lastvalue"]},"id":1}
```

## 6. 検証コマンド（サーバ側）

Zabbix サーバのコンテナに `zabbix_get` があります。

```bash
S=<Zabbixサーバのコンテナ名>
R=192.0.2.20          # collector の Tailscale IP

docker exec $S zabbix_get -s $R -p 10050 -k "agent.ping"
docker exec $S zabbix_get -s $R -p 10050 \
  -k "vfs.file.contents[/hostfs/var/lib/traffic-viz/metrics/trafficviz_decapd_active]"
docker exec $S zabbix_get -s $R -p 10050 \
  -k "vfs.file.time[/hostfs/var/lib/traffic-viz/metrics/trafficviz_check_ok,modify]"
```

正常時の応答例:

```
trafficviz_decapd_active       -> 1
trafficviz_decapd_flush_age    -> 54
trafficviz_decapd_mape_samples -> 1
trafficviz_sflow_ingest_age    -> 38
trafficviz_db_ok               -> 1
```

---

## 7. トラブルシュート

| 症状 | 原因 | 対処 |
|------|------|------|
| `Connection reset by peer` | パッシブチェックの送信元が `Server=` に無い | Tailscale IP で監視する。または collector のエージェントに `ZBX_PASSIVESERVERS` を追加 |
| `ZBX_NOTSUPPORTED: Cannot obtain file ...` | メトリクスファイルが無い | `sudo systemctl start traffic-viz-health.service` を実行し、`traffic-viz-health.timer` を確認 |
| 値が更新されない | health タイマー停止 | `systemctl list-timers traffic-viz-health.timer` / `journalctl -u traffic-viz-health` |
| `decapd_mape_samples` が 0 のまま | MAP-E 通信が無い（正常なこともある） | 装置側 `show sflow information` で flow samples が増えているか確認 |
| すべて 999999 | decapd の stats ファイルが無い | `journalctl -u decapd` で起動エラーを確認。`/var/lib/traffic-viz/` の権限（pmacct が書けるか） |
| `flush stalled` が散発する | （2026-10-20 修正済み）旧実装は「最後に DB へ**書き込んだ**時刻」基準だったため、MAP-E 通信が少ない時間帯に誤発報した | 現在は収集ループの**ハートビート**基準。`trafficviz_decapd_flush_age` は常に 0〜90 秒に収まる |

---

## 8. Discord 通知

### 現状（実測で確認済み）

zabbix の Zabbix には **既に「Discord 通知 (Warning 以上)」（action id=7）が有効**で、
`Warning` 以上のすべてのトリガーを Discord へ送っています。traffic-viz の
トリガーも自動的に含まれるため、**追加設定なしで decapd / sfacctd の障害が通知されます**。

| 項目 | 内容 |
|------|------|
| アクション | `Discord 通知 (Warning 以上)`（有効） |
| 条件 | 深刻度 `>= Warning` / メンテナンス中でない |
| 通知先 | メディアタイプ `Discord`(id=71) → ユーザー `discord-notify`(id=3) |
| 復旧通知 | あり（`Resolved in 3m 0s: ...` 形式） |

### 動作確認（2026-10-20 00:26 実施）

`trafficviz_decapd_active` を一時的に `0` にして発報させ、実際の配信を確認した。

```
00:26:21  SENT  Discord  Problem: traffic-viz: decapd stopped
   Problem started at 00:26:21 on 2026.10.20
   Problem name: traffic-viz: decapd stopped
   Host: collector
   Severity: Disaster

00:29:21  SENT  Discord  Resolved in 3m 0s: traffic-viz: decapd stopped
```

通知ログは Zabbix の `alert.get` API で `status=1 (SENT)` を確認済み。
**decapd / sfacctd の障害は Discord に届きます。**

### 専用アクションに分けたい場合

traffic-viz だけ別チャンネル／別メッセージにしたい場合は
`scripts/zabbix-discord-action.py` で専用アクションを作れる。

```bash
ZABBIX_TOKEN=<token> python3 scripts/zabbix-discord-action.py
```

> **注意**: この場合、既存の「Discord 通知 (Warning 以上)」と**二重に通知**される。
> 分離するなら、既存アクションに
> `トリガー名「traffic-viz」を含まない` という除外条件を追加すること
> （逆に、専用アクション側に条件を足すだけでは重複は消えない）。

### 通知を絞りたい場合

| 目的 | 操作 |
|------|------|
| Warning を通知しない（High 以上のみ） | 既存アクションの条件を `>= High` に変更 |
| traffic-viz を Discord に出さない | 既存アクションに `トリガー名 not like traffic-viz` を追加 |
| 復旧通知を止める | 既存アクションの復旧操作を削除 |
| 再通知（エスカレーション）を追加 | 既存アクションに 2 ステップ目の操作を追加 |

## 9. 補足: 別方式（参考）

| 方式 | 必要なもの | 長所 / 短所 |
|------|-----------|------------|
| **本ドキュメントの方式** | なし（既存エージェントをそのまま利用） | 追加サービス不要。ファイル経由なので疎結合 |
| アクティブチェック | なし | 送信元制限を回避できる。サーバ側でアイテム種別を「アクティブ」にするだけ |
| `zabbix_sender` + cron | collector に zabbix_sender | 外向き 10051 のみで済む。エージェント不要 |
| HTTP エージェント | collector に小さな HTTP サーバ | 追加サービスとポート開放が必要。本方式で足りるなら不要 |
| SSH エージェント（サーバ側） | Zabbix サーバ → collector の SSH 鍵 | エージェント不要だが鍵管理が必要 |
