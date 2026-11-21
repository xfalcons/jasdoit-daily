# 引擎操作手冊（daily-loop engine runbook）

對象：operator（你）。引擎 = `tools/daily-loop.py` ＋ `tools/generate-post.py`／`verify-post.py`／`tts-upload.py`／`loop_support.py` ＋ `.github/workflows/daily-loop.yml`。規格見 `aidd-docs/plans/agent-loop-design.md`（含 2026-11-21 解釋備註）。

核心安全屬性：dry-run 一律零寫入（只在 temp sandbox 跑全流程）；live 才會 commit／push／上傳 R2；引擎永不改已發布的貼文（同 day 已存在 → noop）。

## 0. 詞彙

- **Arming**：`ENGINE_LIVE` repo variable ＝ `"true"` 時 schedule 才會 live；dispatch 只有勾 `live` input 才 live；其他一律 dry-run（含本機不帶 `--live` 的執行）。
- **Verdict**：PASS → push main 直接發布；FLAG → `loop/day-NNN` branch ＋ PR 進 review queue；FAIL → 不出內容，只 commit log（Ruling B），Actions email 會通知。
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
# 選填覆寫（預設 GEN z-ai/glm-5.3、VERIFY openai/gpt-4o，廠商必須不同）
GEN_MODEL=
VERIFY_MODEL=
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
gh secret set R2_ACCESS_KEY_ID      --body "$R2_ACCESS_KEY_ID"
gh secret set R2_SECRET_ACCESS_KEY  --body "$R2_SECRET_ACCESS_KEY"
gh secret set R2_ENDPOINT           --body "https://55fb12a63c05303d4072271e85c3fc28.r2.cloudflarestorage.com"
gh secret set R2_BUCKET             --body "jasdoit-daily"
gh secret set OPENROUTER_API_KEY    --body "$OPENROUTER_API_KEY"
gh secret set LOOP_PAT              --body "$LOOP_PAT"
gh variable set R2_PUBLIC_BASE      --body "https://pub-xxxx.r2.dev"
# ENGINE_LIVE 先不設（未設＝disarmed）；arming 見 §6
```

## 5. 第一次 dry dispatch（驗整套管線，零風險）

Actions → Daily loop → Run workflow →（不勾 live）→ Run。或本機：

```
LOOP_NOW=2026-11-22T07:00:00+08:00 \
GEN_FAKE_PATH=tests/fixtures/day-019-chiisana-shouri-wo-iwau.md \
VERIFY_FAKE_PATH=tests/fixtures/verify-pass.txt \
uv run --python 3.13.7 \
  --with edge-tts --with boto3 --with fugashi==1.5.2 --with unidic-lite==1.0.8 \
  --with jamdict==0.1a11.post2 --with jamdict_data==1.5 --with pykakasi==2.3.0 \
  python tools/daily-loop.py --dry-run
```

檢查：transcript 依序出現 select→generate→annotate→gate→verify→tts→finalize；TTS 印出 `key audio/day-0NN.mp3` 與 public URL 但 `upload skipped (dry-run)`；結尾 `DISARMED` banner；`git status` 乾淨；exit 0。真模型 dry-run（不設 fake env、`.env` 有 key）同理，只差在會打 OpenRouter。

## 6. Arming（`ENGINE_LIVE`）與第一天 live

```
gh variable set ENGINE_LIVE --body "true"
```

- 隔天 06:00（台北）cron 自動跑 live。想立即驗：Actions → Daily loop → Run workflow → 勾 `live`。
- 第一天 live 後檢查：`git log` 出現 `content: day NNN post (engine pass)` ＋ `loop: day NNN pass ledger` 兩個 commit；`pages.yml` 被觸發；R2 bucket 出現 `audio/day-NNN.mp3`；文章頁有 `<audio controls preload="none" src="…">`。
- FLAG 不會 push main：檢查 PR `loop: day NNN FLAG`，人工 review 後 merge。

要 disarm：`gh variable set ENGINE_LIVE --body "false"`（或 `gh variable delete ENGINE_LIVE`）。

## 7. 補賽（backfill）與手動補音（補音不改正文）

- 補賽過去缺文：Actions → Daily loop → Run workflow → `day` 填過去天數、勾 `live`。僅限主題表已填且「該 day 貼文不存在」的日子（存在 → noop，引擎永不改歷史）。
- 補音（`tts_failed` FLAG 之後）：重跑該天 TTS → 只 patch `audio_url` → 重跑 annotate（freshness）→ commit：

```
uv run --with edge-tts --with boto3 python tools/tts-upload.py source/_posts/day-0NN-<slug>.md
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

CI pins Python `3.13.7` ＋五個 annotation 套件的確切版本（見 workflow）。基準版本以任一 `data/annotations/*.json` 的 `sources` 為準：

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
| `verifier_vendor` FLAG | `GEN_MODEL`／`VERIFY_MODEL` 同廠或格式錯（需 `vendor/model`）；自動 PASS 被擋，改成不同廠 |
| gold replay 紅 | 套件或 Python 版本漂移；見 §9 |
