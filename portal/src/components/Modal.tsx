// Reusable modal shell + a danger-confirm dialog. One overlay, two uses:
//   <Modal>        — host for an edit form (caller renders inputs + footer)
//   <ConfirmDialog>— destructive confirm with name echo + impact line
//
// Closes on Esc or backdrop click; focus moves into the panel on open; the
// fade-in is skipped under prefers-reduced-motion (CSS-driven).
import { type ReactNode, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = panel.current;
    const sel =
      'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])';
    const focusables = () =>
      Array.from(node?.querySelectorAll<HTMLElement>(sel) ?? []).filter(
        (el) => !el.hasAttribute("disabled"),
      );

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") return onClose();
      if (e.key !== "Tab") return;
      const f = focusables();
      if (f.length === 0) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    window.addEventListener("keydown", onKey);
    // Focus the first input (edit) or the panel (confirm) — never an action
    // button, so Enter never lands on Delete by accident.
    (focusables().find((el) => el.tagName === "INPUT") ?? node)?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panel}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h3 className="modal-title">{title}</h3>
        {children}
      </div>
    </div>
  );
}

// Destructive confirm: title + an impact line (resource name, "N keys use it"),
// then Cancel / Delete. The delete button shows a busy label while pending.
export function ConfirmDialog({
  title,
  impact,
  busy,
  onConfirm,
  onClose,
}: {
  title: string;
  impact: ReactNode;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal title={title} onClose={onClose}>
      <p className="modal-impact">{impact}</p>
      <div className="modal-actions">
        <button type="button" className="btn-sm" onClick={onClose} disabled={busy}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="btn-sm btn-danger-solid"
          onClick={onConfirm}
          disabled={busy}
        >
          {busy ? t("common.deleting") : t("common.delete")}
        </button>
      </div>
    </Modal>
  );
}
