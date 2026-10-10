import { useCallback, useEffect, useRef, useState } from 'react';

interface State<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

/** 极简数据获取 hook：避免渲染竞态 + 支持手动重载 */
export function useApi<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [state, setState] = useState<State<T>>({ data: null, loading: true, error: null });
  const seq = useRef(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const load = useCallback(async () => {
    const id = ++seq.current;
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await fnRef.current();
      if (seq.current === id) setState({ data, loading: false, error: null });
    } catch (e) {
      if (seq.current === id) {
        setState({ data: null, loading: false, error: (e as Error).message ?? String(e) });
      }
    }
  }, []);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    void load();
  }, deps);

  return { ...state, reload: load };
}
