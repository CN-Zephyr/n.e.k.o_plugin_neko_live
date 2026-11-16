import {
  Alert,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  Grid,
  Stack,
  StatusBadge,
  Text,
  Textarea,
  useEffect,
  useState,
} from "@neko/plugin-ui"

type PanelTranslator = (key: string) => string

export function MemoryForgetRecovery({ state, disabled, onRetry, t }: {
  state?: { available?: boolean; pending?: Array<{ uid: string; platform: string; target: string }> }
  disabled: boolean
  onRetry: (uid: string, scope: { platform: string; target: string }) => Promise<void>
  t: PanelTranslator
}) {
  const [selected, setSelected] = useState<{ uid: string; platform: string; target: string } | null>(null)
  const [pending, setPending] = useState(false)
  const rows = Array.isArray(state?.pending) ? state.pending : []
  if (state?.available !== false && !rows.length) return null
  return (
    <Card title={t("panel.privacy.recoveryTitle")}>
      <Stack>
        <Text>{t("panel.privacy.recoveryHint")}</Text>
        {state?.available === false && <Alert tone="warning">{t("panel.privacy.recoveryUnavailable")}</Alert>}
        {rows.map(row => <div key={`${row.platform}:${row.target}:${row.uid}`}>
          <Text>{`${row.platform} / ${row.target} / UID: ${row.uid}`}</Text>
          <Button disabled={disabled || pending} onClick={() => { setSelected({ ...row }) }}>{t("panel.privacy.retryForget")}</Button>
        </div>)}
        <ConfirmDialog
          open={!!selected} title={t("panel.privacy.retryForget")} tone="danger"
          message={`${t("panel.privacy.forgetConfirm")} ${selected?.platform || ""} / ${selected?.target || ""} / UID: ${selected?.uid || ""}`}
          confirmLabel={t("panel.privacy.retryForget")} cancelLabel={t("panel.actions.cancel")}
          onCancel={() => { if (!pending) setSelected(null) }}
          onConfirm={async () => {
            if (!selected || pending || disabled) return
            setPending(true)
            try {
              await onRetry(selected.uid, { platform: selected.platform, target: selected.target })
              setSelected(null)
            } catch {
              // The parent reports the error; keep the original scope for retry.
            } finally { setPending(false) }
          }}
        />
      </Stack>
    </Card>
  )
}

export function ContentFilterSettings({ value, saving, onSave, t }: {
  value: string; saving: boolean; onSave: (value: string) => Promise<boolean>; t: PanelTranslator
}) {
  const [draft, setDraft] = useState(value)
  const [dirty, setDirty] = useState(false)
  const [failed, setFailed] = useState(false)
  useEffect(() => { if (!dirty) setDraft(value) }, [value, dirty])
  return (
    <Card title={t("panel.contentFilter.title")}>
      <Stack>
        <Text>{t("panel.contentFilter.hint")}</Text>
        <Textarea value={draft} disabled={saving} onChange={(text) => { setDraft(text); setDirty(true); setFailed(false) }} />
        {failed ? <Alert tone="warning">{t("panel.contentFilter.invalid")}</Alert> : null}
        <Button disabled={saving || !dirty} onClick={async () => {
          const terms = draft.split(/\r?\n/).map(term => term.trim()).filter(Boolean)
          if (draft.length > 8192 || terms.length > 128 || terms.some(term => [...term].length > 64)) { setFailed(true); return }
          if (await onSave(draft)) { setDirty(false); setFailed(false) }
          else setFailed(true)
        }}>{t("panel.actions.save")}</Button>
      </Stack>
    </Card>
  )
}

const moduleTitleKeys: Record<string, string> = {
  bili_live_ingest: "panel.modules.biliLiveInput",
  douyin_live_ingest: "panel.modules.douyinLiveInput",
  bili_identity: "panel.modules.biliIdentity",
  douyin_identity: "panel.modules.douyinIdentity",
  live_audience_session: "panel.modules.liveAudienceSession",
  viewer_profile: "panel.modules.viewerProfile",
  avatar_roast: "panel.interaction.module.avatarRoast.title",
  danmaku_response: "panel.interaction.module.danmakuResponse.title",
  danmaku_filter: "panel.interaction.module.danmakuFilter.title",
  live_support_events: "panel.interaction.module.liveSupportEvents.title",
  active_engagement: "panel.interaction.module.activeEngagement.title",
  warmup_hosting: "panel.interaction.module.warmupHosting.title",
  developer_sandbox: "panel.modules.developerSandbox",
  live_events: "panel.modules.liveEvents",
}

export function localizedModuleTitle(module: any, t: PanelTranslator): string {
  const key = moduleTitleKeys[String(module?.id || "")]
  return key ? t(key) : String(module?.title || module?.id || "-")
}

export function ModuleHealthBadge({ module, t }: { module: any; t: PanelTranslator }) {
  if (module && module.degraded) return <StatusBadge tone="danger" label={t("panel.modules.degraded")} />
  const on = !!(module && module.enabled)
  const reserved = !!(module && module.status && module.status.reserved)
  return (
    <StatusBadge
      tone={on ? "success" : (reserved ? "default" : "warning")}
      label={on ? t("panel.modules.online") : (reserved ? t("panel.modules.soon") : t("panel.modules.off"))}
    />
  )
}

export function ModuleRenderBoundary({
  title,
  render,
  t,
}: {
  title: any
  render: () => any
  t: PanelTranslator
}) {
  try {
    return render()
  } catch (err) {
    const msg = err && (err as any).message ? String((err as any).message) : ""
    return (
      <Card title={title}>
        <Stack gap={8}>
          <StatusBadge tone="danger" label={t("panel.modules.degraded")} />
          <Alert tone="danger">{t("panel.modules.renderError")}</Alert>
          {msg ? <Text>{msg}</Text> : null}
        </Stack>
      </Card>
    )
  }
}

export function ToggleSwitch(props: {
  checked: boolean
  label?: any
  disabled?: boolean
  tone?: string
  onChange: (value: boolean) => void
}) {
  const checked = !!props.checked
  const disabled = !!props.disabled
  // Use host theme variables so dark mode follows the shell.
  const onColor = props.tone === "success" ? "var(--success)" : "var(--primary)"
  const onGlow = props.tone === "success" ? "0 0 0 2px rgba(103, 194, 58, 0.18)" : "0 0 0 2px rgba(64, 158, 255, 0.18)"
  const trackColor = disabled ? "var(--border)" : checked ? onColor : "var(--muted)"
  const labelColor = disabled ? "var(--muted)" : "var(--text)"

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked ? "true" : "false"}
      disabled={disabled}
      onClick={() => {
        if (!disabled) {
          props.onChange(!checked)
        }
      }}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "8px",
        minHeight: "32px",
        padding: "0",
        border: "0",
        background: "transparent",
        color: labelColor,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.68 : 1,
      }}
    >
      <span
        aria-hidden="true"
        style={{
          position: "relative",
          width: "42px",
          height: "24px",
          borderRadius: "999px",
          background: trackColor,
          transition: "background 160ms ease",
          boxShadow: checked ? onGlow : "inset 0 0 0 1px rgba(148, 163, 184, 0.45)",
          flex: "0 0 auto",
        }}
      >
        <span
          style={{
            position: "absolute",
            top: "2px",
            left: "2px",
            width: "20px",
            height: "20px",
            borderRadius: "50%",
            background: "#ffffff",
            transform: checked ? "translateX(18px)" : "translateX(0)",
            transition: "transform 160ms ease",
            boxShadow: "0 1px 3px rgba(17, 24, 39, 0.32)",
          }}
        />
      </span>
      {props.label ? <span>{props.label}</span> : null}
    </button>
  )
}

export function AvatarPreview(props: { src?: string; alt: any }) {
  // Only the explicit backend lookup can supply a normalized, bounded JPEG.
  // Never let the browser resolve arbitrary avatar URLs, including overrides.
  const src = typeof props.src === "string" ? props.src : ""
  const maxLength = "data:image/jpeg;base64,".length + 4 * Math.ceil(12 * 1024 / 3)
  if (src.length > maxLength || !/^data:image\/jpeg;base64,[A-Za-z0-9+/]+={0,2}$/.test(src)) {
    return (
      <div
        style={{
          width: "72px",
          height: "72px",
          borderRadius: "8px",
          border: "1px solid var(--border)",
          background: "var(--surface)",
        }}
      />
    )
  }

  return (
    <img
      src={src}
      alt={props.alt}
      style={{
        width: "72px",
        height: "72px",
        borderRadius: "8px",
        objectFit: "cover",
        border: "1px solid var(--border)",
        background: "var(--surface)",
      }}
    />
  )
}

export function unwrapActionResult(envelope: any): Record<string, any> {
  if (envelope && typeof envelope === "object") {
    if (envelope.result && typeof envelope.result === "object") return envelope.result
    return envelope
  }
  return {}
}

export function AuthCard({
  t,
  loginState,
  loginLoggedIn,
  loginName,
  loginUid,
  disabled = false,
  logoutDisabled = disabled,
  onLogin,
  onLoginCheck,
  onLogout,
}: {
  t: PanelTranslator
  loginState: any
  loginLoggedIn: boolean
  loginName: string
  loginUid: string
  disabled?: boolean
  logoutDisabled?: boolean
  onLogin: () => void
  onLoginCheck: () => void
  onLogout: () => void
}) {
  return (
    <Card title={t("panel.auth.title")}>
      <Stack>
        <Text>
          {loginLoggedIn
            ? t("panel.auth.loggedIn") + (loginName ? ": " + loginName : "") + (loginUid ? " (UID " + loginUid + ")" : "")
            : t("panel.auth.loggedOut")}
        </Text>
        {loginLoggedIn ? (
          <Grid cols={2}>
            <Button tone="info" disabled={disabled} onClick={onLoginCheck}>{t("panel.actions.biliLoginCheck")}</Button>
            <Button tone="danger" disabled={logoutDisabled} onClick={onLogout}>{t("panel.actions.biliLogout")}</Button>
          </Grid>
        ) : (
          <Stack>
            <Grid cols={2}>
              <Button tone="info" disabled={disabled} onClick={onLogin}>{t("panel.actions.biliLogin")}</Button>
              <Button tone="success" disabled={disabled} onClick={onLoginCheck}>{t("panel.actions.biliLoginCheck")}</Button>
              {loginState?.logout_error ? <Button tone="danger" disabled={logoutDisabled} onClick={onLogout}>{t("panel.actions.biliLogout")}</Button> : null}
            </Grid>
            {loginState?.qrcode_image ? (
              <Stack>
                {/* hosted-ui strips data: URLs from img src, so the QR code uses a CSS background image. */}
                <button
                  type="button"
                  disabled={disabled}
                  onClick={onLogin}
                  aria-label={t("panel.auth.refreshHint")}
                  title={t("panel.auth.refreshHint")}
                  style={{
                    width: "180px",
                    height: "180px",
                    boxSizing: "border-box",
                    padding: "8px",
                    borderRadius: "8px",
                    border: "none",
                    cursor: disabled ? "not-allowed" : "pointer",
                    backgroundColor: "#ffffff",
                    backgroundImage: `url("${loginState.qrcode_image}")`,
                    backgroundRepeat: "no-repeat",
                    backgroundPosition: "center",
                    backgroundSize: "contain",
                    backgroundOrigin: "content-box",
                  }}
                />
                <Text>{t("panel.auth.scanHint")}</Text>
                <Text>{t("panel.auth.refreshHint")}</Text>
              </Stack>
            ) : null}
            {loginState?.message ? <Text>{loginState.message}</Text> : null}
          </Stack>
        )}
      </Stack>
    </Card>
  )
}

export function StatusBadgeRow({
  items,
  t,
}: {
  items: Array<{ key: string; tone?: "success" | "warning" | "danger" | "default" }>
  t: PanelTranslator
}) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
      {items.map((item) => (
        <span key={item.key}>
          <StatusBadge tone={item.tone || "default"} label={t(item.key)} />
        </span>
      ))}
    </div>
  )
}

export function ModuleOverviewCard({ modules, t }: { modules: Array<Record<string, any>>; t: PanelTranslator }) {
  return (
    <Card title={t("panel.tabs.modules")}>
      {modules.length ? (
        <DataTable
          data={modules.map((item: any, index: number) => ({ ...item, id: item.id || String(index) }))}
          rowKey="id"
          columns={[
            { key: "title", label: t("panel.modules.name"), render: (row: any) => localizedModuleTitle(row, t) },
            { key: "status", label: t("panel.modules.status"), render: (row: any) => <ModuleHealthBadge module={row} t={t} /> },
            { key: "id", label: "ID", render: (row: any) => row.id || "-" },
          ]}
        />
      ) : (
        <Text>{t("panel.modules.empty")}</Text>
      )}
    </Card>
  )
}

export function ComingSoonSection({ title, desc, t }: { title: any; desc: any; t: PanelTranslator }) {
  return (
    <Stack>
      <div style={{ opacity: 0.7 }}>
        <Card>
          <Stack gap={10}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px" }}>
              <span style={{ color: "var(--text)", fontSize: "15px", fontWeight: 720 }}>{title}</span>
              <StatusBadge tone="info" label={t("panel.modules.soon")} />
            </div>
            <Text>{desc}</Text>
          </Stack>
        </Card>
      </div>
    </Stack>
  )
}
