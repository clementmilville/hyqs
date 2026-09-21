import { useCallback, useEffect, useRef, useState } from "react";

export function useAIStream() {
  const [streaming, setStreaming] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [text, setText] = useState("");
  const [toolEvents, setToolEvents] = useState([]);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [usage, setUsage] = useState(null);
  const cancelRef = useRef(null);

  useEffect(() => {
    return () => {
      if (cancelRef.current) cancelRef.current();
    };
  }, []);

  const stop = useCallback(() => {
    if (cancelRef.current) {
      cancelRef.current();
      cancelRef.current = null;
    }
    setStreaming(false);
    setReconnecting(false);
  }, []);

  const start = useCallback((streamFn, ...args) => {
    if (cancelRef.current) cancelRef.current();
    setText("");
    setToolEvents([]);
    setError(null);
    setResult(null);
    setUsage(null);
    setReconnecting(false);
    setStreaming(true);

    const { cancel } = streamFn(...args, {
      onText: (delta) => {
        if (delta === "") {
          // Empty-string sentinel from streamAI: reset accumulated state for retry
          setText("");
          setToolEvents([]);
        } else {
          setText((t) => t + delta);
        }
      },
      onToolUse: (ev) => setToolEvents((evs) => [...evs, ev]),
      onReconnecting: () => setReconnecting(true),
      onResult: (r) => {
        cancelRef.current = null;
        if (r.usage) setUsage(r.usage);
        setResult(r);
        setReconnecting(false);
        setStreaming(false);
      },
      onError: (err) => {
        cancelRef.current = null;
        setError(err);
        setReconnecting(false);
        setStreaming(false);
      },
    });
    cancelRef.current = cancel;
  }, []);

  return { streaming, reconnecting, text, toolEvents, error, result, usage, start, stop };
}
