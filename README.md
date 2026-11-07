# 日語日常 · Jasdoit Daily

以生活短文學習日語的 Hexo 網站，搭配本專案自製的 **Jasdoit** 主題。暖白紙感與墨綠配色，將主要空間留給日文閱讀、繁中翻譯、單字與文法解析。

## 預覽與建置

需要 Node.js 22 以上。首次使用：

```bash
npm ci
npm run preview
```

開啟終端機顯示的本機網址，預設為 `http://127.0.0.1:4000/`。

產生網站：

```bash
npm run build
```

網站輸出到 `public/`。正式上線前，請將 `_config.yml` 的 `url` 改成實際網站網址；若放在子目錄，也須調整 `root`。本專案目前提供本機預覽與靜態輸出。

本次設計的獨立 HTML 快照可直接以瀏覽器開啟，不需要啟動伺服器：

- [首頁預覽](preview/index.html)
- [感恩文章預覽](preview/gratitude-in-daily-life.html)

這兩份預覽是本次建置的快照；修改內容或主題後，請用 `npm run preview` 查看最新結果。

## 主題與閱讀功能

主題位於 `themes/jasdoit/`，由 `_config.yml` 的 `theme: jasdoit` 啟用。

- 首頁閱讀列表，以及桌面側欄／手機橫列文章目錄。
- 漢字標音顯示／隱藏，保留原本的 `<ruby>`、`<rt>` 與備用 `<rp>`。
- 中譯顯示／隱藏，只控制明確標記的翻譯，不隱藏文法解析。
- 三種字級；閱讀設定以瀏覽器本機儲存保留，不需登入。
- 手機版面、鍵盤焦點、跳至正文連結與列印樣式。
- JavaScript 未載入時，原文、標音與翻譯仍完整顯示。

主題目前以文章閱讀為主，未提供站內搜尋或學習進度帳號系統。可透過一般 Markdown 標題自動產生目錄，內容不依賴特定第三方主題標籤。

## 撰寫文章

文章放在 `source/_posts/`。現有範例：[感恩主題文章](source/_posts/gratitude-in-daily-life.md)，包含日文正文、中譯、8 個單字與 4 個文法。

在 YAML front matter 加上以下欄位，可啟用學習工具與簡短標題：

```yaml
layout: post
learning: true
reading_title: 把感謝說出口
level: N3–N1
```

標音使用原生 HTML，例如：

```html
<ruby>感謝<rp>（</rp><rt>かんしゃ</rt><rp>）</rp></ruby>
```

將中文翻譯放入帶有 `data-translation` 的 span，工具列才會控制該段文字：

```markdown
> <span lang="zh-Hant" data-translation>這裡是繁體中文翻譯。</span>
```

一般說明、提醒與文法解析不加這個標記，保持可見。日文例句與中文翻譯在同一個引用區塊時，只標記中文那一行。

## 主題選型與維護

[主題比較研究](docs/theme-research.md) 比較 NexT Pisces、Stellar、Fluid 與自製主題。若偏好長期使用成熟現成主題，首選 NexT Pisces；本次自製主題則將標音、中譯與閱讀操作整合到同一版面。

- 品牌名稱與標語：`themes/jasdoit/_config.yml`
- 配色、字型與行距：`themes/jasdoit/source/css/learning.css`
- 閱讀互動：`themes/jasdoit/source/js/learning.js`
- 頁面版型：`themes/jasdoit/layout/`

格式與版型依據：[Hexo Writing](https://hexo.io/docs/writing)、[Hexo Themes](https://hexo.io/docs/themes)、[Hexo Templates](https://hexo.io/docs/templates)、[hexo-renderer-marked](https://github.com/hexojs/hexo-renderer-marked)。
