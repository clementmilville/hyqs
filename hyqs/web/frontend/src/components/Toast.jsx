import { createContext, useCallback, useContext, useMemo, useState } from "react";
import { createPortal } from "react-dom";

const ToastCtx = createContext({ success: () => {}, error: () => {}, info: () => {} });

let _nextId = 0;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);

  const add = useCallback((msg, variant) => {
    const id = ++_nextId;
    setToasts((ts) => [...ts, { id, msg, variant }]);
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 4000);
  }, []);

  const api = useMemo(
    () => ({
      success: (msg) => add(msg, "ok"),
      error: (msg) => add(msg, "bad"),
      info: (msg) => add(msg, "info"),
    }),
    [add]
  );

  return (
    <ToastCtx.Provider value={api}>
      {children}
      {createPortal(
        <div className="toast-container">
          {toasts.map((t) => (
            <div key={t.id} className={`toast toast-${t.variant}`}>
              {t.msg}
            </div>
          ))}
        </div>,
        document.body
      )}
    </ToastCtx.Provider>
  );
}

export function useToast() {
  return useContext(ToastCtx);
}
