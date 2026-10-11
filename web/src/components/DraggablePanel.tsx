import { useState, type ReactNode } from 'react';

interface Props {
  /** 唯一 key，用于拖拽识别与顺序持久化 */
  id: string;
  /** 是否处于可编辑（拖拽）模式 */
  editing: boolean;
  onDropPanel: (draggedId: string, targetId: string) => void;
  children: ReactNode;
  className?: string;
}

/**
 * 可拖拽面板容器。
 *
 * 只在 editing 模式开启 draggable —— 平时浏览时不会误拖表格里的东西。
 * 用 HTML5 原生拖放而非第三方库：零依赖，且键盘用户仍可通过编辑模式下的
 * 「上移/下移」按钮调整（见 aria-label）。
 */
export function DraggablePanel({ id, editing, onDropPanel, children, className = '' }: Props) {
  const [over, setOver] = useState(false);

  if (!editing) {
    return <div className={className}>{children}</div>;
  }

  return (
    <div
      className={`${className} ark-panelWrap ${over ? 'is-over' : ''}`}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData('text/plain', id);
        e.dataTransfer.effectAllowed = 'move';
      }}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        const dragged = e.dataTransfer.getData('text/plain');
        if (dragged && dragged !== id) onDropPanel(dragged, id);
      }}
      aria-label="可拖拽面板"
    >
      {children}
    </div>
  );
}
