# 個人 AI 日語學習 App — 專案計畫（2026-11-07 v4：抄寫日語 100 天，repo 中心架構）

定位：**自己每天要用的學習工具，不是販售的產品或服務。** v3 起以「**抄寫日語 100 天**」為方法主幹 — 每日方法採用五步驟（盲聽 → 理解 → Shadowing → 抄寫 → 朗讀；方法概念不受著作權保護，內容全部 AI 原創）。v4 落地架構：內容由每日 Agent Loop 產製，markdown 直接入庫 jasdoit-daily repo（hexo ＋ GitHub Pages），**push 即發布**；音檔存 R2；Day X/100 同步發布到網站與 FB/IG（手動分享）。引擎規格見 `aidd-docs/plans/agent-loop-design.md`；儲存自本版起為 git ＋ R2（Postgres 延至 Phase 2）；標記 ESTIMATE 者為本版預估。

## 1. 目標與成功定義

三個目標，按優先序：
1. **完成抄寫日語 100 天** — Day 1 = 2026-11-04，Day 100 ≈ 2027-02-11。每天五步驟 15–25 分鐘，不中斷。
2. **可驗收的語言進步** — 目標 N3；每月一次 JLPT 模擬考（考古題）追蹤。
3. **內容資產** — 100 篇 AI 原創 N3 短文（植物成長 10 階段弧線，主題表見 `aidd-docs/references/japanese-theme-rotation.md`）完整入庫 jasdoit-daily repo 並同步發布於 GitHub Pages，就是一份現成的百日課程（Phase 2 app 的原料）。

**北極星指標：Day X/100 完成天數 ＋ WELD**。一天成立 ＝ 完成五步驟（§2）。**「不錯過兩次」規則**：斷 1 天不懲罰、不補賽，隔天繼續；連續斷 2 天才算真的斷 — 習慣的連續性比完美主義重要。

**品質哲學（無審校版）**：分層信任、工具驗證、事後修正，驗證內建於每日 loop（§3、§4）。

前置條件（沒有 NO-GO，只有先做/後做）：
1. [ ] **手動 loop 跑滿 14 天（Day 1–14，第一個 gate）** — 不寫引擎 code（網站與文件屬發布基礎建設，不在此限）。每天早上：在 repo 內以 `AGENTS-teacher.md` persona 生成 1 篇當日主題短文（主題照 `aidd-docs/references/japanese-theme-rotation.md`；手動流程見 agent-loop-design.md §7）→ 免費 TTS 出音檔 → 五步驟走完 → 拍下抄寫頁＋3 個單字卡，貼文標「Day X/100」。全程 ≤30 分鐘。14 天後三問：(a) 我真的每天會做嗎；(b) 內容我自己想讀嗎；(c) 哪種分享格式有人互動。**任一不成立 → 不蓋引擎**；全部成立 → 14 篇真實樣本即引擎規格與 gold sample 來源。
2. [ ] **時間盤點** — 引擎 56–82 h ＋ app 44–64 h ≈ 100–146 h ESTIMATE（v4 儲存簡化、圖卡暫緩後）。引擎期落在 Day 15–50、app 期落在 Day 50–100，**期間每日五步驟照常進行** — 100 天練習不等人、不中斷，工程在練習進行中逐步接管（手動 → 引擎 → app）。
3. [ ] **LLM bake-off（1 天，與手動 loop 並行）** — 10 項真實任務（含「生成 N3 勵志短文＋zh-TW 譯文」與「verifier 挑錯」兩種角色）比較 gpt-6-luna vs Claude Haiku 5.5 vs DeepSeek V4 Flash；選定首選＋備援，全部透過 gateway 抽象層。
4. [ ] **版本鎖定（1 天，與手動 loop 並行）** — lockfile 鎖定 fugashi ＋ unidic-lite 確切版本；30 篇樣本 tokenization 目視檢查。
5. [ ] **gold sample 50 題（與手動 loop 並行）** — 自建讀音/QA 案例對字典核對，作為驗證閘門回歸基準。

Phase 地圖（晚間與週末基準；Day = 抄寫練習天）：

| Phase | 時程 | 出口條件 |
|---|---|---|
| 0 — 手動五步驟 ＋ 準備 | Day 1–14（2026-11-04 → 11-17） | 手動 loop 滿 14 天且三問全過；bake-off、版本鎖定、gold sample 就緒 |
| 1 — 內容引擎（含 TTS）：Agent Loop ＋ git/R2 ＋ GitHub Pages 發布 | Day 15–50（3–5 週） | 連續 7 天 loop 全自動（生成→驗證→TTS→commit→push→Pages 部署）；每日手動工作 <5 分鐘 |
| 2 — 學習 app：五步驟引導 ＋ SRS ＋ 週寫作回饋 | Day 50–100（5–8 週） | 用 app 完成五步驟連續 14 天；首次月度模擬考有數據 |
| 3 — 發音評分（選用） | Day 100 之後 | 自行決定 |

發布不是獨立 phase：GitHub Pages 網站自 Day 1 隨 repo 上線、每日 push 出文；FB/IG 手動分享自 Day 1 開始，自動化（Graph API／Computer Use）是選用升級（§6、§14）。

## 2. 產品定義：每日五步驟（日文版）

| 步驟 | 做法 | 日文版注意點 |
|---|---|---|
| 1. 盲聽（Ear-only） | 不看文字，播放音檔 1–2 遍，只用耳朵 | 適應日語語速與節奏；聽不懂是正常的，是測量不是考試 |
| 2. 閱讀與理解（Read for Meaning） | 看文字＋ruby＋zh-TW 譯文，搭配音檔再聽 1 遍；點擊查詢不懂的詞 | 查過的詞餵 SRS（app 期）；沒聽懂的詞在這步對答案 |
| 3. Shadowing（影子跟讀） | 不看（熟練）或微看（初期）文字，落後音檔 1–2 個字同步跟讀 | 注意促音（っ）、長音（ー）、pitch accent、句尾語氣（です/ます 的收尾） |
| 4. 抄寫（Handwriting） | 紙本筆記本，一字不漏親手抄寫 | 漢字筆畫與送り仮名是日文版的「第三人稱單數加 s」；助詞（は/が/に/で）的細節靠手寫內化 |
| 5. 朗讀（Read Aloud） | 看自己抄的文字，大聲、有感情朗讀 1–2 遍 | 視覺＋觸覺＋聽覺（自己的聲音）完成記憶閉環 |

每天 15–25 分鐘：五步驟約 15 分鐘 ＋ SRS 複習約 5 分鐘（SRS 自 app 期加入；手動期查過的詞記在筆記本即可，不做電子化）。

**每週日 ＋1 篇原創寫作**：應用本週抄過的句型寫 5–8 句，餵 AI 回饋（1–2 個問題 ＋ 改寫建議）。模仿（日日抄寫）與創作（週週寫作）分工 — N3 階段先吸收模式、再產出。

內容規格：100 篇 AI 原創 N3 短文，主題表見 `aidd-docs/references/japanese-theme-rotation.md` — 以植物成長 10 階段弧線（種をまく → 毎日の水やり → 根を張る → 風に立つ → 陽を仰ぐ → 枝を広げる → 蕾が膨らむ → 花が咲く → 実を結ぶ → 次の種）每 10 天一階段；難度曲線對齊弧線（Day 1–30 偏易 250–300 字 → Day 31–70 標準 300–400 字 → Day 71–100 偏難 ≤450 字）；每主題一句「核心句」作為記憶錨點與朗讀收尾（圖卡主視覺暫緩）。每篇內含：日文本文、ruby 標註、zh-TW 譯文、3 個單字卡、1 個文法點、TTS 音檔、（週日用）寫作提示。完整資料契約（front-matter、檔名、內文模板）見 agent-loop-design.md §1。週日可加發 8 單字＋4 文法深讀版（kind: deep-dive，如 `gratitude-in-daily-life.md`），屬選用加值，不佔每日 15–25 分鐘。

| 範圍內 | 範圍外 |
|---|---|
| 五步驟每日 session（app 引導；抄寫在紙上） | App 內手寫辨識（觸覺在紙本，app 不碰） |
| TTS 音檔（手動期用免費工具；引擎期 Azure 預生成） | 發音評分／ASR（Day 100 後再議，§8） |
| 點擊查詢 ＋ ruby ＋ SRS（app 期） | 每日原創寫作回饋（改每週一篇） |
| 週寫作回饋（1–2 問題 ＋ 改寫，second-pass） | 多段速度 TTS（暫緩；若 shadowing 實測需要慢速，可提前重啟） |
| Day X/100 分享：手寫抄寫照 ＋ 單字卡 | FB 個人檔案／社團 API 自動化（不存在） |
| 內容庫：git ＋ R2（repo markdown ＋ 音檔，一份內容多種視圖） | 第三套儲存系統；多使用者、金流、原生 App |

## 3. 品質保證（無審校）

分層信任 — 不同內容用不同機制，不對 AI 生成內容做假信賴：

| 內容 | 信任基礎 | 機制 |
|---|---|---|
| 讀音/詞義 | 高（決定論式） | fugashi+unidic-lite → JMdict 交叉檢查 → pykakasi 備援；gold sample 50 題對答案一次 |
| zh-TW 譯文 | 中 | 跨模型 verifier 複查；你自己就是 zh-TW native speaker，讀不順一眼可辨 |
| 文法說明 | 低 | (a) prompt 限定只講該篇實際出現的文法點、保守措辭；(b) 跨模型 verifier 挑錯，分歧者標記；(c) 對照權威 spot check（新完全マスター N3、Tae Kim、jisho.org） |
| 勵志短文本文 | 中 | Rule QA（N3 詞彙/文法清單比對、長度上限）；verifier 檢查自然度；你每天親讀親抄，異常句自己會有感 |
| 理解題/答案 key | 中 | Rule QA（正確答案必須能在文中定位）＋ verifier 複查 |
| TTS 音檔 | 高 | Azure Neural ja-JP 預生成，機器輸出不需人審 |

**驗證內建於 loop（§4 第 4 步）**：每日產出只有 PASS／FLAG／FAIL — PASS 自動入庫＋進發布佇列；FLAG 進 review queue（每天 3–5 分鐘）；FAIL 不出門。

三個免費的品質迴圈：
1. **自己就是 QA** — 每天親讀親抄就是最強的錯誤偵測器（抄寫會放大任何不自然）；錯誤 → error log → 新版本 → 改進 prompt。
2. **社群當免費審校** — Day X/100 貼文收到糾正留言 → error log → 修正。
3. **模擬考當最終驗收** — 每月一次 JLPT 考古題；指標是「分數有進步」。

UI 原則：高信任（字典讀音、TTS）與低信任（AI 講解、譯文）視覺區分 — AI 生成內容一律帶「AI 生成，僅供參考」標示。

## 4. 每日 AI Agent Loop（內容引擎，Phase 1）

觸發：每天固定時間 cron — GitHub Actions scheduled workflow（`daily-loop.yml`，免費、免維運；UTC 排程，台北 06:00 出文）。**確定式管線**，不是讓 agent 自由發揮。完整規格（stage 表、驗證判準、冪等、降級、已知坑）見 `aidd-docs/plans/agent-loop-design.md`。

八個步驟（每步產物落 repo；任一步 FAIL，該篇不出門）：
1. **選題**：依 Day X 從主題表（`aidd-docs/references/japanese-theme-rotation.md`）對應（Day → 階段 → 當日主題與核心句），附關鍵字去重；日期以主題表為準，不用 runner 的 UTC 時鐘。
2. **生成**：日文本文 ＋ zh-TW 譯文 ＋ 3 單字卡 ＋ 1 文法點 ＋ 寫作提示（預設 1 篇/天，批次參數保留，上限寫死）。
3. **標註**：fugashi+unidic-lite → token span → JMdict 交叉檢查 → pykakasi 備援 → 專有名詞/低信心標記。
4. **驗證閘門**：rule QA ＋ 跨模型 verifier ＋ gold-sample 回歸 → PASS／FLAG／FAIL。
5. **TTS**：Azure Neural ja-JP 生成音檔 → R2（免費額度每月 50 萬字元，100 篇遠遠用不完）。
6. **入庫＝發布**：markdown post commit 進本 repo — PASS → push main 自動觸發 Pages 部署；FLAG → 開 PR 進 review queue；FAIL → 不 commit。音檔 URL 記於 front-matter `audio_url`。
7. **分享準備**：預寫 caption ＋ 站上文章 URL → 手動一鍵分享（手寫抄寫照由你自己拍，最有人味）；圖卡 renderer 暫緩（§14），需要時以文章截圖代替。
8. **記錄**：成本、latency、驗證結果、FLAG 清單 → `data/loop-log.jsonl`；失敗即通知（Actions email；LINE Notify 選用）。

冪等與失敗處理：Day X 檔案存在即 no-op，隔天重跑不重複；斷日以 `workflow_dispatch` 帶 day 參數補跑。loop 連續失敗 → 降級手動模式（repo 內手動生成，agent-loop-design.md §7）— **內容供應不中斷優先於自動化**（§12）。

你每天的角色：3–5 分鐘 FLAG 清單 ＋ 15 分鐘五步驟 ＋ 5 分鐘分享。

## 5. Phase 1 — 內容引擎 workstreams

| # | Workstream | 時數（ESTIMATE） | 可驗證的完成狀態 |
|---|---|---|---|
| 1 | 儲存與資料契約：front-matter contract（agent-loop-design.md §1）＋ R2 bucket（音檔）＋ `.gitignore` 整理（db.json、public/） | 2–4 | 一篇樣本 post 含 `audio_url` 入 repo 並部署上線 |
| 2 | 標註 pipeline：fugashi+unidic-lite → token span → JMdict 交叉檢查 → pykakasi 備援 → flags | 20–28 | 30 篇語料讀音與 gold sample 一致率 ≥99%（風險最高，最先做） |
| 3 | 選題與生成：主題表（`aidd-docs/references/japanese-theme-rotation.md`；Phase 1–3 已定義，Phase 4–10 於 Day 30 前補完）＋ prompt 模板（含譯文與 N3 難度約束）＋ gateway 客戶端 | 10–14 | 連續 10 天生成穩定、單篇成本可預期 |
| 4 | 驗證閘門：rule QA ＋ 跨模型 verifier ＋ gold 回歸，PASS/FLAG/FAIL 狀態機 | 10–14 | 30 篇歷史語料 FLAG 率 <30% ESTIMATE；>50% 先校準 verifier 再上線 |
| 5 | TTS ＋ audio 樣板：Azure 音檔批次生成 → R2；article layout 內嵌 `<audio>` 播放器 | 6–10 | 音檔可串流播放，文章頁內嵌 |
| 6 | Loop 編排：`daily-loop.yml`（cron ＋ dispatch 補跑）、PAT push、冪等、失敗通知 | 8–12 | 連續 7 天無人工介入跑完 |

合計：56–82 h ESTIMATE（v4 儲存簡化、圖卡暫緩後；v3 為 66–94 h）。

## 6. 發布策略（FB/IG：手動先行，Day X/100 敘事）

**日常（手動，自 Day 1 起）**：引擎產出「caption ＋ 站上文章 URL（https://xfalcons.github.io/jasdoit-daily/）」→ 你加上自己的手寫抄寫照，貼到 FB/IG，每天 ≤5 分鐘；圖卡（Day X/100 PNG）為暫緩升級（§14），需要時以文章截圖代替。

Day X/100 百日挑戰是比單篇圖卡更強的社群格式：進度敘事（Day 7/100）、手寫的真實感、可預期的完結篇（Day 100）。貼文可誠實標註「方法啟發自《抄寫英語的奇蹟》，內容為 AI 原創日文」— 致敬誠實且加分。

自動化（選用；觸發條件：手動穩定 ≥4 週且開始覺得煩）：
1. **Graph API**（正規路）— FB 專頁 ＋ IG Professional，Development mode 自用免 app review；只能發專頁/IG。
2. **Agent ＋ Computer Use**（研究選項）— 可操作個人檔案/Dcard 等無 API 平台，但：平台 ToS 多禁止自動化個人帳號（帳號風險自負）、UI 改版 selector 就壞；僅限「agent 備好、你按送出」的半自動。

建議順序：手動 → Graph API → Computer Use 僅用於無 API 平台且永遠半自動。

回饋閉環：糾正留言 → error log（§10）→ 修正內容（新版本）→ 改進 prompt。

## 7. Phase 2 — 學習 app workstreams（Day 50–100）

app 是五步驟的引導器，不是手寫辨識器。與發布共用同一份內容庫（v4：jasdoit-daily repo 的 markdown；Phase 2 需要查詢時才移植 Postgres）。

| # | Workstream | 時數（ESTIMATE） | 可驗證的完成狀態 |
|---|---|---|---|
| 1 | PWA shell ＋ API（先讀 hexo 產出的靜態內容；需要 SRS 查詢時才上 Postgres）＋ Day X/100 進度首頁 | 6–10 | 手機 ＋ 桌機開啟當日文章與音檔 |
| 2 | 五步驟引導模式：盲聽播放器 → 閱讀器（ruby 切換、點擊查詢）→ shadowing 播放器 → 抄寫計時/完成勾選（紙本）→ 朗讀完成 | 14–20 | 五步驟在 app 引導下 15 分鐘走完；查詢餵入 SRS |
| 3 | SRS 引擎：固定 1/3/7/14/30、FSRS-migratable log schema | 8–12 | 模擬 14 天歷史，到期佇列正確 |
| 4 | 週寫作回饋：gateway、1–2 問題 ＋ 改寫、second-pass 檢查 | 8–12 | 一篇自己的週寫作得到合理回饋 |
| 5 | Dashboard：Day X/100、WELD、SRS 到期完成率、FLAG 清單 | 8–10 | 能回答 §12 全部 KPI |

合計：44–64 h ESTIMATE。

## 8. Day 100 之後 — 發音評分（選用）

五步驟的 shadowing ＋ 朗讀已經提供口說練習；ASR 發音評分是加值，不是前置依賴。做法沿用 tech-assessment：gpt-transcribe（務必 `language="ja"`）＋ Azure ja-JP 評估、只顯示平滑化 word-level 分數（phoneme-level 會被單一錯誤拖累；ProsodyScore 僅 en-US，絕不碰）；transcript 絕不當 ground truth。上線前先以 50 段自己的錄音（含已知 促音/長音/pitch 錯誤）做 1–2 天 spike 驗證鑑別度。

## 9. 架構與儲存

元件：GitHub Actions（cron，`daily-loop.yml`）→ loop（生成／標註／驗證／TTS）→ **git（jasdoit-daily repo；markdown 內容庫，版本化＝commit 歷史）＋ R2（音檔）** → push main → GitHub Pages（hexo）＝現行視圖；FB/IG 手動分享；未來視圖：學習 PWA（先讀 hexo 靜態產出，需要 SRS 查詢時上 Postgres＋pgvector）/ mobile app（同一 API）。

儲存原則：結構化內容與 log → git（`source/_posts/`、`data/loop-log.jsonl`）；二進位資產（音檔）→ R2；日後查詢／向量 → Postgres＋pgvector（Phase 2 評估，markdown 一支 import script 匯入）。**兩個儲存系統是上限（git ＋ R2）。**

| 關注點 | 選擇（沿用 tech-assessment） | 否決／暫緩 |
|---|---|---|
| Tokenizer | fugashi + unidic-lite，版本鎖定 | SudachiPy 0.7.*；SudachiPy+ipadic（錯誤配對） |
| 讀音 | token 讀音 → JMdict 交叉檢查 → pykakasi 備援；專有名詞標記 | pykakasi 單一來源（會誤讀人名） |
| 字典層 | JMdict（CC BY-SA 4.0；分享附姓名標示） | RAG/vector store（pgvector 保留日後） |
| LLM | 便宜等級 behind gateway；生成與 verifier 不同供應商 | GPT-4o 等級（過貴） |
| TTS（引擎期） | Azure Neural ja-JP，預生成、快取、單一速度；免費額度內 $0 | 自架 Style-Bert-VITS2；多段速度（shadowing 實測需要再重啟） |
| STT/發音評分（Day 100 後） | gpt-transcribe（`language="ja"`）／Azure 評估 word-level | 自架 Whisper |
| 儲存 | git（repo 即內容庫）＋ R2（音檔）；Postgres 延至 Phase 2 | 第三套儲存；過早分庫 |
| Loop runtime | GitHub Actions scheduled workflow（jasdoit-daily repo 內） | 自架 worker/queue |
| SRS | 固定 1/3/7/14/30，FSRS-migratable log schema | 樣本太小不調校 FSRS |
| Client | PWA；錄音（若做）僅限 turn-based | 原生 App（§14） |

## 10. 內容庫治理與 QA

- **版本化**：內容入庫後不可變；修正 = 新 commit（不改檔名，permalink `:title/` 穩定），已分享的舊文連結不壞。
- **錯誤處理**：error log 三來源（自己發現／社群糾正／verifier 分歧）→ 新版本 → 已公開內容以留言更正。
- **月結**：錯誤率、FLAG 率、每篇成本；錯誤率 >2% ESTIMATE → 收緊 prompt／提高 verifier 門檻。
- **gold sample**：Phase 0 自建 50 題，loop 每日回歸基準。
- **授權紀律**：**方法不受著作權保護（著作權只保護表達，思想/方法/概念除外），五步驟與「100 天 × 10 段」通用格式可自由採用；主題表為自創（植物成長弧線，見 `aidd-docs/references/japanese-theme-rotation.md`），與原書主題、措辭不重複；《抄寫英語的奇蹟》的內文、譯文不可抄，書名不可混淆性使用** — 公開分享自主命名（「抄寫日語 100 天」）＋ 誠實致敬即可；未來若商業化（§12），品牌名必須完全原創。內容面：AI 原創；不複製『いろどり』/NHK/青空文庫；JMdict/EDRDG 姓名標示。

## 11. 成本（個人版；單價於 2026-11-02 驗證）

| 項目 | 金額 |
|---|---|
| 手動 loop 期（Day 1–14） | $0（chat 訂閱 ＋ 免費 TTS 工具） |
| 引擎 LLM（1–3 篇/天含 verifier） | ~$0.5–2/月 |
| TTS | $0（Azure 每月 50 萬字元免費額度內） |
| R2 ＋ GitHub Pages | $0（R2 免費層；Postgres 延至 Phase 2 再議） |
| 發布（手動） | $0 |
| Day 100 後加發音評分 | ＋ ~$1/月 |

合計：**~US$1–3/月**。每季瞄一眼單價即可。

## 12. KPI 與調整準則（自用版）

KPI：Day X/100 完成率（目標 ≥90% 的天數完成五步驟）；WELD；每週寫作達成率；每月模擬考成績；loop 成功率（≥95%）；FLAG 率；每篇成本；分享互動與糾正回饋數；SRS 到期完成率。

調整準則（自用沒有 kill，只有改）：
- 斷 2 天（「不錯過兩次」規則破了）→ 不重罰；把五步驟砍到最小版本（盲聽 1 遍 ＋ 抄寫）先恢復連續，再恢復全套。
- 手動 loop 前 14 天斷 ≥3 天 → 不蓋引擎；先解決時間/題材。
- 單日五步驟經常 >25 分鐘 → 縮短文章（生成時長度上限往下調），**不是砍步驟** — 步驟是方法的本體。
- 連續 2 個月模擬考無進步 → 提高輸出比重（週寫作加長）或提前考慮發音評分。
- Loop 連 3 天失敗 → 降級手動生成，修好再恢復。
- 分享連續 2 週造成壓力 → 降頻或暫停；分享讓位給習慣。

**升級回商業版的觸發條件**（不設日程）：分享累積 ≥1,000 追蹤且持續有人詢問 → 重跑 docs/researchs/ 市場評估；屆時 100 篇百日課程就是現成產品原型，但品牌與內容需全面原創化檢查（§10）。

## 13. 風險登錄表（前 8 項）

| # | 風險 | 緩解 | 觸發點 |
|---|---|---|---|
| 1 | **100 天中途斷裂**（第一死因） | 「不錯過兩次」規則；Day X/100 公開敘事本身就是續航機制；最小版本五步驟備援 | 連續斷 2 天 |
| 2 | **為不存在的習慣蓋工廠** | 手動 14 天 gate 通過才動工；workstream 時數硬上限 | gate 前就開始寫引擎 code |
| 3 | 五步驟時間膨脹 → 拖垮習慣 | 生成時嚴格控管 N3 難度與長度（按 §2 難度曲線設上限）；超時縮文不砍步驟 | 連續一週單日 >25 分鐘 |
| 4 | AI 內容有錯而公開分享（含譯文、勵志內容的自然度） | 分層信任（§3）＋ loop 驗證閘門；每日親讀親抄；社群糾正；AI 內容標示 | 月錯誤率 >2% 或公開重大指正 |
| 5 | 平台自動化風險（ToS／帳號／selector） | 手動為本；Graph API 正規路；Computer Use 僅半自動 | 帳號驗證/警告；selector 連壞 2 次 |
| 6 | Ruby/token 讀音 UX 卡關 | WS2 前置、gold sample、低信心標記 | 30 篇語料一致率 <99% |
| 7 | 分享變成負擔 | 預寫 caption、一鍵分享、自由降頻 | 連續 2 週壓力 |
| 8 | LLM 價格/淘汰；loop 成本膨脹 | gateway 抽象、備援鏈；每日篇數上限寫死、cost metering | 首選模型淘汰公告；單月成本 >2 倍 |

## 14. 明確暫緩清單

| 暫緩項目 | 重啟條件 |
|---|---|
| Postgres＋pgvector 內容庫（自 git 移植） | Phase 2 app 需要 SRS 查詢／進度資料時 |
| 圖卡 renderer（Day X/100 PNG） | 手動分享穩定 ≥4 週且文章截圖不夠用 |
| 發音評分（gpt-transcribe ＋ Azure） | Day 100 之後自行決定（§8） |
| 多段速度 TTS | shadowing 實測確實需要慢速（可能提前） |
| 發布自動化：Graph API | 手動穩定 ≥4 週且覺得煩 |
| 發布自動化：Agent ＋ Computer Use | Graph API 上線後仍有不便平台；僅半自動 |
| Mobile app | Web site 之後且 PWA 不夠用 |
| 多使用者／商業化 | §12 升級條件成立；品牌全面原創化檢查 |
| FSRS 排程 | 複習 log ≥3 個月 |
| RAG/pgvector | 語料超出 100 篇規模 |
| 新聞線／經典作品線／全級數（N4–N5、N1） | N3 模擬考達標後 |
| 手寫辨識（OCR）、 JLPT 模擬試題產生器 | 實際需求出現 |

---

方法：v4（2026-11-07）由 v3 改寫，落地架構更新為 repo 中心：jasdoit-daily（hexo ＋ GitHub Pages）即內容庫與發布管線（git ＋ R2，push 即發布），引擎規格抽至 `aidd-docs/plans/agent-loop-design.md`，Postgres 延至 Phase 2，圖卡 renderer 暫緩，主題表路徑修正為 `aidd-docs/references/japanese-theme-rotation.md`。v3（2026-11-03）由 v2（同日，Agent Loop ＋ Postgres/S3 內容庫版）改寫，納入「抄寫日語 100 天」方法層：五步驟每日 session、100 篇/10 主題內容包、TTS 提前至引擎期、原創寫作改每週回饋、Day X/100 分享敘事。學習方法概念（五步驟、100 天結構、主題輪動）不受著作權保護；所有內容 AI 原創。v1（個人自用版）與商業版（2026-11-02，基於 codex-report.md、zai-report.md 及三份 swarm 評估）之結論沿用於技術選項；標記 ESTIMATE 者為本版預估。原始評估文件保留於 docs/researchs/。
