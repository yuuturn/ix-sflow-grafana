# 運用手順

対象ホスト: `collector.example.net` (192.0.2.10)
配置: `/opt/traffic-viz`

## 1. 状態確認（まずこれ）

```bash
ssh collector.example.net
sudo systemctl status sfacctd --no-pager
systemctl list-timers 'traffic-viz*' --no-pager
docker ps --format '{{.Names}}\t{{.Status}}' | grep tv-
curl -s http://127.0.0.1:3000/api/health
```

正常時の目安:
- `sfacctd` = active (running)
- 3つのタイマーが active
- `tv-postgres` = Up (healthy)、`tv-grafana` = Up
- API health が `{"database": "ok"}`

## 2. アクセス

| 用途 | URL |
|------|-----|
| Grafana | `http://<コレクタのIP>:3000/` |
| ログイン | `admin` / `/opt/traffic-viz/.env` の `GRAFANA_ADMIN_PASSWORD` |
| DB へ直接 | `docker exec -it tv-postgres psql -U tvadmin -d traffic` |

### 外部から公開する場合

Grafana は 3000 番で待ち受けているので、VPN やリバースプロキシ経由で公開する。
以下は [Tailscale serve](https://tailscale.com/kb/1242/tailscale-serve) の例
（テイルネット内にのみ公開され、インターネットには出ない）。

```bash
# 443 が空いている場合
sudo tailscale serve --bg --https=443 http://127.0.0.1:3000

# 443 を別サービスが使っている場合はポートを分ける
sudo tailscale serve --bg --https=8443 http://127.0.0.1:3000

# 確認と解除
tailscale serve status
sudo tailscale serve --https=8443 off
```

この場合、`.env` の `GRAFANA_ROOT_URL` を実際にアクセスする URL に合わせること
（共有リンクや通知リンクの生成に使われる）。

> パス方式（`https://host/grafana/`）で公開する場合は
> `GF_SERVER_SERVE_FROM_SUB_PATH=true` が必要なうえ、
> `http://<コレクタのIP>:3000/` での直接アクセスが壊れる点に注意。

### root_url の扱い

`GF_SERVER_ROOT_URL` は `https://collector.example.net:8443/` に設定している
（共有リンクや通知リンクが外部から開けるようにするため）。
LAN から `http://192.0.2.10:3000/` で開いても、画面・ログイン・ダッシュボードは
相対パスで動くので問題なく使える（共有リンクだけが Tailscale の URL になる）。

```bash
sudo grep GRAFANA_ADMIN_PASSWORD /opt/traffic-viz/.env
```

## 3. 起動・停止・再起動

```bash
# 全部止める
sudo systemctl stop sfacctd
cd /opt/traffic-viz && docker compose down

# 全部起動
cd /opt/traffic-viz && docker compose up -d
sudo systemctl start sfacctd

# 設定変更後の反映（推奨: インストーラを再実行）
cd /opt/traffic-viz && sudo ./scripts/install.sh
```

`install.sh` は冪等。`pmacct/sfacctd.conf` を編集した場合はこれを実行すると
`/etc/pmacct/sfacctd.conf` に反映され `sfacctd` が再起動される。

> **注意**: `grafana/provisioning/` 配下を編集した場合、`docker compose up -d` では
> **再起動されません**（コンテナ定義が変わらないため）。必ず
> `docker compose restart grafana` を実行してください。
> データソースのプロビジョニングは起動時にしか読まれません。

> **データソースの `database` は 2 箇所に必要**: Grafana 13 の UI / 変数クエリは
> `jsonData.database` を、古いバックエンド経路はトップレベルの `database` を参照する。
> 片方だけだと「You do not currently have a default database configured」で
> 全パネルが No data になる（実際に発生した）。

## 3b. decapd（sFlow の入口）

`ルータ → 6343 decapd → 6345 sfacctd` の直列構成なので、
**decapd が止まると可視化全体が止まる**。systemd が5秒で再起動する。

```bash
sudo systemctl status decapd --no-pager
sudo journalctl -u decapd -f
# 統計（datagrams / flow_samples / map_e_samples）が flush ログに出る
```

`map_e_samples` が増えていれば MAP-E トラフィックを正しく拾えている。
0 のままなら MAP-E のパケットが来ていない（正常なこともある）。

## 3c. 監視用メトリクス

`traffic-viz-health.timer` が1分ごとに
`/var/lib/traffic-viz/metrics/` へ1ファイル1値で状態を書き出す。
Zabbix（zabbix）は collector のエージェント経由でこれを読む。
詳細は `docs/zabbix-monitoring.md`。

```bash
sudo /opt/traffic-viz/scripts/health.sh      # 手動実行
cat /var/lib/traffic-viz/metrics/trafficviz_decapd_active
sudo journalctl -u traffic-viz-health -n 20 --no-pager
```

## 4. ログ

```bash
# sfacctd（受信・DB書き込み）
sudo journalctl -u sfacctd -f
sudo tail -f /var/log/pmacct/sfacctd.log

# タイマー実行結果
sudo journalctl -u traffic-viz-rollup.service -n 20 --no-pager
sudo journalctl -u traffic-viz-asn.service -n 20 --no-pager

# コンテナ
docker logs -f tv-grafana
docker logs -f tv-postgres
```

## 5. データ確認

```bash
docker exec -it tv-postgres psql -U tvadmin -d traffic
```

```sql
-- 直近10分に届いたデータ
SELECT count(*), sum(bytes), max(stamp_inserted)
FROM flows WHERE stamp_inserted > (now() AT TIME ZONE 'utc') - interval '10 min';

-- ロールアップの状況（バケット数と最終更新）
SELECT count(*) AS buckets, max(bucket) AS newest FROM flow_5m;

-- ASN 辞書のサイズ
SELECT count(*) FROM ip_asn;

-- ASN データの鮮度
SELECT * FROM asn_meta;

-- ディスク使用量
SELECT pg_size_pretty(pg_database_size('traffic'));
SELECT relname, pg_size_pretty(pg_total_relation_size(oid))
FROM pg_class WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace
ORDER BY pg_total_relation_size(oid) DESC;
```

## 6. よくあるトラブル

### ダッシュボードが空 / 更新されない

1. `flows` にデータが入っているか（上記 SQL）
2. ロールアップが動いているか: `sudo systemctl start traffic-viz-rollup.service`
3. タイマーが止まっていないか: `systemctl list-timers 'traffic-viz*'`

`refresh_flow_5m()` は直近90分を再計算する。それより古いデータは
`flows` から手動で再生成する:

```sql
SELECT refresh_flow_5m((now() AT TIME ZONE 'utc') - interval '7 days');
```

### ASN が `(未解決)` になる

```bash
sudo systemctl start traffic-viz-asn.service
sudo journalctl -u traffic-viz-asn.service -n 30 --no-pager
```

iptoasn.com へ到達できない場合は、既存の `asn_range` で動作を継続する（データは消えない）。

### Sankey が描画されない

- ノードにサイクルがあると d3 が描画を諦める（設計上 `S|`/`D|`/`P|` の接頭辞で回避済み）
- データが 1 行も無い場合は空表示になる
- ブラウザのコンソールにエラーが出ていないか確認

### ディスクが増える

```sql
-- 保持期間を短くする（既定 flows 7日 / flow_5m 45日）
SELECT * FROM prune_traffic('3 days', '30 days');
```

恒久的に変える場合は `/opt/traffic-viz/scripts/prune.sh` の
`KEEP_FLOWS` / `KEEP_ROLLUP` を変更。

### DB に入らず `ERROR` が出る

`journalctl -u sfacctd` で SQL エラーの全文が出る（デバッグ時）。
列名の不一致（`column "xxx" does not exist`）なら `flows` テーブルの
レイアウトが pmacct とずれている。`docs/design.md` §4 を参照。

## 7. データ保持ポリシー

| テーブル | 保持 | 用途 | 変更方法 |
|---------|------|------|---------|
| `flows` | 7日 | 詳細・再集計・調査 | `scripts/prune.sh` |
| `flow_5m` | 45日 | ダッシュボード | `scripts/prune.sh` |
| `asn_range` | 常に最新 | ASN 変換 | 週次で自動更新 |
| `ip_asn` | 自動管理 | 辞書キャッシュ | ASN 更新時に再構築 |

## 8. バックアップ

設定のみで十分（時系列データは再生成可能）:

```bash
cd /opt/traffic-viz
sudo tar czf ~/traffic-viz-config-$(date +%F).tar.gz \
  --exclude .env docker-compose.yml sql pmacct grafana scripts systemd docs
# .env は別管理（パスワードを含むため）
```

## 9. セキュリティ: 作業後にやること

構築時に一時的に `NOPASSWD` sudo を追加してもらった。**作業完了後は削除すること**:

```bash
sudo rm /etc/sudoers.d/sflow-tmp
sudo visudo -c        # 構文チェック
sudo -k; sudo -v      # パスワードが要求されることを確認
```

また、Grafana の admin パスワードは `.env` にあるランダム値。
必要なら Grafana UI から変更し、`.env` と `docker-compose.yml` の
`GF_SECURITY_ADMIN_PASSWORD` を更新する（`docker compose up -d` で反映）。
