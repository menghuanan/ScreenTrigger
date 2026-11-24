/**
 * 复合条件树编辑（TASK-016；02 结构权威）。
 *
 * 结构（02）：分支 `{op:"all"|"any",children:[Node,...]}`；叶 `{id,type,params,roi?:LocalRect}`。
 * - **无 NOT**：分支仅 all/any（与 ``types.conditions._validate_condition_tree`` 的 op 枚举一致）；
 * - 叶 type 四枚举 template/contour/ocr/color；叶 params 按类型编辑；
 * - 叶 `roi` 为 **LocalRect**（相对候选 bbox、宽高 ≥1；**非** Region，不受 10×10 限制）；
 * - 叶 ID 由本编辑器生成（`cN`，树内唯一），anchor 以单选设置；
 * - anchor 叶的 ROI 在求值时**被忽略**（恒整帧，02:78）——UI 给出行内提示。
 *
 * 本组件是**受控**的（`value`/`onChange` + `anchorConditionId`/`onAnchorChange`），
 * 不改动父状态以外的任何东西；不触发任何输入动作。
 */

import type {
  ColorHsv,
  ColorParams,
  ColorRgb,
  ConditionBranch,
  ConditionLeaf,
  ConditionNode,
  ContourParams,
  OcrParams,
  TemplateParams,
  ToleranceHsv,
  ToleranceRgb,
} from "../../lib/types/conditions";
import type { LocalRect } from "../../lib/types";

/** 叶类型枚举（02，逐字）。 */
export const LEAF_TYPE_OPTIONS = ["template", "contour", "ocr", "color"] as const;
export type LeafType = (typeof LEAF_TYPE_OPTIONS)[number];

const LEAF_TYPE_LABELS: Record<LeafType, string> = {
  template: "模板",
  contour: "轮廓",
  ocr: "文字（OCR）",
  color: "颜色",
};

/** 分支节点判定（有 op 即分支）。 */
export function isBranch(node: ConditionNode): node is ConditionBranch {
  return typeof (node as ConditionBranch).op === "string";
}

/** DFS 前序收集叶 ID（供 anchor/唯一性判断）。 */
export function collectLeafIds(node: ConditionNode): string[] {
  if (isBranch(node)) {
    const ids: string[] = [];
    for (const child of node.children) {
      ids.push(...collectLeafIds(child));
    }
    return ids;
  }
  return [node.id];
}

/** DFS 前序收集叶节点。 */
export function collectLeaves(node: ConditionNode): ConditionLeaf[] {
  if (isBranch(node)) {
    const leaves: ConditionLeaf[] = [];
    for (const child of node.children) {
      leaves.push(...collectLeaves(child));
    }
    return leaves;
  }
  return [node];
}

/** 生成树内最小的未用叶 ID（`cN`）。 */
export function nextLeafId(node: ConditionNode): string {
  const used = new Set(collectLeafIds(node));
  let index = 1;
  while (used.has(`c${index}`)) {
    index += 1;
  }
  return `c${index}`;
}

/** 某类型叶的默认 params（可直接通过权威校验；ocr 的 model_id 需用户填）。 */
export function defaultParams(type: LeafType): ConditionLeaf["params"] {
  switch (type) {
    case "template":
      return { threshold: 0.85, allow_scale: false } satisfies TemplateParams;
    case "contour":
      return {
        threshold: 0.85,
        min_area: 16,
        canny_low: 50,
        canny_high: 150,
      } satisfies ContourParams;
    case "ocr":
      return { text: "OK", model_id: "" } satisfies OcrParams;
    case "color":
      return {
        color_space: "rgb",
        color: { r: 255, g: 0, b: 0 } satisfies ColorRgb,
        tolerance: { r: 15, g: 15, b: 15 } satisfies ToleranceRgb,
      } satisfies ColorParams;
    default:
      return { threshold: 0.85 } satisfies TemplateParams;
  }
}

/** 新建叶。 */
export function newLeaf(conditionId: string, type: LeafType = "color"): ConditionLeaf {
  return { id: conditionId, type, params: defaultParams(type) };
}

/** 默认初始条件树（单叶 all 分支；anchor=该叶）。 */
export function defaultConditionTree(): { conditions: ConditionBranch; anchorConditionId: string } {
  const anchorLeaf = newLeaf("c1", "color");
  return {
    conditions: { op: "all", children: [anchorLeaf] },
    anchorConditionId: "c1",
  };
}

interface ConditionTreeEditorProps {
  value: ConditionNode;
  onChange: (next: ConditionNode) => void;
  anchorConditionId: string;
  onAnchorChange: (id: string) => void;
}

export default function ConditionTreeEditor({
  value,
  onChange,
  anchorConditionId,
  onAnchorChange,
}: ConditionTreeEditorProps) {
  const leafCount = collectLeafIds(value).length;
  return (
    <div className="ct-editor">
      <p className="muted">
        条件叶数：{leafCount}（1–10）；分支：all（全部满足）/ any（任一满足）；本版无 NOT。
      </p>
      <NodeEditor
        node={value}
        depth={0}
        isRoot
        anchorConditionId={anchorConditionId}
        onAnchorChange={onAnchorChange}
        onReplace={onChange}
        onRemove={null}
      />
    </div>
  );
}

interface NodeEditorProps {
  node: ConditionNode;
  depth: number;
  isRoot: boolean;
  anchorConditionId: string;
  onAnchorChange: (id: string) => void;
  onReplace: (next: ConditionNode) => void;
  onRemove: (() => void) | null;
}

/** 递归节点编辑器（分支 / 叶）。 */
function NodeEditor(props: NodeEditorProps) {
  const { node } = props;
  if (isBranch(node)) {
    return <BranchEditor {...props} node={node} />;
  }
  return <LeafEditor {...props} node={node} />;
}

function BranchEditor({
  node,
  depth,
  isRoot,
  anchorConditionId,
  onAnchorChange,
  onReplace,
  onRemove,
}: NodeEditorProps & { node: ConditionBranch }) {
  const children = node.children;

  const replaceChild = (index: number, next: ConditionNode): void => {
    const nextChildren = children.map((child, i) => (i === index ? next : child));
    onReplace({ op: node.op, children: nextChildren });
  };
  const removeChild = (index: number): void => {
    onReplace({ op: node.op, children: children.filter((_, i) => i !== index) });
  };
  const addLeaf = (): void => {
    const id = nextLeafId(node);
    onReplace({ op: node.op, children: [...children, newLeaf(id, "color")] });
  };
  const addBranch = (): void => {
    const id = nextLeafId(node);
    onReplace({ op: node.op, children: [...children, { op: "all", children: [newLeaf(id)] }] });
  };

  return (
    <div className="ct-branch" style={{ marginLeft: depth === 0 ? 0 : 12 }}>
      <div className="ct-branch-head">
        <label className="ct-op">
          分支：
          <select
            value={node.op}
            onChange={(e) =>
              onReplace({ op: e.target.value === "any" ? "any" : "all", children })
            }
          >
            <option value="all">all（全部满足）</option>
            <option value="any">any（任一满足）</option>
          </select>
        </label>
        {!isRoot && onRemove !== null && children.length > 1 && (
          <button type="button" className="btn ct-remove" onClick={onRemove}>
            删除该分组
          </button>
        )}
      </div>
      <div className="ct-children">
        {children.map((child, index) => (
          <NodeEditor
            key={childKey(child, index)}
            node={child}
            depth={depth + 1}
            isRoot={false}
            anchorConditionId={anchorConditionId}
            onAnchorChange={onAnchorChange}
            onReplace={(next) => replaceChild(index, next)}
            onRemove={children.length > 1 ? () => removeChild(index) : null}
          />
        ))}
      </div>
      <div className="ct-add">
        <button type="button" className="btn" onClick={addLeaf}>
          + 条件叶
        </button>
        <button type="button" className="btn" onClick={addBranch}>
          + 分组
        </button>
      </div>
    </div>
  );
}

function LeafEditor({
  node,
  anchorConditionId,
  onAnchorChange,
  onReplace,
  onRemove,
}: NodeEditorProps & { node: ConditionLeaf }) {
  const isAnchor = node.id === anchorConditionId;

  const changeType = (type: LeafType): void => {
    onReplace({ id: node.id, type, params: defaultParams(type), roi: node.roi });
  };
  const changeParams = (params: ConditionLeaf["params"]): void => {
    onReplace({ id: node.id, type: node.type, params, roi: node.roi });
  };
  const setRoi = (roi: LocalRect | undefined): void => {
    const next: ConditionLeaf = { id: node.id, type: node.type, params: node.params };
    if (roi !== undefined) {
      next.roi = roi;
    }
    onReplace(next);
  };

  return (
    <div className={`ct-leaf${isAnchor ? " ct-leaf-anchor" : ""}`}>
      <div className="ct-leaf-head">
        <span className="ct-leaf-id">#{node.id}</span>
        <label>
          类型：
          <select value={node.type} onChange={(e) => changeType(e.target.value as LeafType)}>
            {LEAF_TYPE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {LEAF_TYPE_LABELS[option]}
              </option>
            ))}
          </select>
        </label>
        <label className="ct-anchor-pick">
          <input
            type="radio"
            name="ct-anchor"
            checked={isAnchor}
            onChange={() => onAnchorChange(node.id)}
          />
          锚点
        </label>
        {onRemove !== null && !isAnchor && (
          <button type="button" className="btn ct-remove" onClick={onRemove}>
            删除
          </button>
        )}
      </div>

      <ParamsEditor type={node.type} params={node.params} onChange={changeParams} />

      <RoiEditor roi={node.roi} isAnchor={isAnchor} onChange={setRoi} />
    </div>
  );
}

/** 稳定 key（分支用 op+index，叶用 id）。 */
function childKey(node: ConditionNode, index: number): string {
  return isBranch(node) ? `branch-${node.op}-${index}` : `leaf-${node.id}`;
}

// ---------------------------------------------------------------------------
// 参数编辑器（按叶类型）
// ---------------------------------------------------------------------------

interface ParamsEditorProps {
  type: LeafType;
  params: ConditionLeaf["params"];
  onChange: (params: ConditionLeaf["params"]) => void;
}

function ParamsEditor({ type, params, onChange }: ParamsEditorProps) {
  switch (type) {
    case "template":
      return <TemplateParamsEditor params={params as TemplateParams} onChange={onChange} />;
    case "contour":
      return <ContourParamsEditor params={params as ContourParams} onChange={onChange} />;
    case "ocr":
      return <OcrParamsEditor params={params as OcrParams} onChange={onChange} />;
    case "color":
      return <ColorParamsEditor params={params as ColorParams} onChange={onChange} />;
    default:
      return null;
  }
}

function TemplateParamsEditor({
  params,
  onChange,
}: {
  params: TemplateParams;
  onChange: (p: TemplateParams) => void;
}) {
  const allowScale = params.allow_scale === true;
  const scaleRange = params.scale_range ?? [0.95, 1.05];
  return (
    <div className="ct-params">
      <label>
        threshold（0–1）：
        <input
          type="number"
          min={0}
          max={1}
          step={0.01}
          value={params.threshold}
          onChange={(e) => onChange({ ...params, threshold: Number(e.target.value) })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={allowScale}
          onChange={(e) =>
            onChange({ ...params, allow_scale: e.target.checked })
          }
        />
        启用多尺度
      </label>
      {allowScale && (
        <span className="ct-inline">
          scale_range：
          <input
            type="number"
            step={0.01}
            value={scaleRange[0]}
            onChange={(e) =>
              onChange({ ...params, scale_range: [Number(e.target.value), scaleRange[1]] })
            }
          />
          –
          <input
            type="number"
            step={0.01}
            value={scaleRange[1]}
            onChange={(e) =>
              onChange({ ...params, scale_range: [scaleRange[0], Number(e.target.value)] })
            }
          />
          step：
          <input
            type="number"
            step={0.01}
            min={0.001}
            value={params.step ?? 0.01}
            onChange={(e) => onChange({ ...params, step: Number(e.target.value) })}
          />
        </span>
      )}
    </div>
  );
}

function ContourParamsEditor({
  params,
  onChange,
}: {
  params: ContourParams;
  onChange: (p: ContourParams) => void;
}) {
  return (
    <div className="ct-params">
      <label>
        threshold（0–1）：
        <input
          type="number"
          min={0}
          max={1}
          step={0.01}
          value={params.threshold}
          onChange={(e) => onChange({ ...params, threshold: Number(e.target.value) })}
        />
      </label>
      <label>
        min_area（≥1）：
        <input
          type="number"
          min={1}
          step={1}
          value={params.min_area ?? 16}
          onChange={(e) => onChange({ ...params, min_area: Math.trunc(Number(e.target.value)) })}
        />
      </label>
      <label>
        canny_low：
        <input
          type="number"
          min={0}
          max={255}
          step={1}
          value={params.canny_low ?? 50}
          onChange={(e) => onChange({ ...params, canny_low: Math.trunc(Number(e.target.value)) })}
        />
      </label>
      <label>
        canny_high：
        <input
          type="number"
          min={0}
          max={255}
          step={1}
          value={params.canny_high ?? 150}
          onChange={(e) => onChange({ ...params, canny_high: Math.trunc(Number(e.target.value)) })}
        />
      </label>
    </div>
  );
}

function OcrParamsEditor({
  params,
  onChange,
}: {
  params: OcrParams;
  onChange: (p: OcrParams) => void;
}) {
  return (
    <div className="ct-params">
      <label>
        目标文本：
        <input
          type="text"
          value={params.text}
          onChange={(e) => onChange({ ...params, text: e.target.value })}
        />
      </label>
      <label>
        model_id：
        <input
          type="text"
          value={params.model_id}
          placeholder="已安装模型 id"
          onChange={(e) => onChange({ ...params, model_id: e.target.value })}
        />
      </label>
      <label>
        匹配：
        <select
          value={params.match_mode ?? "exact"}
          onChange={(e) =>
            onChange({ ...params, match_mode: e.target.value as OcrParams["match_mode"] })
          }
        >
          <option value="exact">exact</option>
          <option value="contains">contains</option>
          <option value="fuzzy">fuzzy</option>
        </select>
      </label>
    </div>
  );
}

function ColorParamsEditor({
  params,
  onChange,
}: {
  params: ColorParams;
  onChange: (p: ColorParams) => void;
}) {
  const isRgb = params.color_space === "rgb";
  const rgb = isRgb ? (params.color as ColorRgb) : null;
  const hsv = !isRgb ? (params.color as ColorHsv) : null;
  const tolRgb = isRgb ? ((params.tolerance as ToleranceRgb | undefined) ?? {}) : null;
  const tolHsv = !isRgb ? ((params.tolerance as ToleranceHsv | undefined) ?? {}) : null;

  const switchSpace = (space: "rgb" | "hsv"): void => {
    if (space === "rgb") {
      onChange({
        ...params,
        color_space: "rgb",
        color: { r: 255, g: 0, b: 0 },
        tolerance: { r: 15, g: 15, b: 15 },
      });
    } else {
      onChange({
        ...params,
        color_space: "hsv",
        color: { h: 0, s: 100, v: 100 },
        tolerance: { h: 10, s: 20, v: 20 },
      });
    }
  };

  return (
    <div className="ct-params">
      <label>
        色彩空间：
        <select
          value={params.color_space}
          onChange={(e) => switchSpace(e.target.value === "hsv" ? "hsv" : "rgb")}
        >
          <option value="rgb">RGB</option>
          <option value="hsv">HSV（h∈[0,360)，实现域 ≤359）</option>
        </select>
      </label>
      {isRgb && rgb !== null && tolRgb !== null ? (
        <span className="ct-inline">
          <ChannelInput label="R" value={rgb.r} onChange={(v) => onChange({ ...params, color: { ...rgb, r: v } })} />
          <ChannelInput label="G" value={rgb.g} onChange={(v) => onChange({ ...params, color: { ...rgb, g: v } })} />
          <ChannelInput label="B" value={rgb.b} onChange={(v) => onChange({ ...params, color: { ...rgb, b: v } })} />
          <span className="ct-tol-label">容差</span>
          <ChannelInput label="R" max={255} value={tolRgb.r ?? 15} onChange={(v) => onChange({ ...params, tolerance: { ...tolRgb, r: v } })} />
          <ChannelInput label="G" max={255} value={tolRgb.g ?? 15} onChange={(v) => onChange({ ...params, tolerance: { ...tolRgb, g: v } })} />
          <ChannelInput label="B" max={255} value={tolRgb.b ?? 15} onChange={(v) => onChange({ ...params, tolerance: { ...tolRgb, b: v } })} />
        </span>
      ) : (
        hsv !== null &&
        tolHsv !== null && (
          <span className="ct-inline">
            <ChannelInput label="H" max={359} value={hsv.h} onChange={(v) => onChange({ ...params, color: { ...hsv, h: v } })} />
            <ChannelInput label="S" max={100} value={hsv.s} onChange={(v) => onChange({ ...params, color: { ...hsv, s: v } })} />
            <ChannelInput label="V" max={100} value={hsv.v} onChange={(v) => onChange({ ...params, color: { ...hsv, v: v } })} />
            <span className="ct-tol-label">容差</span>
            <ChannelInput label="H" max={180} value={tolHsv.h ?? 10} onChange={(v) => onChange({ ...params, tolerance: { ...tolHsv, h: v } })} />
            <ChannelInput label="S" max={100} value={tolHsv.s ?? 20} onChange={(v) => onChange({ ...params, tolerance: { ...tolHsv, s: v } })} />
            <ChannelInput label="V" max={100} value={tolHsv.v ?? 20} onChange={(v) => onChange({ ...params, tolerance: { ...tolHsv, v: v } })} />
          </span>
        )
      )}
    </div>
  );
}

function ChannelInput({
  label,
  value,
  max = 255,
  onChange,
}: {
  label: string;
  value: number;
  max?: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="ct-channel">
      {label}
      <input
        type="number"
        min={0}
        max={max}
        step={1}
        value={value}
        onChange={(e) => onChange(Math.trunc(Number(e.target.value)))}
      />
    </label>
  );
}

// ---------------------------------------------------------------------------
// ROI 编辑器（LocalRect，相对候选 bbox）
// ---------------------------------------------------------------------------

function RoiEditor({
  roi,
  isAnchor,
  onChange,
}: {
  roi: LocalRect | undefined;
  isAnchor: boolean;
  onChange: (roi: LocalRect | undefined) => void;
}) {
  const enabled = roi !== undefined;
  const current = roi ?? { x: 0, y: 0, width: 1, height: 1 };
  return (
    <div className="ct-roi">
      <label>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) =>
            onChange(e.target.checked ? { x: 0, y: 0, width: 1, height: 1 } : undefined)
          }
        />
        限定局部 ROI（相对候选框；宽高 ≥1）
      </label>
      {enabled && (
        <span className="ct-inline">
          x：
          <input
            type="number"
            step={1}
            value={current.x}
            onChange={(e) => onChange({ ...current, x: Math.trunc(Number(e.target.value)) })}
          />
          y：
          <input
            type="number"
            step={1}
            value={current.y}
            onChange={(e) => onChange({ ...current, y: Math.trunc(Number(e.target.value)) })}
          />
          w：
          <input
            type="number"
            min={1}
            step={1}
            value={current.width}
            onChange={(e) =>
              onChange({ ...current, width: Math.max(1, Math.trunc(Number(e.target.value))) })
            }
          />
          h：
          <input
            type="number"
            min={1}
            step={1}
            value={current.height}
            onChange={(e) =>
              onChange({ ...current, height: Math.max(1, Math.trunc(Number(e.target.value))) })
            }
          />
        </span>
      )}
      {enabled && isAnchor && (
        <p className="muted">锚点叶在整帧产生候选，其 ROI 在求值时不生效（02:78）。</p>
      )}
    </div>
  );
}
