import { useId, useState } from 'react'
import { avatarGradient, initials } from '../format'
import { haptic } from '../tg'
import { IArrowRight, ICloseCircle, IDanger, ISearch } from '../icons'

// Компоненты в стиле референса BotFather: .tm-main-intro, .tm-section, .tm-row, .tm-field …

const cx = (...a) => a.filter(Boolean).join(' ')

export function Page({ children, className }) {
  return <main className={cx('tm-main', className)}>{children}</main>
}

/** Шапка экрана: картинка/иконка, заголовок, текст. */
export function Intro({ media, title, text, children }) {
  return (
    <div className="tm-main-intro">
      {media && <div className="tm-main-intro-media">{media}</div>}
      {title && <h1 className="tm-main-intro-header">{title}</h1>}
      {text && <div className="tm-main-intro-text">{text}</div>}
      {children}
    </div>
  )
}

/** Круглая иконка-логотип с градиентом. */
export function Logo({ icon: Icon, size = 72, tone = 'blue' }) {
  return (
    <div className={cx('tm-logo', `tm-logo-${tone}`)} style={{ width: size, height: size }}>
      <Icon width={size * 0.5} height={size * 0.5} />
    </div>
  )
}

export function Avatar({ id, name, size = 34, active }) {
  return (
    <div className="tm-avatar" style={{ width: size, height: size, background: avatarGradient(id), fontSize: size * 0.4 }}>
      {initials(name)}
      {active !== undefined && <span className={cx('tm-avatar-status', active ? 'on' : 'off')} />}
    </div>
  )
}

/** after — содержимое под блоком на фоне страницы (подсказка к полю, чипы), до footer. */
export function Section({ title, desc, action, children, after, footer, className }) {
  return (
    <section className={cx('tm-section', className)}>
      {(title || action) && (
        <div className="tm-section-header">
          <h2 className="tm-section-header-text">{title}</h2>
          {action}
        </div>
      )}
      {desc && <div className="tm-section-desc">{desc}</div>}
      {children && <div className="tm-table-wrap">{children}</div>}
      {after}
      {footer && <p className="help-text">{footer}</p>}
    </section>
  )
}

/**
 * Строка списка. onClick → кликабельна (со стрелкой, если chevron !== false).
 * tone: 'accent' (синяя, как «Create a New Bot») | 'destructive' (красная, как «Delete Bot»).
 */
export function Row({ icon: Icon, media, title, desc, value, valueTone, onClick, chevron, tone, children, className, multiline }) {
  const clickable = !!onClick
  const showChevron = chevron ?? (clickable && tone !== 'accent' && tone !== 'destructive')
  const Tag = clickable ? 'button' : 'div'
  return (
    <Tag
      type={clickable ? 'button' : undefined}
      className={cx('tm-row', clickable && 'tm-row-action', showChevron && 'tm-row-link', tone && `tm-row-${tone}`, multiline && 'tm-row-multiline', className)}
      onClick={onClick}
    >
      {Icon && <Icon className="tm-icon" width={24} height={24} />}
      {media}
      <div className="tm-row-main">
        <div className="tm-row-title">{title}</div>
        {desc && <div className="tm-row-description">{desc}</div>}
      </div>
      {value !== undefined && value !== null && <div className={cx('tm-row-status', valueTone && `tone-${valueTone}`)}>{value}</div>}
      {children}
    </Tag>
  )
}

export function Toggle({ on }) {
  return (
    <span className={cx('tm-toggle', on && 'tm-toggle-on')}>
      <span className="tm-toggle-handle" />
    </span>
  )
}

export function ToggleRow({ icon, title, desc, on, onChange, disabled }) {
  return (
    <Row
      icon={icon}
      title={title}
      desc={desc}
      chevron={false}
      onClick={disabled ? undefined : () => {
        haptic.select()
        onChange(!on)
      }}
    >
      <Toggle on={on} />
    </Row>
  )
}

/** Выбор одного варианта (круглый чекбокс как .tm-main-checkbox). */
export function CheckRow({ title, desc, checked, onSelect, value }) {
  return (
    <button type="button" className="tm-row tm-row-action tm-row-checkbox" onClick={() => !checked && onSelect()}>
      <span className={cx('tm-check', checked && 'checked')} />
      <div className="tm-row-main">
        <div className="tm-row-title">{title}</div>
        {desc && <div className="tm-row-description">{desc}</div>}
      </div>
      {value !== undefined && <div className="tm-row-status">{value}</div>}
    </button>
  )
}

/** Раскрывающаяся строка (FAQ, подсказки) — как .tm-row-count + .tr-expand. */
export function Expand({ icon: Icon, title, children }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        className={cx('tm-row tm-row-action tm-row-expand', open && 'open')}
        onClick={() => {
          haptic.select()
          setOpen(!open)
        }}
      >
        {Icon && <Icon className="tm-icon" width={24} height={24} />}
        <div className="tm-row-main">
          <div className="tm-row-title">{title}</div>
        </div>
        <span className="tm-row-count" />
      </button>
      <div className={cx('tm-expand', open && 'open')}>
        <div className="tm-expand-inner">
          <div className="tm-expand-body">{children}</div>
        </div>
      </div>
    </>
  )
}

export function Field({ label, prefix, suffix, mono, error, right, inputRef, multiline, ...props }) {
  const id = useId()
  const Input = multiline ? 'textarea' : 'input'
  return (
    <div className={cx('tm-field', error && 'tm-field-error')}>
      {label && <label htmlFor={id} className="tm-field-label">{label}</label>}
      {prefix && <span className="tm-field-const-prefix">{prefix}</span>}
      <Input
        id={id}
        ref={inputRef}
        className={cx('form-control tm-input', mono && 'tm-input-mono', prefix && 'has-prefix')}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        {...props}
      />
      {suffix && <span className="tm-field-suffix">{suffix}</span>}
      {right}
    </div>
  )
}

export function SearchField({ value, onChange, placeholder = 'Поиск' }) {
  return (
    <div className="tm-field tm-search-field">
      <ISearch className="tm-search-icon" width={22} height={22} />
      <input
        className="form-control tm-search-input"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        enterKeyHint="search"
        autoComplete="off"
        autoCorrect="off"
        spellCheck={false}
      />
      {value && (
        <button type="button" className="tm-search-clear" onClick={() => onChange('')} aria-label="Очистить">
          <ICloseCircle width={20} height={20} />
        </button>
      )}
    </div>
  )
}

/** Подсказка под полем: loading / success / error (как .hint-text-* в референсе). */
export function Hint({ state, children }) {
  if (!children) return null
  return <div className={cx('hint-text', state && `hint-text-${state}`)}>{children}</div>
}

export function Warning({ children, tone = 'warning' }) {
  return (
    <div className={cx('tm-warning', `tm-warning-${tone}`)}>
      <IDanger width={18} height={18} />
      <div>{children}</div>
    </div>
  )
}

export function Pill({ tone, children }) {
  return <span className={cx('tm-pill', tone && `tm-pill-${tone}`)}>{children}</span>
}

export function Stats({ items }) {
  return (
    <div className="tm-stats">
      {items.map((it) => (
        <div className="tm-stat" key={it.label}>
          <div className="tm-stat-value">{it.value}</div>
          <div className="tm-stat-label">{it.label}</div>
        </div>
      ))}
    </div>
  )
}

export function Spinner({ size = 22 }) {
  return <span className="tm-spinner" style={{ width: size, height: size }} />
}

export function Loading() {
  return (
    <div className="tm-empty-wrap">
      <Spinner size={28} />
    </div>
  )
}

export function Empty({ icon: Icon, title, text, action, onAction }) {
  return (
    <div className="tm-empty-wrap">
      <div className="tm-empty">
        {Icon && <Icon width={56} height={56} className="tm-empty__icon" />}
        <div className="tm-empty__title">{title}</div>
        {text && <div className="tm-empty__desc">{text}</div>}
        {action && (
          <button type="button" className="tm-empty__btn" onClick={onAction}>
            {action}
          </button>
        )}
      </div>
    </div>
  )
}

export function LinkMore({ children, onClick }) {
  return (
    <button type="button" className="tm-link-more" onClick={onClick}>
      {children}
      <IArrowRight width={14} height={14} />
    </button>
  )
}

export { cx }
