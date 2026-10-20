# 設計書 — sFlow トラフィック可視化基盤

## 1. 構成

```
  ┌────────────────────┐        sFlow v5 (udp/6343)
  │ NEC UNIVERGE IX    │ ─────────────────────────────┐
  │ 192.0.2.1        │   サンプリング 1:N             │
  └────────────────────┘                               ▼
                                        ┌──────────────────────────────┐
                                        │ collector  192.0.2.10          │
                                        │                              │
                                        │  sfacctd (systemd)           │
                                        │   ・sFlow 受信/復号           │
                                        │   ・サンプリングレート補正     │
                                        │   ・5分バケット集約            │
                                        │        │ pgsql プラグイン      │
                                        │        ▼                      │
                                        │  tv-postgres  (Docker)        │
                                        │   flows      … 生5分集約      │
                                        │   ip_asn     … IP→ASN 辞書    │
                                        │   asn_range  … ASN レンジ     │
                                        │   flow_5m    … 表示用ロールアップ│
                                        │        ▲                      │
                                        │        │ SQL (読み取り専用)    │
                                        │  tv-grafana  (Docker) :3000   │
                                        │   Sankey / 時系列 / ランキング │
                                        └──────────────────────────────┘
                                          ▲
                        systemd timers ───┘
                          2分: 辞書+ロールアップ更新
                          日次: 保持期間削除
                          週次: ASN データ更新
```

## 2. データフロー

1. ルータが WAN インターフェースのパケットを 1:N でサンプリングし、
   sFlow v5 データグラムを 192.0.2.10:6343 に送る。
2. `sfacctd` がデコードし `(src_ip, dst_ip, src_port, dst_port, proto, tos)` で集約。
   `sfacctd_renormalize: true` により bytes/packets は「サンプル数 × サンプリングレート」に換算される。
3. `sql_history: 5m` / `sql_refresh_time: 60` で、5分バケットを60秒ごとに UPSERT。
4. `refresh_ip_asn()` が未解決 IP を ASN 辞書 `ip_asn` に追加（2分間隔）。
5. `refresh_flow_5m()` が直近90分のバケットを `flow_5m` に再集約（2分間隔）。
6. Grafana は `flow_5m` のみを読む（高速）。

## 3. なぜ2段階の集約なのか（性能設計）

実測（Raspberry Pi 4, 100,000 生行, 24時間レンジ）:

| 方式 | クエリ時間 | 問題 |
|------|-----------|------|
| `v_flows` を直接 + 行ごと ASN 検索 | **16.6 s** | ASN 検索が約100µs/回 |
| `ip_asn` 辞書キャッシュを追加 | 2.8 s | まだ遅い、行数に比例 |
| **`flow_5m` ロールアップ** | **45〜80 ms** | ✅ 採用 |

さらに `is_private()` が 1回 約9µs かかり、全行で2回呼ぶと支配的だったため、
`flows.src_private` / `flows.dst_private` を **生成列（GENERATED ... STORED）** にして
INSERT 時に1回だけ計算するようにした。

`flow_5m` の行数はサンプリングレートに依存しない:
公開 IP は ASN に丸め、プライベート IP のみホストとして保持するため、
1バケットあたり「内部ホスト数 × 宛先AS数 × ポート数」で頭打ちになる。

## 4. DB スキーマ

### flows（pmacct の v9 レイアウト）

**重要**: Ubuntu の pmacct パッケージは `--enable-l2` 付きでビルドされており、
同梱の `pmacct-create-table_v9.pgsql`（`vlan` 列）とは異なる。
実際に sfacctd が発行する INSERT は次の19列:

```
stamp_updated, stamp_inserted, vlan_in, ip_src, ip_dst, as_src, as_dst,
port_src, port_dst, tcp_flags, tos, ip_proto, tag, class_id, mac_src,
mac_dst, packets, bytes, flows
```

UPSERT キーは上記から `tcp_flags` を除いた14列 + 主キー制約。
列名を変えると sfacctd が SQL エラーになるので変更しないこと。

追加で Postgres が自動計算する列:
- `src_private`, `dst_private`（生成列。pmacct は書き込まない）

### asn_range

IP → ASN の元データ（iptoasn.com）。IP を符号なし整数に変換して保持するため
IPv4 と IPv6 を同じ列で扱える（IPv4 < 2^32 < IPv6）。

| 列 | 内容 |
|----|------|
| ip_start, ip_end | アドレス範囲（NUMERIC(40,0)） |
| asn, org, country | AS 番号、組織名、国コード |

### ip_asn（辞書キャッシュ）

実際に観測した IP ごとに ASN を1行で保持。`v_flows` はここにハッシュ結合する。
ASN データ更新時に TRUNCATE され、次のタイマー実行で再構築される。

### flow_5m（表示用ロールアップ）

| 列 | 内容 |
|----|------|
| bucket | 5分バケット（UTC） |
| direction | outbound / inbound / internal / transit |
| src_asn, dst_asn, src_org, dst_org | AS 情報 |
| src_host, dst_host | プライベート IP のみ実値、公開側は 0.0.0.0 |
| svc_port | サーバ側と推定したポート（小さい方の非0ポート） |
| bytes, packets, samples | 集計値 |

## 5. 方向判定

ifIndex ではなく内部/外部の判定で決める（単一アップリンクの家庭用ルータではこちらが確実）。

| 条件 | direction |
|------|-----------|
| internal → external | outbound（送信） |
| external → internal | inbound（受信） |
| internal ↔ internal | internal |
| external ↔ external | transit |

**内部の定義は `lan_prefix` テーブル**（`10/8`, `172.16/12`, `192.168/16`,
`100.64/10`, `127/8`, `fc00::/7`, `fe80::/10` に加えて、LAN の IPv6 `/64`）。

RFC1918 だけでは不十分: デュアルスタック環境では LAN ホストがグローバル IPv6 を
持つため、すべて `transit`（外部→外部）に分類されてしまう。
本環境の LAN プレフィックスは `2001:db8:aaaa:bbbb::/64`（RA で広告中）。

プレフィックスが変わった場合（DHCPv6-PD の更新、ISP 変更）は
`lan_prefix` を更新して `ip_asn` を再構築する:

```sql
DELETE FROM lan_prefix WHERE note LIKE 'LAN%';
INSERT INTO lan_prefix VALUES ('2001:db8:aaaa:cccc::/64', 'LAN');
TRUNCATE ip_asn;      -- 次の refresh_ip_asn() で再構築される
```

## 5.1 sFlow の取得方向

`sflow sampling-rate <rate> {in|out}` で方向を指定でき、省略時は `in`。
標準構成は **LAN ポートの `in` と `out`** を有効にする:

| ポート | 方向 | 取得できる方向 |
|--------|------|---------------|
| `GigaEthernet2`（LAN） | `in` | アップロード（内部 → 外部） |
| `GigaEthernet2`（LAN） | `out` | ダウンロード（外部 → 内部） |

同じパケットが両方に現れることはないので二重計上は起きない。LAN を `in` のみで
運用するとダウンロードが 0 件になる（実測で確認）。

サンプリングはルータの転送処理を通った後に行われるため、`out` で見える
ダウンロードは **NAPT 後・デカプセル後の IPv4** で、宛先は LAN ホストの実
アドレスになる。そのため MAP-E のデカプセルは不要（§5.3）。

WAN 側の `in` を併用すると、ダウンロードが「WAN `in` の 4in6」と
「LAN `out` の IPv4」で二重計上される。WAN を使うのは `out` が無い機種だけ。

## 5.2 sFlow の受け口

標準構成では装置から sfacctd へ直接送る。

```
ルータ ──udp/6343──▶ sfacctd（pmacct）──▶ PostgreSQL
```

`out` が使えないファームウェア向けに、`decapd` を挟む構成を任意で用意している
（`scripts/install.sh --with-decapd`）。

```
ルータ ──udp/6343──▶ decapd ──┬──▶ 127.0.0.1:6345  sfacctd（pmacct）
                              └──▶ デカプセル → flows_decap
```

| 構成 | 6343/udp | 6345/udp | 対象 |
|------|----------|----------|------|
| 標準（既定） | sfacctd | — | LAN `in` + `out` が使える装置 |
| `--with-decapd` | decapd | sfacctd | `out` が使えず WAN `in` しかない装置 |

IX シリーズは **sFlow コレクタを1つしか持てない**（2つ目を設定すると上書きされる。
機種によっては複数指定できる場合があるので `show sflow information` で確認）。
そのためフォールバック構成では `decapd` が 6343 を受けて sfacctd へ中継する。

## 5.3 MAP-E の扱い

標準構成では、LAN `out` の時点で既にデカプセルされた IPv4 が見えるため
MAP-E を意識する必要はない。

フォールバック構成（WAN `in`）では、IPv4 over IPv6 のパケットは外側 IPv6
ヘッダだけが sFlow に乗り、pmacct ではプロトコル 4（IP-in-IP）として記録される。
pmacct のトンネルデコードは sFlow 非対応（`CONFIG-KEYS` で `tunnel_0` が
`NO_SFACCTD`）のため、`optional/decapd/` の daemon がサンプルヘッダを自前で
解析して内側 IPv4 の 5-tuple を取り出す。

`sflow max-header-size 128` であれば
Ethernet(14) + IPv6(40) + 内側 IPv4(20) + トランスポートヘッダ が収まり、
内側の送信元/宛先 IP・ポート・プロトコルまで復元できる（実測で確認済み）。

### データの流れ

`flows_decap` はフォールバック構成でのみ書き込まれる。標準構成では空のまま。

| 経路 | 内容 |
|------|------|
| `flows` | pmacct が記録した通常のフロー＋外側 MAP-E レコード |
| `flows_decap` | decapd が復元した内側 IPv4 フロー（フォールバック時のみ） |
| `v_flows` | 両者を統合。デキャップ実績のあるバケットは外側レコードを**除外**（二重計上防止）し、デキャップ実績が無いバケットは外側をそのまま見せる（データ欠落防止） |

### ラベル

フォールバック構成でのみ現れる。

| 状態 | `dst_asn` | 表示 |
|------|-----------|------|
| デキャップ済み | `-2` | `(MAP-E 経由のダウンロード)` |
| デキャップ前の外側 | `-1` | `MAP-E (IPv4-in-IPv6)` |

デキャップ行は定義上すべて WAN ingress = `inbound`。内部ホストは MAP-E の後段で
NAPT されるため特定できない（宛先は WAN の公開IP）。これが標準構成（LAN `out`）を
既定にしている理由でもある。

## 6. Sankey の設計

`flows` から直接ではなく `sankey_flows(from, to, direction, top_n)` が
`(source, target, value)` の3列を返し、Plotly がそのまま描画する。

### 3段構成

```
  S|<内部IP or AS>  ──▶  D|<内部IP or AS>  ──▶  P|<ポート>
        送信元                  宛先                    サービス
```

### ノード名に接頭辞を付ける理由

同じ AS が送信元にも宛先にも現れると、名前だけでは同一ノードに統合され
**グラフにサイクルができて d3-sankey が描画を諦める**。
`S|` `D|` `P|` と名前空間を分けることで必ず DAG になる（検証済み: 後方エッジ 0）。
表示時に接頭辞は除去する。

### ノード数の制限

`top_n`（既定12）で送信元・宛先それぞれ上位のみ残し、残りは `(other)` に集約。
ノードが多すぎるとラベルが重なって読めなくなるため。

### なぜ Plotly パネルなのか

Grafana 13 には **Sankey のコアパネルが存在しない**（`/api/plugins?type=panel` で確認。
28パネル中 sankey は無い）。そのためコミュニティプラグイン
`ae3e-plotly-panel`（Plotly、Sankey トレースをネイティブサポート）を採用した。
ダッシュボード JSON にスクリプトを埋め込んでいるので、GUI 操作なしで再現できる。

## 7. 時刻の扱い

- すべて **UTC** で保存（pmacct は `TZ=UTC`、DB は `timezone=UTC`）
- `stamp_inserted` は `timestamp without time zone`（pmacct のスキーマ要件）
- Grafana のデータソースはセッション TZ を UTC に固定し、表示時にブラウザ TZ へ変換
- `$__timeFilter()` が生成する `'...Z'` リテラルは `timestamp` へキャストされると
  タイムゾーン部分が切り捨てられるため、UTC 保存と整合する（検証済み）

## 8. 常駐・自動化

| ユニット | 間隔 | 処理 |
|---------|------|------|
| `sfacctd.service` | 常駐 | sFlow 受信（`Restart=always`、停止時 SIGINT でフラッシュ） |
| `traffic-viz-rollup.timer` | 2分 | `refresh_ip_asn()` + `refresh_flow_5m()` |
| `traffic-viz-prune.timer` | 日次 04:15 | `prune_traffic()`（flows 7日 / flow_5m 45日） |
| `traffic-viz-asn.timer` | 週次 日曜 03:15 | `update-asn.sh` |

Docker コンテナは `restart: unless-stopped`、PostgreSQL は healthcheck 付き。
`sfacctd` は `ExecStartPre` で DB の待ち合わせを行うため、再起動順序の問題は起きない。

## 9. セキュリティ

| 項目 | 設定 |
|------|------|
| PostgreSQL の公開範囲 | `127.0.0.1:5432` のみ（LAN 非公開） |
| DB ロール | `pmacct`（flows へ INSERT/UPDATE）、`grafana`（SELECT のみ） |
| 認証情報 | `/opt/traffic-viz/.env`（0600） |
| `sfacctd.conf` | root:pmacct 0640 |
| Grafana | 匿名アクセス無効、サインアップ無効 |
| ポート | 3000（Grafana）、6343/udp（sFlow）のみ待受 |
