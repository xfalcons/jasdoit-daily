# 目錄結構與 GitHub Pages 發布

本專案是 `xfalcons/jasdoit-daily` 的 Hexo 網站，正式網址設定為 `https://xfalcons.github.io/jasdoit-daily/`。

## 目錄用途

| 路徑 | 用途 | 是否包含於發布的網站？ |
| --- | --- | --- |
| `source/_posts/` | Markdown 學習文章，包含標音與翻譯 | 轉成 HTML 後發布 |
| `themes/jasdoit/layout/` | EJS 頁面版型 | 發布其渲染結果 |
| `themes/jasdoit/source/` | 網站 CSS、JavaScript 與圖片 | 是 |
| `_config.yml` | Hexo 設定，包含 GitHub Pages 網址與根路徑 | 建置時使用 |
| `package.json`、`package-lock.json`、`.nvmrc` | 套件版本與 Node.js 版本 | 建置時使用 |
| `.github/workflows/pages.yml` | 自動建置及發布工作流程 | 否 |
| `aidd-docs/` | AI 產生的技術文件、研究筆記與預覽資料 | 否 |
| `public/` | 建置產生的網站，由 Git 忽略，交由工作流程上傳 | 只發布此目錄的內容 |
| `node_modules/`、`db.json` | 本機套件與 Hexo 快取，由 Git 忽略 | 否 |
| `AGENTS.md`、`LICENSE` | 既有的專案指示與授權文件 | 否 |

所有新增的 LLM／AI 專案文件均放在 `aidd-docs/`，包括計畫、研究、設定說明、報告、截圖與獨立設計預覽。正式學習文章屬於網站內容，即使由 AI 協助撰寫，仍放在 `source/_posts/`。程式、建置設定與既有專案指示檔則保留在各自的標準位置。

本次搬移項目：

- 根目錄 `README.md` → [`aidd-docs/README.md`](README.md)。
- `docs/theme-research.md` → [`aidd-docs/theme-research.md`](theme-research.md)。
- `preview/` → [`aidd-docs/preview/`](preview/)。

採用 GitHub Actions 發布，因此不需要另外準備 `docs/` 發布目錄或 `gh-pages` 分支。Hexo 讀取 `source/` 與自製主題後，將網站產生到 `public/`；工作流程只上傳 `public/`，開發文件不會進入網站。這符合 [Hexo 的 GitHub Actions 發布方式](https://hexo.io/docs/github-pages)與 [GitHub Pages 自訂工作流程](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

## 本機開發

在專案根目錄使用 Node.js 22。若已安裝 nvm，可先執行 `nvm use`，再執行：

```bash
npm ci
npm run preview
```

開啟 `http://127.0.0.1:4000/jasdoit-daily/`。本機保留與正式網站相同的子路徑，方便及早發現圖片、樣式或導覽連結問題。

乾淨建置：

```bash
npm run clean
npm run build
```

`public/` 的內容會直接對應網站的 `/jasdoit-daily/`，不需要額外建立 `public/jasdoit-daily/`。主題透過 Hexo 的 `url_for` 輔助函式，替導覽、樣式、程式與圖片網址加上根路徑。參考 [Hexo 網址輔助函式](https://hexo.io/docs/helpers#url-for)。

[`preview/`](preview/) 的獨立 HTML 快照仍可直接從磁碟開啟，兩個頁面使用相對連結互相導覽。這些檔案屬於開發資料，不會隨 `npm run build` 自動更新。

## 首次啟用發布

1. 開啟儲存庫的 [Settings → Pages](https://github.com/xfalcons/jasdoit-daily/settings/pages)，將 **Build and deployment → Source** 設為 **GitHub Actions**。
2. 將整理好的原始碼與工作流程提交並推送到 `main`。
3. 在儲存庫 **Actions** 頁面查看 **GitHub Pages** 工作流程。發布成功後，工作流程的部署環境會顯示實際網站網址。

以 `main` 為目標的 Pull Request 會執行套件安裝與正式建置，不會發布網站。推送至 `main` 後，工作流程會建置網站、上傳 `public/`，並使用 GitHub 自動提供的權杖發布。手動執行工作流程時，也只有在 `main` 分支才會發布。此流程不需要個人存取權杖或額外的 Git 發布外掛；發布工作已宣告[必要的 `pages: write` 與 `id-token: write` 權限](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages#deploying-github-pages-artifacts)。

本次目錄整理完成的是本機檔案準備；尚未提交、推送或變更遠端 GitHub Pages 設定。

## 更名或使用自訂網域

請一併修改 `_config.yml` 的 `url` 與 `root`。專案型網站的兩個值都須包含儲存庫名稱；若網站放在網域根目錄，`url` 改為該網域，`root` 改為 `/`，然後重新建置。參考 [Hexo 專案型網站設定](https://hexo.io/docs/github-pages#Project-page)。
