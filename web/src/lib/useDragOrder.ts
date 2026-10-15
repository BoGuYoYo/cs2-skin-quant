import { useCallback, useEffect, useState } from 'react';

/**
 * 面板顺序管理 —— HTML5 拖放 + localStorage 持久化。
 *
 * 顺序只存 key 列表，新加的面板会自动追加到末尾，删掉的面板会被自动忽略，
 * 所以后续往页面里加模块不需要清缓存。
 */
export function useDragOrder(storageKey: string, defaults: string[]) {
  const [order, setOrder] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const saved = JSON.parse(raw) as string[];
        if (Array.isArray(saved)) {
          const kept = saved.filter((k) => defaults.includes(k));
          const added = defaults.filter((k) => !kept.includes(k));
          return [...kept, ...added];
        }
      }
    } catch {
      /* 存储损坏就回退默认 */
    }
    return defaults;
  });

  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(order));
    } catch {
      /* 隐私模式下写不了就算了 */
    }
  }, [storageKey, order]);

  const move = useCallback((dragged: string, target: string) => {
    setOrder((prev) => {
      const from = prev.indexOf(dragged);
      const to = prev.indexOf(target);
      if (from < 0 || to < 0 || from === to) return prev;
      const next = [...prev];
      next.splice(from, 1);
      next.splice(to, 0, dragged);
      return next;
    });
  }, []);

  const reset = useCallback(() => setOrder(defaults), [defaults]);

  const moveBy = useCallback((id: string, delta: number) => {
    setOrder((prev) => {
      const from = prev.indexOf(id);
      const to = from + delta;
      if (from < 0 || to < 0 || to >= prev.length) return prev;
      const next = [...prev];
      next.splice(from, 1);
      next.splice(to, 0, id);
      return next;
    });
  }, []);

  return { order, move, moveBy, reset, setOrder };
}
