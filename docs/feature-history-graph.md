# 電力履歴グラフ機能 設計書（Phase ②.5）

## 要件

- ダッシュボード最下段に履歴グラフセクションを追加
- **第一軸（左・棒グラフ）**: PV発電量 と 充電量＋消費電力（Tracer負荷＋BMS負荷）の2本を並べ、電力バランスを比較できるようにする
- **第二軸（右・折れ線）**: Tracerバッテリー電圧・バッテリー温度のトレンド
- 横軸スケールを **分・時・日・週・月** で切り替え（各スケールの最新値を表示）
- 左方向へのスクロール（スワイプ/ドラッグ）で過去データを遡れる
- ストレージ戦略: 生データはUSB-HDDへ日次バッチ保存。SDカードには**1日分のバッファ＋グラフ表示用の集約データのみ**を保持

---

## 1. データ設計

### 1.1 保存先の役割分担

| 場所 | 内容 | 書き込み頻度 |
|---|---|---|
| SDカード `/home/pi/cariot/history.db`（SQLite, WAL） | 分粒度バッファ（48時間分）＋ 時/日/週/月ロールアップ | 1分に1 insert |
| USB-HDD `/mnt/storage/tracer_log/tracer_YYYYMMDD.csv` | 生データ（分粒度）の日次バッチ書き出し | **1日1回**（00:05） |

- 現行の `main.py write_csv()`（毎分HDD直書き）は廃止し、SQLite insert に置換
  → HDDへの書き込みが毎分→1日1回に削減（スピンアップ・断片化対策）
- 日次バッチ後、SQLiteの48時間より古い分粒度行を削除（バッファは常に約2日分）

### 1.2 テーブル構成

```sql
-- 分粒度バッファ（48h保持・日次でHDDへ退避後削除）
CREATE TABLE minute (
  ts          INTEGER PRIMARY KEY,  -- epoch秒（分頭に丸め）
  pv_w        REAL,   -- PV発電電力（平均W）
  chg_w       REAL,   -- 充電電力 = Tracer bat_power（平均W）
  load_tr_w   REAL,   -- Tracer負荷電力（平均W）
  load_bms_w  REAL,   -- BMS負荷電力 = Σ max(0, -pack_w)（放電分のみ・平均W）
  bat_v       REAL,   -- バッテリー電圧（平均V）
  bat_temp    REAL    -- バッテリー温度（平均℃）
);

-- ロールアップ（永年保持・容量は微小）
CREATE TABLE rollup (
  scale       TEXT,     -- 'hour' | 'day' | 'week' | 'month'
  ts          INTEGER,  -- 期間開始 epoch秒
  pv_wh       REAL,     -- 期間内発電量 Wh
  chg_wh      REAL,
  load_tr_wh  REAL,
  load_bms_wh REAL,
  bat_v_avg   REAL, bat_v_min REAL, bat_v_max REAL,
  temp_avg    REAL, temp_min  REAL, temp_max  REAL,
  PRIMARY KEY (scale, ts)
);
```

### 1.3 電力の定義（符号規約）

```
PV発電    = Tracer pv_power
充電      = Tracer bat_power（充電電流×バッテリー電圧）
Tracer負荷 = Tracer load_power（負荷端子）
BMS負荷   = Σ_pack max(0, -pack_w)   ※JBDは充電時 pack_a>0 / 放電時<0
            → 夜間などバッテリーから持ち出している電力のみ計上

バランス式: PV ≈ 充電 + Tracer負荷 + 変換損失（昼間）
            BMS負荷はバッテリー放電（夜間消費）の見える化
```

※ 実機データで符号・整合性を確認のうえ実装時に微調整する。

### 1.4 ロールアップ生成

- **hour**: 毎時0分に直前1時間の minute 行から生成（Wh = ΣW / 60）
- **day**: 日次バッチ（00:05）で前日の hour 24行から生成
- **week / month**: day 確定時に該当週（月曜始まり）・該当月の行を upsert 再計算
- 起動時に未生成のロールアップを補完（停電・再起動対策）

### 1.5 容量見積り

| データ | サイズ |
|---|---|
| SD: minute 48時間分 | 約 300 KB |
| SD: rollup 1年分（hour 8,760行＋day/week/month） | 約 1 MB/年 |
| HDD: 日次CSV | 約 150 KB/日 ≒ 55 MB/年 |

→ SDカード負荷は WAL + 毎分1 insert のみで摩耗問題なし。

---

## 2. API設計

```
GET /api/history?scale=minute|hour|day|week|month&before=<epoch>&limit=<n>
```

- `before` 省略時は最新から `limit` 件（デフォルト: 分=120, 時=72, 日=60, 週=52, 月=24）
- 戻り値:

```json
{
  "scale": "hour",
  "points": [
    {"t": 1780000000, "pv": 32.5, "chg": 25.1, "load_tr": 4.2,
     "load_bms": 0.0, "bat_v": 13.25, "bat_temp": 22.3}
  ],
  "has_more": true
}
```

- 単位: `minute` → 平均W、`hour` 以上 → Wh
- `minute` は SQLite minute テーブル、それ以外は rollup テーブルから返す
- 1ページ約 5〜10 KB → povo 128kbps でも1〜2秒で取得可能

---

## 3. フロントエンド設計（ダッシュボード最下段）

### 3.1 ライブラリ

- **Chart.js v4** ＋ **chartjs-plugin-zoom** ＋ **hammer.js**（タッチパン用）
- CDNは使わず `static/` にバンドルして Flask から配信
  （povo 128kbps・ZTR01ローカル接続のオフライン環境でも動作。初回ロード後はブラウザキャッシュ）

### 3.2 チャート構成（mixed chart）

| 系列 | 種別 | 軸 |
|---|---|---|
| PV発電 | 棒（バー1） | 左軸 y: W / Wh |
| 充電 ＋ Tracer負荷 ＋ BMS負荷 | **積み上げ棒**（バー2・色分け） | 左軸 y |
| バッテリー電圧 | 折れ線 | 右軸 y1: V |
| バッテリー温度 | 折れ線（破線） | 右軸 y2: ℃ |

- バー2を積み上げにすることで「PVがどこに使われたか」の内訳とバランスが一目で分かる
- 右軸はV用と℃用の2本（Chart.jsは複数軸対応）

### 3.3 操作

- スケール切替タブ: `分 | 時 | 日 | 週 | 月`（タブ切替で /api/history を再取得）
- **過去への遡り**: zoom プラグインの pan（マウスドラッグ／スワイプ）で左へ移動し、
  端に到達したら `before=<最古点>` で前ページを取得して先頭に prepend（無限スクロール方式）
- 「最新へ」ボタンで右端へ復帰
- 既存の /api/status 5秒ポーリングとは独立（グラフは表示時＋スケール切替時のみ取得）

---

## 4. 実装ステップ

| # | 内容 | ファイル |
|---|---|---|
| 1 | SQLiteストア新規作成（insert / rollup / query / 日次エクスポート＋prune） | `history_store.py`（新規） |
| 2 | `write_csv()` を `history_store.record(status)` に置換、日次バッチ呼び出し追加 | `main.py` |
| 3 | `/api/history` エンドポイント追加 | `webui.py` |
| 4 | ダッシュボード最下段にグラフセクション＋スケールタブ＋パン実装 | `webui.py`（_DASHBOARD_HTML） |
| 5 | Chart.js / zoom plugin / hammer.js を同梱 | `static/`（新規） |
| 6 | ロールアップ・ページング・容量のユニットテスト | `test_history.py`（新規） |
| 7 | デプロイ → systemd再起動 → 実機確認 | — |

### 互換性メモ

- 既存HDD CSVフォーマットは日次バッチ出力でも列互換を維持（＋BMS負荷列を追加）
- 過去のHDD CSVからのロールアップ初期投入スクリプトはオプション（必要なら実装）

---

## 5. 確認チェックリスト

- [ ] SQLite minute insert が毎分動作（journalctl確認）
- [ ] 毎時0分に hour ロールアップ生成
- [ ] 00:05 に前日CSVがHDDへ出力され、SQLiteから48h超の行が削除される
- [ ] /api/history 各スケールで正しい件数・単位が返る
- [ ] ダッシュボード最下段にグラフ表示（棒2本＋折れ線2本＋右軸2本）
- [ ] 分/時/日/週/月タブ切替動作
- [ ] 左スワイプで過去データが継ぎ足し読み込みされる
- [ ] povo回線（128kbps）経由でも初回表示が実用速度
- [ ] 再起動（停電想定）後にロールアップ欠損が自動補完される
