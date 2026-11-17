"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, Check, ShieldCheck, X } from "lucide-react";
import { InterruptData } from "@/lib/types";
import {
  ApprovalLine,
  buildResume,
  editable,
  parseApproval,
} from "@/lib/approval-view";

interface Props {
  data: InterruptData;
  onResume: (resume: Record<string, unknown>) => void;
}

const RISK_CLASS: Record<string, string> = {
  high: "bg-red-100 text-red-700",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-slate-100 text-slate-600",
};

function cloneLines(lines: ApprovalLine[]): ApprovalLine[] {
  return lines.map((line) => ({ ...line }));
}

export default function ApprovalCard({ data, onResume }: Props) {
  const view = useMemo(() => parseApproval(data), [data]);
  const [drafts, setDrafts] = useState<ApprovalLine[][]>(() => view.actions.map((action) => cloneLines(action.lines)));
  const dirty = view.actions.some((action, index) =>
    editable(action) && (drafts[index] || []).some((line, lineIndex) => {
      const original = action.lines[lineIndex];
      return !!original && (line.quantity !== original.quantity || line.unitPrice !== original.unitPrice);
    }),
  );

  const updateLine = (actionIndex: number, lineIndex: number, patch: Partial<ApprovalLine>) => {
    setDrafts((current) => current.map((lines, index) => {
      if (index !== actionIndex) return lines;
      return lines.map((line) => {
        if (line.lineIndex !== lineIndex) return line;
        const next = { ...line, ...patch };
        next.subtotal = next.quantity !== null && next.unitPrice !== null
          ? Math.round(next.quantity * next.unitPrice * 100) / 100
          : null;
        return next;
      });
    }));
  };

  return (
    <div className="mx-6 mb-3 animate-fade-in">
      <div className="max-w-3xl mx-auto border border-blue-200 bg-blue-50 rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3">
          <ShieldCheck size={16} className="text-blue-600" />
          <span className="text-sm font-medium text-blue-800">写入审批</span>
        </div>

        <div className="mb-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>该操作将写入 ERP</span>
        </div>

        {view.actions.map((action, actionIndex) => {
          const lines = drafts[actionIndex] || action.lines;
          const total = lines.some((line) => line.subtotal !== null)
            ? Math.round(lines.reduce((sum, line) => sum + (line.subtotal || 0), 0) * 100) / 100
            : action.totalAmount;
          const canEdit = editable(action);
          return (
            <div key={`${action.toolName}-${actionIndex}`} className="mb-4 rounded-lg border border-blue-100 bg-white/80 p-3">
              <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium text-gray-800">{action.toolName || "写操作"}</span>
                {action.riskLabel && (
                  <span className={`rounded px-1.5 py-0.5 ${RISK_CLASS[action.riskLevel] || RISK_CLASS.low}`}>
                    {action.riskLabel}
                  </span>
                )}
                {action.supplier && <span className="text-gray-500">供应商 {action.supplier}</span>}
              </div>

              {lines.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-gray-400">
                        <th className="py-1 pr-2 font-medium">零件</th>
                        <th className="py-1 pr-2 font-medium">编码</th>
                        <th className="py-1 pr-2 font-medium">数量</th>
                        <th className="py-1 pr-2 font-medium">单价</th>
                        <th className="py-1 font-medium">小计</th>
                      </tr>
                    </thead>
                    <tbody>
                      {lines.map((line) => (
                        <tr key={line.lineIndex} className="border-t border-blue-50">
                          <td className="py-1.5 pr-2 text-gray-800">{line.partName || "—"}</td>
                          <td className="py-1.5 pr-2 text-gray-600">{line.partCode || "—"}</td>
                          <td className="py-1.5 pr-2">
                            {canEdit && line.quantity !== null ? (
                              <input
                                aria-label="数量"
                                type="number"
                                min={1}
                                step="1"
                                value={line.quantity}
                                onChange={(event) => updateLine(actionIndex, line.lineIndex, { quantity: Number(event.target.value) })}
                                className="w-20 rounded border border-gray-200 px-1.5 py-1"
                              />
                            ) : (
                              <span>{line.quantity ?? "—"}</span>
                            )}
                          </td>
                          <td className="py-1.5 pr-2">
                            {canEdit && line.unitPrice !== null ? (
                              <input
                                aria-label="单价"
                                type="number"
                                min={0.01}
                                step="0.01"
                                value={line.unitPrice}
                                onChange={(event) => updateLine(actionIndex, line.lineIndex, { unitPrice: Number(event.target.value) })}
                                className="w-24 rounded border border-gray-200 px-1.5 py-1"
                              />
                            ) : (
                              <span>{line.unitPrice ?? "—"}</span>
                            )}
                          </td>
                          <td className="py-1.5 text-gray-800">{line.subtotal ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {action.fields.length > 0 && (
                <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                  {action.fields.map((field) => (
                    <div key={field.label} className="contents">
                      <dt className="text-gray-400">{field.label}</dt>
                      <dd className="text-gray-800">{field.value}</dd>
                    </div>
                  ))}
                </dl>
              )}

              {total !== null && (
                <div className="mt-2 text-right text-xs text-gray-700">
                  合计 <span className="font-medium text-gray-900">{total.toFixed(2)}</span>
                </div>
              )}
            </div>
          );
        })}

        <div className="flex gap-3">
          <button
            onClick={() => onResume(buildResume(view, "approve", drafts))}
            className="flex items-center gap-1.5 px-4 py-2 bg-green-600 hover:bg-green-700 text-white text-sm font-medium rounded-lg transition-colors"
          >
            <Check size={14} />
            {dirty ? "保存修改并批准" : "批准执行"}
          </button>
          <button
            onClick={() => onResume(buildResume(view, "reject", drafts))}
            className="flex items-center gap-1.5 px-4 py-2 bg-red-500 hover:bg-red-600 text-white text-sm font-medium rounded-lg transition-colors"
          >
            <X size={14} />
            拒绝
          </button>
        </div>
      </div>
    </div>
  );
}
