interface Props {
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  emptyText?: string;
  children?: React.ReactNode;
}

export function State({ loading, error, empty, emptyText = '暂无数据', children }: Props) {
  if (loading) return <div className="loading">加载中…</div>;
  if (error) return <div className="error">请求失败：{error}</div>;
  if (empty) return <div className="empty">{emptyText}</div>;
  return <>{children}</>;
}
