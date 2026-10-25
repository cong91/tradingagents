import type { Metadata } from "next";
import { messages } from "@/lib/i18n/messages";
import { PageHeader } from "@/components/page-header";
import { PipelineScreen } from "@/components/pipeline/pipeline-screen";

export const metadata: Metadata = { title: messages.en.pages.pipeline.title };

export default function PipelinePage() {
  return (
    <div>
      <PageHeader page="pipeline" />
      <PipelineScreen />
    </div>
  );
}
