# 日文學習網站的 Hexo 主題選型

研究日期：2026-11-07。以下產品能力依官方文件與儲存庫確認；「適合程度」及開發取捨屬於針對本專案的判斷，並非主題官方宣稱。

## 建議

**現成主題首選 NexT 的 Pisces 版型。**目前內容以約 500 字日文短文、逐段繁中翻譯、8 個單字及 4 個文法為主，重點是長文閱讀及章節跳轉。Pisces 的雙欄結構與文章側欄目錄，適合讓讀者快速回到單字或文法段落。若偏好更簡潔的單欄，可選 Mist。[版型說明](https://theme-next.js.org/docs/theme-settings/)、[側欄與目錄](https://theme-next.js.org/docs/theme-settings/sidebar)

**若希望做出更貼合學習流程的體驗，可以自製輕量 Hexo 主題。**漢字標音顯示／隱藏、只看日文、逐段展開中譯、單字與文法區塊導覽，都值得納入；這些功能須自行實作與驗證，不能因為選了現成主題就假定已經具備。自製主題可依 Hexo 的 `layout`、`source`、`scripts` 與 `_config.yml` 結構開發。[Hexo 主題結構](https://hexo.io/docs/themes)

## 現成主題比較

| 主題 | 官方已確認的相關能力 | 對本網站的判斷 | 需要客製的部分 |
| --- | --- | --- | --- |
| **NexT Pisces／Mist** | Pisces 為雙欄，Mist 為緊湊單欄；可調整側欄位置、寬度及目錄；支援獨立客製樣式與版型檔。[版型](https://theme-next.js.org/docs/theme-settings/)、[側欄](https://theme-next.js.org/docs/theme-settings/sidebar)、[Custom Files](https://theme-next.js.org/docs/advanced-settings/custom-files) | 適合先把文章式學習網站做穩定，內容與導覽的關係清楚。 | 日文專用字型、ruby 行距、逐段中譯開關、學習控制列。 |
| **Stellar** | 支援部落格、Wiki、專欄與筆記；共用導覽、搜尋與側欄；本地搜尋可定位章節，窄螢幕會優先保留正文空間，另有配色與字體設定。[官方介紹](https://github.com/xaoxuu/hexo-theme-stellar#readme) | 若未來要依 JLPT 等級、文法系列與課程建立知識庫，組織能力較符合需求；目前只有少量文章時，需要處理較多設定。 | ruby 與中譯的學習互動仍應另做；現成選項卡或折疊元件不等於完整的閱讀模式。 |
| **Fluid** | Material Design 風格；提供本地搜尋、字型、客製 JS／CSS、暗色模式設定；文章可顯示側欄目錄，支援調整頁首大圖高度。[開始使用](https://fluid-dev.github.io/hexo-fluid-docs/start/)、[設定指南](https://fluid-dev.github.io/hexo-fluid-docs/guide/)、[目錄設定](https://github.com/fluid-dev/hexo-theme-fluid/blob/master/_config.yml) | 適合想搭配日本生活攝影的文章型網站；本案建議降低頁首大圖高度，讓讀者進頁就能開始閱讀。 | 日語閱讀工具與標音細節，仍須客製。 |

## 搜尋與手機閱讀

- **NexT**：目前官方設定將本地搜尋列為選配，依賴 `hexo-generator-searchdb`；不能只打開開關就略過索引產生器。官方側欄文件也區分桌面雙欄與手機寬度設定。[主題設定原始檔](https://github.com/next-theme/hexo-theme-next/blob/master/_config.yml)、[索引產生器](https://github.com/next-theme/hexo-generator-searchdb)、[側欄說明](https://theme-next.js.org/docs/theme-settings/sidebar)
- **Stellar**：官方明確說明本地搜尋能定位到章節、窄螢幕會優先顯示正文，適合未來較長的學習筆記。[官方介紹](https://github.com/xaoxuu/hexo-theme-stellar#readme)
- **Fluid**：官方指南說明已整合本地搜尋；文章目錄的預設位置可設在左右兩側。但目錄在實際手機尺寸上的呈現，仍需用文章測試，不能把桌面側欄效果當成手機效果。[本地搜尋](https://fluid-dev.github.io/hexo-fluid-docs/guide/#本地搜索)、[目錄設定](https://github.com/fluid-dev/hexo-theme-fluid/blob/master/_config.yml)

日文搜尋需額外測試「漢字」與「讀音」：移除 HTML 標籤不代表已妥善處理 `<rt>` 內容，漢字與讀音可能被串接。也不應假定一般子字串搜尋提供日文斷詞或語意搜尋。這是本案應驗證的內容處理問題。[NexT 索引輸出格式](https://github.com/next-theme/hexo-generator-searchdb#options)

## 環境與安裝要求

| 主題 | 目前官方資料能確認的要求 | 安裝前的注意事項 |
| --- | --- | --- |
| NexT | 官方建議 Hexo 5.0 以上使用 npm 安裝。檢視的主題 `package.json` 未宣告 Node `engines`；不可把開發依賴中的 Hexo 版本當成執行最低版本。[安裝文件](https://theme-next.js.org/docs/getting-started/)、[package.json](https://github.com/next-theme/hexo-theme-next/blob/master/package.json) | 使用選定的正式版本安裝後實際產生網站；客製檔放在網站目錄，不直接修改 `node_modules` 內主題。 |
| Stellar | 官方目前要求 Node.js 22 以上、Hexo 8 以上；主題 `package.json` 亦宣告 Node ≥22。[官方環境要求](https://github.com/xaoxuu/hexo-theme-stellar#环境要求)、[package.json](https://github.com/xaoxuu/hexo-theme-stellar/blob/main/package.json) | 確認採用的正式版本與對應文件一致，不混用舊版設定。 |
| Fluid | 官方建議 Hexo 5.0 以上使用 npm 安裝；目前主題 metadata 宣告 Node ≥8.10.0 與 `nunjucks` peer dependency。這不代表應使用舊 Node，實際還受 Hexo 與其他依賴限制。[安裝文件](https://fluid-dev.github.io/hexo-fluid-docs/start/)、[package.json](https://github.com/fluid-dev/hexo-theme-fluid/blob/master/package.json) | 用符合所選 Hexo 版本要求的現行 Node，並確認模板 renderer 與 peer dependency 已滿足。 |

此筆記未安裝或切換任何主題；以上是選型研究，不是三個主題的相容性測試結果。

## 漢字標音與翻譯的共同限制

既有文章有 190 處 ruby。`<ruby>`／`<rt>` 本身是 HTML 標音機制；主題的工作是保留內容並提供合適字級、行距及排版。沒有在上述主題文件中找到足以保證本案「標音開關＋逐段繁中翻譯切換」的完整內建學習流程，因此應視為另外實作的功能，而非說三者完全不能顯示標音。[HTML ruby 說明](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/ruby)

建議驗收項目：

1. 所有 190 個 `<ruby>` 與 `<rt>` 經 Hexo 產生後都保留。
2. 手機 375px 寬度時，標音不碰到上一行，正文沒有水平捲動。
3. 切換標音不刪除原文；重新顯示時讀音正確。
4. 翻譯區塊要有明確標記，避免把文法解析中的一般引用區塊一併隱藏。
5. 目錄文字不混入重複讀音，連結能跳到原文章節。
6. 無 JavaScript 時仍可閱讀日文、標音與翻譯。

## 自製輕量主題的設計與成本

以下是本案設計建議與工程判斷：

- **視覺**：暖白底、墨灰正文、抹茶綠導覽，少量朱紅作重點提示；避免大面積動態效果。
- **閱讀**：正文寬度約 680～760px，日文正文約 19～20px，行距約 2.1～2.3；標音尺寸另調，不把整頁字級一律放大。
- **學習操作**：顯示標音、顯示中文、字級調整；以語意化按鈕提供狀態提示。
- **章節**：文章、單字、文法三個主要入口；桌面側欄，手機提供折疊導覽。
- **首頁**：主題、JLPT 範圍、單字與文法數量，以及繼續閱讀入口。內容仍由 Markdown 管理。

| 路線 | 初期工作 | 後續責任 |
| --- | --- | --- |
| NexT 加客製樣式／小工具 | 已有文章頁、首頁、目錄與主題設定；主要補日文排版與學習控制 | 保持客製檔與主題分離，每次升級確認掛載位置與樣式 |
| Stellar 加客製學習介面 | 先安排 Blog／Wiki／專欄的內容結構，再加閱讀控制 | 除學習工具外，還需維護較完整的內容設定與主題升級相容性 |
| 自製輕量主題 | 自行做首頁、文章頁、目錄、導覽、手機版、無障礙與學習控制 | 自己負責搜尋、分頁、分類、SEO metadata 等需要的完整度及長期相容性 |

若目標是盡快穩定寫文章，採 **NexT Pisces 加少量客製**。若希望整個介面圍繞「先讀日文，再看翻譯，最後複習單字文法」，自製輕量主題更容易保持體驗一致；應先以現有感恩文章完成可用範例，再擴充首頁與內容索引。
