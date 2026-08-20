import { Check, Eye, EyeOff, KeyRound, RefreshCw, ShieldCheck, Trash2, X } from "lucide-react";
import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { fetchSecretProviders, mutateSecret } from "../api";
import { SecretProvider } from "../types";
import { formatDate } from "../utils";

const STATUS_LABELS: Record<string, string> = {
  not_configured: "未配置",
  configured_unverified: "已配置 · 未验证",
  valid: "有效",
  invalid: "无效",
  storage_unavailable: "存储不可用",
};

type EditAction = "set" | "rotate";

export function SecretDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const drawerRef = useRef<HTMLElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [providers, setProviders] = useState<SecretProvider[]>([]);
  const [csrfToken, setCsrfToken] = useState<string | null>(null);
  const [notice, setNotice] = useState("打开面板后读取状态");
  const [error, setError] = useState(false);
  const [pending, setPending] = useState(false);
  const [editing, setEditing] = useState<{ providerId: string; action: EditAction } | null>(null);
  const [secret, setSecret] = useState("");
  const [connect, setConnect] = useState(false);
  const [showSecret, setShowSecret] = useState(false);

  const load = async (quiet = false) => {
    if (!quiet) { setNotice("正在读取 SecretStore 状态"); setError(false); }
    try {
      const result = await fetchSecretProviders();
      setProviders(result.providers);
      setCsrfToken(result.csrfToken);
      if (!quiet) setNotice("SecretStore 状态已更新");
    } catch (caught) {
      setProviders([]);
      setCsrfToken(null);
      setError(true);
      setNotice(`SecretStore 状态读取失败：${caught instanceof Error ? caught.message : String(caught)}`);
    }
  };

  useEffect(() => {
    if (open) {
      previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      void load();
      window.requestAnimationFrame(() => drawerRef.current?.querySelector<HTMLElement>("button")?.focus());
    } else {
      setSecret(""); setConnect(false); setShowSecret(false); setEditing(null);
      const previous = previousFocusRef.current;
      previousFocusRef.current = null;
      if (previous && document.contains(previous)) window.requestAnimationFrame(() => previous.focus());
    }
  }, [open]);

  useEffect(() => {
    if (open && editing) window.requestAnimationFrame(() => drawerRef.current?.querySelector<HTMLInputElement>("[data-secret-candidate]")?.focus());
  }, [editing, open]);

  const trapFocus = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab") return;
    const focusable = [...(drawerRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? [])]
      .filter((item) => item.getClientRects().length > 0);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };

  const run = async (providerId: string, action: "verify" | "delete", shouldConnect = false) => {
    if (!csrfToken || pending) return;
    const label = action === "delete" ? "删除" : shouldConnect ? "执行连接验证" : "执行本地验证";
    if (!window.confirm(`${label} ${providerId}？`)) return;
    setPending(true); setError(false); setNotice("正在执行 SecretStore 操作");
    try {
      await mutateSecret(providerId, action, csrfToken, action === "delete"
        ? { confirmation: "DELETE" }
        : { connect: shouldConnect, confirmation: shouldConnect ? "CONNECT" : "VERIFY" });
      await load(true);
      setNotice("SecretStore 操作已完成");
    } catch (caught) {
      await load(true);
      setError(true); setNotice(`SecretStore 操作失败：${caught instanceof Error ? caught.message : String(caught)}`);
    } finally { setPending(false); }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!editing || !csrfToken || pending || secret.length < 8) return;
    if (editing.action === "rotate" && !window.confirm(`确认替换 ${editing.providerId === "deepseek" ? "DeepSeek" : editing.providerId} 的现有密钥？`)) return;
    if (connect && !window.confirm("连接验证可能产生一次 Provider 调用。确认继续？")) return;
    setPending(true); setError(false); setNotice("正在保存 Secret");
    const confirmation = editing.action === "set" ? (connect ? "CONNECT" : "SET") : (connect ? "ROTATE_CONNECT" : "ROTATE");
    try {
      await mutateSecret(editing.providerId, editing.action, csrfToken, { secret, connect, confirmation });
      setSecret(""); setConnect(false); setEditing(null); setShowSecret(false);
      await load(true);
      setNotice("SecretStore 操作已完成");
    } catch (caught) {
      await load(true);
      setError(true); setNotice(`SecretStore 操作失败：${caught instanceof Error ? caught.message : String(caught)}`);
    } finally { setPending(false); }
  };

  if (!open) return null;
  return (
    <>
      <button className="settings-scrim" type="button" aria-label="关闭设置" onClick={onClose} />
      <aside ref={drawerRef} className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-title" onKeyDown={trapFocus}>
        <header><div><KeyRound size={18} /><h2 id="settings-title">Provider 密钥</h2></div><button className="icon-button" type="button" aria-label="关闭设置" title="关闭设置" onClick={onClose}><X size={18} /></button></header>
        <div className="drawer-body">
          <div className="secret-summary"><strong>SecretStore</strong><span>仅限本机同源</span></div>
          <p className={`drawer-notice${error ? " is-error" : ""}`}>{notice}</p>
          <div className="secret-provider-list">
            {providers.length ? providers.map((provider) => (
              <section className="secret-provider" key={provider.provider_id}>
                <div className="secret-provider-header"><div><h3>{provider.provider_id === "deepseek" ? "DeepSeek" : provider.provider_id}</h3><code>{provider.provider_id}</code></div><span data-state={provider.status}>{STATUS_LABELS[provider.status] ?? "状态未知"}</span></div>
                <dl><dt>存储后端</dt><dd>{provider.backend || "--"}</dd><dt>最近验证</dt><dd>{provider.last_validated_at ? `${provider.validation_scope === "connection" ? "连接" : "本地"} · ${formatDate(provider.last_validated_at)}` : "未验证"}</dd><dt>持久化</dt><dd>{provider.persistent ? "是" : "否"}</dd></dl>
                {provider.repair && <p className="secret-repair">{provider.repair}</p>}
                {provider.status !== "storage_unavailable" && <div className="secret-actions">
                  {!provider.configured ? <button className="command-button primary" type="button" disabled={pending} onClick={() => setEditing({ providerId: provider.provider_id, action: "set" })}><KeyRound size={14} />配置</button> : <>
                    <button className="command-button" type="button" disabled={pending} onClick={() => setEditing({ providerId: provider.provider_id, action: "rotate" })}><RefreshCw size={14} />替换</button>
                    <button className="command-button" type="button" disabled={pending} onClick={() => void run(provider.provider_id, "verify", false)}><Check size={14} />本地验证</button>
                    <button className="command-button" type="button" disabled={pending} onClick={() => void run(provider.provider_id, "verify", true)}><ShieldCheck size={14} />连接验证</button>
                    <button className="command-button danger" type="button" disabled={pending} onClick={() => void run(provider.provider_id, "delete")}><Trash2 size={14} />删除</button>
                  </>}
                </div>}
                {editing?.providerId === provider.provider_id && <form className="secret-form" onSubmit={(event) => void submit(event)}>
                  <label>{editing.action === "rotate" ? "新密钥" : "密钥"}<span className="secret-input-row"><input data-secret-candidate type={showSecret ? "text" : "password"} autoComplete="off" autoCapitalize="none" spellCheck={false} minLength={8} maxLength={8192} required value={secret} onChange={(event) => setSecret(event.target.value)} /><button type="button" className="icon-button" title={showSecret ? "隐藏密钥" : "显示密钥"} aria-label={showSecret ? "隐藏密钥" : "显示密钥"} aria-pressed={showSecret} onClick={() => setShowSecret(!showSecret)}>{showSecret ? <EyeOff size={16} /> : <Eye size={16} />}</button></span></label>
                  <label className="check-option"><input type="checkbox" checked={connect} onChange={(event) => setConnect(event.target.checked)} /><span>保存后执行连接验证，可能产生一次 Provider 调用</span></label>
                  <div><button type="button" className="command-button" onClick={() => { setSecret(""); setEditing(null); }}>取消</button><button type="submit" className="command-button primary" disabled={pending || secret.length < 8}>{editing.action === "rotate" ? "确认替换" : "保存"}</button></div>
                </form>}
              </section>
            )) : <div className="drawer-empty">没有可管理的 Provider</div>}
          </div>
        </div>
      </aside>
    </>
  );
}
