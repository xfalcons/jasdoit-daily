(() => {
  const article = document.querySelector(
    '.article-content[data-learning="true"]',
  );
  if (!article) return;
  const tools = document.querySelector(".reading-tools");
  if (!tools) return;

  // Explicit translation markers keep explanations and other blockquotes visible.
  const translations = article.querySelectorAll("[data-translation]");
  translations.forEach((node) => {
    const parent = node.parentElement;
    if (
      parent?.tagName === "P" &&
      parent.textContent.trim() === node.textContent.trim()
    ) {
      parent.classList.add("translation-block");
      if (
        parent.parentElement?.tagName === "BLOCKQUOTE" &&
        parent.parentElement.children.length === 1
      ) {
        parent.parentElement.classList.add("translation-block");
      }
    }
  });

  let settings = { furigana: true, translation: true, font: "normal" };
  try {
    const saved = JSON.parse(localStorage.getItem("jasdoit-reading") || "{}");
    if (typeof saved.furigana === "boolean") settings.furigana = saved.furigana;
    if (typeof saved.translation === "boolean")
      settings.translation = saved.translation;
    if (["normal", "large", "larger"].includes(saved.font))
      settings.font = saved.font;
  } catch {
    /* Reading controls also work when storage is unavailable. */
  }

  function applySettings() {
    article.classList.toggle("hide-furigana", !settings.furigana);
    article.classList.toggle("hide-translations", !settings.translation);
    article.dataset.font = settings.font;
    tools.querySelectorAll("[data-toggle]").forEach((button) => {
      const enabled = settings[button.dataset.toggle];
      button.setAttribute("aria-pressed", String(enabled));
      button.querySelector("[data-state]").textContent = enabled ? "開" : "關";
    });
    tools.querySelector("select").value = settings.font;
  }

  function saveSettings() {
    applySettings();
    try {
      localStorage.setItem("jasdoit-reading", JSON.stringify(settings));
    } catch {
      /* Optional persistence. */
    }
  }

  tools.querySelectorAll("[data-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      settings[button.dataset.toggle] = !settings[button.dataset.toggle];
      saveSettings();
    });
  });
  tools.querySelector("select").addEventListener("change", (event) => {
    settings.font = event.target.value;
    saveSettings();
  });

  applySettings();
  tools.hidden = false;
})();
