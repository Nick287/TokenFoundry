// Lightweight toast: a context provider + useToast() hook. One stack, bottom
// right, auto-dismiss in 3s. Used for success confirmation after create / save
// / delete, so destructive actions get acknowledgement instead of silence.
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useRef,
  useState,
} from "react";

type Tone = "ok" | "error";
type Toast = { id: number; text: string; tone: Tone };

const ToastCtx = createContext<(text: string, tone?: Tone) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);

  const push = useCallback((text: string, tone: Tone = "ok") => {
    const id = ++seq.current;
    setToasts((prev) => [...prev, { id, text, tone }]);
    window.setTimeout(
      () => setToasts((prev) => prev.filter((x) => x.id !== id)),
      3000,
    );
  }, []);

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {toasts.map((to) => (
          <div key={to.id} className={`toast toast-${to.tone}`}>
            {to.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);
