"use client";

import type { ReactNode } from "react";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { SettingField } from "./setting-field";
import type { SettingRow, SettingValue } from "./types";

export function SettingsSection({
  title,
  description,
  rows,
  values,
  errors,
  onChange,
  action,
}: {
  title: string;
  description: string;
  rows: SettingRow[];
  values: Record<string, SettingValue>;
  errors: Record<string, string>;
  onChange: (key: string, value: SettingValue) => void;
  action?: ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
        {action ? <CardAction>{action}</CardAction> : null}
      </CardHeader>
      <CardContent>
        {rows.map((row) => (
          <SettingField
            key={row.key}
            row={row}
            value={values[row.key] ?? ""}
            error={errors[row.key]}
            onChange={(value) => onChange(row.key, value)}
          />
        ))}
      </CardContent>
    </Card>
  );
}
