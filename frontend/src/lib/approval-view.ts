import { InterruptData } from "./types";

export interface ApprovalLine {
  partName: string;
  partCode: string;
  quantity: number | null;
  unitPrice: number | null;
  subtotal: number | null;
  supplier: string;
  lineIndex: number;
}

export interface ApprovalField {
  label: string;
  value: string;
}

export interface ApprovalAction {
  toolName: string;
  approvalId: string;
  riskLevel: string;
  riskLabel: string;
  supplier: string;
  totalAmount: number | null;
  lines: ApprovalLine[];
  fields: ApprovalField[];
  allowedDecisions: string[];
  args: Record<string, unknown>;
}

export interface BusinessView {
  writesErp: boolean;
  hint: string;
  actions: ApprovalAction[];
}

const RISK_LABELS: Record<string, string> = {
  high: "高风险",
  medium: "中风险",
  low: "低风险",
};

function text(...values: unknown[]): string {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    return String(value);
  }
  return "";
}

function num(value: unknown): number | null {
  if (typeof value === "boolean" || value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function money(value: number): number {
  return Math.round(value * 100) / 100;
}

function parseOrder(args: Record<string, unknown>): Record<string, unknown> | null {
  const raw = args.order_data ?? args.orderData;
  if (raw && typeof raw === "object") return raw as Record<string, unknown>;
  if (typeof raw === "string" && raw.trim()) {
    try {
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch {
      return null;
    }
  }
  if (args.orderDetail || args.totalAmount) return args;
  return null;
}

function lineFrom(raw: Record<string, unknown>, index: number, supplier: string): ApprovalLine {
  const quantity = num(raw.quantity ?? raw.qty);
  const unitPrice = num(raw.unitPrice ?? raw.unit_price ?? raw.purchasePrice ?? raw.purchase_price);
  return {
    partName: text(raw.partName, raw.part_name, raw.name),
    partCode: text(raw.partCode, raw.part_code, raw.partId, raw.part_id),
    quantity,
    unitPrice,
    subtotal: quantity !== null && unitPrice !== null ? money(quantity * unitPrice) : null,
    supplier: text(raw.supplierName, raw.supplier, raw.supplier_name, supplier),
    lineIndex: index,
  };
}

function actionFrom(name: string, args: Record<string, unknown>, allowed?: string[]): ApprovalAction {
  const order = parseOrder(args);
  const supplier = text(order?.supplierName, order?.supplier, args.supplier_name, name.startsWith("supplier") ? args.name : "");
  let lines: ApprovalLine[] = [];
  let totalAmount = order ? num(order.totalAmount ?? order.total_amount) : null;
  if (order && Array.isArray(order.orderDetail)) {
    lines = order.orderDetail
      .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
      .map((item, index) => lineFrom(item, index, supplier));
  } else if (name === "inventory_inbound" || name === "inventory_outbound") {
    lines = [lineFrom({ partId: args.part_id ?? args.partId, quantity: args.quantity }, 0, "")];
  } else if (name === "part_create") {
    lines = [lineFrom({ partCode: args.part_code, name: args.name, purchase_price: args.purchase_price, quantity: 1 }, 0, text(args.supplier_id))];
    totalAmount = num(args.purchase_price);
  }
  const fields: ApprovalField[] = [];
  if (name === "supplier_create") {
    fields.push(
      { label: "供应商编码", value: text(args.supplier_code) },
      { label: "供应商", value: text(args.name) },
      { label: "联系人", value: text(args.contact_person) },
    );
  } else if (name === "supplier_update_status") {
    fields.push({ label: "供应商 ID", value: text(args.id) }, { label: "合作状态", value: text(args.status) });
  } else if (name === "order_update_status") {
    fields.push({ label: "订单 ID", value: text(args.id) }, { label: "新状态", value: text(args.status) });
  } else if (name === "inventory_inbound" || name === "inventory_outbound") {
    fields.push({ label: "仓库位置", value: text(args.warehouse_location) });
  }
  const riskLevel = text(args.risk_level);
  return {
    toolName: name,
    approvalId: text(args.approval_id),
    riskLevel,
    riskLabel: RISK_LABELS[riskLevel] || riskLevel,
    supplier,
    totalAmount,
    lines,
    fields: fields.filter((item) => item.value),
    allowedDecisions: allowed && allowed.length ? allowed : ["approve", "reject", "edit"],
    args,
  };
}

function normalizeAction(raw: Record<string, unknown>): ApprovalAction {
  const lines = Array.isArray(raw.lines)
    ? raw.lines.filter((item): item is Record<string, unknown> => !!item && typeof item === "object").map((item, index) => ({
        partName: text(item.partName, item.part_name),
        partCode: text(item.partCode, item.part_code),
        quantity: num(item.quantity),
        unitPrice: num(item.unitPrice ?? item.unit_price),
        subtotal: num(item.subtotal),
        supplier: text(item.supplier),
        lineIndex: num(item.lineIndex ?? item.line_index) ?? index,
      }))
    : [];
  const allowed = (raw.allowedDecisions || raw.allowed_decisions) as string[] | undefined;
  const riskLevel = text(raw.riskLevel, raw.risk_level);
  return {
    toolName: text(raw.toolName, raw.tool_name),
    approvalId: text(raw.approvalId, raw.approval_id),
    riskLevel,
    riskLabel: text(raw.riskLabel, raw.risk_label, RISK_LABELS[riskLevel]),
    supplier: text(raw.supplier),
    totalAmount: num(raw.totalAmount ?? raw.total_amount),
    lines,
    fields: Array.isArray(raw.fields) ? raw.fields as ApprovalField[] : [],
    allowedDecisions: allowed?.length ? allowed : ["approve", "reject"],
    args: (raw.args as Record<string, unknown>) || {},
  };
}

export function parseApproval(data: InterruptData): BusinessView {
  const provided = data.business_view;
  if (provided?.actions?.length) {
    return {
      writesErp: true,
      hint: provided.hint || "该操作将写入 ERP",
      actions: provided.actions.map((action) => normalizeAction(action as Record<string, unknown>)),
    };
  }
  const requests = Array.isArray(data.order_data)
    ? data.order_data
    : Array.isArray(data.tool_args)
      ? data.tool_args
      : null;
  if (requests) {
    const actions = requests
      .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
      .map((item) => actionFrom(text(item.name), (item.args as Record<string, unknown>) || {}));
    return { writesErp: true, hint: "该操作将写入 ERP", actions };
  }
  const args = (data.tool_args && !Array.isArray(data.tool_args) ? data.tool_args : data.order_data) as Record<string, unknown> | undefined;
  const name = data.tool_name || "";
  return {
    writesErp: true,
    hint: "该操作将写入 ERP",
    actions: name || args ? [actionFrom(name, args || {})] : [],
  };
}

export function editable(action: ApprovalAction): boolean {
  return action.allowedDecisions.includes("edit") && action.lines.some((line) => line.quantity !== null || line.unitPrice !== null);
}

export function applyEdits(action: ApprovalAction, lines: ApprovalLine[]): Record<string, unknown> {
  const args = { ...action.args };
  const order = parseOrder(args);
  if (order && Array.isArray(order.orderDetail)) {
    const details = order.orderDetail.map((item, index) => {
      const source = item && typeof item === "object" ? { ...(item as Record<string, unknown>) } : {};
      const edited = lines.find((line) => line.lineIndex === index);
      if (!edited) return source;
      if (edited.quantity !== null) source.quantity = edited.quantity;
      if (edited.unitPrice !== null) {
        if ("unitPrice" in source || !("unit_price" in source)) source.unitPrice = edited.unitPrice;
        else source.unit_price = edited.unitPrice;
      }
      return source;
    });
    const total = money(details.reduce((sum, item) => {
      const row = item as Record<string, unknown>;
      const quantity = num(row.quantity) || 0;
      const price = num(row.unitPrice ?? row.unit_price) || 0;
      return sum + quantity * price;
    }, 0));
    const nextOrder = { ...order, orderDetail: details, totalAmount: total };
    if (typeof args.order_data === "string" || args.order_data) args.order_data = JSON.stringify(nextOrder);
    else if (args.orderData) args.orderData = nextOrder;
    else Object.assign(args, nextOrder);
  } else if (action.toolName === "inventory_inbound" || action.toolName === "inventory_outbound") {
    if (lines[0]?.quantity !== null) args.quantity = lines[0].quantity;
  } else if (action.toolName === "part_create" && lines[0]?.unitPrice !== null) {
    args.purchase_price = lines[0].unitPrice;
  }
  if (action.approvalId) args.approval_id = action.approvalId;
  return args;
}

export function linesChanged(before: ApprovalLine[], after: ApprovalLine[]): boolean {
  return after.some((line, index) => {
    const original = before[index];
    if (!original) return true;
    return line.quantity !== original.quantity || line.unitPrice !== original.unitPrice;
  });
}

export function buildResume(view: BusinessView, decision: "approve" | "reject", drafts: ApprovalLine[][]): Record<string, unknown> {
  if (!view.actions.length) {
    return { decisions: [{ type: decision }] };
  }
  const approvalIds = view.actions.map((action) => action.approvalId).filter(Boolean);
  const decisions = view.actions.map((action, index) => {
    const original = action.lines;
    const next = drafts[index] || original;
    if (decision === "reject") return { type: "reject" };
    if (editable(action) && linesChanged(original, next)) {
      return { type: "edit", edited_action: { name: action.toolName, args: applyEdits(action, next) } };
    }
    return { type: "approve" };
  });
  return {
    approval_id: approvalIds[0],
    approval_ids: approvalIds,
    decisions,
  };
}
