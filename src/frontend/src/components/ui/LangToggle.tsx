import { setLang, useLang } from '../../lib/i18n';

/** 中/英语言切换（顶栏与登录页共用）。 */
export function LangToggle() {
  const lang = useLang();
  return (
    <button
      className="btn btn-ghost sm"
      style={{ fontWeight: 600, minWidth: 44 }}
      title={lang === 'zh' ? 'Switch to English' : '切换为中文'}
      aria-label={lang === 'zh' ? 'Switch to English' : '切换为中文'}
      onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}
    >
      {lang === 'zh' ? 'EN' : '中'}
    </button>
  );
}
