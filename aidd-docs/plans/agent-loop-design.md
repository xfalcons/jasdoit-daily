# 每日內容 Agent Loop 設計（2026-11-07 v1）

定位：本文是「內容引擎」的規格書，對應計畫 v4 的 §4 與 §5。計畫（`ai-japanese-learning-app-plan.md`）定義目的與 Phase 地圖，本文定義引擎怎麼蓋。核心決策一句話：**jasdoit-daily repo（hexo ＋ GitHub Pages）本身就是內容庫與發布管線**，push 即發布。

## 0. 核心決策

| 決策 | 內容 | 與計畫 v3 的差異 |
|---|---|---|
| 內容庫 | git（markdown posts）＋ R2（音檔） | Postgres ＋ S3 → Postgres 延至 Phase 2（§9） |
| 發布 | push 到 main → `pages.yml` 自動部署 Pages | 「入庫」與「發布準備」合併為一次 git commit |
| 管線 | 確定式八 stage，不是 agent 自由發揮 | 不變 |
| 動工 gate | 手動 loop 滿 14 天（2026-11-17，計畫 §1） | 不變；網站與本文屬發布基礎建設與規格，不算引擎 code |

## 1. 資料契約（Data Contract）

### 1.1 Front-matter

```yaml
layout: post
learning: true
kind: daily            # daily | deep-dive
day: 4                 # 唯一 key；deep-dive 同日可並存
phase: 1               # 主題表 1–10 階段
date: 2026-11-07 06:00:00
lang: zh-TW
reading_title: わたしの理由
level: N3              # Day 1–30 固定 N3；deep-dive 可 N3–N1
core_sentence: 理由がはっきりすれば、道は続く。   # 僅 daily
audio_url:             # 引擎期填 R2 URL；手動期可空
status: pass           # pass | flag | fail
generated_by: <model>@<gateway>
verified_by: <other-vendor-model>@<gateway>
cost_usd: 0.012
categories:
  - 日語學習
tags:
  - 抄寫日語100天
  - Day 4
description: "..."
```

欄位規則：
- `day` ＋ `kind` 是複合唯一 key；冪等與補賽都靠它（§4）。
- `kind: deep-dive` 為週日選用加值（8 單字＋4 文法，如 `source/_posts/gratitude-in-daily-life.md`），不計入五步驟 15–25 分鐘。
- `status` 三狀態對應三條發布路徑（§2 stage 6）。

### 1.2 檔名與 permalink

- daily 貼文：`source/_posts/day-XXX-<slug>.md`（day 補零三位，slug 用 romaji 或英文小寫連字號）。
- deep-dive 貼文：`source/_posts/deep-XXX-<slug>.md`（不與同日 daily 撞名）。
- permalink 為 `:title/`（＝檔名），**修正內容不改檔名**，舊連結不壞（計畫 §10 版本化）。
- 既有 `gratitude-in-daily-life.md` 為 deep-dive 範例，不帶 day，保留原樣。

### 1.3 內文模板（daily）

1. 導言 1–2 句 ＋ `<!-- more -->`
2. 日文本文（長度按計畫 §2 難度曲線）：每個漢字 `<ruby>漢字<rp>（</rp><rt>かんじ</rt><rp>）</rp></ruby>` 標音，每段後 blockquote `<span lang="zh-Hant" data-translation>` 中譯
3. 3 個單字卡（詞性、意思、常見搭配、例句＋中譯）
4. 1 個文法點（接續、核心語意、例句、相似比較）
5. 週日加寫作提示（5–8 句引導，對應計畫 §2 每週寫作）
6. 授權標示行（JMdict／EDRDG 姓名標示，計畫 §10）

## 2. 管線八 stage

| # | Stage | 做什麼 | 產物 | FAIL 處理 |
|---|---|---|---|---|
| 0 | 觸發 | `daily-loop.yml` cron（UTC；台北 06:00 ＝ 前日 22:00 UTC）或 `workflow_dispatch` 補跑 | run 啟動 | 延遲可容忍（§6.2） |
| 1 | 選題 | 讀 `aidd-docs/references/japanese-theme-rotation.md` 的 Day X row；掃 `_posts/` 關鍵字去重 | 當日主題＋核心句 | row 未填（Day 31＋）→ FAIL＋通知（Day 30 前補表） |
| 2 | 生成 | `AGENTS-teacher.md` persona ＋ 主題 row ＋ 難度/長度約束 ＋ §1 模板；預設 1 篇/天（批次參數保留，上限寫死） | draft markdown | 重試 1 次，再敗 FAIL |
| 3 | 標註 | fugashi＋unidic-lite → JMdict 交叉檢查 ruby 讀音 → pykakasi 備援 → 低信心標記 | annotation report | 讀音分歧 → FLAG |
| 4 | 驗證閘門 | rule QA ＋ 跨模型 verifier ＋ gold sample 回歸 | PASS／FLAG／FAIL | 判準見 §3 |
| 5 | TTS | 剝掉 ruby 的純文字 → Azure Neural ja-JP → mp3 → R2 `day-XXX.mp3` | `audio_url` | 重試 1 次；再敗則文先出（`audio_url` 留空＋FLAG），音後補 |
| 6 | 入庫＝發布 | PASS → commit＋push main（PAT）；FLAG → branch＋PR；FAIL → 不 commit | git commit／PR | 見 §4 §5 |
| 7 | 記錄 | cost、latency、verdict、FLAG 清單 append 進 `data/loop-log.jsonl`；失敗用 Actions email 通知 | log 一行 | — |

## 3. 驗證閘門判準

- **PASS**：rule QA 全過（長度上限、§1.3 章節齊全、ruby 完整、逐段中譯存在、N3 詞表比對）＋ 跨模型 verifier 無重大分歧 ＋ gold sample 回歸全綠 → 自動發布。
- **FLAG**：可疑但不確定（讀音低信心、verifier 分歧未達重大、自然度疑慮）→ PR 進 review queue，每天 3–5 分鐘人工處理。歷史語料 FLAG 率 >30% 先校準 verifier 再上線（計畫 §5 WS4）。
- **FAIL**：硬規則不過（超長、缺章節、讀音錯、gold sample 回歸紅）→ 不出門，通知。

## 4. 冪等與補賽

- 重跑同一天：`_posts/` 已存在該 `day＋kind` → no-op 結束（不重生成、不重 push）。
- R2 音檔以 `day-XXX.mp3` 為 key，同 key 覆寫等價。
- crash 半途重跑：從現存檔案推進度（post 在就跳過生成、音檔在就不再生），不留需人工清理的中間狀態。
- 補賽（「不錯過兩次」規則的事後補）：`workflow_dispatch` 帶 `day` 參數；僅允許過去日期且主題 row 已填。
- Day 100 之後 cron 自動 no-op（day > 100）。
- 日期一律以主題表的 Day→日期 對照為準，不用 runner 的 UTC 時鐘。

## 5. 失敗與降級

- 單篇失敗：跳過並通知，不影響隔天。
- 連 3 天失敗 → 降級手動模式（§7 流程或 chat 生成）。**內容供應不中斷優先於自動化**（計畫 §12）。
- 恢復條件：修復後手動 dispatch 連 2 天 PASS，再回全自動。

## 6. 已知坑（實作前讀一次）

1. 預設 `GITHUB_TOKEN` push **不會**觸發 `on: push` 的 `pages.yml`（GitHub documented behavior）。解法：loop 用 fine-grained PAT（repo secret，如 `LOOP_PAT`）push；替代方案是 daily-loop 自己 build＋deploy。
2. cron 全 UTC；scheduled workflow 常態延遲數分鐘、偶爾更久。台北 06:00 出文有緩衝，自用可接受。
3. repo 60 天無 activity 會停用排程；每天有 push，不會遇到。
4. `db.json`、`public/` 目前被 git 追蹤，屬 build artifact，加進 `.gitignore`（Pages 由 Actions 重建）。
5. 主題尚無 audio player；article layout 加 `<audio controls src=...>`，吃 `audio_url`。
6. hexo `future: false`：post `date` 用當日台北時間即會即時生成。

## 7. 手動期上軌道（Day 4–14，不寫引擎 code）

每天早上（生成＋review ≤10 分鐘）：
1. agent session 載入 `AGENTS-teacher.md` persona ＋ 主題表當日 row ＋ 本文 §1 契約。
2. 生成 `source/_posts/day-XXX-<slug>.md`；`generated_by` 記模型，`status` 由你確認後填 `pass`。
3. 你掃 ruby 與中譯 3–5 分鐘，改明顯錯。
4. commit ＋ push，站上即出文。
5. 五步驟照走；盲聽用免費 TTS，`audio_url` 引擎期再補。

14 篇樣本即引擎的 prompt 範本與 gold sample 來源。

## 8. 對應計畫 workstream（計畫 §5）

| WS | 本文落點 |
|---|---|
| 1 儲存與資料契約 | §1 ＋ R2 開桶 ＋ `.gitignore` 整理 |
| 2 標註 pipeline | §2 stage 3 |
| 3 選題與生成 | §2 stage 1–2（prompt 模板放 `aidd-docs/prompts/`） |
| 4 驗證閘門 | §3 |
| 5 TTS ＋ audio 樣板 | §2 stage 5 ＋ §6.5 |
| 6 Loop 編排 | §2 stage 0／6／7 ＋ §4 §5 |

## 9. 延後項（沿用計畫 §14）

Postgres＋pgvector（Phase 2 app 需要查詢；markdown 一支 import script 搬移）、圖卡 renderer（手動分享穩定 ≥4 週）、Graph API／Computer Use 發布自動化、多段速度 TTS。
