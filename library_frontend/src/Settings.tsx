import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import {
  BUILTIN_APPEARANCE_PRESETS,
  type AppearanceSettings,
  fetchAppearanceSettings,
  readAppearanceSettings,
  resetAppearanceSettings,
  saveAppearanceSettings
} from "./appearance";

type AppearanceKey = keyof AppearanceSettings;

const COLOR_FIELDS: Array<{ key: AppearanceKey; label: string; description: string }> = [
  { key: "backgroundColor", label: "배경 색", description: "전체 화면, 사이드바와 패널의 기준 색상" },
  { key: "textColor", label: "글자 색", description: "제목, 본문과 테이블의 주요 글자 색상" },
  { key: "accentColor", label: "포인트 컬러", description: "활성 메뉴, 주요 버튼과 포커스 표시" }
];

export function SettingsPage() {
  const initial = readAppearanceSettings();
  const [saved, setSaved] = useState(initial);
  const [draft, setDraft] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const dirty = useMemo(() => JSON.stringify(saved) !== JSON.stringify(draft), [saved, draft]);

  useEffect(() => {
    let cancelled = false;
    fetchAppearanceSettings().then((response) => {
      if (cancelled || !response.persisted) return;
      setSaved(response.settings);
      setDraft(response.settings);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  const update = (key: AppearanceKey, value: string) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setNotice("");
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await saveAppearanceSettings(draft);
      setSaved(result);
      setDraft(result);
      setNotice("색상 설정을 저장하고 전체 화면에 적용했습니다.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "색상 설정을 저장하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await resetAppearanceSettings();
      setSaved(result);
      setDraft(result);
      setNotice("기본 색상으로 복원했습니다.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "기본 색상을 복원하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  return <>
    <header className="page-header">
      <div><span className="eyebrow">SETTINGS · APPEARANCE</span><h1>화면 색상 설정</h1><p>컨트롤서버와 같은 3색 구조입니다. 배경·글자·포인트를 정하면 패널과 테두리 등 나머지 색상은 자동으로 계산됩니다.</p></div>
    </header>
    {notice && <div className="inline-notice"><span>{notice}</span></div>}
    {error && <div className="inline-error">{error}</div>}
    <section className="settings-layout">
      <form className="panel appearance-panel" onSubmit={save}>
        <div className="panel-title"><div><span className="eyebrow">CUSTOM COLORS</span><h2>직접 설정</h2></div><span className={dirty ? "settings-dirty" : "settings-saved"}>{dirty ? "저장하지 않은 변경" : "저장됨"}</span></div>
        <div className="appearance-fields">
          {COLOR_FIELDS.map((field) => <label className="appearance-field" key={field.key}>
            <span><strong>{field.label}</strong><small>{field.description}</small></span>
            <span className="appearance-color-control">
              <HexColorInput value={draft[field.key]} onChange={(value) => update(field.key, value)} label={`${field.label} HEX 값`} />
              <input className="appearance-picker" type="color" value={draft[field.key]} aria-label={field.label} onChange={(event) => update(field.key, event.target.value)} />
            </span>
          </label>)}
        </div>
        <ThemePreview settings={draft} />
        <footer className="settings-actions">
          <button className="button ghost" type="button" disabled={busy} onClick={reset}>기본값 복원</button>
          <button className="button primary" disabled={busy || !dirty}>{busy ? "저장 중…" : "저장하고 적용"}</button>
        </footer>
      </form>
      <section className="panel preset-panel">
        <div className="panel-title"><div><span className="eyebrow">PRESETS</span><h2>빠른 테마</h2></div></div>
        <p>프리셋을 선택한 뒤 왼쪽에서 세부 색상을 조정하고 저장할 수 있습니다.</p>
        <div className="appearance-presets">
          {BUILTIN_APPEARANCE_PRESETS.map((preset) => <button type="button" key={preset.name} onClick={() => { setDraft(preset.settings); setNotice(""); }}>
            <span className="preset-swatches"><i style={{ background: preset.settings.backgroundColor }} /><i style={{ background: preset.settings.textColor }} /><i style={{ background: preset.settings.accentColor }} /></span>
            <strong>{preset.name}</strong>
            <small>{preset.settings.backgroundColor} · {preset.settings.accentColor}</small>
          </button>)}
        </div>
        <small className="settings-storage-note">저장값은 서버의 <code>.dedup_state/library-server/appearance.json</code>과 현재 브라우저에 함께 보관됩니다.</small>
      </section>
    </section>
  </>;
}

function ThemePreview({ settings }: { settings: AppearanceSettings }) {
  return <div className="appearance-preview" style={{ background: settings.backgroundColor, color: settings.textColor }}>
    <div><strong>미리보기</strong><span style={{ background: settings.accentColor, color: contrastText(settings.accentColor) }}>주요 버튼</span></div>
    <p>도서 관리 화면의 제목과 본문 예시입니다.</p>
    <div className="appearance-preview-bars"><i style={{ background: settings.textColor }} /><i style={{ background: settings.accentColor }} /><i style={{ background: settings.textColor }} /></div>
  </div>;
}

function HexColorInput({ value, onChange, label }: { value: string; onChange: (value: string) => void; label: string }) {
  const [text, setText] = useState(value);
  const lastValid = useRef(value);
  const focused = useRef(false);
  useEffect(() => {
    lastValid.current = value;
    if (!focused.current) setText(value);
  }, [value]);
  const update = (raw: string) => {
    let next = raw.trim();
    if (next && !next.startsWith("#")) next = `#${next}`;
    if (!/^#[0-9a-fA-F]{0,6}$/.test(next)) return;
    setText(next);
    if (/^#[0-9a-fA-F]{6}$/.test(next)) onChange(next.toLowerCase());
  };
  const commit = () => {
    if (/^#[0-9a-fA-F]{6}$/.test(text)) {
      const normalized = text.toLowerCase();
      setText(normalized);
      onChange(normalized);
    } else {
      setText(lastValid.current);
    }
  };
  return <input className="appearance-hex" value={text} maxLength={7} spellCheck={false} aria-label={label}
    onFocus={() => { focused.current = true; }} onBlur={() => { focused.current = false; commit(); }}
    onChange={(event) => update(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); commit(); event.currentTarget.blur(); } }} />;
}

function contrastText(hex: string): string {
  const value = hex.replace("#", "");
  const r = Number.parseInt(value.slice(0, 2), 16);
  const g = Number.parseInt(value.slice(2, 4), 16);
  const b = Number.parseInt(value.slice(4, 6), 16);
  return r * 0.299 + g * 0.587 + b * 0.114 > 160 ? "#0a0c10" : "#ffffff";
}
