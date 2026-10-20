# NEC UNIVERGE IX シリーズ — sFlow 設定手順

**IX2207 で実機確認済み**（2026-10-19）。モデルによってコマンド体系が
異なる場合があるため、投入前に `?` ヘルプで確認してください。

## 1. 確定した設定箇所

sFlow は **`device`（物理ポート）単位**で設定する。インターフェース（`.0`）配下には
`sflow` コマンドは存在しない（`% sflow -- Invalid command.` を確認済み）。
`Tunnel0` などの論理インターフェースは `device ?` の選択肢に出ない（物理IFのみ）。

| 設定箇所 | サブコマンド |
|---------|-------------|
| `(config)# sflow` | `agent` / `collector` / `max-datagram-size` |
| `(config)# device GigaEthernetN` | `sflow sampling-rate` / `sflow max-header-size` / `sflow polling-interval` |

## 2. 投入した設定（全量）

```
! エージェントとコレクタ（グローバル）
sflow agent ip 192.0.2.1
sflow collector ip 192.0.2.10 6343

! LAN 側 = アップロードの取得
device GigaEthernet2
  sflow sampling-rate 1000
  sflow max-header-size 128
  sflow polling-interval 30

! WAN 側 = ダウンロードの取得
device GigaEthernet0
  sflow sampling-rate 1000
  sflow max-header-size 128
  sflow polling-interval 30
```

```
(config)# write memory
```

## 3. なぜ LAN と WAN の両方が必要か

`sflow sampling-rate` の第 2 引数で方向を指定できる。**省略すると `in` 扱い**になる。

```
RT1-komone(config-GigaEthernet2)# sflow sampling-rate 1000 ?
  in    -- Apply flow sampling to input packets
  out   -- Apply flow sampling to output packets
```

本構成は **両ポートとも `in`** を有効にしている:

| ポート | 方向 | サンプリングしている通信 |
|--------|------|-------------------------|
| `GigaEthernet2`（LAN） | `in` | LAN ホスト → インターネット（**アップロード**） |
| `GigaEthernet0`（WAN） | `in` | インターネット → LAN ホスト（**ダウンロード**） |

両方とも `in` 同士なので**二重計上は原理的に発生しない**。

LAN を `in` だけで有効にするとダウンロードは記録されない（LAN ポートにとって
ダウンロードは `out` 方向のため）。実測でも DB の「内部宛ての行」が **0 件**で、
WAN を追加して初めて inbound が出現した。

### 代替案: LAN の `out` を採る

`out` を使えば、ダウンロードを LAN ポートの egress で**素の IPv4 として**取得できる。
この場合 §4 の `decapd`（MAP-E デカプセル）は不要になる。

```
device GigaEthernet2
  sflow sampling-rate 1000 in     ! アップロード
  sflow sampling-rate 1000 out    ! ダウンロード（IPv4 そのまま）
```

ただし **WAN の `in` と併用してはいけない**。ダウンロードが「WAN `in` の 4in6」と
「LAN `out` の IPv4」で二重に記録される。`out` を採るなら WAN の設定は外す。

| 構成 | ダウンロードの見え方 | 失われる情報 |
|------|--------------------|-------------|
| LAN `in` + WAN `in`（本リポジトリの既定） | 4in6 → `decapd` で復元 | — |
| LAN `in` + LAN `out` | 素の IPv4 | NAPT 後のグローバル IPv4 / PSID、4in6 オーバーヘッド、BR 分布、ルータ自身の通信 |

可視化だけが目的なら LAN `in` + `out` の方が構成は単純。本リポジトリは
トンネル側も見たいため WAN `in` を既定にしている。

## 4. MAP-E 環境での制約（重要）

本環境は IPv4 over IPv6 の MAP-E 構成。WAN 側で観測される IPv4 ダウンロードは
**IPv6 にカプセル化された状態（プロトコル番号 4）**で届くため、sFlow には
外側の IPv6 ヘッダしか乗らない。

```
2404:9200:225:100::64  →  2001:db8:aaaa:bbbb:6a:484d:a000:5500   proto 4
```

- 内側の IPv4 5-tuple は**復元不可**（pmacct 側にデカプセル機能はない）
- 実測では実トラフィックの 7〜35% で変動（IPv4 の大容量ダウンロード時に増える）
- 対策として `v_flows` がプロトコル 4 を検出し、
  `MAP-E (IPv4-in-IPv6)` というラベルで Sankey に表示する（誤った AS に紐づけない）

IPv6 の直接通信（NTT の IPv6 網）は通常どおり 5-tuple が取れるため影響なし。

## 5. パラメータの決め方

| パラメータ | 値 | 根拠 |
|-----------|-----|------|
| `sampling-rate` | 1000 | 1Gbps で毎秒約125サンプル。5分で約37kサンプル相当 |
| `max-header-size` | 128 | Ethernet(14)+IPv4(20)+TCP(20)=54 に十分。ポート番号まで取れる |
| `polling-interval` | 30 | カウンタサンプル用。本構成では未使用だが既定に合わせた |

サンプリングレートを上げる（2000 等）と装置負荷は下がるが精度も下がる。
CPU 使用率が気になる場合のみ調整すればよい。

## 6. 確認コマンド

```
# show sflow information
```

```
sFlow information:
  Collector:
    IP address: 192.0.2.10, port: 6343
  Agent:
    IP address: 192.0.2.1, port: 57498
  Statistics:
    N outputs, 0 errors
    M flow samples, M counter samples
```

- `flow samples` が**増え続けていれば正常**
- `outputs` も同様に増える
- `errors` が 0 でない場合はコレクタ側の受信状態を確認

コレクタ側:

```bash
sudo tcpdump -ni eth0 "udp port 6343" -c 5
# → 192.0.2.1.57498 > 192.0.2.10.6343: sFlowv5, IPv4 agent 192.0.2.1
```

## 7. トラブルシュート

| 症状 | 原因 | 対処 |
|------|------|------|
| `0 flow samples` のまま | `sampling-rate` 未設定 | `device GigaEthernetN` 配下で設定。グローバルには無い |
| アップロードしか出ない | ダウンロードは LAN ポートの `out` 方向のため未取得 | LAN に `out` を足すか、WAN に `in` を設定（併用は二重計上） |
| `sflow` がインターフェース配下で invalid | 設定箇所が違う | `device <物理IF>` 配下で設定する |
| 流量が 1/1000 に見える | レート補正が効いていない | コレクタ側 `sfacctd_renormalize: true` を確認 |
| IPv4 のダウンロードが粗い | MAP-E カプセル化 | 仕様。`MAP-E (IPv4-in-IPv6)` ラベルで表示される |
