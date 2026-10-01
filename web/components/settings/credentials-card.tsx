"use client";

import { KeyRoundIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { CredentialRow } from "./types";

// Quy tắc bất di bất dịch hợp đồng §1: không bao giờ trả nguyên văn API key —
// thẻ này chỉ render trạng thái `configured` và chuỗi `masked` của máy chủ.
export function CredentialsCard({ credentials }: { credentials: CredentialRow[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRoundIcon className="size-4" aria-hidden="true" />
          Khóa API
        </CardTitle>
        <CardDescription>
          Không bao giờ hiển thị nguyên văn — chỉ trạng thái cấu hình và 4 ký tự cuối.
          Đặt khóa qua biến môi trường hoặc .env, không sửa được từ UI.
        </CardDescription>
        <CardAction>
          <Badge variant="outline">Chỉ đọc</Badge>
        </CardAction>
      </CardHeader>
      <CardContent>
        {credentials.length === 0 ? (
          <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            Máy chủ không trả về khóa nào. Khởi động backend rồi bấm Làm mới để xem
            trạng thái khóa API.
          </p>
        ) : (
          <ul className="space-y-2">
            {credentials.map((credential) => (
              <li
                key={credential.env_var}
                className="flex flex-wrap items-center justify-between gap-2 border-t py-2 first:border-t-0 first:pt-0"
              >
                <span className="font-mono text-xs">{credential.env_var}</span>
                {credential.configured && credential.masked ? (
                  <Badge variant="secondary">
                    Đã cấu hình <span className="font-mono">{credential.masked}</span>
                  </Badge>
                ) : (
                  <Badge variant="outline">Chưa cấu hình</Badge>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
