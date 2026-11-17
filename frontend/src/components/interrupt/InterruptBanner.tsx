"use client";

import { InterruptData } from "@/lib/types";
import SupplementForm from "./SupplementForm";
import ApprovalCard from "./ApprovalCard";

interface Props {
  data: InterruptData;
  onSupplement: (text: string) => void;
  onResume: (resume: Record<string, unknown>) => void;
}

export default function InterruptBanner({
  data,
  onSupplement,
  onResume,
}: Props) {
  if (data.interrupt_type === "order_info_supplement") {
    return <SupplementForm data={data} onSubmit={onSupplement} />;
  }

  if (data.interrupt_type === "hitl_approval") {
    return <ApprovalCard data={data} onResume={onResume} />;
  }

  return null;
}
