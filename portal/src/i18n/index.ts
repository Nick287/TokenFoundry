// i18n init — EN/中文, persisted to localStorage (key tf_lang), fallback en.
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import { en } from "./en";
import { zh } from "./zh";

export const LANG_KEY = "tf_lang";

const stored =
  (typeof localStorage !== "undefined" && localStorage.getItem(LANG_KEY)) || "en";

i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    zh: { translation: zh },
  },
  lng: stored,
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

export default i18n;
