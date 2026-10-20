# NEC UNIVERGE IX シリーズ — sFlow 設定手順

**IX2207 で実機確認済み**（2026-10-20）。モデルやファームウェアによって
コマンド体系が異なる場合があるため、投入前に `?` ヘルプで確認してください。

## 1. 確定した設定箇所

sFlow は **`device`（物理ポート）単位**で設定する。インターフェース（`.0`）配下には
`sflow` コマンドは存在しない（`% sflow -- Invalid command.` を確認済み）。
`Tunnel0` などの論理インターフェースは `device ?` の選択肢に出ない（物理IFのみ）。

| 設定箇所 | サブコマンド |
|---------|-------------|
| `(config)# sflow` | `agent` / `collector` / `max-datagram-size` |
| `(config)# device GigaEthernetN` | `sflow sampling-rate <rate> [in\|out]` / `sflow max-header-size` / `sflow polling-interval` |

## 2. 投入する設定（標準構成）

**LAN ポート 1 つで `in` と `out` の両方**を有効にするのが基本です。

```
! エージェントとコレクタ（グローバル）
sflow agent ip 192.0.2.1
sflow collector ip 192.0.2.10 6343

! LAN 側: 上り（in）と下り（out）
device GigaEthernet2
  sflow sampling-rate 1000 in
  sflow sampling-rate 1000 out
  sflow max-header-size 128
  sflow polling-interval 30
```

```
(config)# write memory
```

WAN 側の設定は**不要**です。むしろ入れてはいけません（後述の二重計上）。

## 3. 方向の指定（`in` / `out`）

`sflow sampling-rate` の第 2 引数で方向を指定します。**省略すると `in` 扱い**に
なります。

```
RT1-komone(config-GigaEthernet2)# sflow sampling-rate 1000 ?
  in    -- Apply flow sampling to input packets
  out   -- Apply flow sampling to output packets
```

| ポート | 方向 | サンプリングしている通信 | ダッシュボードの `direction` |
|--------|------|-------------------------|------------------------------|
| `GigaEthernet2`（LAN） | `in` | LAN ホスト → インターネット | `outbound`（アップロード） |
| `GigaEthernet2`（LAN） | `out` | インターネット → LAN ホスト | `inbound`（ダウンロード） |

LAN ポートでは上りが `in`、下りが `out` なので、**2 行で両方向が揃い、
二重計上も発生しない**（同じパケットが両方に現れることはない）。

### なぜ LAN だけで足りるのか

サンプリングはルータの転送処理を通った後に行われるため、`out` で見える
ダウンロードは **NAPT 後・デカプセル後の IPv4** です。したがって
MAP-E（IPv4-in-IPv6）のデカプセルは不要で、宛先も LAN ホストの実アドレスに
なります。

実測（IX2207 + MAP-E）:

| 構成 | inbound の宛先 |
|------|---------------|
| LAN `in` のみ | **0 件**（ダウンロードが記録されない） |
| LAN `in` + `out` | `192.168.2.93` / `.85` / `.21` … と実アドレスが入る |

`out` を指定し忘れるとダウンロードが一切取れません。`show sflow information` の
`Out:` 行が増えているかを必ず確認してください。

## 4. `out` が使えない機種向けのフォールバック

一部のファームウェアでは `out` が実装されていない、または `sflow sampling-rate`
に方向引数がありません。その場合だけ **WAN `in` + `decapd`** を使います
（設定例: [`examples/nec-ix-sflow-decapd.conf`](../examples/nec-ix-sflow-decapd.conf)）。

```
! LAN = アップロード
device GigaEthernet2
  sflow sampling-rate 1000 in

! WAN = ダウンロード（MAP-E でカプセル化されたまま）
device GigaEthernet0
  sflow sampling-rate 1000 in
```

このとき MAP-E 環境では、WAN 側のダウンロードが **IPv6 にカプセル化された
状態（プロトコル番号 4）**で届き、sFlow には外側の IPv6 ヘッダしか乗りません。

```
2404:9200:225:100::64  →  2001:db8:aaaa:bbbb:6a:484d:a000:5500   proto 4
```

pmacct 側にデカプセル機能はないため、`optional/decapd/` の daemon で
サンプルヘッダから内側 IPv4 の 5-tuple を取り出します。導入手順は
[design.md](design.md) の 5.2 を参照してください（`scripts/install.sh --with-decapd`）。

このフォールバックには制約があります。

- 剥がした内側 IPv4 の宛先は **NAPT 前の公開アドレス**なので、
  ダウンロードを LAN ホスト単位に割り当てられない
- 外側トンネルの統計（BR ごとの分布、MTU など）は別途集計が必要
- ルータ自身が発する通信は LAN ポートを通らない

> **標準構成と併用しないこと。** WAN `in` と LAN `out` を同時に有効にすると、
> ダウンロードが「WAN `in` の 4in6」と「LAN `out` の IPv4」で二重計上されます。

## 5. パラメータの決め方

| パラメータ | 値 | 根拠 |
|-----------|-----|------|
| `sampling-rate` | 1000 | 1Gbps で毎秒約125サンプル。`in`+`out` で最大 250 サンプル/秒 |
| `max-header-size` | 128 | IPv6 + トランスポートヘッダまで収まる。decapd フォールバックでは内側 IPv4 も必要 |
| `polling-interval` | 30 | カウンタサンプル用。本構成では未使用 |

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
Data source information:
  GigaEthernet2
    Flow sampling:
      In:
        ... N samples ...
      Out:
        ... M samples ...
```

- **`In:` と `Out:` の両方**の `samples` が増え続けていれば正常
- `outputs` も同様に増える（`outputs ÷ sampling-rate ≒ samples`）
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
| アップロードしか出ない | `out` を指定していない（省略時は `in`） | `sflow sampling-rate <rate> out` を追加 |
| `Out:` の samples が 0 のまま | `out` が使えないファームウェア | §4 の WAN `in` + decapd に切り替える |
| `sflow` がインターフェース配下で invalid | 設定箇所が違う | `device <物理IF>` 配下で設定する |
| 流量が 1/1000 に見える | レート補正が効いていない | コレクタ側 `sfacctd_renormalize: true` を確認 |
| ダウンロードが二重に出る | WAN `in` と LAN `out` を併用 | どちらか一方にする |
| `MAP-E (IPv4-in-IPv6)` ラベルが出る | WAN `in` で受けた 4in6 をデカプセルしていない | 標準構成なら WAN の設定を外す。フォールバックなら decapd を導入する |
