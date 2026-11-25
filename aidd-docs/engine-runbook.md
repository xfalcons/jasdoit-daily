# 引擎操作手冊（daily-loop engine runbook）

對象：operator（你）。引擎 = `tools/daily-loop.py` ＋ `tools/generate-post.py`／`verify-post.py`／`tts-upload.py`／`loop_support.py` ＋ `.github/workflows/daily-loop.yml`。規格見 `aidd-docs/plans/agent-loop-design.md`（含 2026-11-21 解釋備註）；流程架構與各階段 actor（誰寫初稿、誰校稿、誰審批、誰訂正）見 `aidd-docs/engine-loop-architecture.md`。

核心安全屬性：dry-run 一律零寫入（只在 temp sandbox 跑全流程）；live 才會 commit／push／上傳 R2；引擎永不改已發布的貼文（同 day 已存在 → noop）。

## 0. 詞彙

- **Arming**：`ENGINE_LIVE` repo variable ＝ `"true"` 時 schedule 才會 live；dispatch 只有勾 `live` input 才 live；其他一律 dry-run（含本機不帶 `--live` 的執行）。
- **Verdict**：PASS → push main 直接發布；FLAG → `loop/day-NNN` branch ＋ PR 進 review queue（verifier findings 直接附在 PR body 的 `## verifier findings` 區塊，不用翻 Actions log）；FAIL → 不出內容，只 commit log（Ruling B），Actions email 會通知。判準（2026-11-25 校準）：FLAG 只留會讓學習者學錯的內容（誤導性解說、中文詞誤標日語讀音、同形異義混淆、示範錯誤用法）；JLPT 級別疑慮與語感建議只進 findings，不影響 verdict。
- **Degrade**：live 連 3 天 FAIL（主題表連續日期）→ `data/engine-state.json` 記 `degraded: true`，之後 schedule 一律強制 dry；手動 dispatch 連 2 天 PASS 才恢復。

## 1. 本機 `.env`（gitignored；與 Actions secrets 同名）

在 repo 根目錄建立：

```
R2_ACCESS_KEY_ID=<Cloudflare R2 S3 API token access key>
R2_SECRET_ACCESS_KEY=<同組 secret key>
R2_ENDPOINT=https://55fb12a63c05303d4072271e85c3fc28.r2.cloudflarestorage.com
R2_BUCKET=jasdoit-daily
R2_PUBLIC_BASE=<啟用 public access 後的公開 base URL，無 trailing slash>
OPENROUTER_API_KEY=<OpenRouter key>
LOOP_PAT=<fine-grained PAT，見 §3>
# 選填覆寫（預設 GEN z-ai/glm-5.3、VERIFY openai/gpt-6.1-sol；每個 verifier 廠商必須與 GEN 不同）。
# 留空等同不設：空值不會覆蓋預設。要覆寫才取消註解並填「非空」值。
#GEN_MODEL=anthropic/claude-opus-5.5
#VERIFY_MODEL=openai/gpt-6.1-sol
#VERIFY_MODEL2=z-ai/glm-5.3
```

工具會自己讀 `.env`（既有的環境變數優先），值永不印出。

## 2. R2 public access（音檔公開 URL）

1. Cloudflare dashboard → R2 → bucket `jasdoit-daily` → Settings → Public Access。
2. 啟用 `r2.dev` subdomain（或綁自訂網域，較建議）。
3. 把公開 base URL 填進 `.env` 的 `R2_PUBLIC_BASE`（例：`https://pub-xxxx.r2.dev`），並設成 repo variable（§4）。

## 3. LOOP_PAT（fine-grained PAT scopes）

GitHub → Settings → Developer settings → Fine-grained tokens → Generate：

- Repository access：only `jasdoit-daily`。
- Permissions：**Contents: Read and write**（push）＋ **Pull requests: Read and write**（開 PR）。不需要其他 scope。
- 原因：預設 `GITHUB_TOKEN` push 不會觸發 `pages.yml`（spec §6.1）。引擎 push 走 `git -c http…extraheader`（header 在 process 內組，PAT 不進 argv／log），開 PR 走 `GH_TOKEN`。

## 4. GitHub secrets 與 variables（六個 secret ＋ 兩個 variable）

```
# secrets 一律走 stdin，值不進 argv／shell history（--body 只給非機密的 variable）
printf %s "$R2_ACCESS_KEY_ID"     | gh secret set R2_ACCESS_KEY_ID
printf %s "$R2_SECRET_ACCESS_KEY" | gh secret set R2_SECRET_ACCESS_KEY
printf %s "https://55fb12a63c05303d4072271e85c3fc28.r2.cloudflarestorage.com" | gh secret set R2_ENDPOINT
printf %s "jasdoit-daily"         | gh secret set R2_BUCKET
printf %s "$OPENROUTER_API_KEY"   | gh secret set OPENROUTER_API_KEY
printf %s "$LOOP_PAT"             | gh secret set LOOP_PAT
gh variable set R2_PUBLIC_BASE --body "https://pub-xxxx.r2.dev"
# 模型覆寫也是 repo variables（workflow 傳 GEN_MODEL／VERIFY_MODEL／VERIFY_MODEL2 給引擎）：
gh variable set GEN_MODEL --body "anthropic/claude-opus-5.5"
gh variable set VERIFY_MODEL2 --body "z-ai/glm-5.3"
# ENGINE_LIVE 先不設（未設＝disarmed）；arming 見 §6
```

## 5. 第一次 dry dispatch（驗整套管線，零風險）

Actions → Daily loop → Run workflow →（不勾 live）→ Run。或本機：

```
LOOP_NOW=2026-11-22T07:00:00+08:00 \
GEN_FAKE_PATH=/Users/xfalcons/dev/xfalcons/jasdoit-daily/tests/fixtures/day-019-chiisana-shouri-wo-iwau.md \
VERIFY_FAKE_PATH=/Users/xfalcons/dev/xfalcons/jasdoit-daily/tests/fixtures/verify-pass.txt \
uv run --python 3.13.7 \
  --with edge-tts==7.3.1 --with boto3==1.44.9 --with fugashi==1.5.2 --with unidic-lite==1.0.8 \
  --with jamdict==0.1a11.post2 --with jamdict_data==1.5 --with pykakasi==2.3.0 \
  python tools/daily-loop.py --dry-run
```

fake path 用絕對路徑：stage tools 的 cwd 是 temp sandbox，相對路徑會指進 sandbox 裡不存在的 fixtures（引擎會把相對 fake path 以 repo root 解析成絕對路徑，但寫絕對路徑最不容易出錯）。版本 pin 對齊 `tools/requirements-engine.txt`。

檢查：transcript 依序出現 select→generate→annotate→gate→verify→tts→finalize；TTS 印出 `key audio/day-0NN.mp3` 與 public URL 但 `upload skipped (dry-run)`；結尾 `DISARMED` banner；`git status` 乾淨；exit 0。真模型 dry-run（不設 fake env、`.env` 有 key）同理，只差在會打 OpenRouter。

## 6. Arming（`ENGINE_LIVE`）與第一天 live

```
gh variable set ENGINE_LIVE --body "true"
```

- 隔天 03:00（台北）cron 自動跑 live；post 蓋當日 03:00 時戳，引擎 push 觸發的 `pages.yml` build 直接上架（約 03:10）。想立即驗：Actions → Daily loop → Run workflow → 勾 `live`。
- 第一天 live 後檢查：`git log` 出現 `content: day NNN post (engine pass)` ＋ `loop: day NNN pass ledger` 兩個 commit；`pages.yml` 被觸發；R2 bucket 出現 `audio/day-NNN.mp3`；文章頁有 `<audio controls preload="none" src="…">`。
- FLAG 不會 push main：檢查 PR `loop: day NNN FLAG`，人工 review 後 merge。

要 disarm：`gh variable set ENGINE_LIVE --body "false"`（或 `gh variable delete ENGINE_LIVE`）。

### 6.1 只走 PR（engine live，但每天的文章都進 PR）

PASS 也開 PR、不 push main，適合剛上線的前幾天：你每天看到的是一篇已完成讀音驗證、gate、音檔與 verifier 審查的完整 PR，花 3–5 分鐘看完就 merge。

```bash
gh variable set AUTO_PUSH_N3 --body "false"
```

夠了之後恢復自動發布：`gh variable delete AUTO_PUSH_N3`。未設或不是 `"false"` 時，PASS 直接 push main。

## 7. 補賽（backfill）與手動補音（補音不改正文）

- 補賽過去缺文：Actions → Daily loop → Run workflow → `day` 填過去天數、勾 `live`。僅限主題表已填且「該 day 貼文不存在」的日子（存在 → noop，引擎永不改歷史）。
- 補音（`tts_failed` FLAG 之後）：重跑該天 TTS → 只 patch `audio_url` → 重跑 annotate（freshness）→ commit：

```
uv run --with edge-tts==7.3.1 --with boto3==1.44.9 \
  python tools/tts-upload.py source/_posts/day-0NN-<slug>.md
# 用印出的 URL patch 該 post front-matter 的 audio_url（正文與檔名不動）
uv run --with fugashi==1.5.2 --with unidic-lite==1.0.8 --with jamdict==0.1a11.post2 \
  --with jamdict_data==1.5 --with pykakasi==2.3.0 \
  python tools/annotate-post.py source/_posts/day-0NN-<slug>.md
git add source/_posts/day-0NN-<slug>.md data/annotations/day-0NN-<slug>.json
git commit -m "content: day NNN audio backfill"
git push
```

## 8. 降級與恢復

- 連 3 天 FAIL → log 出現 `DEGRADE: 3 consecutive failures — switch to manual mode`，`data/engine-state.json` 記 `degraded: true`。之後 schedule 全部強制 dry（transcript 會說明原因）。
- 恢復：修好問題後，手動 dispatch 勾 `live` 連續 2 天 PASS → 自動恢復（log 出現 `DEGRADE: recovered`）。
- 手動期照 spec §7：載入 `AGENTS-teacher.md` ＋主題表手動生成，內容供應不中斷優先。

## 9. 版本鎖定（gold byte-identity）

CI pins Python `3.13.7` ＋全部七個引擎套件的確切版本（`tools/requirements-engine.txt`，含 `edge-tts`／`boto3`）。基準版本以任一 `data/annotations/*.json` 的 `sources` 為準：

```
python3 -c "import json;print(json.load(open('data/annotations/day-018-nakama-no-sonzai.json'))['sources'])"
```

升版會改 gold bytes 屬預期紅燈（regression by design）：先本機重跑 annotate 全 corpus 比對，確認只有版本欄位差異才同步 workflow pins 與 baselines。

## 10. 故障速查

| 症狀 | 處置 |
|---|---|
| dispatch 帶 `day` 卻報 usage error | 該天還沒過去（台北）或 >100；補賽只收過去日 |
| `pending_pr` FAIL | 該天已有開著的 PR（branch `loop/day-NNN`）；先處理 PR |
| push 失敗 / PR 開不成 | 檢查 `LOOP_PAT` scope（§3）與 repo 權限；branch 已推成功時，重跑同一 day 會走 resume 只補開 PR |
| `verifier_vendor` FLAG | 某個 verifier lane 與 `GEN_MODEL` 同廠或格式錯（需 `vendor/model`）；該 lane 被跳過，run 走 PR。全部 lane 都同廠＝無跨模型審校。改成不同廠 |
| live FLAG `tts_failed` 且 `audio_url` 空 | `R2_PUBLIC_BASE` 未設（§2）或 TTS render／upload 重試後仍敗；走 PR 不 push main，之後依 §7 補音 |
| gold replay 紅 | 套件或 Python 版本漂移；見 §9 |
| `revision_failed` FAIL | 標註 divergent 後已修訂一次，複檢仍 divergent（或修訂工具本身失敗）；看 transcript 的 finding 行（`L<行>:<欄>` 與 unidic 證據），重跑 dispatch，連續出現則照 §8 走手動期 |
| `revision_violation` FAIL | 修訂稿動了 `<rt>` 讀音以外的文字，引擎已拒絕（transcript 標出行號）；重跑 dispatch，連續出現再調 `build_revision_prompt` |
| 本機 dry-run 在 generate 報 `CERTIFICATE_VERIFY_FAILED` | uv 管的 Python 在 macOS 不讀系統 Keychain 的 CA 憑證；帶 `SSL_CERT_FILE=/etc/ssl/cert.pem` 重跑。CI 的 Ubuntu 有系統憑證，不受影響 |

## 11. 讀音修訂（單次）與成本上限

- 標註回報 divergent 時，引擎先做恰好一次讀音修訂（`tools/generate-post.py --revise`，模型同 `GEN_MODEL`），只改被標記 ruby 的 `<rt>` 讀音，再重跑標註。仍 divergent 才 FAIL（`revision_failed`）；改到讀音以外的文字直接 FAIL（`revision_violation`）。
- 成本：2026-11-22 四次真實 dry-run 實測（glm-5.3 生成與修訂、gpt-6.1-sol 驗證），單次生成 US$0.06–0.15（隨輸出長度變動），修訂 US$0.03–0.05，驗證 US$0.02。走完全程的 run 實測 US$0.13，最壞情況約 US$0.22；原估的 US$0.15 上限已被單次生成突破過，不再當上限用。ledger 的 `cost_usd` 是合計。2026-11-24 起 GEN 換 anthropic/claude-opus-5.5、verify 加 `VERIFY_MODEL2`（glm-5.3 驗證費趨近零，OpenRouter 報價 in $4/M、out $20/M 對比 glm-5.3 的 $0.07/$1.4），生成成本會高於上列實測，以 ledger 為準。
- run record 多兩個欄位：`revised`（這個 run 是否修訂過）與 `revision_cost_usd`；`cost_usd` 是生成、修訂、驗證的合計。
- 字數宣稱：draft gate 之後、驗證之前，引擎用 gate 的實測字數覆寫 front-matter `description` 與導言裡的「約 N 字」（取到十位）。模型自己的估計不會進到發布文。
- 長度上限容忍：`length.maximum` 的上限帶 10% 容忍（day≤30 上限 300 字，330 字內含仍 PASS，超過即 FAIL）。
