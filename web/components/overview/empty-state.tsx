import Link from "next/link";

/**
 * Empty-state của màn M1: luôn nêu rõ việc cần làm tiếp và đường đi (hợp đồng
 * §0.2 — empty không phải lỗi). Link chạm tối thiểu 44px (WCAG 2.5.5).
 */
export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: { label: string; href: string };
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed px-6 py-10 text-center">
      <p className="text-sm font-medium">{title}</p>
      {hint ? (
        <p className="max-w-md text-sm text-muted-foreground">{hint}</p>
      ) : null}
      {action ? (
        <Link
          href={action.href}
          className="mt-2 inline-flex min-touch items-center rounded-lg border px-3 text-sm font-medium hover:bg-muted"
        >
          {action.label}
        </Link>
      ) : null}
    </div>
  );
}
